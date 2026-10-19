#!/usr/bin/env python3
"""宏观·多空策略：上证 + 深成 + 创业板 · 连4日 MA60 状态叠加（近120交易日）。

取数与判定与空窗任务一致（新浪日K scale=240，不足则东财日K）：

  连4日：每一日收盘 > 当日 MA60，且每一日 MA60 > 前一日 MA60 → +1
  连4日：每一日收盘 < 当日 MA60，且每一日 MA60 < 前一日 MA60 → −1
  其余 → 0

叠加 = 三指数当日分数之和（−3～+3）。不含科创板。
缓存：index_ma60_macro_results/YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "index_ma60_macro_results"
SCHEMA = 1
WINDOW = 120
MA = 60
CHECK = 4
FETCH_LEN = 800

INDICES: tuple[tuple[str, str, str], ...] = (
    ("上证", "sh000001", "1.000001"),
    ("深成", "sz399001", "0.399001"),
    ("创业板", "sz399006", "0.399006"),
)

NOTE = ""

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_CTX),
    )


def _http_json(url: str, *, timeout: float = 30, referer: str = "") -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": referer or "https://finance.sina.com.cn/",
        },
    )
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def fetch_daily_sina(symbol: str, datalen: int = FETCH_LEN) -> list[dict]:
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={int(datalen)}"
    )
    data = _http_json(url, timeout=30)
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for it in data:
        try:
            rows.append(
                {"date": str(it["day"])[:10], "close": float(it["close"])}
            )
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


def fetch_daily_em(secid: str, limit: int = FETCH_LEN) -> list[dict]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=101&fqt=1&end=20500101&lmt={int(limit)}"
    )
    payload = _http_json(url, timeout=30, referer="https://quote.eastmoney.com/")
    kl = ((payload or {}).get("data") or {}).get("klines") or []
    rows: list[dict] = []
    for line in kl:
        parts = str(line).split(",")
        if len(parts) < 3:
            continue
        try:
            rows.append({"date": parts[0][:10], "close": float(parts[2])})
        except ValueError:
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


def get_daily(symbol: str, secid: str) -> list[dict]:
    last_err: Exception | None = None
    try:
        rows = fetch_daily_sina(symbol, FETCH_LEN)
        if len(rows) >= 200:
            return rows
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        last_err = exc
    try:
        rows = fetch_daily_em(secid, FETCH_LEN)
        if rows:
            return rows
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        last_err = exc
    if last_err:
        raise RuntimeError(f"日K拉取失败 {symbol}: {last_err}") from last_err
    raise RuntimeError(f"日K为空 {symbol}")


def ma60_series(closes: list[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 < MA:
            continue
        out[i] = sum(closes[i - MA + 1 : i + 1]) / MA
    return out


def judge_at(closes: list[float], mas: list[float | None], end_i: int, check: int = CHECK) -> int:
    """end_i inclusive：连续 check 日收盘在当日 MA60 同侧，且这 check 日 MA60 连续同向。"""
    if end_i < check - 1:
        return 0
    for j in range(check):
        i = end_i - check + 1 + j
        if i < 1 or mas[i] is None or mas[i - 1] is None:
            return 0
    up = True
    down = True
    for j in range(check):
        i = end_i - check + 1 + j
        if not (closes[i] > mas[i] and mas[i] > mas[i - 1]):  # type: ignore[operator]
            up = False
        if not (closes[i] < mas[i] and mas[i] < mas[i - 1]):  # type: ignore[operator]
            down = False
    if up and down:
        return 0
    if up:
        return 1
    if down:
        return -1
    return 0


def _flags_for(rows: list[dict]) -> dict[str, int]:
    closes = [float(r["close"]) for r in rows]
    mas = ma60_series(closes)
    out: dict[str, int] = {}
    for i, r in enumerate(rows):
        out[r["date"]] = judge_at(closes, mas, i)
    return out


def compute_macro_bias(as_of: date | datetime | str, *, progress: bool = False) -> dict:
    as_of_d = _as_date(as_of)
    per: dict[str, dict[str, int]] = {}
    last_dates: dict[str, str] = {}
    for name, symbol, secid in INDICES:
        rows = get_daily(symbol, secid)
        if not rows:
            raise RuntimeError(f"{name} 日K为空")
        last = rows[-1]["date"]
        last_dates[name] = last
        if last != as_of_d.isoformat():
            raise RuntimeError(
                f"{name} 日K停在 {last}，报表日 {as_of_d.isoformat()}，未核对，不采用"
            )
        per[name] = _flags_for(rows)
        if progress:
            print(f"[macro-bias] {name} {rows[0]['date']}→{last} n={len(rows)}")

    common = set(per["上证"]) & set(per["深成"]) & set(per["创业板"])
    days = sorted(d for d in common if d <= as_of_d.isoformat())
    if len(days) < WINDOW:
        raise RuntimeError(f"三指数对齐交易日仅 {len(days)}，不足 {WINDOW}")
    days = days[-WINDOW:]
    if days[-1] != as_of_d.isoformat():
        raise RuntimeError(f"叠加序列末日 {days[-1]} ≠ {as_of_d.isoformat()}")

    series = []
    for d in days:
        sh = per["上证"][d]
        sz = per["深成"][d]
        cy = per["创业板"][d]
        series.append(
            {
                "date": d,
                "sh": sh,
                "sz": sz,
                "cy": cy,
                "sum": sh + sz + cy,
            }
        )
    payload = {
        "schema": SCHEMA,
        "as_of": as_of_d.isoformat(),
        "window": WINDOW,
        "check": CHECK,
        "ma": MA,
        "note": NOTE,
        "last_bar": last_dates,
        "series": series,
    }
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_macro_bias(
    as_of: date | datetime | str,
    *,
    latest: date | datetime | str | None = None,
    force: bool = False,
    progress: bool = False,
) -> dict:
    as_of_d = _as_date(as_of)
    latest_d = _as_date(latest) if latest is not None else as_of_d
    path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if as_of_d == latest_d or force or not path.is_file():
        return compute_macro_bias(as_of_d, progress=progress)
    return json.loads(path.read_text(encoding="utf-8"))
