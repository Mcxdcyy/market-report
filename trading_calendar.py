#!/usr/bin/env python3
"""A 股交易日日历：不依赖大盘数据.numbers。

真源优先开盘啦实际量能日线（`kpl_market_volume.json`），并与新高序列等本地缓存并集。
更新报表时：`resolve_report_as_of()` → 当天若为交易日用当天，否则用 ≤ 当天的最近交易日。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
CACHE_FILE = BASE / "trading_calendar.json"


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _collect_local_dates() -> set[str]:
    """从已有本地缓存收集交易日（ISO）。"""
    out: set[str] = set()
    vol = BASE / "kpl_market_volume.json"
    if vol.exists():
        try:
            data = json.loads(vol.read_text(encoding="utf-8"))
            for row in data.get("daily") or []:
                ds = str(row.get("date") or "")[:10]
                if len(ds) == 10:
                    out.add(ds)
        except json.JSONDecodeError:
            pass
    series = BASE / "new_high_count_results" / "series.json"
    if series.exists():
        try:
            data = json.loads(series.read_text(encoding="utf-8"))
            for k in (data.get("days") or {}):
                if len(str(k)) >= 10:
                    out.add(str(k)[:10])
        except json.JSONDecodeError:
            pass
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for ds in data.get("days") or []:
                if len(str(ds)) >= 10:
                    out.add(str(ds)[:10])
        except json.JSONDecodeError:
            pass
    return out


def refresh_calendar(*, as_of: date | None = None, progress: bool = True) -> list[date]:
    """拉取/对齐开盘啦量能日线，写回 trading_calendar.json，返回升序交易日列表。"""
    as_of_d = as_of or date.today()
    try:
        from fetch_kpl_volume import ensure_kpl_volume_series

        # 拉足量：日历覆盖尽量长
        ensure_kpl_volume_series(as_of_d, n=400, force=False, progress=progress)
    except Exception as exc:  # noqa: BLE001
        if progress:
            print(f"[calendar] 开盘啦量能刷新失败，沿用本地: {exc}")

    days_s = sorted(_collect_local_dates())
    if not days_s:
        raise RuntimeError("无法构建交易日日历：开盘啦量能与本地缓存皆空")
    payload = {
        "as_of": as_of_d.isoformat(),
        "n": len(days_s),
        "days": days_s,
        "source": "开盘啦实际量能日线 ∪ 本地序列缓存",
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(f"[calendar] 交易日 {len(days_s)} 日 · 末={days_s[-1]} → {CACHE_FILE.name}")
    return [_as_date(x) for x in days_s]


def load_trading_days(*, refresh: bool = False, as_of: date | None = None) -> list[date]:
    if refresh or not CACHE_FILE.exists():
        return refresh_calendar(as_of=as_of or date.today(), progress=True)
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        days = [_as_date(x) for x in (data.get("days") or [])]
        if days:
            # 若缓存末日早于 today/as_of，自动再刷一次
            tip = as_of or date.today()
            if days[-1] < tip and (tip - days[-1]).days <= 10:
                return refresh_calendar(as_of=tip, progress=True)
            return days
    except json.JSONDecodeError:
        pass
    return refresh_calendar(as_of=as_of or date.today(), progress=True)


def resolve_report_as_of(
    ref: date | datetime | str | None = None,
    *,
    refresh: bool = True,
    progress: bool = True,
) -> date:
    """报表日：ref（默认今天）若为交易日则用之，否则用 ≤ref 的最近交易日。"""
    tip = _as_date(ref) if ref is not None else date.today()
    days = load_trading_days(refresh=refresh, as_of=tip)
    # 只要 ≤ tip 的最后一天
    cand = [d for d in days if d <= tip]
    if not cand:
        raise RuntimeError(f"交易日日历在 {tip} 及之前为空")
    as_of = cand[-1]
    if progress:
        if as_of == tip:
            print(f"[calendar] 报表日 = {as_of}（当日为交易日）")
        else:
            print(f"[calendar] 报表日 = {as_of}（{tip} 非交易日，取最近交易日）")
    return as_of


def trading_days_ending(end: date | datetime | str, n: int, *, refresh: bool = False) -> list[date]:
    end_d = _as_date(end)
    days = [d for d in load_trading_days(refresh=refresh, as_of=end_d) if d <= end_d]
    return days[-n:] if len(days) >= n else days


def next_trading_day_after(d: date | datetime | str, *, refresh: bool = False) -> date:
    """严格大于 d 的下一交易日；日历不足时回退：跳过周末。"""
    day = _as_date(d)
    days = load_trading_days(refresh=refresh, as_of=day + timedelta(days=14))
    for x in days:
        if x > day:
            return x
    nd = day + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def is_trading_day(d: date | datetime | str, *, refresh: bool = False) -> bool:
    day = _as_date(d)
    return day in set(load_trading_days(refresh=refresh, as_of=day))
