#!/usr/bin/env python3
"""新高数量：每日创历史新高个股家数（近30 / 近120 交易日）。

口径：
- 创新高：当日最高价 = 截至当日可用日 K 的历史最高价（严格高于此前最高）
- 排除：北交所、ST
- 样本：当日上市交易日数严格大于 10（含上市首日累计交易日 > 10）
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from numbers_parser import Document

import trend_strength as ts

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "new_high_count_results"
DATA_FILE = BASE / "大盘数据.numbers"
SERIES_DAYS = 120
SCHEMA = 1
MIN_LIST_TRADING_DAYS = 10  # 严格大于 10 → 至少 11 个交易日


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def trading_days_ending(end: date, n: int) -> list[date]:
    doc = Document(str(DATA_FILE))
    t = doc.sheets[0].tables[0]
    dates: list[date] = []
    for r in range(2, t.num_rows):
        v = t.cell(r, 0).value
        if hasattr(v, "year"):
            d = v.date() if isinstance(v, datetime) else v
            if d <= end:
                dates.append(d)
    dates = sorted(set(dates))
    return dates[-n:] if len(dates) >= n else dates


def _listed_trading_days(
    list_date: date | None,
    d: date,
    i: int,
    trading_set: set[date],
) -> int:
    """截至 d（含）已上市交易日数。优先用上市日+交易日历；否则用已有 K 线根数。"""
    if list_date is not None:
        return sum(1 for td in trading_set if list_date <= td <= d)
    return i + 1


def _is_new_high_at(highs: list[float], i: int) -> bool:
    """第 i 根是否创截至当日的可见历史新高（最高价严格大于此前全部）。"""
    if i <= 0 or i >= len(highs):
        return False
    cur = highs[i]
    if cur <= 0:
        return False
    prev_max = max(highs[:i])
    return cur > prev_max


def _count_on_day(
    series: list[tuple],
    d: date,
    trading_set: set[date],
) -> int:
    ds = d.isoformat()
    n = 0
    for s, idx, _closes, highs, _lows in series:
        bucket = s.get("bucket")
        if bucket in ("bj", "st"):
            continue
        if ts.is_bj_code(s.get("code") or "") or ts.is_st_name(s.get("name") or ""):
            continue
        i = idx.get(ds)
        if i is None:
            continue
        listed = _listed_trading_days(s.get("list_date"), d, i, trading_set)
        if listed <= MIN_LIST_TRADING_DAYS:
            continue
        if _is_new_high_at(highs, i):
            n += 1
    return n


def compute_new_high_count(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """计算并缓存近 SERIES_DAYS 日新高家数。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if (
                cached.get("as_of") == as_of_d.isoformat()
                and cached.get("schema") == SCHEMA
                and int(cached.get("n_days") or 0) >= SERIES_DAYS
                and cached.get("daily")
            ):
                return cached
        except json.JSONDecodeError:
            pass

    # 多取 1 日：首柱较前日着色
    need = SERIES_DAYS + 1
    if trading_days is None:
        days = trading_days_ending(as_of_d, need)
    else:
        days = sorted({d for d in trading_days if d <= as_of_d})
        if len(days) < need:
            days = sorted(set(days) | set(trading_days_ending(as_of_d, need)))
        days = days[-need:]

    if len(days) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": SCHEMA,
            "daily": [],
            "note": "交易日不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    series_days = days[-SERIES_DAYS:]
    k_days = days
    if progress:
        print(f"[newhigh] 近{len(series_days)}日新高数量 → {series_days[0]}…{series_days[-1]}")

    long_series = ts._prepare_long_series(
        as_of_d, k_days, progress=progress, log_tag="newhigh"
    )
    # 上市交易日判定用更长日历（含上市日可能早于窗口）
    cal_all = trading_days_ending(as_of_d, max(need + 30, 200))
    trading_set = set(cal_all)

    t0 = time.time()
    daily: list[dict[str, Any]] = []
    for d in days:
        cnt = _count_on_day(long_series, d, trading_set)
        daily.append({"date": d.isoformat(), "count": cnt})

    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": SCHEMA,
        "start": series_days[0].isoformat(),
        "end": series_days[-1].isoformat(),
        "n_days": len(series_days),
        "daily": daily,  # 含窗口前 1 日，供首柱着色
        "note": (
            "新高：当日最高价创截至当日可用日K历史新高。"
            "不含北交所、ST；上市交易日数须严格大于10。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        last = daily[-1] if daily else {}
        print(
            f"[newhigh] 完成 · 末日 {last.get('count')} 家 "
            f"用时{time.time()-t0:.0f}s → {out_path.name}"
        )
    return payload


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_new_high_count(day, force=force, progress=True)
