#!/usr/bin/env python3
"""大小盘相对强弱（试看）：中证1000 − 沪深300 近5日涨跌幅差。

  R = 小盘近 N 日涨跌% − 大盘近 N 日涨跌%
  大 = 沪深300 sh000300；小 = 中证1000 sh000852；N = 5

柱图横轴近 120 日；零轴 = 0（上红=小盘相对强 / 下绿=大盘相对强）。
缓存：style_ls_spread_results/YYYY-MM-DD.json
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from index_ma60_macro import get_daily

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "style_ls_spread_results"
SCHEMA = 1
CHART_DAYS = 120
MEAN_DAYS = 200
RET_N = 5
FETCH_NEED = MEAN_DAYS + RET_N + 20

# (name, sina_symbol, em_secid)
LARGE = ("沪深300", "sh000300", "1.000300")
SMALL = ("中证1000", "sh000852", "1.000852")


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _close_map(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        ds = str(r.get("date") or "")[:10]
        try:
            out[ds] = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _ret_n(closes_by_date: dict[str, float], dates: list[str], i: int, n: int) -> float | None:
    """dates[i] 相对 dates[i-n] 的涨跌幅%（需恰好隔 n 个交易日）。"""
    if i < n:
        return None
    d0, d1 = dates[i - n], dates[i]
    c0, c1 = closes_by_date.get(d0), closes_by_date.get(d1)
    if c0 is None or c1 is None or c0 == 0:
        return None
    return (c1 / c0 - 1.0) * 100.0


def compute_style_ls_spread(as_of: date | datetime | str, *, force: bool = False) -> dict:
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if int(cached.get("schema") or 0) == SCHEMA and cached.get("as_of") == as_of_d.isoformat():
                return cached
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    large_rows = get_daily(LARGE[1], LARGE[2])
    small_rows = get_daily(SMALL[1], SMALL[2])
    large_m = _close_map(large_rows)
    small_m = _close_map(small_rows)
    dates = sorted(set(large_m) & set(small_m))
    dates = [d for d in dates if d <= as_of_d.isoformat()]
    if len(dates) < RET_N + 5:
        raise RuntimeError(f"大小盘日K交集不足：{len(dates)} 日（截至 {as_of_d}）")
    if dates[-1] != as_of_d.isoformat():
        raise RuntimeError(
            f"大小盘日K末日={dates[-1]}，报表日={as_of_d.isoformat()}，日期未对齐"
        )

    series_all: list[dict] = []
    for i, ds in enumerate(dates):
        r_l = _ret_n(large_m, dates, i, RET_N)
        r_s = _ret_n(small_m, dates, i, RET_N)
        if r_l is None or r_s is None:
            continue
        spread = r_s - r_l
        series_all.append(
            {
                "date": ds,
                "value": round(spread, 3),
                "large_ret": round(r_l, 3),
                "small_ret": round(r_s, 3),
            }
        )

    if not series_all:
        raise RuntimeError("大小盘相对强弱序列为空")

    mean_slice = series_all[-MEAN_DAYS:]
    mean_200 = sum(float(x["value"]) for x in mean_slice) / len(mean_slice)
    chart = series_all[-CHART_DAYS:]
    last = series_all[-1]

    payload = {
        "schema": SCHEMA,
        "as_of": as_of_d.isoformat(),
        "ret_n": RET_N,
        "chart_days": CHART_DAYS,
        "mean_days": MEAN_DAYS,
        "large": {"name": LARGE[0], "symbol": LARGE[1]},
        "small": {"name": SMALL[0], "symbol": SMALL[1]},
        "mean_200d": round(mean_200, 4),
        "today": last["value"],
        "series": chart,
        "note": (
            f"R = {SMALL[0]}近{RET_N}日涨跌% − {LARGE[0]}近{RET_N}日涨跌%；"
            "上红=小盘相对强，下绿=大盘相对强；零轴=0。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_style_ls_spread(
    as_of: date | datetime | str,
    *,
    latest: date | datetime | str | None = None,
    force: bool = False,
) -> dict:
    as_of_d = _as_date(as_of)
    latest_d = _as_date(latest) if latest is not None else as_of_d
    is_latest = as_of_d == latest_d
    path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if path.exists() and not (force and is_latest):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if int(cached.get("schema") or 0) == SCHEMA:
                return cached
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if not is_latest and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return compute_style_ls_spread(as_of_d, force=force or is_latest)


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    out = compute_style_ls_spread(day, force=True)
    print(out["as_of"], "today", out["today"], "mean200", out["mean_200d"], "n", len(out["series"]))
