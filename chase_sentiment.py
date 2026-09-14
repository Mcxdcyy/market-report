#!/usr/bin/env python3
"""资金追高情绪：主板 / 创板（创业板+科创板）近30日三指标均值。

追高定义：（当日最高 − 昨收）/ 昨收 ≥ 7%。

指标（池不同）：
1. 昨追-赚钱效应：取**前一交易日**追高池，算当日 (今高−昨高)/昨收 均值
2. 昨追-今日承接：取**前一交易日**追高池，算当日 (今收−昨高)/昨高 均值
3. 今追-回落指数：取**当日**追高池，算当日 (今收−今高)/今高 均值

排除 ST、北交所；上市日历天数 ≤10 的个股不计入追高池。
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
RESULT_DIR = BASE / "chase_sentiment_results"
DATA_FILE = BASE / "大盘数据.numbers"
SERIES_DAYS = 30
CHASE_PCT = 0.07  # 日内最高相对昨收冲高 ≥7%
MIN_BARS = 3  # 至少需要 i>=2 才能判定昨追（需昨收相对前日）
SCHEMA = 3  # +上市≤10日排除

GROUP_ORDER = (
    ("main", "主板追高"),
    ("cyb", "创板追高"),  # 创业板 + 科创板
)

METRIC_KEYS = (
    ("money", "昨追-赚钱效应"),
    ("loss", "昨追-今日承接"),
    ("pullback", "今追-回落指数"),
)


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


def _group_of(bucket: str) -> str | None:
    if bucket == "main":
        return "main"
    if bucket in ("cyb", "kcb"):
        return "cyb"
    return None


def _is_chase_at(closes: list[float], highs: list[float], i: int) -> bool:
    """第 i 根 K 是否追高：(当日最高−昨收)/昨收 ≥ 7%。"""
    if i < 1:
        return False
    prev_c = closes[i - 1]
    cur_h = highs[i]
    if prev_c <= 0 or cur_h <= 0:
        return False
    return (cur_h - prev_c) / prev_c >= CHASE_PCT


def _day_metric_means(
    series: list[tuple],
    d: date,
) -> dict[str, dict[str, float | int | None]]:
    """某日两组：昨追池→赚钱/承接；今追池→回落。"""
    ds = d.isoformat()
    acc: dict[str, dict[str, list[float]]] = {
        "main": {"money": [], "loss": [], "pullback": []},
        "cyb": {"money": [], "loss": [], "pullback": []},
    }
    for s, idx, closes, highs, _lows in series:
        g = _group_of(s["bucket"])
        if g is None:
            continue
        list_date = s.get("list_date")
        if list_date is not None and (d - list_date).days <= ts.LIST_DAYS_MIN:
            continue
        i = idx.get(ds)
        if i is None or i < 1:
            continue
        n_bars = i + 1
        if n_bars < MIN_BARS:
            continue
        if list_date is None and n_bars <= ts.LIST_DAYS_MIN:
            continue

        prev_c = closes[i - 1]
        prev_h = highs[i - 1]
        cur_h = highs[i]
        cur_c = closes[i]
        if prev_c <= 0 or prev_h <= 0 or cur_h <= 0:
            continue

        # 昨追池：前一交易日追高 → 今日赚钱 / 承接
        if i >= 2 and _is_chase_at(closes, highs, i - 1):
            money = (cur_h - prev_h) / prev_c
            loss = (cur_c - prev_h) / prev_h
            acc[g]["money"].append(money)
            acc[g]["loss"].append(loss)

        # 今追池：当日追高 → 今日回落
        if _is_chase_at(closes, highs, i):
            pullback = (cur_c - cur_h) / cur_h
            acc[g]["pullback"].append(pullback)

    out: dict[str, dict[str, float | int | None]] = {}
    for g in ("main", "cyb"):
        n_yday = len(acc[g]["money"])
        n_today = len(acc[g]["pullback"])
        out[g] = {
            "n_yday": n_yday,
            "n_today": n_today,
            # 兼容旧字段：页面优先用分指标 n
            "n": n_today,
            "money": round(100.0 * sum(acc[g]["money"]) / n_yday, 4) if n_yday else None,
            "loss": round(100.0 * sum(acc[g]["loss"]) / n_yday, 4) if n_yday else None,
            "pullback": round(100.0 * sum(acc[g]["pullback"]) / n_today, 4) if n_today else None,
        }
    return out


def compute_chase_sentiment(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """计算并缓存近 SERIES_DAYS 日追高情绪。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if (
                cached.get("as_of") == as_of_d.isoformat()
                and cached.get("groups")
                and cached.get("schema") == SCHEMA
                and int(cached.get("n_days") or 0) >= SERIES_DAYS
            ):
                return cached
        except json.JSONDecodeError:
            pass

    # 多取 1 日：首日昨追池需要再往前一天的追高判定
    need = SERIES_DAYS + 1
    if trading_days is None:
        days = trading_days_ending(as_of_d, need)
    else:
        days = [d for d in trading_days if d <= as_of_d]
        days = sorted(set(days))
        if len(days) < need:
            extra = trading_days_ending(as_of_d, need)
            days = sorted(set(days) | set(extra))
        days = days[-need:]

    if len(days) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": SCHEMA,
            "groups": {},
            "note": "交易日不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    series_days = days[-SERIES_DAYS:]
    # 长 K 覆盖到 series 前一日，便于判定首日昨追
    k_days = days[-(SERIES_DAYS + 1) :] if len(days) >= SERIES_DAYS + 1 else days
    if progress:
        print(f"[chase] 近{len(series_days)}日追高情绪 → {series_days[0]}…{series_days[-1]}")

    long_series = ts._prepare_long_series(
        as_of_d, k_days, progress=progress, log_tag="chase"
    )

    daily_rows: list[dict[str, Any]] = []
    t0 = time.time()
    for d in series_days:
        means = _day_metric_means(long_series, d)
        daily_rows.append({"date": d.isoformat(), **means})

    groups: dict[str, dict] = {}
    for gkey, gname in GROUP_ORDER:
        metrics: dict[str, list] = {mk: [] for mk, _ in METRIC_KEYS}
        for row in daily_rows:
            g = row.get(gkey) or {}
            n_yday = int(g.get("n_yday") or 0)
            n_today = int(g.get("n_today") or 0)
            for mk, _ in METRIC_KEYS:
                n = n_today if mk == "pullback" else n_yday
                metrics[mk].append(
                    {
                        "date": row["date"],
                        "value": g.get(mk),
                        "n": n,
                    }
                )
        groups[gkey] = {
            "name": gname,
            "metrics": {
                mk: {"name": mname, "series": metrics[mk]}
                for mk, mname in METRIC_KEYS
            },
        }

    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": SCHEMA,
        "start": series_days[0].isoformat(),
        "end": series_days[-1].isoformat(),
        "n_days": len(series_days),
        "chase_pct": CHASE_PCT * 100,
        "groups": groups,
        "note": (
            "追高：日内最高相对昨收≥7%。"
            "昨追-赚钱效应/昨追-今日承接：取前一交易日追高池，分别算(今高−昨高)/昨收、(今收−昨高)/昨高。"
            "今追-回落指数：取当日追高池，算(今收−今高)/今高。"
            "创板=创业板+科创板；不含ST、北交所；不含上市日历天数≤10的个股。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        main = daily_rows[-1].get("main") or {}
        cyb = daily_rows[-1].get("cyb") or {}
        print(
            f"[chase] 完成 · 末日主板昨追{main.get('n_yday')}家/今追{main.get('n_today')}家 · "
            f"创板昨追{cyb.get('n_yday')}家/今追{cyb.get('n_today')}家 "
            f"用时{time.time()-t0:.0f}s → {out_path.name}"
        )
    return payload


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_chase_sentiment(day, force=force, progress=True)
