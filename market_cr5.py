#!/usr/bin/env python3
"""10cm 大涨贡献集中度（全市场缩圈 / 抱团指针）。

口径（固定）：
- 样本：沪深主板（10cm），排除 ST / 北交所 / 创业板 / 科创板
- 池：当日涨幅 ≥7%，有成交额
- 权重 w = 涨幅(%) × 成交额
- CR5 = 前 5 名权重 / 池内权重合计（×100 为百分比）
- 页面柱图近 30 个交易日；零轴 = 近 200 个交易日 CR5 均值

缓存：`market_cr5_results/series.json`
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "market_cr5_results"
SERIES_FILE = RESULT_DIR / "series.json"

CHART_DAYS = 30
MEAN_DAYS = 200
PCT_MIN = 7.0
CR_TOP = 5
SCHEMA = 1
SOURCE = "local_10cm_cr5"
NOTE = (
    "10cm缩圈：主板涨幅≥7% 池内，权重=涨幅×成交额，CR5=前5/合计；"
    "柱图近30日，零轴近200日均值。"
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
        return {"schema": SCHEMA, "note": NOTE, "days": {}, "updated": ""}
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
    data["note"] = NOTE
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SERIES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _is_10cm_main(code: str, name: str) -> bool:
    import trend_strength as ts

    if ts.is_st_name(name) or ts.is_bj_code(code):
        return False
    return ts.classify_bucket(code, name) == "main"


def _cr5_from_weights(weights: list[float]) -> float | None:
    if not weights:
        return None
    total = sum(weights)
    if total <= 0:
        return None
    top = sum(sorted(weights, reverse=True)[:CR_TOP])
    return round(100.0 * top / total, 2)


def _day_cr5(
    cache: dict[str, list[dict]],
    stocks: list[dict],
    d: date,
) -> tuple[float | None, int]:
    """返回 (cr5_pct, n_pool)。"""
    ds = d.isoformat()
    weights: list[float] = []
    for s in stocks:
        code = str(s.get("code") or "")
        rows = cache.get(code) or []
        if len(rows) < 2:
            continue
        idx = {r["date"]: i for i, r in enumerate(rows)}
        i = idx.get(ds)
        if i is None or i < 1:
            continue
        row = rows[i]
        prev = rows[i - 1]
        try:
            close = float(row["close"])
            pc = float(prev["close"])
            amt = float(row["amount"]) if row.get("amount") is not None else 0.0
        except (TypeError, ValueError, KeyError):
            continue
        if pc <= 0 or close <= 0 or amt <= 0:
            continue
        pct = (close / pc - 1.0) * 100.0
        if pct < PCT_MIN:
            continue
        weights.append(pct * amt)
    return _cr5_from_weights(weights), len(weights)


def _day_ok(cur: dict) -> bool:
    return (
        str(cur.get("source") or "") == SOURCE
        and cur.get("cr5") is not None
        and int(cur.get("schema") or 0) == SCHEMA
    )


def ensure_series_window(
    as_of: date | str,
    *,
    n_days: int = MEAN_DAYS,
    force: bool = False,
    progress: bool = True,
) -> list[dict]:
    """对齐 as_of 近 n_days 交易日均有 10cm CR5。"""
    import trend_strength as ts
    from trading_calendar import trading_days_ending

    as_of_d = _as_date(as_of)
    cal = trading_days_ending(as_of_d, n_days + 5, refresh=False)
    if len(cal) < 10:
        raise RuntimeError("交易日日历不足，无法算 10cm CR5")
    window = cal[-n_days:] if len(cal) >= n_days else cal

    series_file = load_series()
    days_map = series_file.get("days") or {}
    missing = [d for d in window if force or not _day_ok(days_map.get(d.isoformat()) or {})]
    # 窗口末日（通常=报表日）每次重算：收盘后成交额可能更新
    if window and not force and window[-1] not in missing:
        missing.append(window[-1])

    if missing or force:
        if progress:
            print(
                f"[cr5-10cm] 窗口 {window[0]}…{window[-1]} · "
                f"待算 {len(missing) if not force else len(window)}/{len(window)} 日"
            )
        # 长K线须带成交额（need_avg）
        prep_days = trading_days_ending(as_of_d, n_days + 40, refresh=False)
        ts._prepare_long_series(
            as_of_d,
            prep_days,
            progress=progress,
            log_tag="cr5-10cm",
            need_avg=True,
        )
        cache = ts.load_long_kline_cache()
        universe = ts.fetch_universe()
        stocks = [
            s
            for s in universe
            if _is_10cm_main(str(s.get("code") or ""), str(s.get("name") or ""))
        ]
        if progress:
            print(f"[cr5-10cm] 主板样本 {len(stocks)} 只")

        targets = window if force else missing
        for d in targets:
            cr5, n_pool = _day_cr5(cache, stocks, d)
            days_map[d.isoformat()] = {
                "cr5": cr5,
                "n_pool": n_pool,
                "source": SOURCE,
                "schema": SCHEMA,
                "pct_min": PCT_MIN,
                "cr_top": CR_TOP,
            }
            if progress:
                print(
                    f"[cr5-10cm] {d.isoformat()} → "
                    f"{'—' if cr5 is None else f'{cr5:.1f}%'} · 池 {n_pool}"
                )
        series_file["days"] = days_map
        series_file["as_of"] = as_of_d.isoformat()
        save_series(series_file)

    return series_daily_rows(as_of_d, n_days=n_days)


def series_daily_rows(as_of: date | str, *, n_days: int | None = None) -> list[dict]:
    """升序 [{date, cr5, n_pool}, …]，截到 as_of。"""
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
        if not _day_ok(val):
            continue
        rows.append(
            {
                "date": d.isoformat(),
                "cr5": float(val["cr5"]),
                "n_pool": int(val.get("n_pool") or 0),
            }
        )
    if n_days is not None and len(rows) > n_days:
        rows = rows[-n_days:]
    return rows


def compute_market_cr5(
    as_of: date | datetime | str,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    """供报表：近 MEAN_DAYS 序列 + 近 CHART_DAYS 柱图窗 + 200 日均值。"""
    as_of_d = _as_date(as_of)
    rows = ensure_series_window(
        as_of_d, n_days=MEAN_DAYS, force=force, progress=progress
    )
    # 只取 as_of 及之前
    rows = [r for r in rows if r["date"] <= as_of_d.isoformat()]
    mean_rows = rows[-MEAN_DAYS:] if len(rows) >= MEAN_DAYS else rows
    vals = [float(r["cr5"]) for r in mean_rows if r.get("cr5") is not None]
    mean_200d = (sum(vals) / len(vals)) if vals else None
    chart = rows[-CHART_DAYS:] if len(rows) >= CHART_DAYS else rows
    return {
        "as_of": as_of_d.isoformat(),
        "chart_days": CHART_DAYS,
        "mean_days": MEAN_DAYS,
        "mean_200d": round(mean_200d, 2) if mean_200d is not None else None,
        "daily": chart,
        "daily_200": mean_rows,
        "note": NOTE,
        "source": SOURCE,
    }


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    out = compute_market_cr5(day, force=force, progress=True)
    print(
        f"as_of={out['as_of']} mean_200d={out['mean_200d']} "
        f"chart_n={len(out['daily'])} last={out['daily'][-1] if out['daily'] else None}"
    )
