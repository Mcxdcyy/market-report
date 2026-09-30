#!/usr/bin/env python3
"""A 股交易日日历：不依赖大盘数据.numbers。

真源优先开盘啦实际量能日线（`kpl_market_volume.json`），并与新高序列等本地缓存并集。
更新报表时：`resolve_report_as_of()` → 当天若为交易日用当天，否则用 ≤ 当天的最近交易日。
「下一交易日」若缓存轴尚未覆盖未来（长假前），按周末 + 法定休市推算，禁止仅跳周末。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
BASE = Path(__file__).resolve().parent
CACHE_FILE = BASE / "trading_calendar.json"
HOLIDAY_CACHE_FILE = BASE / "ashare_holidays.json"


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _fetch_cn_holiday_mmdd(year: int) -> dict[str, int]:
    """拉取国内节假日表（mmdd → 类型码；非 0 视为休市相关）。

    源：tool.bitefu.net/jiari（法定假日/调休）。周末未必列入；交易日判定另跳周末。
    """
    import urllib.request

    url = f"http://tool.bitefu.net/jiari/?d={int(year)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    block = raw.get(str(year)) if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise RuntimeError(f"节假日接口无 {year} 数据")
    out: dict[str, int] = {}
    for k, v in block.items():
        ks = str(k).zfill(4)
        if len(ks) != 4:
            continue
        try:
            out[ks] = int(v)
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"节假日接口 {year} 为空")
    return out


def load_cn_holiday_mmdd(year: int, *, refresh: bool = False) -> dict[str, int]:
    """按年缓存国内节假日 mmdd 表。"""
    year = int(year)
    cache: dict[str, Any] = {}
    if HOLIDAY_CACHE_FILE.exists():
        try:
            cache = json.loads(HOLIDAY_CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    years = cache.get("years") if isinstance(cache.get("years"), dict) else {}
    hit = years.get(str(year))
    if isinstance(hit, dict) and hit and not refresh:
        return {str(k).zfill(4): int(v) for k, v in hit.items()}

    fetched = _fetch_cn_holiday_mmdd(year)
    years[str(year)] = fetched
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "tool.bitefu.net/jiari",
        "years": years,
    }
    HOLIDAY_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return fetched


def _is_cn_statutory_rest(d: date) -> bool:
    """是否法定节假日/调休休息日（不含普通周末；周末另判）。"""
    try:
        table = load_cn_holiday_mmdd(d.year, refresh=False)
    except Exception:
        try:
            table = load_cn_holiday_mmdd(d.year, refresh=True)
        except Exception:
            return False
    code = table.get(f"{d.month:02d}{d.day:02d}")
    return code is not None and int(code) != 0


def is_projected_ashare_trading_day(d: date | datetime | str) -> bool:
    """推算是否 A 股交易日：非周末且非法定休市日。

    用于日历末日之后的「下一交易日」（量能缓存尚无未来日）。
    调休上班的周六日：节假日表通常不标休 → 若 weekday≥5 仍视为休市
    （A 股极少周末交易；若未来有例外再改）。
    """
    day = _as_date(d)
    if day.weekday() >= 5:
        return False
    if _is_cn_statutory_rest(day):
        return False
    return True


def _project_next_trading_day(day: date, *, horizon: int = 40) -> date:
    """从 day 之后向前推算下一交易日（跳过周末 + 法定休市）。"""
    nd = day + timedelta(days=1)
    for _ in range(max(1, int(horizon))):
        if is_projected_ashare_trading_day(nd):
            return nd
        nd += timedelta(days=1)
    raise RuntimeError(f"无法在 {horizon} 日内推算 {day} 之后的下一交易日（请检查节假日表）")


def _dates_from_long_kline_cache() -> set[str]:
    """从长K线缓存抽交易日（与趋势强度近200日均值同源覆盖）。

    开盘啦量能日线目前约百余日，不足200；长K（约260根）可补齐交易日轴。
    只取覆盖最长的一只样本股日期，避免全市场解析。
    """
    path = BASE / "trend_klines_cache_long.json"
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    best: list = []
    for rows in (raw or {}).values():
        if not rows:
            continue
        if len(rows) > len(best):
            best = rows
            if len(best) >= 260:
                break
    out: set[str] = set()
    for line in best:
        ds = str(line).split(",")[0][:10]
        if len(ds) == 10:
            out.add(ds)
    return out


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
    out |= _dates_from_long_kline_cache()
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
        "source": "开盘啦实际量能日线 ∪ 长K线交易日 ∪ 本地序列缓存",
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
    """严格大于 d 的下一交易日。

    优先用已缓存交易日轴；若轴尚未覆盖未来（如长假前），按「周末 + 法定休市」推算，
    **禁止**再仅跳周末把国庆等休市日当成下一交易日。
    """
    day = _as_date(d)
    days = load_trading_days(refresh=refresh, as_of=day)
    for x in days:
        if x > day:
            return x
    return _project_next_trading_day(day)


def is_trading_day(d: date | datetime | str, *, refresh: bool = False) -> bool:
    day = _as_date(d)
    known = set(load_trading_days(refresh=refresh, as_of=day))
    if day in known:
        return True
    # 已知轴末日之后：用节假日推算；轴内缺失日（停牌日等）仍以轴为准视为非交易日
    if known and day > max(known):
        return is_projected_ashare_trading_day(day)
    return False
