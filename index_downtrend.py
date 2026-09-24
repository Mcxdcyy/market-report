#!/usr/bin/env python3
"""指数大局观：近 10 个交易日 · 上证 / 深成指 / 创业板 / 科创50 下跌通道对比。

口径与自选池「下跌预警」一致（60 分钟 K，收盘价 SMA）：

  满足任意一条 → 下跌通道；都不满足 → 非下跌（页面填「-」）：
  1. 最近 5 根均满足 收盘 < MA10
  2. 最近 5 根均满足 最高价 < MA20

  K 线不足、算不出 MA20 时：只看第 1 条。

缓存：`index_downtrend_results/YYYY-MM-DD.json`
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "index_downtrend_results"
SCHEMA = 2
SERIES_DAYS = 10
NOTE = (
    "下跌通道（与自选池下跌预警同口径）：60分钟K线；"
    "连续5根收盘价在MA10下方，或连续5根最高价在MA20下方（任一即下跌通道）；"
    "算不出MA20时只看第1条。页面：近10个交易日对比，非下跌填「-」。"
)

# 展示名 → 新浪代码
INDICES: tuple[tuple[str, str], ...] = (
    ("上证指数", "sh000001"),
    ("深成指", "sz399001"),
    ("创业板指数", "sz399006"),
    ("科创板指数", "sh000688"),  # 科创50
)

CHECK_BARS = 5
MA10 = 10
MA20 = 20
FETCH_LEN = 200  # 覆盖 MA20 缓冲 + 近10日 60分钟K

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


def _http_json(url: str, *, timeout: float = 20, referer: str = "") -> Any:
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


def fetch_60m_sina(symbol: str, *, datalen: int = FETCH_LEN) -> list[dict]:
    """新浪 60 分钟 K：[{day, open, high, low, close, volume}, …] 升序。"""
    q = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "scale": 60,
            "ma": "no",
            "datalen": int(datalen),
        }
    )
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?{q}"
    )
    data = _http_json(url, timeout=20)
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for it in data:
        try:
            rows.append(
                {
                    "day": str(it.get("day") or ""),
                    "open": float(it["open"]),
                    "high": float(it["high"]),
                    "low": float(it["low"]),
                    "close": float(it["close"]),
                    "volume": float(it.get("volume") or 0),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def fetch_60m_tencent(symbol: str, *, datalen: int = FETCH_LEN) -> list[dict]:
    """腾讯 60 分钟 K 兜底。"""
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline"
        f"?param={symbol},m60,,{int(datalen)}"
    )
    payload = _http_json(url, timeout=20, referer="https://finance.qq.com/")
    block = ((payload or {}).get("data") or {}).get(symbol) or {}
    m60 = block.get("m60") or []
    rows: list[dict] = []
    for it in m60:
        if not isinstance(it, (list, tuple)) or len(it) < 5:
            continue
        try:
            ts = str(it[0])
            if len(ts) >= 12:
                day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:00"
            else:
                day = ts
            o, c, h, lo = float(it[1]), float(it[2]), float(it[3]), float(it[4])
            rows.append(
                {
                    "day": day,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": float(it[5]) if len(it) > 5 else 0.0,
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def fetch_60m(symbol: str) -> list[dict]:
    last_err: Exception | None = None
    for fetcher in (fetch_60m_sina, fetch_60m_tencent):
        try:
            rows = fetcher(symbol)
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
            time.sleep(0.2)
    if last_err:
        raise RuntimeError(f"60分钟K拉取失败 {symbol}: {last_err}") from last_err
    return []


def _sma(values: list[float], end_i: int, window: int) -> float | None:
    if end_i + 1 < window or end_i < 0:
        return None
    chunk = values[end_i - window + 1 : end_i + 1]
    if len(chunk) < window:
        return None
    return sum(chunk) / window


def judge_downtrend(bars: list[dict]) -> dict[str, Any]:
    """判定是否下跌通道。返回 downtrend / status / rule1 / rule2。"""
    n = len(bars)
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]

    can_ma10 = n >= MA10 + CHECK_BARS - 1
    can_ma20 = n >= MA20 + CHECK_BARS - 1

    rule1 = False
    if can_ma10:
        rule1 = True
        for j in range(CHECK_BARS):
            i = n - CHECK_BARS + j
            ma = _sma(closes, i, MA10)
            if ma is None or closes[i] >= ma:
                rule1 = False
                break

    rule2 = False
    if can_ma20:
        rule2 = True
        for j in range(CHECK_BARS):
            i = n - CHECK_BARS + j
            ma = _sma(closes, i, MA20)
            if ma is None or highs[i] >= ma:
                rule2 = False
                break

    if not can_ma10:
        status = "unknown"
        downtrend = False
    elif can_ma20:
        downtrend = rule1 or rule2
        status = "down" if downtrend else "ok"
    else:
        downtrend = rule1
        status = "down" if downtrend else "ok"

    last = bars[-1] if bars else {}
    return {
        "downtrend": bool(downtrend),
        "status": status,
        "rule1_close_below_ma10": bool(rule1) if can_ma10 else None,
        "rule2_high_below_ma20": bool(rule2) if can_ma20 else None,
        "bars": n,
        "last_bar": last.get("day") or "",
        "last_close": float(last["close"]) if last else None,
    }


def _cell_label(status: str) -> str:
    """页面单元格：下跌通道 / 非下跌与不足一律「-」。"""
    return "下跌通道" if status == "down" else "-"


def _out_path(day: date) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    return RESULT_DIR / f"{day.isoformat()}.json"


def _trading_days(as_of_d: date, n: int) -> list[date]:
    from trading_calendar import trading_days_ending

    return trading_days_ending(as_of_d, n, refresh=False)


def compute_index_downtrend(
    as_of: date | datetime | str,
    *,
    force: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    """对齐 as_of 重算近 SERIES_DAYS 日下跌通道对比，写缓存并返回。"""
    as_of_d = _as_date(as_of)
    path = _out_path(as_of_d)
    if path.exists() and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                cached.get("schema") == SCHEMA
                and cached.get("as_of") == as_of_d.isoformat()
                and cached.get("series_days") == SERIES_DAYS
                and len(cached.get("days") or []) == SERIES_DAYS
                and len(cached.get("indices") or []) == len(INDICES)
            ):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    days = _trading_days(as_of_d, SERIES_DAYS)
    day_keys = [d.isoformat() for d in days]

    indices: list[dict[str, Any]] = []
    for name, symbol in INDICES:
        if progress:
            print(f"[index-down] {name} ({symbol}) …", flush=True)
        series: list[dict[str, Any]] = []
        err: str | None = None
        latest_judged: dict[str, Any] = {
            "downtrend": False,
            "status": "unknown",
            "rule1_close_below_ma10": None,
            "rule2_high_below_ma20": None,
            "bars": 0,
            "last_bar": "",
            "last_close": None,
        }
        try:
            bars_all = fetch_60m(symbol)
            for d in days:
                cutoff = f"{d.isoformat()} 23:59:59"
                bars = [b for b in bars_all if str(b.get("day") or "") <= cutoff]
                judged = judge_downtrend(bars)
                series.append(
                    {
                        "date": d.isoformat(),
                        "status": judged["status"],
                        "label": _cell_label(str(judged["status"])),
                        "downtrend": bool(judged["downtrend"]),
                        "last_close": judged.get("last_close"),
                    }
                )
                if d == as_of_d:
                    latest_judged = judged
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if progress:
                print(f"[index-down] {name} 失败: {exc}", flush=True)
            series = [
                {
                    "date": dk,
                    "status": "unknown",
                    "label": "-",
                    "downtrend": False,
                    "last_close": None,
                }
                for dk in day_keys
            ]

        status = latest_judged.get("status") or "unknown"
        indices.append(
            {
                "name": name,
                "symbol": symbol,
                "label": _cell_label(str(status)),
                "pill": "bad" if status == "down" else ("ok" if status == "ok" else "warn"),
                **latest_judged,
                "series": series,
                "error": err,
            }
        )

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": as_of_d.isoformat(),
        "series_days": SERIES_DAYS,
        "days": day_keys,
        "note": NOTE,
        "fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(f"[index-down] 已写入 {path.name}", flush=True)
    return payload


def load_index_downtrend(
    as_of: date | datetime | str,
    *,
    latest: date | datetime | str | None = None,
    force: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    """历史日读缓存；最新交易日可重算。"""
    as_of_d = _as_date(as_of)
    latest_d = _as_date(latest) if latest is not None else as_of_d
    is_latest = as_of_d == latest_d
    path = _out_path(as_of_d)

    if path.exists() and not is_latest and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if is_latest or force or not path.exists():
        return compute_index_downtrend(as_of_d, force=force or is_latest, progress=progress)

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return compute_index_downtrend(as_of_d, force=True, progress=progress)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="指数大局观 · 近10日下跌通道对比")
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    day = args.date or date.today().isoformat()
    out = compute_index_downtrend(day, force=args.force, progress=True)
    days = out.get("days") or []
    print("日期:", " ".join(d[5:] for d in days))
    for it in out.get("indices") or []:
        cells = " ".join(
            ("跌" if s.get("downtrend") else "-") for s in (it.get("series") or [])
        )
        print(f"  {it['name']}: {cells}")
