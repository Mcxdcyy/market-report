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


def ensure_kpl_volume_series(
    as_of: date | datetime | str,
    *,
    n: int = 120,
    type_: int = TYPE_HSJ,
    force: bool = False,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """确保缓存有完整日线，返回按日期升序、截至 as_of 的近 n 日实际量能（亿元）。"""
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
    if need_fetch:
        if progress:
            print(f"[kpl-vol] 拉取开盘啦实际量能日线 Type={type_} …")
        raw = fetch_market_capacity_kline(type_=type_)
        raw_asc = sorted(raw, key=lambda r: r["date"])
        live = fetch_market_capacity_live(type_=type_)
        if live and live.get("date"):
            ds = live["date"]
            replaced = False
            for row in raw_asc:
                if row["date"] == ds:
                    row["amount_yi"] = live["amount_yi"]
                    row["last_point"] = live["last_point"]
                    replaced = True
                    break
            if not replaced:
                raw_asc.append(
                    {
                        "date": ds,
                        "amount_yi": live["amount_yi"],
                        "last_point": live["last_point"],
                    }
                )
                raw_asc.sort(key=lambda r: r["date"])
        series = raw_asc
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
