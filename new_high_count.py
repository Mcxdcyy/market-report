#!/usr/bin/env python3
"""新高数量：问财口径 · 本地序列累积（不依赖大盘数据.numbers）。

问句（固定）：
  今日最高价创历史新高，非st，非退市，非北京证券交易所，上市天数大于10天
历史日用「YYYY年M月D日最高价创历史新高，…」替换「今日」。

数据策略：
  1. 真源 = `new_high_count_results/series.json`（历史已固化；含早期 numbers 灌入段，之后只增不问表）
  2. 每日问财结果写入 `days/YYYY-MM-DD.json` 并合并进 series
  3. 缺 series.json → 报错（不再回读大盘表）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "new_high_count_results"
DAY_DIR = RESULT_DIR / "days"
SERIES_FILE = RESULT_DIR / "series.json"
SERIES_DAYS = 120
SCHEMA = 3

WENCAI_QUERY_TODAY = (
    "今日最高价创历史新高，非st，非退市，非北京证券交易所，上市天数大于10天"
)
WENCAI_QUERY_DATED = (
    "{y}年{m}月{d}日最高价创历史新高，非st，非退市，非北京证券交易所，上市天数大于10天"
)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 历史固化段 source 名（早期自大盘表灌入，现仅作标签，不再读表）
HISTORY_SOURCES = frozenset({"numbers_seed", "history"})


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def wencai_query_for(day: date, *, today: date | None = None) -> str:
    today = today or date.today()
    if day == today:
        return WENCAI_QUERY_TODAY
    return WENCAI_QUERY_DATED.format(y=day.year, m=day.month, d=day.day)


def load_series() -> dict[str, Any]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if not SERIES_FILE.exists():
        raise RuntimeError(
            f"缺少新高序列 {SERIES_FILE.name}。历史已应固化在此文件；"
            "请勿依赖大盘数据.numbers。若误删请从 git 恢复。"
        )
    try:
        data = json.loads(SERIES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{SERIES_FILE.name} JSON 损坏: {exc}") from exc
    if not isinstance(data.get("days"), dict) or not data["days"]:
        raise RuntimeError(f"{SERIES_FILE.name} 无有效 days 序列")
    return data


def save_series(data: dict[str, Any]) -> None:
    data["schema"] = SCHEMA
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["query_today"] = WENCAI_QUERY_TODAY
    data["note"] = (
        "新高家数本地序列：历史段已固化（source=numbers_seed/history）；"
        "此后仅按日写入问财结果，不读大盘数据.numbers。"
    )
    SERIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _day_path(day: date) -> Path:
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    return DAY_DIR / f"{day.isoformat()}.json"


def save_wencai_day(
    day: date | str,
    count: int,
    *,
    names: list[str] | None = None,
    codes: list[str] | None = None,
    query: str | None = None,
    source: str = "wencai",
) -> dict[str, Any]:
    """写入单日问财结果，并合并进 series。"""
    d = _as_date(day)
    query = query or wencai_query_for(d)
    names = list(names or [])
    codes = list(codes or [])
    entry = {
        "date": d.isoformat(),
        "count": int(count),
        "names": names,
        "codes": codes,
        "query": query,
        "source": source,
        "fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = _day_path(d)
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

    series = load_series()
    days = series.setdefault("days", {})
    days[d.isoformat()] = {
        "count": int(count),
        "source": source,
        "query": query,
        "names": names,
        "codes": codes,
        "fetched": entry["fetched"],
    }
    save_series(series)
    print(f"[newhigh] 保存 {d} = {count} 家 ({source}) → {path.name}")
    return entry


def _fetch_via_chrome_cdp(day: date, timeout_s: int = 60) -> dict[str, Any] | None:
    """本机 Chrome headless + CDP。沙箱/无 GUI 环境可能失败，失败返回 None。"""
    if not Path(CHROME).exists():
        return None
    try:
        import requests
    except ImportError:
        return None
    try:
        import websocket
    except ImportError:
        try:
            subprocess.check_call(
                [os.environ.get("PYTHON", "python3"), "-m", "pip", "install", "websocket-client", "-q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import websocket
        except Exception:
            return None

    port = 9331
    user_data = Path("/tmp/chrome-wencai-newhigh")
    user_data.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], capture_output=True)
    logf = open("/tmp/chrome-wencai-newhigh.log", "w")
    proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={port}",
            "--headless=new",
            "--disable-gpu",
            f"--user-data-dir={str(user_data)}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=logf,
        stderr=logf,
    )
    tabs = None
    for _ in range(25):
        time.sleep(0.4)
        try:
            tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
            if tabs:
                break
        except Exception:
            tabs = None
    if not tabs:
        proc.kill()
        return None

    query = wencai_query_for(day)
    url = "https://www.iwencai.com/unifiedwap/result?w=" + quote(query) + "&querytype=stock"
    ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=timeout_s)
    msg_id = 0

    def cdp(method: str, params: dict | None = None) -> dict:
        nonlocal msg_id
        msg_id += 1
        mid = msg_id
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == mid:
                return data

    try:
        cdp("Page.enable")
        cdp("Runtime.enable")
        cdp("Page.navigate", {"url": url})
        count = None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(2)
            ev = cdp(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const t = document.body ? document.body.innerText : '';"
                        " const m = t.match(/选出A股\\s*(\\d+)/);"
                        " return m && m[1]; })()"
                    ),
                    "returnByValue": True,
                },
            )
            val = (((ev or {}).get("result") or {}).get("result") or {}).get("value")
            if val is not None:
                count = int(val)
                break
        if count is None:
            return None
        return {
            "count": count,
            "names": [],
            "codes": [],
            "query": query,
            "source": "wencai_chrome",
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass
        proc.terminate()


def fetch_wencai_new_high(day: date | str, *, progress: bool = True) -> dict[str, Any]:
    """拉取指定交易日问财新高家数。优先 Chrome CDP；失败抛错。"""
    d = _as_date(day)
    if progress:
        print(f"[newhigh] 问财拉取 {d} …")
    got = _fetch_via_chrome_cdp(d)
    if got and got.get("count") is not None:
        return save_wencai_day(
            d,
            int(got["count"]),
            names=got.get("names") or [],
            codes=got.get("codes") or [],
            query=got.get("query"),
            source=str(got.get("source") or "wencai"),
        )
    raise RuntimeError(
        f"问财自动拉取失败（{d}）。请用浏览器打开问财跑："
        f"「{wencai_query_for(d)}」，然后执行：\n"
        f"  python3 new_high_count.py save --date {d.isoformat()} --count N "
        f"--names 股1,股2,…"
    )


def _is_wencai_source(source: str | None) -> bool:
    return bool(source) and str(source).startswith("wencai")


def ensure_wencai_day(
    day: date | str,
    *,
    force: bool = False,
    progress: bool = True,
    allow_fetch: bool = True,
) -> dict[str, Any]:
    """保证 series 中有该日；force 时重拉问财。"""
    d = _as_date(day)
    series = load_series()
    days = series.get("days") or {}
    cur = days.get(d.isoformat())
    if cur and not force and _is_wencai_source(cur.get("source")):
        return {
            "date": d.isoformat(),
            "count": int(cur["count"]),
            "names": list(cur.get("names") or []),
            "codes": list(cur.get("codes") or []),
            "query": cur.get("query"),
            "source": cur.get("source"),
        }
    if cur and not force and not allow_fetch:
        return {
            "date": d.isoformat(),
            "count": int(cur["count"]),
            "source": cur.get("source"),
            "names": list(cur.get("names") or []),
            "codes": list(cur.get("codes") or []),
            "query": cur.get("query"),
        }
    if not allow_fetch and not cur:
        raise RuntimeError(f"序列中无 {d}，且不允许拉取")
    # 报表日：历史固化或缺失 → 尝试问财；已有 day 文件则直接吃
    if allow_fetch and (
        force or not cur or cur.get("source") in HISTORY_SOURCES or not _is_wencai_source(cur.get("source"))
    ):
        p = _day_path(d)
        if p.exists() and not force:
            try:
                cached = json.loads(p.read_text(encoding="utf-8"))
                if _is_wencai_source(cached.get("source")) and cached.get("count") is not None:
                    return save_wencai_day(
                        d,
                        int(cached["count"]),
                        names=cached.get("names") or [],
                        codes=cached.get("codes") or [],
                        query=cached.get("query"),
                        source=str(cached.get("source") or "wencai"),
                    )
            except json.JSONDecodeError:
                pass
        return fetch_wencai_new_high(d, progress=progress)
    assert cur is not None
    return {
        "date": d.isoformat(),
        "count": int(cur["count"]),
        "source": cur.get("source"),
        "names": list(cur.get("names") or []),
        "codes": list(cur.get("codes") or []),
        "query": cur.get("query"),
    }


def series_daily_rows(end: date, *, need: int = SERIES_DAYS + 1) -> list[dict[str, Any]]:
    """截至 end 的日序列（含窗口前 1 日），供柱图。"""
    series = load_series()
    days = series.get("days") or {}
    keys = sorted(k for k in days if k <= end.isoformat())
    use = keys[-need:] if len(keys) >= need else keys
    return [{"date": k, "count": int(days[k]["count"])} for k in use]


def apply_new_high_to_df(df: Any) -> Any:
    """把序列中的新高家数写回 DataFrame「新高」列（有则覆盖）。兼容旧调用。"""
    import pandas as pd

    series = load_series()
    days = series.get("days") or {}
    if "date" not in df.columns or "新高" not in df.columns:
        return df
    out = df.copy()

    def _one(v: Any) -> Any:
        if hasattr(v, "date") and not isinstance(v, date):
            try:
                v = v.date()
            except Exception:
                return None
        if isinstance(v, datetime):
            v = v.date()
        if isinstance(v, date):
            return v.isoformat()
        if pd.isna(v):
            return None
        return str(v)[:10]

    for i, row in out.iterrows():
        key = _one(row.get("date"))
        if key and key in days:
            out.at[i, "新高"] = int(days[key]["count"])
    return out


def compute_new_high_count(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,  # noqa: ARG001
    *,
    force: bool = False,
    progress: bool = True,
    fetch: bool = True,
) -> dict:
    """构建近 SERIES_DAYS 日新高序列缓存（schema 3 · 问财累积）。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"

    try:
        ensure_wencai_day(as_of_d, force=force, progress=progress, allow_fetch=fetch)
    except RuntimeError as exc:
        series = load_series()
        if as_of_d.isoformat() not in (series.get("days") or {}):
            raise
        if progress:
            print(f"[newhigh] 警告: {exc}")

    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if (
                cached.get("as_of") == as_of_d.isoformat()
                and cached.get("schema") == SCHEMA
                and int(cached.get("n_days") or 0) >= min(SERIES_DAYS, 1)
                and cached.get("daily")
            ):
                series = load_series()
                cur = (series.get("days") or {}).get(as_of_d.isoformat()) or {}
                if int(cached.get("latest_count") or -1) == int(cur.get("count") or -2):
                    return cached
        except json.JSONDecodeError:
            pass

    daily = series_daily_rows(as_of_d)
    if len(daily) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": SCHEMA,
            "daily": daily,
            "note": "新高序列不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    series_days = daily[-SERIES_DAYS:] if len(daily) >= SERIES_DAYS else daily[1:] if len(daily) > 1 else daily
    if not series_days and daily:
        series_days = daily
    series = load_series()
    cur = (series.get("days") or {}).get(as_of_d.isoformat()) or {}
    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": SCHEMA,
        "start": series_days[0]["date"],
        "end": series_days[-1]["date"],
        "n_days": len(series_days),
        "daily": daily,
        "latest_count": int(daily[-1]["count"]),
        "latest_source": cur.get("source"),
        "query": cur.get("query") or WENCAI_QUERY_TODAY,
        "note": (
            "新高数量：本地 series.json 累积；历史已固化，日报问财写入。"
            f"问句：{WENCAI_QUERY_TODAY}"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(
            f"[newhigh] {as_of_d} 最新 {payload['latest_count']} 家 "
            f"（{payload.get('latest_source')} · {payload['start']}…{payload['end']}）→ {out_path.name}"
        )
    return payload


def _cli() -> None:
    p = argparse.ArgumentParser(description="问财新高数量：拉取 / 保存 / 重算序列（不读大盘表）")
    sub = p.add_subparsers(dest="cmd")

    p_save = sub.add_parser("save", help="手动写入某日问财结果（浏览器拉完后用）")
    p_save.add_argument("--date", required=True)
    p_save.add_argument("--count", type=int, required=True)
    p_save.add_argument("--names", default="", help="逗号分隔股票简称")
    p_save.add_argument("--codes", default="", help="逗号分隔股票代码")

    p_pull = sub.add_parser("pull", help="自动拉取某日（Chrome CDP）")
    p_pull.add_argument("--date", default=date.today().isoformat())
    p_pull.add_argument("--force", action="store_true")

    p_status = sub.add_parser("status", help="查看本地序列覆盖（不读大盘表）")
    p_comp = sub.add_parser("compute", help="重算 as_of 报表缓存")
    p_comp.add_argument("--date", default=date.today().isoformat())
    p_comp.add_argument("--force", action="store_true")
    p_comp.add_argument("--no-fetch", action="store_true")

    args = p.parse_args()
    if args.cmd == "save":
        names = [x.strip() for x in str(args.names).split(",") if x.strip()]
        codes = [x.strip() for x in str(args.codes).split(",") if x.strip()]
        save_wencai_day(args.date, args.count, names=names, codes=codes, source="wencai_browser")
        return
    if args.cmd == "pull":
        ensure_wencai_day(args.date, force=args.force, allow_fetch=True, progress=True)
        return
    if args.cmd == "status":
        series = load_series()
        days = series.get("days") or {}
        keys = sorted(days)
        sources: dict[str, int] = {}
        for v in days.values():
            src = str(v.get("source") or "?")
            sources[src] = sources.get(src, 0) + 1
        print(f"[newhigh] {SERIES_FILE.name} · {len(keys)} 日 · {keys[0]}…{keys[-1]}")
        print(f"[newhigh] sources: {sources}")
        return
    if args.cmd == "compute":
        compute_new_high_count(args.date, force=args.force, fetch=not args.no_fetch, progress=True)
        return

    import sys

    day = date.today().isoformat()
    force = "--force" in sys.argv
    for a in sys.argv[1:]:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", a):
            day = a
    compute_new_high_count(day, force=force, progress=True)


if __name__ == "__main__":
    _cli()
