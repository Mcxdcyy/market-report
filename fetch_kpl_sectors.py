#!/usr/bin/env python3
"""从开盘啦「市场情绪·股票列表」同款接口拉取当日涨停原因板块。

用法：
  python3 fetch_kpl_sectors.py              # 默认最近交易日（大盘数据末日）
  python3 fetch_kpl_sectors.py 2026-08-07
"""

from __future__ import annotations

import json
import re
import sys
import uuid
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://apphwshhq.longhuvip.com/w1/api/index.php"


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


def fetch_limit_up_sectors(day: str) -> dict:
    params = {
        "a": "GetPlateInfo_w38",
        "st": "100",
        "c": "DailyLimitResumption",
        "PhoneOSNew": "1",
        "DeviceID": str(uuid.uuid4()),
        "VerSion": "5.21.0.2",
        "Index": "0",
        "apiv": "w42",
        "Day": day,
    }
    req = urllib.request.Request(
        API,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "User-Agent": "lhb/5.21.0.2 (iPhone; iOS 17.0; Scale/3.00)",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode())
    if raw.get("errcode") not in (0, "0"):
        raise RuntimeError(f"开盘啦接口失败: {raw}")

    sectors = []
    for p in raw.get("list") or []:
        name = p.get("ZSName") or ""
        if name == "其他":
            continue
        stocks = []
        codes = []
        for s in p.get("StockList") or []:
            code, sname = s[0], str(s[1]).strip()
            days = parse_days(s[9] if len(s) > 9 else "首板")
            codes.append(code)
            stocks.append({"code": code, "name": sname, "days": days})
        stocks.sort(key=lambda x: (-x["days"], x["code"]))
        sectors.append(
            {
                "name": name,
                "count": len(stocks),
                "codes": codes,
                "stocks": stocks,
                "source": "开盘啦·市场情绪·股票列表",
            }
        )
    sectors.sort(key=lambda x: -x["count"])
    strong = [s for s in sectors if s["count"] >= 5]
    display = strong if strong else sectors[:1]
    return {
        "date": raw.get("date") or day,
        "nums": raw.get("nums") or {},
        "sectors": sectors,
        "display": display,
        # 兼容旧字段名
        "top5": display,
    }


def default_day() -> str:
    try:
        from generate_report import load_market_data

        df = load_market_data()
        return df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else default_day()
    data = fetch_limit_up_sectors(day)
    nums = data["nums"]
    print(
        f"{data['date']} 涨停{nums.get('ZT', '?')} 跌停{nums.get('DT', '?')} "
        f"炸板{nums.get('ZBL', '?')}"
    )
    for i, s in enumerate(data["display"], 1):
        heads = "、".join(f"{x['name']}({x['days']})" for x in s["stocks"][:5])
        print(f"{i}. {s['name']} {s['count']}家 | {heads}")
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("（reason/后续事件/预判仍人工写入 market_news.json；加 --json 可导出骨架）")


if __name__ == "__main__":
    main()
