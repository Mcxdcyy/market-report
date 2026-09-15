#!/usr/bin/env python3
"""新高数量：每日历史新高个股家数（近30 / 近120 交易日）。

口径与大盘表「历史新高」列一致（开盘啦/表格同源），
禁止再用短窗口日K 自行重算「可见新高」（会把阶段高误计为历史新高）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from numbers_parser import Document

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "new_high_count_results"
DATA_FILE = BASE / "大盘数据.numbers"
SERIES_DAYS = 120
SCHEMA = 2  # 改用大盘表历史新高列


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _load_new_high_series(end: date) -> list[dict[str, Any]]:
    """从大盘数据.numbers 读取截至 end 的「历史新高」序列（含窗口前 1 日供着色）。"""
    doc = Document(str(DATA_FILE))
    t = doc.sheets[0].tables[0]
    xh_col = None
    for c in range(t.num_cols):
        a = t.cell(0, c).value
        b = t.cell(1, c).value
        if str(a or "") == "历史新高" or str(b or "") == "历史新高":
            xh_col = c
            break
    if xh_col is None:
        raise RuntimeError("大盘数据.numbers 未找到「历史新高」列")

    rows: list[tuple[date, int]] = []
    for r in range(2, t.num_rows):
        v = t.cell(r, 0).value
        if not hasattr(v, "year"):
            continue
        d = v.date() if isinstance(v, datetime) else v
        if d > end:
            continue
        raw = t.cell(r, xh_col).value
        if raw is None:
            continue
        try:
            n = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        rows.append((d, n))
    # 同日多行取末日
    by_d: dict[date, int] = {}
    for d, n in rows:
        by_d[d] = n
    dates = sorted(by_d)
    need = SERIES_DAYS + 1
    use = dates[-need:] if len(dates) >= need else dates
    return [{"date": d.isoformat(), "count": by_d[d]} for d in use]


def compute_new_high_count(
    as_of: date | datetime | str,
    trading_days: list[date] | None = None,  # 保留签名兼容；忽略，以表格为准
    *,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """读取并缓存近 SERIES_DAYS 日历史新高家数（源：大盘数据.numbers）。"""
    as_of_d = _as_date(as_of)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{as_of_d.isoformat()}.json"
    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if (
                cached.get("as_of") == as_of_d.isoformat()
                and cached.get("schema") == SCHEMA
                and int(cached.get("n_days") or 0) >= min(SERIES_DAYS, 1)
                and cached.get("daily")
            ):
                return cached
        except json.JSONDecodeError:
            pass

    daily = _load_new_high_series(as_of_d)
    if len(daily) < 2:
        payload = {
            "as_of": as_of_d.isoformat(),
            "schema": SCHEMA,
            "daily": daily,
            "note": "历史新高序列不足",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    # daily 含窗口前 1 日；展示窗口为末日 SERIES_DAYS
    series_days = daily[-SERIES_DAYS:] if len(daily) >= SERIES_DAYS else daily[1:] if len(daily) > 1 else daily
    if not series_days and daily:
        series_days = daily
    payload = {
        "as_of": as_of_d.isoformat(),
        "schema": SCHEMA,
        "start": series_days[0]["date"],
        "end": series_days[-1]["date"],
        "n_days": len(series_days),
        "daily": daily,  # 含窗口前 1 日，供首柱着色
        "latest_count": int(daily[-1]["count"]),
        "note": (
            "新高数量取自大盘数据.numbers「历史新高」列（与八维新高指标同源）；"
            "非短窗口日K自行重算。"
        ),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        print(
            f"[newhigh] {as_of_d} 最新 {payload['latest_count']} 家 "
            f"（{payload['start']}…{payload['end']}）→ {out_path.name}"
        )
    return payload


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    compute_new_high_count(day, force=force, progress=True)
