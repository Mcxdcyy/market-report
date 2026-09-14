#!/usr/bin/env python3
"""资金认可度：开盘啦近30日涨停板块整合池 × 近20日均线认可占比。

口径：
- 板块：近 30 个交易日开盘啦涨停原因中出现过的全部板块
- 个股池：该板块 30 日内曾上榜代码去重
- 按日：池内成交额 >3 亿 → 再算收盘>MA5 且收盘>MA10 的占比
- 结果：每板块近 20 个交易日序列
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

from numbers_parser import Document

import trend_strength as ts
from fetch_kpl_sectors import (
    fetch_theme_stocks,
    load_stocks_history,
    save_stocks_history,
)

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "fund_recognition_results"
DATA_FILE = BASE / "大盘数据.numbers"
POOL_DAYS = 30
SERIES_DAYS = 20
AMOUNT_MIN = 3e8  # 3 亿元
KLINE_NEED = 40  # MA10 + 20 日余量
WORKERS = 48


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
    if len(dates) < n:
        return dates
    return dates[-n:]


def ensure_stocks_for_days(days: list[date], *, progress: bool = True) -> dict[str, dict[str, list[str]]]:
    """确保 stocks 历史覆盖给定交易日；不足则拉取。"""
    from fetch_kpl_sectors import load_history, save_history

    hist = load_stocks_history()
    count_hist = load_history()
    missing = [d.isoformat() for d in days if not hist.get(d.isoformat())]
    if missing and progress:
        print(f"[fund] 补开盘啦板块个股 {len(missing)} 日…")
    for ds in missing:
        try:
            stocks_map = fetch_theme_stocks(ds)
        except Exception as exc:  # noqa: BLE001
            if progress:
                print(f"[fund] skip {ds}: {exc}")
            continue
        if not stocks_map and ds == date.today().isoformat():
            from fetch_kpl_sectors import fetch_limit_up_sectors

            try:
                live = fetch_limit_up_sectors(ds)
                stocks_map = {
                    s["name"]: [str(c).zfill(6) for c in (s.get("codes") or [])]
                    for s in live.get("sectors") or []
                    if s.get("name")
                }
            except Exception:
                stocks_map = {}
        if stocks_map:
            hist[ds] = stocks_map
            count_hist[ds] = {name: len(codes) for name, codes in stocks_map.items()}
            if progress:
                print(f"[fund] {ds} 板块 {len(stocks_map)}")
    if missing:
        save_stocks_history(hist)
        save_history(count_hist)
    return hist


def build_sector_pools(
    stocks_hist: dict[str, dict[str, list[str]]],
    pool_days: list[date],
) -> list[dict[str, Any]]:
    """近 pool_days 合并板块池；按累计上榜家次（每日家数之和）降序。"""
    pools: dict[str, set[str]] = {}
    hit_counts: dict[str, int] = {}
    for d in pool_days:
        day_map = stocks_hist.get(d.isoformat()) or {}
        for name, codes in day_map.items():
            if not name:
                continue
            hit_counts[name] = hit_counts.get(name, 0) + len(codes)
            buckets = pools.setdefault(name, set())
            for c in codes:
                code = str(c).zfill(6)
                if code:
                    buckets.add(code)
    items = [
        {
            "name": name,
            "codes": sorted(codes),
            "pool_n": len(codes),
            "hit_count": hit_counts.get(name, 0),
        }
        for name, codes in pools.items()
        if codes
    ]
    items.sort(key=lambda x: (-x["hit_count"], -x["pool_n"], x["name"]))
    return items


def fetch_klines_long(code: str, as_of: date) -> list[dict]:
    """拉取足够覆盖近20日+MA10 的日 K（含成交额）。"""
    sym = ts.sina_symbol(code)
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={sym},day,,,{KLINE_NEED},qfq"
    )
    try:
        payload = ts._http_json(url, timeout=15)
    except Exception:
        payload = None
    rows: list[dict] = []
    if payload:
        block = (payload.get("data") or {}).get(sym) or {}
        day = block.get("qfqday") or block.get("day") or []
        for item in day:
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                continue
            try:
                row = {
                    "date": str(item[0])[:10],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                }
                if len(item) >= 9:
                    try:
                        amt = float(item[8]) * 10000.0
                        if amt > 0:
                            row["amount"] = amt
                    except (TypeError, ValueError):
                        pass
                rows.append(row)
            except (TypeError, ValueError):
                continue
    if len(rows) < 15:
        rows = ts.fetch_klines_for_stock(code, None, as_of) or rows
    return rows


def ensure_klines(codes: list[str], as_of: date, *, progress: bool = True) -> dict[str, list[dict]]:
    cache = ts.load_kline_cache()
    long_cache: dict[str, list[dict]] = {}
    if getattr(ts, "LONG_CACHE_FILE", None) and ts.LONG_CACHE_FILE.exists():
        try:
            long_cache = ts.load_long_kline_cache()
        except Exception:
            long_cache = {}

    need: list[str] = []
    for code in codes:
        rows = cache.get(code) or long_cache.get(code) or []
        if rows and ts.cache_covers(rows, as_of) and len(rows) >= 15:
            if code not in cache:
                cache[code] = rows
            continue
        need.append(code)

    if need and progress:
        print(f"[fund] 补拉 K 线 {len(need)} 只…")
    if need:
        t0 = time.time()
        done = 0

        def _one(code: str) -> tuple[str, list[dict]]:
            return code, fetch_klines_long(code, as_of)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_one, c): c for c in need}
            for fut in as_completed(futs):
                code, rows = fut.result()
                if rows:
                    cache[code] = rows
                done += 1
                if progress and done % 200 == 0:
                    print(f"[fund] K线 {done}/{len(need)} 用时{time.time()-t0:.0f}s")
        ts.save_kline_cache(cache)
        if progress:
            print(f"[fund] K线完成，用时 {time.time()-t0:.0f}s")
    return cache


def _stock_day_flags(rows: list[dict], day: date) -> tuple[bool, bool]:
    """(in_amount_denom, meets_ma). in_amount_denom: 当日成交额>3亿。"""
    ds = day.isoformat()
    idx = {r["date"]: i for i, r in enumerate(rows)}
    i = idx.get(ds)
    if i is None or i < 9:
        return False, False
    row = rows[i]
    amt = row.get("amount")
    if amt is None or float(amt) <= AMOUNT_MIN:
        return False, False
    closes = [float(r["close"]) for r in rows[: i + 1]]
    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    close = closes[-1]
    return True, (close > ma5 and close > ma10)


def compute_sector_series(
    codes: list[str],
    cache: dict[str, list[dict]],
    series_days: list[date],
) -> list[dict]:
    out = []
    for d in series_days:
        n_ok = 0
        n_amt = 0
        for code in codes:
            rows = cache.get(code) or []
            if not rows:
                continue
            truncated = ts.truncate_as_of(rows, d)
            if len(truncated) < 10:
                continue
            in_denom, ok = _stock_day_flags(truncated, d)
            if in_denom:
                n_amt += 1
                if ok:
                    n_ok += 1
        if n_amt <= 0:
            out.append({"date": d.isoformat(), "pct": None, "n_ok": 0, "n_amt": 0})
        else:
            out.append(
                {
                    "date": d.isoformat(),
                    "pct": round(100.0 * n_ok / n_amt, 1),
                    "n_ok": n_ok,
                    "n_amt": n_amt,
                }
            )
    return out


def compute_fund_recognition(
    as_of: date | datetime | str,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if cached.get("items"):
                return cached
        except json.JSONDecodeError:
            pass

    pool_days = trading_days_ending(as_of_d, POOL_DAYS)
    series_days = trading_days_ending(as_of_d, SERIES_DAYS)
    if len(series_days) < 5:
        return {
            "as_of": as_of_d.isoformat(),
            "items": [],
            "note": "交易日不足，暂无资金认可度",
        }

    if progress:
        print(
            f"[fund] as_of={as_of_d} 池窗口 {pool_days[0]}→{pool_days[-1]} "
            f"序列 {series_days[0]}→{series_days[-1]}"
        )

    stocks_hist = ensure_stocks_for_days(pool_days, progress=progress)
    pools = build_sector_pools(stocks_hist, pool_days)
    if progress:
        print(f"[fund] 板块 {len(pools)} 个")

    all_codes: list[str] = sorted({c for p in pools for c in p["codes"]})
    cache = ensure_klines(all_codes, as_of_d, progress=progress)

    items = []
    for p in pools:
        series = compute_sector_series(p["codes"], cache, series_days)
        items.append(
            {
                "name": p["name"],
                "pool_n": p["pool_n"],
                "hit_count": p["hit_count"],
                "series": series,
            }
        )

    result = {
        "as_of": as_of_d.isoformat(),
        "pool_start": pool_days[0].isoformat(),
        "pool_end": pool_days[-1].isoformat(),
        "series_start": series_days[0].isoformat(),
        "series_end": series_days[-1].isoformat(),
        "amount_min_yi": 3,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(f"[fund] 完成 → {out_path.name} · {len(items)} 板块")
    return result


def load_or_compute_fund_recognition(as_of: date | datetime | str, *, force: bool = False) -> dict:
    return compute_fund_recognition(as_of, force=force, progress=True)


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_fund_recognition(day, force=force, progress=True)
