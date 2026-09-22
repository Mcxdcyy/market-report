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
COUNT_SERIES_30D_FILE = RESULT_DIR / "count_series_30d.json"
HOLD_SERIES_120D_FILE = RESULT_DIR / "hold_series_120d.json"
HOLD_SCHEMA = 2  # 昨均价仅成交额/成交量；异常标「数据异常」，无 HL2 兜底
WORKERS = 48
KLINE_LEN = 40
LONG_KLINE_LEN = 340  # 百日新高(200) + 120日图 + 余量；追高/均值亦够用
MEANS_DAYS = 200
COUNT_SERIES_DAYS = 30
HOLD_SERIES_DAYS = 120
HOLD_MEAN_DAYS = 200
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


def _fetch_universe_eastmoney() -> list[dict]:
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
    return stocks


def _fetch_universe_sina() -> list[dict]:
    """新浪 Market_Center hs_a 兜底（含北交所）；无上市日，靠 K 线根数过滤新股。"""
    stocks: list[dict] = []
    page = 1
    while page <= 120:
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node=hs_a"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://finance.sina.com.cn/",
            },
        )
        last_err: Exception | None = None
        raw = None
        for attempt in range(3):
            try:
                with _opener().open(req, timeout=25) as resp:
                    raw = resp.read().decode()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.4 * (attempt + 1))
        if raw is None:
            raise RuntimeError(f"新浪股票列表拉取失败: {last_err}")
        if not raw or raw in ("null", "[]"):
            break
        rows = json.loads(raw)
        if not rows:
            break
        for row in rows:
            code = str(row.get("code") or "").zfill(6)
            name = str(row.get("name") or "")
            if not code or not name or "退" in name:
                continue
            sym = str(row.get("symbol") or "")
            if is_bj_code(code) or sym.startswith("bj"):
                market = 0
            elif sym.startswith("sh") or code.startswith(("6", "5", "9")):
                market = 1
            else:
                market = 0
            stocks.append(
                {
                    "code": code,
                    "name": name,
                    "market": market,
                    "close": _to_float(row.get("trade")),
                    "pct": _to_float(row.get("changepercent")),
                    "amount": _to_float(row.get("amount")),
                    "preclose": _to_float(row.get("settlement")),
                    "list_date": None,
                    "bucket": classify_bucket(code, name),
                }
            )
        if len(rows) < 80:
            break
        page += 1
    return stocks


def fetch_universe() -> list[dict]:
    """全 A 股票列表：优先东财 clist，失败则新浪 hs_a 兜底。"""
    stocks: list[dict] = []
    last_err: Exception | None = None
    try:
        stocks = _fetch_universe_eastmoney()
    except Exception as exc:  # noqa: BLE001
        last_err = exc
        try:
            print(f"[trend] 东财列表失败，改用新浪兜底：{exc}")
            stocks = _fetch_universe_sina()
        except Exception as exc2:  # noqa: BLE001
            raise RuntimeError(f"股票列表拉取失败: eastmoney={last_err}; sina={exc2}") from exc2
    # 去重
    seen: set[str] = set()
    out: list[dict] = []
    for s in stocks:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        out.append(s)
    if not out:
        raise RuntimeError(f"股票列表为空: {last_err}")
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
            row = {
                "date": parts[0][:10],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
            }
            # 新格式: date,o,c,h,l,volume,amount；旧格式: date,o,c,h,l,amount
            if len(parts) >= 7:
                if parts[5]:
                    row["volume"] = float(parts[5])
                if parts[6]:
                    row["amount"] = float(parts[6])
            elif len(parts) >= 6 and parts[5]:
                row["amount"] = float(parts[5])
            rows.append(row)
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
            f"{r['date']},{r['open']},{r['close']},{r['high']},{r['low']},"
            f"{r.get('volume') or ''},{r.get('amount') or ''}"
            for r in rows
        ]
        for code, rows in cache.items()
    }
    LONG_CACHE_FILE.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def fetch_long_klines_qq(code: str, market: int | None, n: int | None = None) -> list[dict]:
    sym = sina_symbol(code, market)
    n_bars = int(n or LONG_KLINE_LEN)
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={sym},day,,,{n_bars},qfq"
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
            # QQ: volume 为手 → 股；amount 为万元 → 元
            if len(item) >= 6:
                try:
                    vol = float(item[5]) * 100.0
                    if vol > 0:
                        row["volume"] = vol
                except (TypeError, ValueError):
                    pass
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
        start = as_of - timedelta(days=560)
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
                        row = {
                            "date": str(item[0])[:10],
                            "open": float(item[1]),
                            "close": float(item[2]),
                            "low": float(item[5]),
                            "high": float(item[6]),
                        }
                        # sohu 常见: … volume, amount_万元
                        if len(item) >= 9:
                            try:
                                vol = float(str(item[7]).replace(",", ""))
                                if vol > 0:
                                    row["volume"] = vol
                            except (TypeError, ValueError):
                                pass
                            try:
                                amt = float(str(item[8]).replace(",", "")) * 10000.0
                                if amt > 0:
                                    row["amount"] = amt
                            except (TypeError, ValueError):
                                pass
                        sohu.append(row)
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


def _cache_rows_need_avg(rows: list[dict]) -> bool:
    """缺成交额/量时无法算均价 VWAP，需补拉。"""
    if not rows:
        return True
    ok = 0
    for r in rows[-5:]:
        if r.get("amount") and r.get("volume"):
            ok += 1
    return ok < 3


def _prepare_long_series(
    as_of_d: date,
    days: list[date],
    *,
    progress: bool = True,
    log_tag: str = "trend-means",
    need_avg: bool = False,
) -> list[tuple]:
    """拉取/复用长K线，返回 (stock, idx, closes, highs, lows) 列表。"""
    universe = fetch_universe()
    cache = load_long_kline_cache()
    day0 = days[0].isoformat()
    need = [
        s
        for s in universe
        if len(cache.get(s["code"]) or []) < max(180, LONG_KLINE_LEN - 40)
        or (cache.get(s["code"]) or [{}])[0].get("date", "9999") > day0
        or (cache.get(s["code"]) or [{}])[-1].get("date", "") < as_of_d.isoformat()
        or (need_avg and _cache_rows_need_avg(cache.get(s["code"]) or []))
    ]
    if need:
        if progress:
            print(f"[{log_tag}] 补拉长K线 {len(need)} 只…")
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
                    print(f"[{log_tag}] K线 {done}/{len(need)} 用时{time.time()-t0:.0f}s")
        save_long_kline_cache(cache)
        if progress:
            print(f"[{log_tag}] 长K线已更新，用时 {time.time()-t0:.0f}s")

    series: list[tuple] = []
    for s in universe:
        rows = cache.get(s["code"]) or []
        if len(rows) < MIN_BARS:
            continue
        closes = [float(r["close"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]
        opens = [float(r["open"]) for r in rows]
        idx = {r["date"]: i for i, r in enumerate(rows)}
        series.append((s, idx, closes, highs, lows, opens))
    return series


def _count_all_on_day(series: list[tuple], d: date) -> tuple[int, int]:
    """某日全场强趋势家数 / 有效样本。"""
    ds = d.isoformat()
    trend_n = 0
    total_n = 0
    for s, idx, closes, highs, lows, _opens in series:
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
        total_n += 1
        c0, c1 = closes[i - 1], closes[i]
        if c0 and is_limit_down(s["code"], s["name"], (c1 / c0 - 1.0) * 100.0, c1, c0):
            continue
        if not _stock_meets_trend_at(closes, highs, lows, i):
            continue
        trend_n += 1
    return trend_n, total_n


def _bar_avg_price(row: dict, *, high: float, low: float) -> float | None:
    """日均价 = 成交额/成交量（成交均价）。

    结果须落在当日高低附近（含手/股单位修正一次）。算不出则返回 None，
    **不用** (最高+最低)/2。
    """
    hi = max(float(high), float(low)) if high and low else float(high or low or 0)
    lo = min(float(high), float(low)) if high and low else float(low or high or 0)
    try:
        amt = float(row["amount"]) if row.get("amount") is not None else None
        vol = float(row["volume"]) if row.get("volume") is not None else None
    except (TypeError, ValueError):
        amt, vol = None, None
    if amt is None or vol is None or vol <= 0 or amt <= 0 or lo <= 0 or hi <= 0:
        return None
    avg = amt / vol
    # 正常均价应在当日高低之间（略放宽浮点误差）
    if lo * 0.98 <= avg <= hi * 1.02:
        return avg
    # 若 volume 被多乘/少乘 100，试一次修正
    for factor in (100.0, 0.01):
        avg2 = amt / (vol * factor)
        if lo * 0.98 <= avg2 <= hi * 1.02:
            return avg2
    return None


def _day_trend_hold_mean(
    series: list[tuple],
    cache: dict[str, list[dict]],
    d: date,
    prev: date,
) -> tuple[float | None, int, int]:
    """取 prev 日强趋势池，算 d 日 (收盘−prev均价)/prev均价 的算术均值（小数，非%）。

    返回 (均值, 有效家数 n_ok, 均价异常家数 n_bad)。
    """
    ds = d.isoformat()
    ps = prev.isoformat()
    vals: list[float] = []
    n_bad = 0
    for s, idx, closes, highs, lows, _opens in series:
        list_date = s.get("list_date")
        if list_date is not None and (prev - list_date).days <= LIST_DAYS_MIN:
            continue
        i_prev = idx.get(ps)
        i_cur = idx.get(ds)
        if i_prev is None or i_cur is None or i_prev < 1:
            continue
        if i_prev + 1 < MIN_BARS:
            continue
        if list_date is None and (i_prev + 1) <= LIST_DAYS_MIN:
            continue
        # 昨池：非跌停 + 五条件
        c0, c1 = closes[i_prev - 1], closes[i_prev]
        if c0 and is_limit_down(
            s["code"], s["name"], (c1 / c0 - 1.0) * 100.0, c1, c0
        ):
            continue
        if not _stock_meets_trend_at(closes, highs, lows, i_prev):
            continue
        rows = cache.get(s["code"]) or []
        if i_prev >= len(rows):
            n_bad += 1
            continue
        avg_prev = _bar_avg_price(
            rows[i_prev], high=float(highs[i_prev]), low=float(lows[i_prev])
        )
        cur_c = float(closes[i_cur])
        if avg_prev is None or avg_prev <= 0 or cur_c <= 0:
            n_bad += 1
            continue
        vals.append((cur_c - avg_prev) / avg_prev)
    if not vals:
        return None, 0, n_bad
    return sum(vals) / len(vals), len(vals), n_bad


def ensure_trend_hold_series_120d(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """近 HOLD_SERIES_DAYS 日「今日趋势承接」序列 + 近 HOLD_MEAN_DAYS 日均值。

    每日取前一交易日强趋势池，算 (今收−昨均价)/昨均价 的池内均值。
    """
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if HOLD_SERIES_120D_FILE.exists() and not force:
        try:
            cached = json.loads(HOLD_SERIES_120D_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if (
            cached
            and cached.get("schema") == HOLD_SCHEMA
            and cached.get("as_of") == as_of_d.isoformat()
            and isinstance(cached.get("daily"), list)
            and len(cached["daily"]) >= min(10, HOLD_SERIES_DAYS)
            and cached.get("mean_200d") is not None
        ):
            return cached

    # 展示 120 日 + 均值 200 日 + 再多 1 日供首日昨池
    need = HOLD_MEAN_DAYS + 1
    from trading_calendar import trading_days_ending as _cal_days

    if trading_days is None:
        days = _cal_days(as_of_d, need, refresh=False)
    else:
        days = [d for d in trading_days if d <= as_of_d]
        days = sorted(set(days))
        if len(days) < need:
            extra = _cal_days(as_of_d, need, refresh=False)
            days = sorted(set(days) | set(extra))
        days = days[-need:]

    if len(days) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": HOLD_SCHEMA,
            "daily": [],
            "note": "交易日不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        HOLD_SERIES_120D_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    # 计算日 = days[1:]，每日前一日为池
    calc_days = days[1:]
    if progress:
        print(
            f"[trend-hold] 近{min(len(calc_days), HOLD_MEAN_DAYS)}日趋势承接 "
            f"→ {calc_days[0]}…{calc_days[-1]}"
        )

    series = _prepare_long_series(
        as_of_d, days, progress=progress, log_tag="trend-hold", need_avg=True
    )
    cache = load_long_kline_cache()
    daily_all: list[dict] = []
    t0 = time.time()
    for j in range(1, len(days)):
        d = days[j]
        prev = days[j - 1]
        mean_v, n, n_bad = _day_trend_hold_mean(series, cache, d, prev)
        daily_all.append(
            {
                "date": d.isoformat(),
                "value": round(100.0 * mean_v, 4) if mean_v is not None else None,
                "n": n,
                "n_bad": n_bad,
            }
        )

    # 近200日均值（有效样本）
    mean_window = daily_all[-HOLD_MEAN_DAYS:]
    mean_vals = [float(x["value"]) for x in mean_window if x.get("value") is not None]
    mean_200d = (
        round(sum(mean_vals) / len(mean_vals), 4)
        if len(mean_vals) >= HOLD_MEAN_DAYS
        else (round(sum(mean_vals) / len(mean_vals), 4) if mean_vals else None)
    )

    daily = daily_all[-HOLD_SERIES_DAYS:]
    last = daily[-1] if daily else {}
    data_error = int(last.get("n_bad") or 0) > 0
    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": HOLD_SCHEMA,
        "start": daily[0]["date"] if daily else None,
        "end": daily[-1]["date"] if daily else None,
        "n_days": len(daily),
        "mean_200d": mean_200d,
        "mean_days": len(mean_vals),
        "data_error": data_error,
        "daily": daily,
        "note": (
            "今日趋势承接：取前一交易日强趋势池（五条件同趋势强度），"
            "算 (今收−昨均价)/昨均价 的池内算术均值；"
            "昨均价=成交额/成交量；算不出则剔除该票并标「数据异常」。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    HOLD_SERIES_120D_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if progress:
        print(
            f"[trend-hold] 完成 · 末日 {last.get('value')}% "
            f"n={last.get('n')} n_bad={last.get('n_bad')} 均值200d={mean_200d} "
            f"用时{time.time()-t0:.0f}s → {HOLD_SERIES_120D_FILE.name}"
        )
    return payload


def _overlay_daily_result(daily: list[dict], as_of_d: date) -> None:
    """末日家数与当日占比缓存对齐（与下方占比图同一数字）。"""
    path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if not path.exists() or not daily:
        return
    try:
        block = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    for it in block.get("items") or []:
        if it.get("key") != "all":
            continue
        last = daily[-1]
        if last.get("date") != as_of_d.isoformat():
            return
        last["trend"] = int(it.get("trend") or 0)
        last["total"] = int(it.get("total") or 0)
        last["ratio"] = float(it.get("ratio") or 0)
        return


def ensure_count_series_30d(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,
    *,
    force: bool = False,
    progress: bool = True,
) -> list[dict]:
    """近 COUNT_SERIES_DAYS 个交易日全场强趋势家数序列（缓存 count_series_30d.json）。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if COUNT_SERIES_30D_FILE.exists() and not force:
        try:
            cached = json.loads(COUNT_SERIES_30D_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if (
            cached
            and cached.get("as_of") == as_of_d.isoformat()
            and isinstance(cached.get("daily"), list)
            and len(cached["daily"]) >= min(10, COUNT_SERIES_DAYS)
        ):
            daily = list(cached["daily"])
            _overlay_daily_result(daily, as_of_d)
            return daily

    if trading_days is None:
        raise ValueError("ensure_count_series_30d 需要 trading_days")
    days = [d for d in trading_days if d <= as_of_d]
    days = sorted(set(days))[-COUNT_SERIES_DAYS:]
    if not days:
        return []

    if progress:
        print(f"[trend-count] 近{len(days)}日强趋势家数 → {days[0]}…{days[-1]}")

    series = _prepare_long_series(as_of_d, days, progress=progress, log_tag="trend-count")
    daily: list[dict] = []
    t0 = time.time()
    for d in days:
        trend_n, total_n = _count_all_on_day(series, d)
        ratio = round(100.0 * trend_n / total_n, 2) if total_n else 0.0
        daily.append(
            {
                "date": d.isoformat(),
                "trend": trend_n,
                "total": total_n,
                "ratio": ratio,
            }
        )
    _overlay_daily_result(daily, as_of_d)
    payload = {
        "as_of": as_of_d.isoformat(),
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "n_days": len(days),
        "daily": daily,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    COUNT_SERIES_30D_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if progress:
        last = daily[-1]
        print(
            f"[trend-count] 完成 · 末日 {last['trend']} 家 "
            f"用时{time.time()-t0:.0f}s → {COUNT_SERIES_30D_FILE.name}"
        )
    return daily


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

    series = _prepare_long_series(as_of_d, days, progress=progress, log_tag="trend-means")

    keys = [k for k, _ in BUCKET_ORDER]
    sum_ratio = {k: 0.0 for k in keys}
    sum_trend = {k: 0.0 for k in keys}
    n_ok = {k: 0 for k in keys}
    daily_all: list[dict] = []

    for d in days:
        ds = d.isoformat()
        trend = {k: 0 for k in keys}
        total = {k: 0 for k in keys}
        for s, idx, closes, highs, lows, _opens in series:
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
        daily_all.append(
            {
                "date": ds,
                "trend": trend["all"],
                "total": total["all"],
                "ratio": round(100.0 * trend["all"] / total["all"], 2) if total["all"] else 0.0,
            }
        )

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

    # 顺带写出近30日家数序列，供柱图复用（避免再扫一遍）
    tail30 = daily_all[-COUNT_SERIES_DAYS:]
    if tail30:
        _overlay_daily_result(tail30, as_of_d)
        COUNT_SERIES_30D_FILE.write_text(
            json.dumps(
                {
                    "as_of": as_of_d.isoformat(),
                    "start": tail30[0]["date"],
                    "end": tail30[-1]["date"],
                    "n_days": len(tail30),
                    "daily": tail30,
                    "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

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
