#!/usr/bin/env python3
"""百日新高数量：本地日K统计（不依赖问财 / 大盘数据.numbers）。

口径（固定）：
  当日最高价 = 近 200 个交易日最高价（含当日）；
  上市日历天数严格 >10；排除 ST、北交所。

缓存：
  - `new_high_count_results/series.json`：按日累积家数
  - `new_high_count_results/days/YYYY-MM-DD.json`：单日明细（可选 codes）
  - 报表近 120 日柱图读 series，由 `compute_new_high_count` 对齐 as_of 重算缺口日
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "new_high_count_results"
DAY_DIR = RESULT_DIR / "days"
SERIES_FILE = RESULT_DIR / "series.json"
SERIES_DAYS = 120
LOOKBACK = 200  # 近 200 个交易日最高价
SCHEMA = 4
SOURCE = "local_200d_high"
NOTE = (
    "百日新高：当日最高价=近200个交易日最高价；"
    "上市日历天数>10；排除ST、北交所。本地日K统计，不问财。"
)


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def load_series() -> dict[str, Any]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if not SERIES_FILE.exists():
        return {
            "schema": SCHEMA,
            "note": NOTE,
            "days": {},
            "updated": "",
        }
    try:
        data = json.loads(SERIES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{SERIES_FILE.name} JSON 损坏: {exc}") from exc
    if not isinstance(data.get("days"), dict):
        data["days"] = {}
    return data


def save_series(data: dict[str, Any]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    data["schema"] = SCHEMA
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["note"] = NOTE
    data.pop("query_today", None)
    data.pop("frozen", None)
    SERIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _day_path(day: date) -> Path:
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    return DAY_DIR / f"{day.isoformat()}.json"


def save_day(
    day: date | str,
    count: int,
    *,
    codes: list[str] | None = None,
    names: list[str] | None = None,
    source: str = SOURCE,
) -> dict[str, Any]:
    d = _as_date(day)
    entry = {
        "date": d.isoformat(),
        "count": int(count),
        "source": source,
        "lookback": LOOKBACK,
        "codes": list(codes or []),
        "names": list(names or []),
        "fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _day_path(d).write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    series = load_series()
    series.setdefault("days", {})[d.isoformat()] = {
        "count": int(count),
        "source": source,
        "lookback": LOOKBACK,
        "names": list(names or [])[:50],
        "codes": list(codes or [])[:50],
        "fetched": entry["fetched"],
    }
    save_series(series)
    return entry


def series_daily_rows(as_of: date | str) -> list[dict]:
    """升序 [{date, count}, …]，截到 as_of（含）。"""
    as_of_d = _as_date(as_of)
    series = load_series()
    rows: list[dict] = []
    for key, val in sorted((series.get("days") or {}).items()):
        try:
            d = _as_date(key)
        except ValueError:
            continue
        if d > as_of_d:
            continue
        if val.get("count") is None:
            continue
        # 只认本地 200 日口径；旧问财/numbers 段跳过
        src = str(val.get("source") or "")
        if src and not src.startswith("local"):
            continue
        rows.append({"date": d.isoformat(), "count": int(val["count"])})
    return rows


def _eligible(stock: dict, d: date, n_bars_to_d: int) -> bool:
    import trend_strength as ts

    name = str(stock.get("name") or "")
    code = str(stock.get("code") or "")
    if ts.is_st_name(name) or ts.is_bj_code(code):
        return False
    list_date = stock.get("list_date")
    if list_date is not None:
        if (d - list_date).days <= ts.LIST_DAYS_MIN:
            return False
    elif n_bars_to_d <= ts.LIST_DAYS_MIN:
        return False
    return True


def count_hundred_day_highs_on_day(
    series: list[tuple],
    d: date,
    *,
    collect: bool = False,
) -> tuple[int, list[str], list[str]]:
    """单日百日新高家数；series 元素 = (stock, idx, closes, highs, lows, opens)。"""
    ds = d.isoformat()
    codes: list[str] = []
    names: list[str] = []
    n = 0
    for stock, idx, _closes, highs, _lows, _opens in series:
        i = idx.get(ds)
        if i is None:
            continue
        bars_n = i + 1
        if bars_n < LOOKBACK:
            continue
        if not _eligible(stock, d, bars_n):
            continue
        window = highs[i - LOOKBACK + 1 : i + 1]
        if len(window) < LOOKBACK:
            continue
        hi = float(highs[i])
        # 当日最高价 = 近 LOOKBACK 日最高价（并列亦计入）
        if hi + 1e-9 < max(window):
            continue
        n += 1
        if collect:
            codes.append(str(stock.get("code") or ""))
            names.append(str(stock.get("name") or ""))
    return n, codes, names


def ensure_local_day(
    day: date | str,
    *,
    force: bool = False,
    progress: bool = True,
    prepared: list[tuple] | None = None,
) -> dict[str, Any]:
    """保证 series 含该日本地百日新高；缺则现算。"""
    d = _as_date(day)
    series = load_series()
    cur = (series.get("days") or {}).get(d.isoformat()) or {}
    if (
        not force
        and str(cur.get("source") or "").startswith("local")
        and cur.get("count") is not None
        and int(cur.get("lookback") or 0) == LOOKBACK
    ):
        return cur

    import trend_strength as ts
    from trading_calendar import trading_days_ending

    if prepared is None:
        # 需要至少 LOOKBACK 根；多取余量
        days = trading_days_ending(d, LOOKBACK + 40)
        if progress:
            print(f"[newhigh] 计算 {d.isoformat()} 百日新高…")
        prepared = ts._prepare_long_series(
            d, days, progress=progress, log_tag="newhigh", need_avg=False
        )

    count, codes, names = count_hundred_day_highs_on_day(
        prepared, d, collect=True
    )
    return save_day(d, count, codes=codes, names=names, source=SOURCE)


def ensure_series_window(
    as_of: date | str,
    *,
    n_days: int = SERIES_DAYS,
    force: bool = False,
    progress: bool = True,
) -> list[dict]:
    """对齐 as_of 近 n_days 交易日均有本地百日新高。"""
    import trend_strength as ts
    from trading_calendar import trading_days_ending

    as_of_d = _as_date(as_of)
    # 日历：图表窗口 + 回看
    cal = trading_days_ending(as_of_d, n_days + LOOKBACK + 20)
    if len(cal) < LOOKBACK:
        raise RuntimeError(f"交易日日历不足 {LOOKBACK} 日，无法算百日新高")

    window = cal[-n_days:] if len(cal) >= n_days else cal
    series_file = load_series()
    days_map = series_file.get("days") or {}

    def _ok(key: str) -> bool:
        cur = days_map.get(key) or {}
        return (
            not force
            and str(cur.get("source") or "").startswith("local")
            and cur.get("count") is not None
            and int(cur.get("lookback") or 0) == LOOKBACK
        )

    missing = [d for d in window if not _ok(d.isoformat())]
    if not missing and not force:
        return series_daily_rows(as_of_d)

    if progress:
        print(
            f"[newhigh] 百日新高窗口 {window[0]}…{window[-1]} · "
            f"待算 {len(missing)}/{len(window)} 日"
        )

    # 长K线只需拉到 as_of，覆盖整个窗口
    prepared = ts._prepare_long_series(
        as_of_d, cal, progress=progress, log_tag="newhigh", need_avg=False
    )

    for d in missing if not force else window:
        ensure_local_day(d, force=True, progress=False, prepared=prepared)
        if progress:
            series_file = load_series()
            c = (series_file.get("days") or {}).get(d.isoformat(), {}).get("count")
            print(f"[newhigh] {d.isoformat()} → {c} 家")

    return series_daily_rows(as_of_d)


def apply_new_high_to_df(df: Any) -> Any:
    """把序列中的百日新高家数写回 DataFrame「新高」列（有则覆盖）。"""
    import pandas as pd

    series = load_series()
    days = series.get("days") or {}
    if "date" not in df.columns or "新高" not in df.columns:
        return df
    out = df.copy()

    def _one(v: Any) -> Any:
        if hasattr(v, "date") and not isinstance(v, date):
            try:
                v = v.date()
            except Exception:
                return None
        if isinstance(v, datetime):
            v = v.date()
        if isinstance(v, date):
            return v.isoformat()
        if pd.isna(v):
            return None
        return str(v)[:10]

    for i, row in out.iterrows():
        key = _one(row.get("date"))
        if not key or key not in days:
            continue
        cur = days[key] or {}
        if not str(cur.get("source") or "").startswith("local"):
            continue
        if cur.get("count") is None:
            continue
        out.at[i, "新高"] = int(cur["count"])
    return out


def compute_new_high_count(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,  # noqa: ARG001
    *,
    force: bool = False,
    progress: bool = True,
    fetch: bool = True,  # noqa: ARG001  — 保留参数兼容；始终本地算
) -> dict:
    """构建近 SERIES_DAYS 日百日新高序列（本地日K）。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"

    daily_all = ensure_series_window(
        as_of_d, n_days=SERIES_DAYS, force=force, progress=progress
    )
    # 截到 as_of，并取近 SERIES_DAYS
    daily = [r for r in daily_all if r["date"] <= as_of_d.isoformat()]
    series_days = daily[-SERIES_DAYS:] if len(daily) >= SERIES_DAYS else daily

    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if (
                cached.get("as_of") == as_of_d.isoformat()
                and cached.get("schema") == SCHEMA
                and int(cached.get("n_days") or 0) >= min(SERIES_DAYS, 1)
                and cached.get("daily")
                and int(cached.get("latest_count") or -1)
                == int((series_days[-1]["count"] if series_days else -2))
            ):
                return cached
        except json.JSONDecodeError:
            pass

    if len(series_days) < 1:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": SCHEMA,
            "daily": series_days,
            "note": "百日新高序列不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": SCHEMA,
        "start": series_days[0]["date"],
        "end": series_days[-1]["date"],
        "n_days": len(series_days),
        "daily": daily,
        "latest_count": int(series_days[-1]["count"]),
        "latest_source": SOURCE,
        "lookback": LOOKBACK,
        "note": NOTE,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(
            f"[newhigh] {as_of_d} 百日新高最新 {payload['latest_count']} 家 "
            f"（近{LOOKBACK}日最高 · {payload['start']}…{payload['end']}）→ {out_path.name}"
        )
    return payload


def _cli() -> None:
    p = argparse.ArgumentParser(description="百日新高数量：本地日K统计（不问财）")
    sub = p.add_subparsers(dest="cmd")

    p_status = sub.add_parser("status", help="查看本地序列覆盖")
    p_comp = sub.add_parser("compute", help="重算 as_of 近120日序列")
    p_comp.add_argument("--date", default=date.today().isoformat())
    p_comp.add_argument("--force", action="store_true")

    p_day = sub.add_parser("day", help="只算单日")
    p_day.add_argument("--date", default=date.today().isoformat())
    p_day.add_argument("--force", action="store_true")

    args = p.parse_args()
    if args.cmd == "status":
        series = load_series()
        days = series.get("days") or {}
        local = {
            k: v
            for k, v in days.items()
            if str(v.get("source") or "").startswith("local")
        }
        keys = sorted(local)
        print(f"[newhigh] {SERIES_FILE.name} · local {len(keys)} 日", end="")
        if keys:
            print(f" · {keys[0]}…{keys[-1]}")
        else:
            print(" · （空）")
        return
    if args.cmd == "day":
        ensure_local_day(args.date, force=args.force, progress=True)
        return
    if args.cmd == "compute":
        compute_new_high_count(args.date, force=args.force, progress=True)
        return

    import sys

    day = date.today().isoformat()
    force = "--force" in sys.argv
    for a in sys.argv[1:]:
        if len(a) == 10 and a[4] == "-" and a[7] == "-":
            day = a
    compute_new_high_count(day, force=force, progress=True)


if __name__ == "__main__":
    _cli()
