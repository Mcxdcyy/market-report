#!/usr/bin/env python3
"""开盘啦「市场情绪 · 市场量能 · 实际量能」历史序列。

接口：
  a=MarketCapacityKLine&c=HisHomeDingPan&Type=0
  Type=0 沪深京 / 1 沪市 / 2 深市 / 3 北交所

字段 lastPoint 单位：万元 → 换算亿元 = lastPoint / 10000。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "kpl_market_volume.json"
UA = "lhb/5.21.0.2 (iPhone; iOS 17.0; Scale/3.00)"
API_HIS = "https://apphis.longhuvip.com/w1/api/index.php"
API_LIVE = (
    "https://apphq.longhuvip.com/w1/api/index.php",
    "https://apphwshhq.longhuvip.com/w1/api/index.php",
    "https://apphwhq.longhuvip.com/w1/api/index.php",
)

# 沪深京实际量能
TYPE_HSJ = 0


def _as_date(v: date | datetime | str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _http_get_json(url: str, *, timeout: float = 20) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*", "Referer": "https://www.longhuvip.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def fetch_market_capacity_kline(*, type_: int = TYPE_HSJ) -> list[dict[str, Any]]:
    """拉取实际量能日线（新→旧）。返回 [{date, amount_yi, last_point}, ...]。"""
    url = (
        f"{API_HIS}?a=MarketCapacityKLine&c=HisHomeDingPan"
        f"&Type={int(type_)}&PhoneOSNew=1"
    )
    data = _http_get_json(url)
    if not data or data.get("errcode") not in (0, "0"):
        raise RuntimeError(f"MarketCapacityKLine 失败: {(data or {}).get('errmsg') or '空响应'}")
    rows = data.get("info") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ds = str(row.get("Date") or row.get("date") or "")[:10]
        if len(ds) < 10:
            continue
        try:
            lp = float(row.get("lastPoint") or 0)
        except (TypeError, ValueError):
            continue
        # lastPoint 万元 → 亿元
        amount_yi = lp / 10000.0
        out.append({"date": ds, "amount_yi": amount_yi, "last_point": lp})
    if not out:
        raise RuntimeError("MarketCapacityKLine 无有效日线")
    return out


def fetch_market_capacity_live(*, type_: int = TYPE_HSJ) -> dict[str, Any] | None:
    """当日实时实际量能（收盘后与 K 线末日应对齐）。"""
    for host in API_LIVE:
        url = f"{host}?a=MarketCapacity&c=HomeDingPan&Type={int(type_)}&PhoneOSNew=1"
        data = _http_get_json(url)
        if not data or data.get("errcode") not in (0, "0"):
            continue
        info = data.get("info") or {}
        if not isinstance(info, dict):
            continue
        try:
            lp = float(info.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if lp <= 0:
            continue
        ds = str(info.get("date") or "")[:10]
        return {
            "date": ds,
            "amount_yi": lp / 10000.0,
            "last_point": lp,
            "ycln": info.get("ycln"),
        }
    return None


def load_volume_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_volume_cache(payload: dict) -> None:
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _overlay_live_day(
    series: list[dict[str, Any]],
    live: dict[str, Any] | None,
    *,
    as_of: date,
) -> bool:
    """用实时实际量能覆盖对应交易日（避免盘中写入后收盘不更新）。返回是否改动。"""
    if not live or not live.get("date"):
        return False
    ds = str(live["date"])[:10]
    if ds > as_of.isoformat():
        return False
    amt = float(live.get("amount_yi") or 0)
    lp = float(live.get("last_point") or 0)
    if amt <= 0:
        return False
    for row in series:
        if row.get("date") == ds:
            if float(row.get("last_point") or 0) == lp and float(row.get("amount_yi") or 0) == amt:
                return False
            row["amount_yi"] = amt
            row["last_point"] = lp
            return True
    series.append({"date": ds, "amount_yi": amt, "last_point": lp})
    series.sort(key=lambda r: r["date"])
    return True


def ensure_kpl_volume_series(
    as_of: date | datetime | str,
    *,
    n: int = 120,
    type_: int = TYPE_HSJ,
    force: bool = False,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """确保缓存有完整日线，返回按日期升序、截至 as_of 的近 n 日实际量能（亿元）。

    每次调用都会用 MarketCapacity 实时接口覆盖 as_of 当日（及实时接口返回日），
    避免盘中生成报表后缓存锁死不完整成交额。
    """
    as_of_d = _as_date(as_of)
    cached = load_volume_cache()
    series = list(cached.get("daily") or [])
    cache_end = series[-1]["date"] if series else ""
    need_fetch = (
        force
        or not series
        or cached.get("type") != type_
        or cache_end < as_of_d.isoformat()
    )
    changed = False
    if need_fetch:
        if progress:
            print(f"[kpl-vol] 拉取开盘啦实际量能日线 Type={type_} …")
        raw = fetch_market_capacity_kline(type_=type_)
        series = sorted(raw, key=lambda r: r["date"])
        changed = True
        if progress:
            print(f"[kpl-vol] 已拉日线 {len(series)} 日")

    # 无论是否重拉日线，都刷新实时量能（修正盘中不完整缓存）
    live = fetch_market_capacity_live(type_=type_)
    if _overlay_live_day(series, live, as_of=as_of_d):
        changed = True
        if progress and live:
            print(
                f"[kpl-vol] 实时覆盖 {live.get('date')} → "
                f"{float(live.get('amount_yi') or 0):.2f} 亿"
            )

    if changed or not cached.get("daily"):
        payload = {
            "as_of": as_of_d.isoformat(),
            "type": type_,
            "source": "开盘啦·市场情绪·市场量能·实际量能（沪深京）",
            "unit": "亿元（由 lastPoint 万元 /10000）",
            "daily": series,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_volume_cache(payload)
        if progress:
            print(f"[kpl-vol] 已缓存 {len(series)} 日 → {CACHE_FILE.name}")

    clipped = [r for r in series if r.get("date", "") <= as_of_d.isoformat()]
    return clipped[-n:] if len(clipped) >= n else clipped


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    force = "--force" in sys.argv
    rows = ensure_kpl_volume_series(day, n=120, force=force, progress=True)
    print(f"n={len(rows)} {rows[0]['date'] if rows else '-'} … {rows[-1]['date'] if rows else '-'}")
    if rows:
        print(f"最新 {rows[-1]['amount_yi']:.2f} 亿 = {rows[-1]['amount_yi']/10000:.2f} 万亿")
