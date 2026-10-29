#!/usr/bin/env python3
"""短期指数趋势：近 10 个交易日 · 上证 / 深成指 / 创业板 / 科创综指。

主口径（B · 日线状态机，页面表格）：
  开灯：连续 3 个交易日收盘 < MA5，且当日最低 = 近 3 日最低 →「下跌」
  收盘 > MA5 →「待确认」（等次日）
  次日仍收盘 > MA5 → 结束为「-」；否则回到「下跌」

前置预警（A · 60 分钟，不进格子）：
  仅当最新日 B 合成仍为「全-」，且 A（原下跌通道：近5根收盘<MA10 或 最高<MA20）
  已为「部分」或「全下跌」时提示。

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
SCHEMA = 4  # 2026-10-30：主口径改日线 B；60 分钟仅作前置预警
SERIES_DAYS = 10
DAILY_LOOKBACK = 80  # 状态机预热（MA5 + 开灯条件）
EARLY_WARN_TEXT = "前置预警：60分钟已有指数转弱，日线下跌趋势尚未确认。"
NOTE = (
    "主口径为日线下跌趋势（连续3日收盘<MA5且创3日低开灯；"
    "收盘>MA5为待确认，次日仍>MA5结束）。"
    "60分钟下跌通道仅在日线仍全非下跌时作前置预警。"
)

INDICES: tuple[tuple[str, str], ...] = (
    ("上证指数", "sh000001"),
    ("深成指", "sz399001"),
    ("创业板指数", "sz399006"),
    ("科创综指", "sh000680"),
)

CHECK_BARS = 5
MA10 = 10
MA20 = 20
FETCH_LEN = 200

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


def _http_json_system(url: str, *, timeout: float = 25, referer: str = "") -> Any:
    """走系统代理/默认 SSL（腾讯等源在空 ProxyHandler 下易失败）。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": referer or "https://finance.qq.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_60m_sina(symbol: str, *, datalen: int = FETCH_LEN) -> list[dict]:
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


def fetch_daily(symbol: str, *, beg: str, end: str) -> list[dict]:
    """日K：优先腾讯，失败再新浪。字段 date/open/close/high/low。"""
    rows = _fetch_daily_qq(symbol, beg=beg, end=end)
    if rows:
        return rows
    return _fetch_daily_sina(symbol, datalen=320)


def _fetch_daily_qq(symbol: str, *, beg: str, end: str) -> list[dict]:
    # beg/end: YYYY-MM-DD
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        + urllib.parse.urlencode(
            {"param": f"{symbol},day,{beg},{end},640,qfq"}
        )
    )
    try:
        payload = _http_json_system(url, referer="https://finance.qq.com/")
    except Exception:
        return []
    node = ((payload.get("data") or {}).get(symbol) or {})
    days = node.get("day") or node.get("qfqday") or []
    rows: list[dict] = []
    for p in days:
        if not isinstance(p, (list, tuple)) or len(p) < 5:
            continue
        try:
            rows.append(
                {
                    "date": str(p[0])[:10],
                    "open": float(p[1]),
                    "close": float(p[2]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def _fetch_daily_sina(symbol: str, *, datalen: int = 320) -> list[dict]:
    q = urllib.parse.urlencode(
        {"symbol": symbol, "scale": 240, "ma": "no", "datalen": int(datalen)}
    )
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?{q}"
    )
    try:
        data = _http_json(url, timeout=25)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for it in data:
        try:
            rows.append(
                {
                    "date": str(it.get("day") or "")[:10],
                    "open": float(it["open"]),
                    "close": float(it["close"]),
                    "high": float(it["high"]),
                    "low": float(it["low"]),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def _sma(values: list[float], end_i: int, window: int) -> float | None:
    if end_i + 1 < window or end_i < 0:
        return None
    chunk = values[end_i - window + 1 : end_i + 1]
    if len(chunk) < window:
        return None
    return sum(chunk) / window


def judge_downtrend(bars: list[dict]) -> dict[str, Any]:
    """A：60 分钟下跌通道（预警用）。"""
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


def _b_cell(status: str) -> dict[str, Any]:
    if status == "down":
        return {"status": "down", "label": "下跌", "risk_on": True, "downtrend": True}
    if status == "pending":
        return {
            "status": "pending",
            "label": "待确认",
            "risk_on": True,
            "downtrend": True,
        }
    return {"status": "ok", "label": "-", "risk_on": False, "downtrend": False}


def compute_daily_b_series(rows: list[dict]) -> list[dict]:
    """对完整日K序列跑 B 状态机，返回与 rows 等长的单元格 dict。"""
    n = len(rows)
    closes = [float(r["close"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    ma5 = [_sma(closes, i, 5) for i in range(n)]
    state = 0  # 0 off, 1 down, 2 pending
    out: list[dict] = []
    for i in range(n):
        if ma5[i] is None:
            out.append(_b_cell("ok"))
            continue
        above = closes[i] > ma5[i]
        if state == 0:
            if i >= 2 and all(ma5[j] is not None for j in range(i - 2, i + 1)):
                three_below = all(
                    closes[j] < ma5[j] for j in range(i - 2, i + 1)  # type: ignore[operator]
                )
                is_low = lows[i] == min(lows[i - 2], lows[i - 1], lows[i])
                if three_below and is_low:
                    state = 1
            out.append(_b_cell("down" if state == 1 else "ok"))
        elif state == 1:
            if above:
                state = 2
                out.append(_b_cell("pending"))
            else:
                out.append(_b_cell("down"))
        else:  # pending
            if above:
                state = 0
                out.append(_b_cell("ok"))
            else:
                state = 1
                out.append(_b_cell("down"))
    return out


def _regime_from_flags(flags: list[bool]) -> str:
    if not flags:
        return "全-"
    n = sum(1 for f in flags if f)
    if n == 0:
        return "全-"
    if n == len(flags):
        return "全下跌"
    return "部分"


def _cell_label(status: str) -> str:
    """兼容旧调用。"""
    if status == "down":
        return "下跌"
    if status == "pending":
        return "待确认"
    return "-"


def _out_path(day: date) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    return RESULT_DIR / f"{day.isoformat()}.json"


def _trading_days(as_of_d: date, n: int) -> list[date]:
    from trading_calendar import trading_days_ending

    return trading_days_ending(as_of_d, n, refresh=False)


def _resolve_as_of_trading_day(as_of_d: date) -> date:
    """非交易日取最近已结束交易日（与报表 resolve 一致）。"""
    days = _trading_days(as_of_d, 1)
    if not days:
        raise RuntimeError(f"无交易日可对齐 as_of={as_of_d}")
    return days[-1]


def compute_index_downtrend(
    as_of: date | datetime | str,
    *,
    force: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    """对齐 as_of 重算近 SERIES_DAYS 日对比（主 B + A 预警），写缓存并返回。"""
    as_of_d = _resolve_as_of_trading_day(_as_date(as_of))
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
                and cached.get("primary") == "daily_b"
            ):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    show_days = _trading_days(as_of_d, SERIES_DAYS)
    warm_days = _trading_days(as_of_d, DAILY_LOOKBACK)
    day_keys = [d.isoformat() for d in show_days]
    beg = warm_days[0].isoformat() if warm_days else (as_of_d.replace(year=as_of_d.year - 1)).isoformat()
    end = as_of_d.isoformat()

    indices: list[dict[str, Any]] = []
    latest_b_flags: list[bool] = []
    latest_a_flags: list[bool] = []

    for name, symbol in INDICES:
        if progress:
            print(f"[index-down] {name} ({symbol}) …", flush=True)
        err: str | None = None
        series: list[dict[str, Any]] = []
        latest_cell = _b_cell("ok")
        latest_a: dict[str, Any] = {
            "downtrend": False,
            "status": "unknown",
            "last_bar": "",
            "last_close": None,
        }
        try:
            daily = fetch_daily(symbol, beg=beg, end=end)
            if not daily:
                raise RuntimeError(f"无日K: {symbol}")
            # 核日期：末日须为 as_of（交易日）
            if daily[-1]["date"] != as_of_d.isoformat():
                raise RuntimeError(
                    f"日K未对齐 as_of：{symbol} 末日={daily[-1]['date']} 期望={as_of_d.isoformat()}"
                )
            b_all = compute_daily_b_series(daily)
            by_date = {
                daily[i]["date"]: (b_all[i], daily[i]) for i in range(len(daily))
            }

            bars_60 = fetch_60m(symbol)
            for d in show_days:
                dk = d.isoformat()
                b_cell, drow = by_date.get(dk, (_b_cell("ok"), {}))
                cutoff = f"{dk} 23:59:59"
                bars = [b for b in bars_60 if str(b.get("day") or "") <= cutoff]
                a_judged = judge_downtrend(bars) if bars else {
                    "downtrend": False,
                    "status": "unknown",
                    "last_bar": "",
                    "last_close": None,
                }
                # 当日 60m 最后一根须落在该交易日
                a_ok_day = bool(bars) and str(bars[-1].get("day") or "")[:10] == dk
                if not a_ok_day:
                    a_judged = {
                        "downtrend": False,
                        "status": "unknown",
                        "last_bar": (bars[-1].get("day") if bars else "") or "",
                        "last_close": None,
                    }
                cell = {
                    "date": dk,
                    **b_cell,
                    "close": drow.get("close"),
                    "a_downtrend": bool(a_judged.get("downtrend")) if a_ok_day else False,
                    "a_status": a_judged.get("status") or "unknown",
                }
                series.append(cell)
                if d == as_of_d:
                    latest_cell = b_cell
                    latest_a = a_judged if a_ok_day else latest_a
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if progress:
                print(f"[index-down] {name} 失败: {exc}", flush=True)
            series = [
                {
                    "date": dk,
                    **_b_cell("ok"),
                    "close": None,
                    "a_downtrend": False,
                    "a_status": "unknown",
                }
                for dk in day_keys
            ]
            latest_cell = _b_cell("ok")

        latest_b_flags.append(bool(latest_cell.get("risk_on")))
        latest_a_flags.append(bool(latest_a.get("downtrend")))
        status = latest_cell.get("status") or "ok"
        indices.append(
            {
                "name": name,
                "symbol": symbol,
                "label": latest_cell.get("label") or "-",
                "status": status,
                "risk_on": bool(latest_cell.get("risk_on")),
                "downtrend": bool(latest_cell.get("risk_on")),
                "pill": (
                    "bad"
                    if status == "down"
                    else ("warn" if status == "pending" else "ok")
                ),
                "a_downtrend": bool(latest_a.get("downtrend")),
                "a_status": latest_a.get("status") or "unknown",
                "last_bar": latest_a.get("last_bar") or "",
                "last_close": latest_a.get("last_close"),
                "series": series,
                "error": err,
            }
        )

    b_regime = _regime_from_flags(latest_b_flags)
    a_regime = _regime_from_flags(latest_a_flags)
    early_warn = b_regime == "全-" and a_regime in ("部分", "全下跌")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": as_of_d.isoformat(),
        "series_days": SERIES_DAYS,
        "days": day_keys,
        "primary": "daily_b",
        "note": NOTE,
        "b_regime": b_regime,
        "a_regime": a_regime,
        "early_warn": early_warn,
        "early_warn_text": EARLY_WARN_TEXT if early_warn else "",
        "fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(f"[index-down] 已写入 {path.name} B={b_regime} A={a_regime} warn={early_warn}", flush=True)
    return payload


def load_index_downtrend(
    as_of: date | datetime | str,
    *,
    latest: date | datetime | str | None = None,
    force: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    """历史日读缓存；最新交易日可重算。schema 不符则重算。"""
    as_of_d = _resolve_as_of_trading_day(_as_date(as_of))
    latest_d = (
        _resolve_as_of_trading_day(_as_date(latest))
        if latest is not None
        else as_of_d
    )
    is_latest = as_of_d == latest_d
    path = _out_path(as_of_d)

    if path.exists() and not is_latest and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("schema") == SCHEMA and cached.get("primary") == "daily_b":
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    if is_latest or force or not path.exists():
        return compute_index_downtrend(as_of_d, force=force or is_latest, progress=progress)

    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("schema") == SCHEMA and cached.get("primary") == "daily_b":
            return cached
    except (json.JSONDecodeError, OSError):
        pass
    return compute_index_downtrend(as_of_d, force=True, progress=progress)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="短期指数趋势 · 日线B主口径 + 60分钟预警")
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    day = args.date or date.today().isoformat()
    out = compute_index_downtrend(day, force=args.force, progress=True)
    days = out.get("days") or []
    print("日期:", " ".join(d[5:] for d in days))
    print("B合成:", out.get("b_regime"), "A合成:", out.get("a_regime"), "预警:", out.get("early_warn"))
    for it in out.get("indices") or []:
        cells = " ".join(str(s.get("label") or "-") for s in (it.get("series") or []))
        print(f"  {it['name']}: {cells}")
