#!/usr/bin/env python3
"""涨停板块 · 近N日核心题材并集（2026-11-06 定稿）。

入选 = 去重并集：
  A 板块-资金认可度「当日页面展示卡」
  ∪ B 近 5 个交易日「有效上榜日」(当日涨停家数≥5) ≥2
  ∪ C 报表当日涨停家数 ≥5

永久排除：ST / 并购重组 / 实控人变更 / 季报·中报·半年报·年报

排序：event_priority P1→P2→P3（无优先级靠后）；同档按当日涨停家数降序。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
FUND_RESULT_DIR = BASE / "fund_recognition_results"

EFF_LOOKBACK = 5
EFF_MIN_DAYS = 2
EFF_COUNT = 5

FUND_EXCLUDE_NAMES = frozenset({"并购重组", "实控人变更"})
FUND_EXCLUDE_REPORT_KW = ("季报", "中报", "半年报", "年报")

_P_RANK = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}


def _as_date(x: date | datetime | str) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _excluded(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if n in FUND_EXCLUDE_NAMES:
        return True
    if any(k in n for k in FUND_EXCLUDE_REPORT_KW):
        return True
    return "ST" in n.upper()


def _fund_display_names(as_of: date, fund_block: dict | None = None) -> set[str]:
    """与页面「板块-资金认可度」展示过滤同口径。"""
    block = fund_block
    if block is None:
        path = FUND_RESULT_DIR / f"{as_of.isoformat()}.json"
        if path.exists():
            import json

            try:
                block = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                block = None
    if not block:
        try:
            from fund_recognition import compute_fund_recognition

            block = compute_fund_recognition(as_of, force=False, progress=False)
        except Exception:  # noqa: BLE001
            return set()

    out: set[str] = set()
    for it in block.get("items") or []:
        name = str(it.get("name") or "").strip()
        if _excluded(name):
            continue
        if int(it.get("pool_n") or 0) < 10:
            continue
        series = it.get("series") or []
        if not series:
            continue
        last = series[-1] or {}
        today_n = int(last.get("n_amt") or 0)
        if today_n < 10:
            continue
        peak_n = 0
        for pt in series:
            if pt.get("before_debut") or pt.get("before_amt10"):
                continue
            peak_n = max(peak_n, int(pt.get("n_amt") or 0))
        if peak_n > 0 and today_n < peak_n * 0.6:
            continue
        streak = 0
        for pt in reversed(series):
            if pt.get("before_debut") or pt.get("before_amt10"):
                break
            pct = pt.get("pct")
            if pct is not None and float(pct) >= 50.0:
                break
            streak += 1
        if streak >= 10:
            continue
        out.add(name)
    return out


def _kpl_counts_codes(
    as_of: date,
) -> tuple[dict[str, int], dict[str, list[str]], list[date]]:
    from fetch_kpl_sectors import load_history, load_stocks_history
    from trading_calendar import trading_days_ending

    days = trading_days_ending(as_of, max(EFF_LOOKBACK, 20), refresh=False)
    counts = load_history()
    stocks = load_stocks_history()
    return counts, stocks, days


def _eff_b_names(as_of: date, counts: dict, days: list[date]) -> set[str]:
    day_i = {d: i for i, d in enumerate(days)}
    if as_of not in day_i:
        return set()
    ai = day_i[as_of]
    # 需要完整轴：trading_days_ending 末位应是 as_of
    out: set[str] = set()
    names: set[str] = set()
    for j in range(EFF_LOOKBACK):
        if ai - j < 0:
            break
        ds = days[ai - j].isoformat()
        for name in (counts.get(ds) or {}):
            if not _excluded(name):
                names.add(name)
    for name in names:
        n_eff = 0
        for j in range(EFF_LOOKBACK):
            if ai - j < 0:
                break
            ds = days[ai - j].isoformat()
            try:
                c = int((counts.get(ds) or {}).get(name) or 0)
            except (TypeError, ValueError):
                c = 0
            if c >= EFF_COUNT:
                n_eff += 1
        if n_eff >= EFF_MIN_DAYS:
            out.add(name)
    return out


def _today_c_names(as_of: date, counts: dict) -> set[str]:
    ds = as_of.isoformat()
    out: set[str] = set()
    for name, raw in (counts.get(ds) or {}).items():
        if _excluded(name):
            continue
        try:
            c = int(raw or 0)
        except (TypeError, ValueError):
            c = 0
        if c >= EFF_COUNT:
            out.add(name)
    return out


def compute_core_sector_names(
    as_of: date | datetime | str,
    *,
    fund_block: dict | None = None,
) -> dict[str, Any]:
    """返回 {names, A, B, C, counts, stocks_map}。"""
    as_of_d = _as_date(as_of)
    counts, stocks, days = _kpl_counts_codes(as_of_d)
    # 确保 as_of 在轴上：若日历末不是 as_of，用更长窗口
    if not days or days[-1] != as_of_d:
        from trading_calendar import trading_days_ending

        days = trading_days_ending(as_of_d, 40, refresh=False)
    A = _fund_display_names(as_of_d, fund_block=fund_block)
    B = _eff_b_names(as_of_d, counts, days)
    C = _today_c_names(as_of_d, counts)
    names = A | B | C
    return {
        "as_of": as_of_d.isoformat(),
        "names": names,
        "A": A,
        "B": B,
        "C": C,
        "counts": counts,
        "stocks": stocks,
        "rule": {
            "B": f"近{EFF_LOOKBACK}日有效上榜(count≥{EFF_COUNT})≥{EFF_MIN_DAYS}",
            "C": f"当日count≥{EFF_COUNT}",
            "A": "资金认可度当日展示卡",
        },
    }


def _count_on(name: str, as_of: date, counts: dict) -> int:
    try:
        return int((counts.get(as_of.isoformat()) or {}).get(name) or 0)
    except (TypeError, ValueError):
        return 0


def _codes_on(name: str, as_of: date, stocks: dict) -> list[str]:
    raw = (stocks.get(as_of.isoformat()) or {}).get(name) or []
    return [str(c).zfill(6) for c in raw if c]


def _p_rank(sec: dict) -> int:
    p = str(sec.get("event_priority") or "").strip().upper()
    return _P_RANK.get(p, 9)


def build_core_display_sectors(
    as_of: date | datetime | str,
    json_sectors: list | None,
    *,
    fund_block: dict | None = None,
) -> list[dict]:
    """并集名单 + 合并 market_news 里的 reason/events/priority；按 P 再按当日家数排序。"""
    as_of_d = _as_date(as_of)
    meta = compute_core_sector_names(as_of_d, fund_block=fund_block)
    names: set[str] = meta["names"]
    counts = meta["counts"]
    stocks = meta["stocks"]
    A, B, C = meta["A"], meta["B"], meta["C"]

    by_name: dict[str, dict] = {}
    for sec in json_sectors or []:
        n = str(sec.get("name") or "").strip()
        if n:
            by_name[n] = dict(sec)

    out: list[dict] = []
    for name in names:
        if _excluded(name):
            continue
        base = by_name.get(name) or {"name": name}
        row = dict(base)
        row["name"] = name
        cnt = _count_on(name, as_of_d, counts)
        row["count"] = cnt
        codes = _codes_on(name, as_of_d, stocks)
        if codes:
            row["codes"] = codes
            if not row.get("stocks"):
                row["stocks"] = [{"code": c, "name": c} for c in codes]
        row["core_via"] = "".join(
            [
                "A" if name in A else "",
                "B" if name in B else "",
                "C" if name in C else "",
            ]
        )
        out.append(row)

    out.sort(key=lambda s: (_p_rank(s), -int(s.get("count") or 0), s.get("name") or ""))
    return out
