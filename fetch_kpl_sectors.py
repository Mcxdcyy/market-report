#!/usr/bin/env python3
"""从开盘啦「市场情绪·股票列表」拉取涨停原因板块。

取数逻辑与出图脚本 `~/Projects/stock-analysis/export_sector_images.py` 对齐：
  1) 优先 GetPlateInfo_w38（DailyLimitResumption）
  2) 若空列表 / 失败 / 返回日期不是目标日（且目标日是今天）
     → 实时兜底：HomeDingPan / DailyLimitPerformance（PidType 1–20）
       按个股涨停原因字段聚合板块

用法：
  python3 fetch_kpl_sectors.py              # 默认最近交易日
  python3 fetch_kpl_sectors.py 2026-08-07
  python3 fetch_kpl_sectors.py 2026-08-07 --history 8   # 回溯 N 个自然日并写入缓存
"""

from __future__ import annotations

import json
import re
import sys
import uuid
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 与出图脚本一致：多主机轮询
API_LIVE_URLS = (
    "https://apphwshhq.longhuvip.com/w1/api/index.php",
    "https://apphwhq.longhuvip.com/w1/api/index.php",
    "https://apphq.longhuvip.com/w1/api/index.php",
)
API_HIS = "https://apphis.longhuvip.com/w1/api/index.php"
HISTORY_FILE = ROOT / "kpl_sector_history.json"
UA = "lhb/5.21.0.2 (iPhone; iOS 17.0; Scale/3.00)"
KPL_VERSION = "5.21.0.2"
KPL_APIV = "w42"

EXCLUDE_REASONS = {"其他", "ST板块", "ST"}


def parse_days(board: str) -> int:
    board = (board or "").strip()
    if not board or board == "首板":
        return 1
    m = re.search(r"(\d+)连板", board)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)天(\d+)板", board)
    if m:
        return int(m.group(2))
    return 1


def is_bse(code: str) -> bool:
    return str(code or "").startswith(("4", "8", "92"))


def is_st(name: str) -> bool:
    return bool(re.search(r"(?:^|\*)ST|退$", name or "", re.I))


def normalize_kpl_date(value) -> str:
    """开盘啦 date/Day → YYYY-MM-DD。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = str(value).strip().replace("/", "-")
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        d = digits[:8]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def _post(url: str, params: dict) -> dict | None:
    body = {**params, "DeviceID": params.get("DeviceID") or str(uuid.uuid4())}
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(body).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    if not raw:
        return None
    return json.loads(raw.decode())


def _post_live(params: dict) -> dict | None:
    """多主机轮询；成功且 errcode=0 才返回。"""
    for url in API_LIVE_URLS:
        try:
            data = _post(url, params)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("errcode") in (0, "0"):
            return data
    return None


def _rows_from_info(info) -> list:
    if not info:
        return []
    if isinstance(info[0], list) and info[0] and isinstance(info[0][0], list):
        return info[0]
    if isinstance(info[0], list) and info[0] and isinstance(info[0][0], str):
        return info
    return []


def _sectors_from_plate_list(raw_list: list, *, source: str) -> list[dict]:
    sectors: list[dict] = []
    for p in raw_list or []:
        name = str(p.get("ZSName") or "").strip()
        if not name or name in EXCLUDE_REASONS:
            continue
        stocks = []
        codes = []
        seen: set[str] = set()
        for s in p.get("StockList") or []:
            if not isinstance(s, (list, tuple)) or len(s) < 2:
                continue
            code = str(s[0] or "").zfill(6)
            sname = str(s[1] or "").strip()
            if not code or code in seen or is_bse(code) or is_st(sname):
                continue
            seen.add(code)
            days = parse_days(s[9] if len(s) > 9 else "首板")
            codes.append(code)
            stocks.append({"code": code, "name": sname, "days": days})
        if not stocks:
            continue
        stocks.sort(key=lambda x: (-x["days"], x["code"]))
        sectors.append(
            {
                "name": name,
                "count": len(stocks),
                "codes": codes,
                "stocks": stocks,
                "source": source,
            }
        )
    sectors.sort(key=lambda x: (-x["count"], x["name"]))
    return sectors


def _display_sectors(sectors: list[dict]) -> list[dict]:
    """与 generate_report.filter_display_sectors 同口径：有 ≥5 只显这些；否则显并列最强。"""
    if not sectors:
        return []
    ranked = sorted(sectors, key=lambda s: (-int(s["count"]), s.get("name") or ""))
    ge5 = [s for s in ranked if int(s["count"]) >= 5]
    if ge5:
        return ge5
    top = int(ranked[0]["count"])
    return [s for s in ranked if int(s["count"]) == top]


def fetch_plate_info_w38(day: str) -> dict:
    """主路径：GetPlateInfo_w38。list 空或失败则抛错，由上层兜底。"""
    raw = _post_live(
        {
            "a": "GetPlateInfo_w38",
            "st": "100",
            "c": "DailyLimitResumption",
            "PhoneOSNew": "1",
            "VerSion": KPL_VERSION,
            "Index": "0",
            "apiv": KPL_APIV,
            "Day": day,
        }
    )
    if not raw:
        raise RuntimeError("GetPlateInfo_w38 无数据")
    if not (raw.get("list") or []):
        raise RuntimeError("GetPlateInfo_w38 空列表")
    sectors = _sectors_from_plate_list(
        raw.get("list") or [],
        source="开盘啦·市场情绪·股票列表",
    )
    got = normalize_kpl_date(raw.get("date")) or normalize_kpl_date(raw.get("Day")) or day
    return {
        "date": got,
        "nums": raw.get("nums") or {},
        "sectors": sectors,
        "source": "GetPlateInfo_w38",
    }


def fetch_realtime_by_reason() -> dict:
    """实时兜底：HomeDingPan / DailyLimitPerformance，按涨停原因聚合。

    与出图脚本 fetch_kpl_realtime_by_reason 同口径；对应 App「股票列表-涨停」原因字段。
    """
    stocks: dict[str, tuple[str, str, int]] = {}
    empty_streak = 0
    for pid in range(1, 21):
        data = _post_live(
            {
                "Order": "0",
                "a": "DailyLimitPerformance",
                "st": "2000",
                "c": "HomeDingPan",
                "PhoneOSNew": "1",
                "VerSion": KPL_VERSION,
                "Index": "0",
                "PidType": str(pid),
                "apiv": KPL_APIV,
                "Type": "4",
            }
        )
        if not data:
            empty_streak += 1
            if empty_streak >= 3 and stocks:
                break
            continue
        rows = _rows_from_info(data.get("info") or [])
        if not rows:
            empty_streak += 1
            if empty_streak >= 3 and stocks:
                break
            continue
        empty_streak = 0
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            code = str(row[0] or "").zfill(6)
            name = str(row[1] or "").strip()
            reason = str(row[5] or "").strip() or "其他"
            if not code or code in stocks or is_bse(code) or is_st(name):
                continue
            # HomeDingPan 行：idx15 为连板高度（整数）；兼容字符串「N连板」
            days = 1
            if len(row) > 15 and row[15] not in (None, ""):
                try:
                    days = max(1, int(row[15]))
                except (TypeError, ValueError):
                    days = parse_days(str(row[15]))
            else:
                days = parse_days(str(row[9]) if len(row) > 9 else "首板")
            stocks[code] = (name, reason, days)

    by_reason: dict[str, list[dict]] = {}
    for code, (name, reason, days) in stocks.items():
        by_reason.setdefault(reason, []).append(
            {"code": code, "name": name, "days": days}
        )

    sectors: list[dict] = []
    for reason, stock_list in by_reason.items():
        if not reason or reason in EXCLUDE_REASONS:
            continue
        stock_list.sort(key=lambda x: (-x["days"], x["code"]))
        sectors.append(
            {
                "name": reason,
                "count": len(stock_list),
                "codes": [x["code"] for x in stock_list],
                "stocks": stock_list,
                "source": "开盘啦·实时涨停原因聚合",
            }
        )
    sectors.sort(key=lambda x: (-x["count"], x["name"]))
    if not sectors:
        raise RuntimeError("实时涨停列表为空")
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "date": today,
        "nums": {"ZT": len(stocks)},
        "sectors": sectors,
        "source": "DailyLimitPerformance/HomeDingPan",
    }


def fetch_limit_up_sectors(day: str) -> dict:
    """当日完整板块列表（与出图脚本同逻辑：GetPlateInfo → 今日实时兜底）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    plate_err: Exception | None = None
    try:
        data = fetch_plate_info_w38(day)
        got = normalize_kpl_date(data.get("date")) or data.get("date") or ""
        if got == day:
            out = data
        elif day == today:
            print(f"  ! GetPlateInfo 仍为 {got or '?'}，改用实时涨停列表按原因聚合")
            out = fetch_realtime_by_reason()
        else:
            # 历史日日期对不上：仍返回，由调用方决定
            data["date_mismatch"] = got
            out = data
    except Exception as exc:
        plate_err = exc
        if day == today:
            print(f"  ! GetPlateInfo 失败（{exc}），改用实时涨停列表")
            out = fetch_realtime_by_reason()
        else:
            raise RuntimeError(f"开盘啦涨停原因拉取失败: {plate_err}") from plate_err

    sectors = out["sectors"]
    display = _display_sectors(sectors)
    return {
        "date": out.get("date") or day,
        "nums": out.get("nums") or {},
        "sectors": sectors,
        "display": display,
        "top5": display,
        "source": out.get("source") or "",
    }


def fetch_theme_counts(day: str) -> dict[str, int]:
    """按开盘啦涨停原因统计家数（历史日可用）。

    合并 HisHomeDingPan / DailyLimitPerformance 的 PidType=1..4
    （约对应 1 板 / 2 板 / 3 板 / 4 板+），与当日股票列表口径一致。
    """
    seen: set[str] = set()
    ctr: Counter[str] = Counter()
    for pid in (1, 2, 3, 4):
        params = {
            "a": "DailyLimitPerformance",
            "c": "HisHomeDingPan",
            "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()),
            "VerSion": KPL_VERSION,
            "apiv": KPL_APIV,
            "Day": day,
            "PidType": str(pid),
            "Type": "4",
            "Index": "0",
            "st": "2000",
        }
        try:
            raw = _post(API_HIS, params) or {}
        except Exception:
            raw = {}
        if raw.get("errcode") not in (None, 0, "0"):
            continue
        stocks = _rows_from_info(raw.get("info") or [])
        for s in stocks:
            if not isinstance(s, (list, tuple)) or len(s) < 2:
                continue
            code = str(s[0] or "").zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            theme = str(s[5]).strip() if len(s) > 5 and s[5] else ""
            if theme and theme not in EXCLUDE_REASONS:
                ctr[theme] += 1
    return dict(ctr)


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_history(hist: dict) -> None:
    HISTORY_FILE.write_text(
        json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure_history_day(day: str, hist: dict | None = None, *, force: bool = False) -> dict[str, int]:
    hist = hist if hist is not None else load_history()
    if not force and isinstance(hist.get(day), dict) and hist[day]:
        return {k: int(v) for k, v in hist[day].items()}
    counts = fetch_theme_counts(day)
    # 当日历史接口未就绪时：用与出图相同的实时列表家数写入缓存
    if not counts and day == datetime.now().strftime("%Y-%m-%d"):
        try:
            live = fetch_limit_up_sectors(day)
            counts = {s["name"]: int(s["count"]) for s in live.get("sectors") or []}
        except Exception:
            counts = {}
    if counts:
        hist[day] = counts
        save_history(hist)
    return counts


def backfill_history(end_day: str, lookback_calendar_days: int = 14) -> dict:
    """回溯自然日，跳过明显无数据的周末空窗；写入 kpl_sector_history.json。"""
    hist = load_history()
    end = datetime.strptime(end_day, "%Y-%m-%d")
    for i in range(lookback_calendar_days):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in hist and hist[d]:
            continue
        try:
            counts = fetch_theme_counts(d)
            if not counts and d == datetime.now().strftime("%Y-%m-%d"):
                live = fetch_limit_up_sectors(d)
                counts = {s["name"]: int(s["count"]) for s in live.get("sectors") or []}
        except Exception as e:
            print(f"skip {d}: {e}")
            continue
        if not counts:
            continue
        hist[d] = counts
        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        print(d, "ZT~", sum(counts.values()), "|", ", ".join(f"{n}:{c}" for n, c in top))
    save_history(hist)
    return hist


def filter_display_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    """与 filter_display_sectors 同口径：有 ≥5 只显这些；否则显并列最强。"""
    items = sorted(((n, int(c)) for n, c in counts.items() if int(c) > 0), key=lambda x: (-x[1], x[0]))
    if not items:
        return []
    ge5 = [it for it in items if it[1] >= 5]
    if ge5:
        return ge5
    top = items[0][1]
    return [it for it in items if it[1] == top]


def default_day() -> str:
    try:
        from generate_report import load_market_data

        df = load_market_data()
        return df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = args[0] if args else default_day()
    hist_n = None
    if "--history" in sys.argv:
        i = sys.argv.index("--history")
        hist_n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 14

    if hist_n:
        backfill_history(day, hist_n)

    data = fetch_limit_up_sectors(day)
    # 同步当日缓存（以列表/兜底接口为准）
    hist = load_history()
    hist[data["date"]] = {s["name"]: int(s["count"]) for s in data["sectors"]}
    save_history(hist)

    nums = data["nums"]
    src = data.get("source") or ""
    print(
        f"{data['date']} 涨停{nums.get('ZT', '?')} 跌停{nums.get('DT', '?')} "
        f"炸板{nums.get('ZBL', '?')} 来源={src}"
    )
    for i, s in enumerate(data["display"], 1):
        heads = "、".join(f"{x['name']}({x['days']})" for x in s["stocks"][:5])
        print(f"{i}. {s['name']} {s['count']}家 | {heads}")
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("（reason/后续事件/预判仍人工写入 market_news.json）")


if __name__ == "__main__":
    main()
