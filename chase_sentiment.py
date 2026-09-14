#!/usr/bin/env python3
"""资金追高情绪：主板 / 创板（创业板+科创板）追高池三指标近20日均值。

追高定义：（当日最高 − 昨收）/ 昨收 ≥ 7%。

池内个股当日三指标：
1. 赚钱效应 =（今高 − 昨高）/ 昨收
2. 亏钱效应 =（今收 − 昨高）/ 昨高
3. 回落指数 =（今收 − 今高）/ 今高

每日取追高池算术均值；排除 ST、北交所。
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
SERIES_DAYS = 20
CHASE_PCT = 0.07  # 日内最高相对昨收冲高 ≥7%
MIN_BARS = 3  # 至少需要昨高/昨收

# 展示用两组
GROUP_ORDER = (
    ("main", "主板追高"),
    ("cyb", "创板追高"),  # 创业板 + 科创板
)

METRIC_KEYS = (
    ("money", "赚钱效应"),
    ("loss", "亏钱效应"),
    ("pullback", "回落指数"),
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
    """main → main；cyb/kcb → cyb；其余排除。"""
    if bucket == "main":
        return "main"
    if bucket in ("cyb", "kcb"):
        return "cyb"
    return None


def _day_pool_means(
    series: list[tuple],
    d: date,
) -> dict[str, dict[str, float | int | None]]:
    """某日两组追高池的三指标均值 + 家数。"""
    ds = d.isoformat()
    acc: dict[str, dict[str, list[float]]] = {
        "main": {"money": [], "loss": [], "pullback": []},
        "cyb": {"money": [], "loss": [], "pullback": []},
    }
    for s, idx, closes, highs, lows in series:
        g = _group_of(s["bucket"])
        if g is None:
            continue
        i = idx.get(ds)
        if i is None or i < 1:
            continue
        if i + 1 < MIN_BARS:
            continue
        prev_c = closes[i - 1]
        prev_h = highs[i - 1]
        cur_h = highs[i]
        cur_c = closes[i]
        if prev_c <= 0 or prev_h <= 0 or cur_h <= 0:
            continue
        # 追高：日内最高相对昨收 ≥7%
        if (cur_h - prev_c) / prev_c < CHASE_PCT:
            continue
        money = (cur_h - prev_h) / prev_c
        loss = (cur_c - prev_h) / prev_h
        pullback = (cur_c - cur_h) / cur_h
        acc[g]["money"].append(money)
        acc[g]["loss"].append(loss)
        acc[g]["pullback"].append(pullback)

    out: dict[str, dict[str, float | int | None]] = {}
    for g in ("main", "cyb"):
        n = len(acc[g]["money"])
        out[g] = {
            "n": n,
            "money": round(100.0 * sum(acc[g]["money"]) / n, 4) if n else None,
            "loss": round(100.0 * sum(acc[g]["loss"]) / n, 4) if n else None,
            "pullback": round(100.0 * sum(acc[g]["pullback"]) / n, 4) if n else None,
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
            if cached.get("as_of") == as_of_d.isoformat() and cached.get("groups"):
                return cached
        except json.JSONDecodeError:
            pass

    if trading_days is None:
        # 多取 1 日供首日着色对照（不写入序列）
        days = trading_days_ending(as_of_d, SERIES_DAYS + 1)
    else:
        days = [d for d in trading_days if d <= as_of_d]
        days = sorted(set(days))
        if len(days) < SERIES_DAYS + 1:
            extra = trading_days_ending(as_of_d, SERIES_DAYS + 1)
            days = sorted(set(days) | set(extra))
        days = days[-(SERIES_DAYS + 1) :]

    if len(days) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "groups": {},
            "note": "交易日不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    series_days = days[-SERIES_DAYS:]
    if progress:
        print(f"[chase] 近{len(series_days)}日追高情绪 → {series_days[0]}…{series_days[-1]}")

    # 复用长 K 线（与趋势均值同缓存）
    long_series = ts._prepare_long_series(
        as_of_d, series_days, progress=progress, log_tag="chase"
    )

    daily_rows: list[dict[str, Any]] = []
    t0 = time.time()
    for d in series_days:
        means = _day_pool_means(long_series, d)
        daily_rows.append({"date": d.isoformat(), **means})

    groups: dict[str, dict] = {}
    for gkey, gname in GROUP_ORDER:
        metrics: dict[str, list] = {mk: [] for mk, _ in METRIC_KEYS}
        for row in daily_rows:
            g = row.get(gkey) or {}
            for mk, _ in METRIC_KEYS:
                metrics[mk].append(
                    {
                        "date": row["date"],
                        "value": g.get(mk),
                        "n": int(g.get("n") or 0),
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
        "start": series_days[0].isoformat(),
        "end": series_days[-1].isoformat(),
        "n_days": len(series_days),
        "chase_pct": CHASE_PCT * 100,
        "groups": groups,
        "note": (
            "追高：日内最高相对昨收≥7%。"
            "赚钱效应=(今高−昨高)/昨收；亏钱效应=(今收−昨高)/昨高；回落指数=(今收−今高)/今高。"
            "创板=创业板+科创板；不含ST、北交所。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        main_n = (daily_rows[-1].get("main") or {}).get("n")
        cyb_n = (daily_rows[-1].get("cyb") or {}).get("n")
        print(
            f"[chase] 完成 · 末日主板{main_n}家 / 创板{cyb_n}家 "
            f"用时{time.time()-t0:.0f}s → {out_path.name}"
        )
    return payload


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_chase_sentiment(day, force=force, progress=True)
