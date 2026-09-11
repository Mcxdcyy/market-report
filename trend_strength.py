#!/usr/bin/env python3
"""全市场「趋势强度」占比统计。

趋势定义（五条同时满足）：
1. 连续 3 日收盘价 > 五日线
2. 连续 5 日最低价 > 十日线
3. 近 3 日最高价 = 近 20 日最高价
4. 今日五日线 > 昨日五日线（严格大于）
5. 当日非跌停

维度：全场（排最前）+ 主板 + 创业板 + 科创板 + 北交所 + ST
样本：上市日历天数 > 10；K 线不足 20 根的不计入分母。
ST 互斥：ST 只进 ST 桶，不进入其余板块桶；全场含全部有效样本。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "trend_strength_results"
CACHE_FILE = BASE / "trend_klines_cache.json"
LONG_CACHE_FILE = BASE / "trend_klines_cache_long.json"
MEANS_200D_FILE = RESULT_DIR / "means_200d.json"
WORKERS = 48
KLINE_LEN = 40
LONG_KLINE_LEN = 260  # 200 交易日均值 + MA20 余量
MEANS_DAYS = 200
MIN_BARS = 20
LIST_DAYS_MIN = 10
# 符合条件个股：当日成交额分档阈值（元）
AMOUNT_SPLIT_YUAN = 5e8  # 5 亿元

FS_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
CLIST_HOSTS = (
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
)

BUCKET_ORDER = (
    ("all", "全场"),
    ("main", "主板"),
    ("cyb", "创业板"),
    ("kcb", "科创板"),
    ("bj", "北交所"),
    ("st", "ST"),
)

_ST_RE = re.compile(r"\*?\s*ST", re.I)
_CTX = ssl.create_default_context()


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_CTX),
    )


def _http_json(url: str, *, timeout: float = 20) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with _opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"请求失败: {url}") from last_err


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def is_st_name(name: str) -> bool:
    return bool(_ST_RE.search(name or ""))


def is_bj_code(code: str) -> bool:
    c = code.strip()
    if c.startswith(("920", "921", "922", "823", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "430", "431", "432", "433", "434", "435", "436", "437", "438", "439")):
        return True
    if len(c) == 6 and c[0] in "48" and not c.startswith(("60", "68", "00", "30")):
        # 北交所常见 8xxxxx / 4xxxxx，排除沪深主板前缀
        if c.startswith(("60", "68", "00", "001", "002", "003", "300", "301")):
            return False
        return c.startswith(("8", "4"))
    return False


def classify_bucket(code: str, name: str) -> str:
    """ST 互斥：ST 只归 st；其余按板块。"""
    if is_st_name(name):
        return "st"
    if code.startswith("688") or code.startswith("689"):
        return "kcb"
    if code.startswith(("300", "301")):
        return "cyb"
    if is_bj_code(code):
        return "bj"
    return "main"


def sina_symbol(code: str, market: int | None = None) -> str:
    if is_bj_code(code):
        return f"bj{code}"
    if market == 1 or code.startswith(("6", "5", "9")):
        # 沪市主板 / 科创；market=1 优先
        if market == 1 or code.startswith(("6", "5")):
            return f"sh{code}"
    if code.startswith(("688", "689", "600", "601", "603", "605")):
        return f"sh{code}"
    return f"sz{code}"


def limit_down_threshold(code: str, name: str) -> float:
    """跌停判定用的跌幅阈值（百分比，含缓冲）。"""
    if is_st_name(name):
        return -4.8
    if code.startswith(("300", "301", "688", "689")):
        return -19.5
    if is_bj_code(code):
        return -29.5
    return -9.5


def is_limit_down(code: str, name: str, pct: float | None, close: float | None, preclose: float | None) -> bool:
    thr = limit_down_threshold(code, name)
    if pct is not None:
        try:
            if float(pct) <= thr:
                return True
        except (TypeError, ValueError):
            pass
    if close is not None and preclose is not None and preclose > 0:
        # 理论跌停价（四舍五入到分）
        floor = round(float(preclose) * (100.0 + thr) / 100.0, 2)
        try:
            if float(close) <= floor + 0.011:
                return True
        except (TypeError, ValueError):
            pass
    return False


def fetch_universe() -> list[dict]:
    """东财 clist 全 A（含京），分页取齐。"""
    fields = "f12,f13,f14,f2,f3,f6,f18,f26"
    stocks: list[dict] = []
    total = None
    pn = 1
    while True:
        params = {
            "pn": pn,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": FS_A,
            "fields": fields,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        q = urllib.parse.urlencode(params)
        payload = None
        last_err = None
        for host in CLIST_HOSTS:
            try:
                payload = _http_json(f"https://{host}/api/qt/clist/get?{q}", timeout=25)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if payload is None:
            raise RuntimeError(f"股票列表拉取失败: {last_err}")
        data = payload.get("data") or {}
        if total is None:
            total = int(data.get("total") or 0)
        diff = data.get("diff") or []
        if not diff:
            break
        for row in diff:
            code = str(row.get("f12") or "").zfill(6)
            name = str(row.get("f14") or "")
            if not code or not name:
                continue
            if "退" in name:
                continue
            stocks.append(
                {
                    "code": code,
                    "name": name,
                    "market": int(row.get("f13") or 0),
                    "close": _to_float(row.get("f2")),
                    "pct": _to_float(row.get("f3")),
                    "amount": _to_float(row.get("f6")),  # 元
                    "preclose": _to_float(row.get("f18")),
                    "list_date": _parse_list_date(row.get("f26")),
                    "bucket": classify_bucket(code, name),
                }
            )
        if total is not None and len(stocks) >= total:
            break
        if len(diff) < 100:
            break
        pn += 1
        if pn > 120:
            break
    # 去重
    seen: set[str] = set()
    out: list[dict] = []
    for s in stocks:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        out.append(s)
    return out


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "-" or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_list_date(v: Any) -> date | None:
    if v is None or v == "-" or v == "":
        return None
    try:
        s = str(int(v)) if not isinstance(v, str) else v.strip()
        if len(s) == 8 and s.isdigit():
            return datetime.strptime(s, "%Y%m%d").date()
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_klines_qq(code: str, market: int | None) -> list[dict]:
    """腾讯财经代理（主源）。"""
    sym = sina_symbol(code, market)
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={sym},day,,,{KLINE_LEN},qfq"
    )
    try:
        payload = _http_json(url, timeout=15)
    except Exception:
        return []
    block = (payload.get("data") or {}).get(sym) or {}
    day = block.get("qfqday") or block.get("day") or []
    rows: list[dict] = []
    for item in day:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        try:
            amt = None
            # QQ: [date,open,close,high,low,volume,{},turn,amount_万元,...]
            if len(item) >= 9:
                try:
                    amt = float(item[8]) * 10000.0  # 万元 → 元
                except (TypeError, ValueError):
                    amt = None
            row = {
                "date": str(item[0])[:10],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
            }
            if amt is not None and amt > 0:
                row["amount"] = amt
            rows.append(row)
        except (TypeError, ValueError):
            continue
    return rows


def fetch_klines_sohu(code: str, market: int | None, as_of: date | None = None) -> list[dict]:
    """搜狐历史行情备用源。hq: 日期,开盘,收盘,涨跌额,涨跌幅,最低,最高,..."""
    end = as_of or date.today()
    start = end - timedelta(days=90)
    url = (
        "https://q.stock.sohu.com/hisHq?"
        + urllib.parse.urlencode(
            {
                "code": f"cn_{code}",
                "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "stat": 1,
                "order": "A",
                "period": "d",
            }
        )
    )
    try:
        payload = _http_json(url, timeout=15)
    except Exception:
        return []
    if not isinstance(payload, list) or not payload:
        return []
    hq = payload[0].get("hq") if isinstance(payload[0], dict) else None
    if not hq:
        return []
    rows: list[dict] = []
    for item in hq:
        if not isinstance(item, (list, tuple)) or len(item) < 7:
            continue
        try:
            row = {
                "date": str(item[0])[:10],
                "open": float(item[1]),
                "close": float(item[2]),
                "low": float(item[5]),
                "high": float(item[6]),
            }
            # sohu 常见: ... volume, amount_万元
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
    rows.sort(key=lambda r: r["date"])
    return rows[-KLINE_LEN:]


def fetch_klines_sina(code: str, market: int | None) -> list[dict]:
    sym = sina_symbol(code, market)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={KLINE_LEN}"
    )
    try:
        data = _http_json(url, timeout=15)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        try:
            rows.append(
                {
                    "date": str(item["day"])[:10],
                    "open": float(item["open"]),
                    "close": float(item["close"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def fetch_klines_tencent(code: str, market: int | None) -> list[dict]:
    """直连腾讯（可能 501，保留兜底）。"""
    return fetch_klines_qq(code, market)


def fetch_klines_for_stock(code: str, market: int | None, as_of: date) -> list[dict]:
    rows = fetch_klines_qq(code, market)
    if len(rows) < MIN_BARS:
        rows = fetch_klines_sohu(code, market, as_of=as_of) or rows
    if len(rows) < MIN_BARS:
        rows = fetch_klines_sina(code, market) or rows
    return rows


def load_kline_cache() -> dict[str, list[dict]]:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, list[dict]] = {}
    for code, lines in (raw or {}).items():
        rows = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 5:
                continue
            try:
                row = {
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                }
                if len(parts) >= 6 and parts[5] not in ("", "None"):
                    amt = float(parts[5])
                    if amt > 0:
                        row["amount"] = amt
                rows.append(row)
            except ValueError:
                continue
        if rows:
            out[code] = rows
    return out


def save_kline_cache(cache: dict[str, list[dict]]) -> None:
    payload = {}
    for code, rows in cache.items():
        if not rows:
            continue
        lines = []
        for r in rows[-KLINE_LEN:]:
            base = f"{r['date']},{r['open']},{r['close']},{r['high']},{r['low']}"
            amt = r.get("amount")
            if amt is not None:
                base = f"{base},{amt}"
            lines.append(base)
        payload[code] = lines
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def truncate_as_of(rows: list[dict], as_of: date) -> list[dict]:
    key = as_of.isoformat()
    return [r for r in rows if r["date"] <= key]


def cache_covers(rows: list[dict], as_of: date) -> bool:
    if not rows:
        return False
    return rows[-1]["date"] >= as_of.isoformat()


def meets_trend(rows: list[dict]) -> bool:
    """rows 已截到 as_of，且长度 >= MIN_BARS。"""
    if len(rows) < MIN_BARS:
        return False
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    n = len(rows)

    # 1) 连续 3 日收盘 > MA5
    for i in range(n - 3, n):
        ma5 = sum(closes[i - 4 : i + 1]) / 5.0
        if closes[i] <= ma5:
            return False

    # 2) 连续 5 日最低价 > MA10
    for i in range(n - 5, n):
        ma10 = sum(closes[i - 9 : i + 1]) / 10.0
        if lows[i] <= ma10:
            return False

    # 3) 3 日最高 = 20 日最高
    if max(highs[-3:]) != max(highs[-20:]):
        return False

    # 4) 今日 MA5 > 昨日 MA5
    ma5_today = sum(closes[-5:]) / 5.0
    ma5_yday = sum(closes[-6:-1]) / 5.0
    if ma5_today <= ma5_yday:
        return False

    return True


def _empty_counts() -> dict[str, dict[str, float]]:
    return {
        k: {
            "trend": 0,
            "total": 0,
            "amt_below": 0.0,
            "amt_above": 0.0,
            "n_below": 0,
            "n_above": 0,
            "n_amt_na": 0,
        }
        for k, _ in BUCKET_ORDER
    }


def _pct(numer, denom) -> float:
    if not denom:
        return 0.0
    return round(100.0 * float(numer) / float(denom), 2)


def _resolve_amount(row: dict, stock: dict, as_of: date) -> float | None:
    """优先 K 线当日成交额；若 as_of 为今日且缺失，回退东财列表 f6。"""
    amt = row.get("amount")
    if amt is not None and amt > 0:
        return float(amt)
    # 列表成交额仅当统计日=今日时可用
    if as_of == date.today() and stock.get("amount"):
        return float(stock["amount"])
    return None


def compute_trend_strength(
    as_of: date | datetime | str,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """计算并缓存某日趋势强度占比。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        cached = json.loads(out_path.read_text(encoding="utf-8"))
        items = cached.get("items") or []
        # 旧缓存无成交额分档时强制重算
        if items and "amt_above_pct" in items[0]:
            return cached

    if progress:
        print(f"[trend] 拉取股票列表 as_of={as_of_d} …")
    universe = fetch_universe()
    if progress:
        print(f"[trend] 列表 {len(universe)} 只，拉取/复用 K 线 …")

    cache = load_kline_cache()
    need_fetch: list[dict] = []
    for s in universe:
        rows = cache.get(s["code"])
        if rows and cache_covers(rows, as_of_d):
            # 缺成交额则重拉（用于分档图）
            last = truncate_as_of(rows, as_of_d)
            if last and last[-1].get("amount"):
                continue
        need_fetch.append(s)

    fetched = 0
    ok_bars = 0
    if need_fetch:
        def _one(stock: dict) -> tuple[str, list[dict]]:
            rows = fetch_klines_for_stock(stock["code"], stock.get("market"), as_of_d)
            return stock["code"], rows

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_one, s): s["code"] for s in need_fetch}
            for fut in as_completed(futs):
                code, rows = fut.result()
                if rows:
                    cache[code] = rows
                    if len(rows) >= MIN_BARS:
                        ok_bars += 1
                fetched += 1
                if progress and fetched % 400 == 0:
                    print(f"[trend] K 线进度 {fetched}/{len(need_fetch)}（有效≥{MIN_BARS}:{ok_bars}）")
        save_kline_cache(cache)
        if progress:
            print(f"[trend] 新拉/补成交额 K 线 {fetched} 只，有效 {ok_bars}，已写缓存")

    counts = _empty_counts()
    skipped = {"list_days": 0, "bars": 0, "no_kline": 0}

    for s in universe:
        code = s["code"]
        list_date = s.get("list_date")
        if list_date is not None and (as_of_d - list_date).days <= LIST_DAYS_MIN:
            skipped["list_days"] += 1
            continue

        raw = cache.get(code) or []
        rows = truncate_as_of(raw, as_of_d)
        if not rows:
            skipped["no_kline"] += 1
            continue
        if len(rows) < MIN_BARS:
            skipped["bars"] += 1
            continue
        if list_date is None and len(rows) <= LIST_DAYS_MIN:
            skipped["list_days"] += 1
            continue

        if rows[-1]["date"] != as_of_d.isoformat():
            skipped["bars"] += 1
            continue

        bucket = s["bucket"]
        counts["all"]["total"] += 1
        counts[bucket]["total"] += 1

        ld = False
        if len(rows) >= 2:
            prev_c, cur_c = rows[-2]["close"], rows[-1]["close"]
            if prev_c:
                est = (cur_c / prev_c - 1.0) * 100.0
                ld = is_limit_down(code, s["name"], est, cur_c, prev_c)
        elif s.get("pct") is not None:
            ld = is_limit_down(code, s["name"], s.get("pct"), s.get("close"), s.get("preclose"))

        ok = (not ld) and meets_trend(rows)
        if not ok:
            continue

        counts["all"]["trend"] += 1
        counts[bucket]["trend"] += 1

        amt = _resolve_amount(rows[-1], s, as_of_d)
        for key in ("all", bucket):
            if amt is None:
                counts[key]["n_amt_na"] += 1
                continue
            if amt < AMOUNT_SPLIT_YUAN:
                counts[key]["amt_below"] += amt
                counts[key]["n_below"] += 1
            else:
                counts[key]["amt_above"] += amt
                counts[key]["n_above"] += 1

    items = []
    for key, label in BUCKET_ORDER:
        c = counts[key]
        t = int(c["trend"])
        n = int(c["total"])
        ab = float(c["amt_below"])
        aa = float(c["amt_above"])
        amt_sum = ab + aa
        items.append(
            {
                "key": key,
                "name": label,
                "trend": t,
                "total": n,
                "ratio": _pct(t, n),
                "n_below": int(c["n_below"]),
                "n_above": int(c["n_above"]),
                "n_amt_na": int(c["n_amt_na"]),
                "amt_below": round(ab, 2),
                "amt_above": round(aa, 2),
                "amt_below_pct": _pct(ab, amt_sum) if amt_sum else 0.0,
                "amt_above_pct": _pct(aa, amt_sum) if amt_sum else 0.0,
                "amt_below_yi": round(ab / 1e8, 2),
                "amt_above_yi": round(aa / 1e8, 2),
            }
        )

    result = {
        "as_of": as_of_d.isoformat(),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe": len(universe),
        "skipped": skipped,
        "amount_split_yi": 5,
        "items": items,
        "note": (
            "强趋势定义（同时满足）：连续3天收盘价在五日线上方，连续5天最低价在十日线上方，"
            "3日内创20日新高，五日线向上，非跌停。"
        ),
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(f"[trend] 完成 → {out_path}")
        for it in items:
            print(
                f"  {it['name']}: {it['trend']}/{it['total']} = {it['ratio']}% | "
                f"<5亿 {it['amt_below_pct']}% / ≥5亿 {it['amt_above_pct']}% "
                f"({it['amt_below_yi']}+{it['amt_above_yi']}亿)"
            )
    return result


def _parse_long_cache_rows(raw_rows: list) -> list[dict]:
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], dict):
        return list(raw_rows)
    rows: list[dict] = []
    for line in raw_rows:
        parts = str(line).split(",")
        if len(parts) < 5:
            continue
        try:
            rows.append(
                {
                    "date": parts[0][:10],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                }
            )
        except ValueError:
            continue
    return rows


def load_long_kline_cache() -> dict[str, list[dict]]:
    if not LONG_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(LONG_CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {code: _parse_long_cache_rows(rows) for code, rows in (raw or {}).items() if rows}


def save_long_kline_cache(cache: dict[str, list[dict]]) -> None:
    out = {
        code: [
            f"{r['date']},{r['open']},{r['close']},{r['high']},{r['low']},{r.get('amount') or ''}"
            for r in rows
        ]
        for code, rows in cache.items()
    }
    LONG_CACHE_FILE.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def fetch_long_klines_qq(code: str, market: int | None) -> list[dict]:
    sym = sina_symbol(code, market)
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={sym},day,,,{LONG_KLINE_LEN},qfq"
    )
    try:
        payload = _http_json(url, timeout=20)
    except Exception:
        return []
    block = (payload.get("data") or {}).get(sym) or {}
    day = block.get("qfqday") or block.get("day") or []
    rows: list[dict] = []
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
    return rows


def fetch_long_klines_for_stock(code: str, market: int | None, as_of: date) -> list[dict]:
    rows = fetch_long_klines_qq(code, market)
    if len(rows) < MIN_BARS:
        # 拉长搜狐窗口
        start = as_of - timedelta(days=420)
        url = (
            "https://q.stock.sohu.com/hisHq?"
            + urllib.parse.urlencode(
                {
                    "code": f"cn_{code}",
                    "start": start.strftime("%Y%m%d"),
                    "end": as_of.strftime("%Y%m%d"),
                    "stat": 1,
                    "order": "A",
                    "period": "d",
                }
            )
        )
        try:
            payload = _http_json(url, timeout=20)
        except Exception:
            payload = None
        if isinstance(payload, list) and payload:
            hq = payload[0].get("hq") if isinstance(payload[0], dict) else None
            if hq:
                sohu: list[dict] = []
                for item in hq:
                    if not isinstance(item, (list, tuple)) or len(item) < 7:
                        continue
                    try:
                        sohu.append(
                            {
                                "date": str(item[0])[:10],
                                "open": float(item[1]),
                                "close": float(item[2]),
                                "low": float(item[5]),
                                "high": float(item[6]),
                            }
                        )
                    except (TypeError, ValueError):
                        continue
                sohu.sort(key=lambda r: r["date"])
                rows = sohu[-LONG_KLINE_LEN:] or rows
    return rows


def _stock_meets_trend_at(closes: list[float], highs: list[float], lows: list[float], i: int) -> bool:
    """与 meets_trend 同口径，索引 i 为当日（含）。"""
    if i + 1 < MIN_BARS:
        return False
    for j in range(i - 2, i + 1):
        ma5 = sum(closes[j - 4 : j + 1]) / 5.0
        if closes[j] <= ma5:
            return False
    for j in range(i - 4, i + 1):
        ma10 = sum(closes[j - 9 : j + 1]) / 10.0
        if lows[j] <= ma10:
            return False
    if max(highs[i - 2 : i + 1]) != max(highs[i - 19 : i + 1]):
        return False
    if sum(closes[i - 4 : i + 1]) / 5.0 <= sum(closes[i - 5 : i]) / 5.0:
        return False
    return True


def ensure_means_200d(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """确保 means_200d.json 对齐 as_of（近 MEANS_DAYS 个交易日各维度占比均值）。

    返回 means 字典（key → {name, mean_ratio_pct, ...}）。
    """
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    cached: dict | None = None
    if MEANS_200D_FILE.exists() and not force:
        try:
            cached = json.loads(MEANS_200D_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if cached and cached.get("as_of") == as_of_d.isoformat() and cached.get("means"):
            return cached["means"]

    if trading_days is None:
        raise ValueError("ensure_means_200d 需要 trading_days（近200个交易日）")
    days = [d for d in trading_days if d <= as_of_d]
    days = sorted(set(days))[-MEANS_DAYS:]
    if len(days) < MEANS_DAYS:
        if progress:
            print(f"[trend-means] 交易日不足 {MEANS_DAYS}（仅 {len(days)}），跳过重算")
        return (cached or {}).get("means") or {}

    if progress:
        print(f"[trend-means] 重算近{len(days)}日均值 → {days[0]}…{days[-1]}")

    universe = fetch_universe()
    cache = load_long_kline_cache()
    need = [
        s
        for s in universe
        if len(cache.get(s["code"]) or []) < 180
        or (cache.get(s["code"]) or [{}])[0].get("date", "9999") > days[0].isoformat()
        or (cache.get(s["code"]) or [{}])[-1].get("date", "") < as_of_d.isoformat()
    ]
    if need:
        if progress:
            print(f"[trend-means] 补拉长K线 {len(need)} 只…")
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {
                ex.submit(fetch_long_klines_for_stock, s["code"], s.get("market"), as_of_d): s["code"]
                for s in need
            }
            for fut in as_completed(futs):
                code = futs[fut]
                rows = fut.result()
                if rows:
                    cache[code] = rows
                done += 1
                if progress and done % 400 == 0:
                    print(f"[trend-means] K线 {done}/{len(need)} 用时{time.time()-t0:.0f}s")
        save_long_kline_cache(cache)
        if progress:
            print(f"[trend-means] 长K线已更新，用时 {time.time()-t0:.0f}s")

    series: list[tuple] = []
    for s in universe:
        rows = cache.get(s["code"]) or []
        if len(rows) < MIN_BARS:
            continue
        closes = [float(r["close"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]
        idx = {r["date"]: i for i, r in enumerate(rows)}
        series.append((s, idx, closes, highs, lows))

    keys = [k for k, _ in BUCKET_ORDER]
    sum_ratio = {k: 0.0 for k in keys}
    sum_trend = {k: 0.0 for k in keys}
    n_ok = {k: 0 for k in keys}

    for d in days:
        ds = d.isoformat()
        trend = {k: 0 for k in keys}
        total = {k: 0 for k in keys}
        for s, idx, closes, highs, lows in series:
            list_date = s.get("list_date")
            if list_date is not None and (d - list_date).days <= LIST_DAYS_MIN:
                continue
            i = idx.get(ds)
            if i is None:
                continue
            n = i + 1
            if n < MIN_BARS:
                continue
            if list_date is None and n <= LIST_DAYS_MIN:
                continue
            bucket = s["bucket"]
            total["all"] += 1
            total[bucket] += 1
            c0, c1 = closes[i - 1], closes[i]
            if c0 and is_limit_down(s["code"], s["name"], (c1 / c0 - 1.0) * 100.0, c1, c0):
                continue
            if not _stock_meets_trend_at(closes, highs, lows, i):
                continue
            trend["all"] += 1
            trend[bucket] += 1
        for k in keys:
            if total[k] > 0:
                sum_ratio[k] += 100.0 * trend[k] / total[k]
                sum_trend[k] += trend[k]
                n_ok[k] += 1

    means: dict[str, dict] = {}
    for key, name in BUCKET_ORDER:
        means[key] = {
            "name": name,
            "mean_ratio_pct": round(sum_ratio[key] / n_ok[key], 2) if n_ok[key] else None,
            "mean_trend_count": round(sum_trend[key] / n_ok[key], 1) if n_ok[key] else None,
            "n_days": n_ok[key],
        }
    payload = {
        "as_of": as_of_d.isoformat(),
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "n_days": len(days),
        "means": means,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    MEANS_200D_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(
            f"[trend-means] 完成 · 全场均值 {means['all']['mean_ratio_pct']}% "
            f"→ {MEANS_200D_FILE.name}"
        )
    return means


def load_or_compute_trend_strength(as_of: date | datetime | str, *, force: bool = False) -> dict:
    return compute_trend_strength(as_of, force=force, progress=True)


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_trend_strength(day, force=force, progress=True)
