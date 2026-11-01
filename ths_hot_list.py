#!/usr/bin/env python3
"""同花顺热榜（小时 / 日）。报表「板块-资金认可度 · 核心个股」用小时榜。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "ths_hot_list_results"
THS_HOT_URL = (
    "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
)


def _http_json(url: str, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://eq.10jqka.com.cn/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_ths_hot_list(period: str = "hour") -> list[dict[str, Any]]:
    """拉取同花顺热榜。period: hour | day。返回按名次升序的列表。"""
    if period not in ("hour", "day"):
        raise ValueError(f"period 须为 hour/day，收到 {period!r}")
    url = f"{THS_HOT_URL}?stock_type=a&type={period}&list_type=normal"
    raw = _http_json(url)
    rows = ((raw.get("data") or {}).get("stock_list") or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "").zfill(6)
        if not code or len(code) != 6:
            continue
        try:
            rank = int(row.get("order") or row.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        if rank <= 0:
            continue
        name = str(row.get("name") or "").strip()
        heat = row.get("rate")
        try:
            heat_f = float(heat) if heat is not None else None
        except (TypeError, ValueError):
            heat_f = None
        pct = row.get("rise_and_fall")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        out.append(
            {
                "rank": rank,
                "code": code,
                "name": name,
                "heat": heat_f,
                "pct": pct_f,
            }
        )
    out.sort(key=lambda x: x["rank"])
    return out


def save_ths_hot_list(
    period: str,
    rows: list[dict[str, Any]],
    *,
    as_of: date | str | None = None,
) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if as_of is None:
        as_of_s = date.today().isoformat()
    elif isinstance(as_of, date):
        as_of_s = as_of.isoformat()
    else:
        as_of_s = str(as_of)[:10]
    path = OUT_DIR / f"{as_of_s}_{period}.json"
    payload = {
        "as_of": as_of_s,
        "period": period,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n": len(rows),
        "items": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fetch_and_cache_hour(
    *,
    as_of: date | str | None = None,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """每次更新报表强制拉最新小时榜并落盘。失败抛错，不静默空榜。"""
    try:
        rows = fetch_ths_hot_list("hour")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"同花顺小时榜抓取失败: {exc}") from exc
    if not rows:
        raise RuntimeError("同花顺小时榜返回空列表")
    path = save_ths_hot_list("hour", rows, as_of=as_of)
    if progress:
        print(f"[ths-hot] 小时榜 {len(rows)} 只 → {path.name}")
    return rows


def rank_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """code → {rank, name, ...}"""
    return {r["code"]: r for r in rows if r.get("code")}


def core_stocks_from_pool(
    pool_codes: list[str] | set[str],
    hot_rows: list[dict[str, Any]],
    *,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """池内个股按小时榜名次升序取前 top_n（含未达 3 亿成交额的）。"""
    pool = {str(c).zfill(6) for c in pool_codes if c}
    if not pool or not hot_rows:
        return []
    hit = [r for r in hot_rows if r.get("code") in pool]
    hit.sort(key=lambda x: int(x.get("rank") or 10**9))
    return hit[:top_n]


if __name__ == "__main__":
    items = fetch_and_cache_hour(progress=True)
    for r in items[:15]:
        print(f"{r['rank']:3} {r['code']} {r['name']} heat={r.get('heat')}")
