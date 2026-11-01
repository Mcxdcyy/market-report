#!/usr/bin/env python3
"""东方财富个股人气名次（全市场排序，非 Top100 榜）。

报表「板块-资金认可度 · 核心个股」用本模块：对池内每只股票查
`stockrank/getCurrentList` 最新 rank，再按名次升序取前 N。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "em_stock_rank_results"
EM_CURRENT_URL = "https://emappdata.eastmoney.com/stockrank/getCurrentList"
EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_APP = {
    "appId": "appId01",
    "globalId": "786e4c21-70dc-435a-93bb-38",
    "marketType": "",
}


def code_to_sc(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("5", "6", "9")) or c[:2] == "68":
        return "SH" + c
    if c.startswith(("4", "8")):
        return "BJ" + c
    return "SZ" + c


def sc_to_secid(sc: str) -> str:
    """SZ/SH/BJ + code → push2 secid。"""
    sc = sc.upper()
    code = sc[2:]
    if sc.startswith("SZ"):
        return f"0.{code}"
    if sc.startswith("BJ"):
        return f"0.{code}"
    return f"1.{code}"


def _post_json(url: str, payload: dict, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Origin": "https://vipmoney.eastmoney.com",
            "Referer": "https://vipmoney.eastmoney.com/",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://guba.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_one_rank(code: str) -> dict[str, Any] | None:
    """查单只最新人气名次。失败返回 None。"""
    code = str(code).zfill(6)
    sc = code_to_sc(code)
    try:
        raw = _post_json(
            EM_CURRENT_URL,
            {**EM_APP, "srcSecurityCode": sc},
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if int(raw.get("status") or 0) != 0:
        return None
    pts = raw.get("data") or []
    if not pts:
        return None
    last = pts[-1] or {}
    try:
        rank = int(last.get("rank") or 0)
    except (TypeError, ValueError):
        return None
    if rank <= 0:
        return None
    return {
        "code": code,
        "sc": sc,
        "rank": rank,
        "calc_time": str(last.get("calcTime") or ""),
    }


def _sina_prefix(code: str) -> str:
    c = str(code).zfill(6)
    sc = code_to_sc(c)
    if sc.startswith("SH"):
        return "sh"
    if sc.startswith("BJ"):
        return "bj"
    return "sz"


def fetch_names(codes: list[str]) -> dict[str, str]:
    """批量补简称（新浪行情；东财 ulist 易断连，作备用）。"""
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return {}
    out: dict[str, str] = {}

    def _parse_sina(text: str) -> None:
        for line in text.splitlines():
            # var hq_str_sz002275="桂林三金,..."
            if "hq_str_" not in line or '="' not in line:
                continue
            try:
                left, right = line.split('="', 1)
                code = left.split("hq_str_")[-1][-6:]
                name = right.split(",", 1)[0].strip()
            except (ValueError, IndexError):
                continue
            if code.isdigit() and name:
                out[code.zfill(6)] = name

    for i in range(0, len(codes), 80):
        chunk = codes[i : i + 80]
        lst = ",".join(f"{_sina_prefix(c)}{c}" for c in chunk)
        url = f"https://hq.sinajs.cn/list={lst}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://finance.sina.com.cn/",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("gbk", errors="replace")
            _parse_sina(text)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    missing = [c for c in codes if c not in out]
    if missing:
        for i in range(0, len(missing), 40):
            chunk = missing[i : i + 40]
            secids = ",".join(sc_to_secid(code_to_sc(c)) for c in chunk)
            url = (
                f"{EM_ULIST_URL}?ut=f057cbcbce2a86e2866ab8877db1d059"
                f"&fltt=2&invt=2&fields=f14,f12&secids={secids}"
            )
            try:
                raw = _get_json(url)
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
                ConnectionError,
            ):
                continue
            for row in ((raw.get("data") or {}).get("diff") or []):
                code = str(row.get("f12") or "").zfill(6)
                name = str(row.get("f14") or "").strip()
                if code and name:
                    out[code] = name
    return out


def fetch_ranks(
    codes: list[str] | set[str],
    *,
    workers: int = 24,
    progress: bool = False,
) -> dict[str, dict[str, Any]]:
    """并行拉多只最新名次。返回 code → {rank, name?, ...}。"""
    uniq = sorted({str(c).zfill(6) for c in codes if c})
    if not uniq:
        return {}
    result: dict[str, dict[str, Any]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one_rank, c): c for c in uniq}
        for fut in as_completed(futs):
            done += 1
            if progress and done % 50 == 0:
                print(f"[em-rank] {done}/{len(uniq)}")
            row = fut.result()
            if row:
                result[row["code"]] = row
    names = fetch_names(list(result.keys()))
    for code, row in result.items():
        if code in names:
            row["name"] = names[code]
    if progress:
        print(f"[em-rank] 完成 {len(result)}/{len(uniq)} 只有名次")
    return result


def save_rank_cache(
    ranks: dict[str, dict[str, Any]],
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
    path = OUT_DIR / f"{as_of_s}.json"
    items = sorted(ranks.values(), key=lambda x: int(x.get("rank") or 10**9))
    payload = {
        "as_of": as_of_s,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n": len(items),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fetch_and_cache_ranks(
    codes: list[str] | set[str],
    *,
    as_of: date | str | None = None,
    progress: bool = True,
) -> dict[str, dict[str, Any]]:
    ranks = fetch_ranks(codes, progress=progress)
    if not ranks:
        raise RuntimeError("东财个股人气名次全部拉取失败")
    path = save_rank_cache(ranks, as_of=as_of)
    if progress:
        print(f"[em-rank] 已缓存 → {path.name}")
    return ranks


def core_stocks_from_pool(
    pool_codes: list[str] | set[str],
    ranks: dict[str, dict[str, Any]],
    *,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """池内按东财人气名次升序取前 top_n（含未达 3 亿成交额）。"""
    pool = {str(c).zfill(6) for c in pool_codes if c}
    hit = [dict(ranks[c]) for c in pool if c in ranks]
    hit.sort(key=lambda x: int(x.get("rank") or 10**9))
    return hit[:top_n]


if __name__ == "__main__":
    demo = fetch_ranks(["002086", "002343", "600715", "000001", "300059"], progress=True)
    for code, row in sorted(demo.items(), key=lambda kv: kv[1]["rank"]):
        print(f"#{row['rank']:4} {code} {row.get('name', '')}")
