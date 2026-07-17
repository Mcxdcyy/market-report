#!/usr/bin/env python3
"""每日复盘概览 HTML 生成器。

交易模式：
  5/10日线试单 & 新高加仓

策略层级：能做 → 按模式执行；不能做 → 空仓。

用法: python3 generate_report.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from numbers_parser import Document

BASE = Path(__file__).resolve().parent
DOCS_DIR = BASE / "docs"
DATA_FILE = BASE / "大盘数据.numbers"
NEWS_FILE = BASE / "market_news.json"
EVENT_CATALOG_FILE = BASE / "event_catalog.json"
WEEKDAY = "一二三四五六日"
TZ_CN = timezone(timedelta(hours=8))
WSCN_CAL_URL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"


def parse_date(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = re.sub(r"[-\u4e00-\u9fff]+$", "", v.strip()).replace("/", "-")
        try:
            return pd.to_datetime(s)
        except Exception:
            return pd.NaT
    return pd.NaT


def load_market_data() -> pd.DataFrame:
    doc = Document(str(DATA_FILE))
    t = doc.sheets[0].tables[0]
    h0 = [t.cell(0, c).value for c in range(t.num_cols)]
    h1 = [t.cell(1, c).value for c in range(t.num_cols)]
    cols, g = [], ""
    for a, b in zip(h0, h1):
        if a:
            g = a
        cols.append(f"{g}_{b}" if b else (a or f"x{len(cols)}"))
    rows = [[t.cell(r, c).value for c in range(t.num_cols)] for r in range(2, t.num_rows)]
    df = pd.DataFrame(rows, columns=cols)
    df["_src_row"] = range(len(df))
    rename = {
        "大盘成交额_成交金额": "成交额",
        "大盘成交额_增量变化": "增量",
        "涨停数量": "涨停",
        "跌停数": "跌停",
        "历史新高": "新高",
        "追高资金：赚钱效应_追高数量": "主追高数",
        "追高资金：赚钱效应_追高爆赚占比": "主爆赚率",
        "追高资金：赚钱效应_追高爆亏占比": "主爆亏率",
        "创业板追高：赚钱效应_创业板活跃度": "创活跃",
        "创业板追高：赚钱效应_追高爆赚占比": "创爆赚率",
        "创业板追高：赚钱效应_追高爆亏占比": "创爆亏率",
        "涨停 / 炸板：日内封板率_封板率": "封板率",
        "涨停 / 炸板：日内封板率_炸板率": "炸板率",
        "追高资金：赚钱效应_追高封板率": "主追封板率",
        "追高资金：赚钱效应_主-冲高回落": "主冲高回落",
        "追高资金：赚钱效应_冲高未回落占比": "主冲高未回落占比",
        "创业板追高：赚钱效应_创-冲高回落": "创冲高回落",
        "创业板追高：赚钱效应_冲高未回落占比": "创冲高未回落占比",
    }
    df = df.rename(columns=rename)
    df["date"] = df["日期"].apply(parse_date)
    df = df[df["date"].notna()].sort_values(["date", "_src_row"])
    df = df.drop_duplicates(subset=["date"], keep="last").drop(columns=["_src_row"]).reset_index(drop=True)
    num_cols = set(rename.values()) | {"主赚差", "创赚差", "主冲高未回落占比", "创冲高未回落占比"}
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["主赚差"] = df["主爆赚率"] - df["主爆亏率"]
    df["创赚差"] = df["创爆赚率"] - df["创爆亏率"]
    return df


def hist_pct(series: pd.Series, val: float) -> float:
    if pd.isna(val):
        return 50.0
    return float((series.dropna() < val).mean() * 100)


HIST_WINDOW = 300

# 大盘量能：规则阈值（成交额，单位与表格一致）
VOL_TWO_DAY_SHRINK = -0.15  # 连续2日缩量时，两日合计较前一日起点缩量超 15%
VOL_NEUTRAL_BAND = (42, 58)  # 震荡场景分数区间
VOL_GOOD_BAND = (65, 85)
VOL_BAD_BAND = (15, 35)


def hist_tail(df: pd.DataFrame, n: int = HIST_WINDOW) -> pd.DataFrame:
    """近 n 个交易日窗口（含当日），用于六维历史分位。"""
    return df.tail(n)


def hybrid_hist_score(series: pd.Series, val: float, *, lower_better: bool = False) -> dict:
    """近300日：分位×50% + 均值偏离度×50%。

    绝对平均偏离值 = Σ|每日值 − 均值| / 交易日数
    均值偏离值 = clamp((当日值 − 均值) / 绝对平均偏离值 × 25, −25, +25)
    均值偏离度 = 均值偏离值 + 25
    分位 = 分位百分比 × 0.5（lower_better 时对分位做 100−pct 翻转）
    总评分 = clamp(分位 + 均值偏离度)
    """
    s = series.dropna()
    if pd.isna(val) or s.empty:
        return {
            "score": 50,
            "pct": 50.0,
            "pct_part": 25.0,
            "dev_part": 25.0,
            "dev_raw": 0.0,
            "mean": float("nan"),
            "mad": float("nan"),
        }

    mean = float(s.mean())
    n = len(s)
    mad = float((s - mean).abs().sum() / n) if n else float("nan")

    pct = hist_pct(s, val)
    if lower_better:
        pct = 100.0 - pct
    pct_part = pct * 0.5

    if pd.isna(mad) or mad < 1e-12:
        mean_dev = 0.0
    else:
        mean_dev = (float(val) - mean) / mad * 25.0
    mean_dev = max(-25.0, min(25.0, mean_dev))
    dev_part = mean_dev + 25.0
    score = clamp100(pct_part + dev_part)
    return {
        "score": score,
        "pct": pct,
        "pct_part": pct_part,
        "dev_part": dev_part,
        "dev_raw": mean_dev,
        "mean": mean,
        "mad": mad,
        "n": n,
    }


def hybrid_note(h: dict, val_fmt: str, mean_fmt: str | None = None) -> str:
    """生成维度脚注：分位/偏离度为评分贡献；偏离值为可验算指标（−25~+25）。"""
    if pd.isna(h["mean"]):
        return "数据不足"
    mean_s = mean_fmt if mean_fmt is not None else f"均值{h['mean']:.2f}"
    return (
        f"分位{int(round(h['pct']))}%（+{h['pct_part']:.0f}），"
        f"{val_fmt}，{mean_s}（偏离值{h['dev_raw']:+.1f}，偏离度{h['dev_part']:.0f}）"
    )


def score_bar(v: int) -> str:
    if v >= 60:
        return "#34C759"
    if v >= 40:
        return "#FF9500"
    return "#FF3B30"


def score_badge(v: int) -> tuple[str, str]:
    if v >= 60:
        return "ok", "偏强"
    if v >= 40:
        return "warn", "一般"
    return "bad", "偏弱"


def clamp100(v: float) -> int:
    return max(0, min(100, int(round(v))))


def _day_vol_change(df: pd.DataFrame, i: int) -> float | None:
    """第 i 日相对前一交易日的成交额变化率（优先用表格「增量」列）。"""
    if i <= 0:
        return None
    day = df.iloc[i]
    inc = day.get("增量")
    if pd.notna(inc):
        return float(inc)
    cur, prev = day.get("成交额"), df.iloc[i - 1].get("成交额")
    if pd.notna(cur) and pd.notna(prev) and prev:
        return float(cur) / float(prev) - 1
    return None


def _vol_streaks(df: pd.DataFrame) -> tuple[int, int]:
    """连续缩量日数、连续放量日数（均含当日）。"""
    shrink, expand = 0, 0
    for i in range(len(df) - 1, 0, -1):
        chg = _day_vol_change(df, i)
        if chg is None:
            break
        if chg < 0:
            if expand > 0:
                break
            shrink += 1
        elif chg > 0:
            if shrink > 0:
                break
            expand += 1
        else:
            break
    return shrink, expand


def _two_day_total_shrink(df: pd.DataFrame) -> tuple[bool, float | None]:
    """连续2日缩量，且两日合计较第1日前一日缩量超 15%。"""
    if len(df) < 3:
        return False, None
    chg1 = _day_vol_change(df, len(df) - 1)
    chg0 = _day_vol_change(df, len(df) - 2)
    if chg1 is None or chg0 is None or chg1 >= 0 or chg0 >= 0:
        return False, None
    v_start = float(df.iloc[-3].get("成交额") or 0)
    v_end = float(df.iloc[-1].get("成交额") or 0)
    if v_start <= 0:
        return False, None
    cum = v_end / v_start - 1
    return cum <= VOL_TWO_DAY_SHRINK, cum


def _vol_prev3(df: pd.DataFrame) -> tuple[float, float]:
    """前三个交易日成交额均值与最大值（不含当日）。"""
    if len(df) >= 4:
        prev = df.iloc[-4:-1]["成交额"].astype(float)
    elif len(df) >= 2:
        prev = df.iloc[:-1]["成交额"].astype(float)
    else:
        v = float(df.iloc[-1].get("成交额") or 0)
        return v, v
    prev = prev.dropna()
    if prev.empty:
        v = float(df.iloc[-1].get("成交额") or 0)
        return v, v
    return float(prev.mean()), float(prev.max())


def _vol_oscillating(df: pd.DataFrame, n: int = 4) -> bool:
    """近 n 日成交额方向反复（震荡），非单边缩/放。"""
    if len(df) < 3:
        return True
    signs: list[int] = []
    for i in range(max(1, len(df) - n + 1), len(df)):
        chg = _day_vol_change(df, i)
        if chg is None or chg == 0:
            continue
        signs.append(1 if chg > 0 else -1)
    if len(signs) < 2:
        return True
    flips = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    return flips >= 1 and not (all(s < 0 for s in signs) or all(s > 0 for s in signs))


def volume_metrics(row: pd.Series, df: pd.DataFrame) -> dict:
    """大盘成交金额规则评分：差 / 好 / 中性（震荡）。

    差：连续2日总缩量>15%，或连续3日缩量，或缩量且低于前三日均
    好：连续2日放量，或成交额突破前三日最大
    中性：其余震荡，分数随近3日均比偏移
    """
    vol = float(row.get("成交额") or 0)
    inc = _day_vol_change(df, len(df) - 1)
    shrink_streak, expand_streak = _vol_streaks(df)
    two_day_sharp, two_day_cum = _two_day_total_shrink(df)
    prev3_mean, prev3_max = _vol_prev3(df)
    ratio3 = vol / prev3_mean if prev3_mean else 1.0
    above_prev3_max = vol > prev3_max if prev3_max else False
    shrink_below_mean = inc is not None and inc < 0 and vol < prev3_mean

    bad = two_day_sharp or shrink_streak >= 3 or shrink_below_mean
    good = expand_streak >= 2 or above_prev3_max
    oscillating = _vol_oscillating(df)

    reasons: list[str] = []
    if bad:
        regime = "bad"
        score = VOL_BAD_BAND[1]
        if shrink_streak >= 3:
            score -= 8 + min(6, (shrink_streak - 3) * 2)
            reasons.append(f"连续缩量{shrink_streak}日")
        if two_day_sharp:
            score -= 6
            pct = abs(two_day_cum or 0)
            reasons.append(f"连续2日总缩量{pct:.0%}")
        if shrink_below_mean:
            score -= 5
            reasons.append("缩量且低于前三日均")
        score = clamp100(max(VOL_BAD_BAND[0], score))
        if shrink_streak >= 3:
            tag = f"连续缩量{shrink_streak}日"
        elif two_day_sharp:
            tag = "2日总缩量超15%"
        else:
            tag = "缩量偏弱"
    elif good:
        regime = "good"
        score = VOL_GOOD_BAND[0]
        if expand_streak >= 2:
            score += min(10, 4 + expand_streak * 2)
            reasons.append(f"连续放量{expand_streak}日")
        if above_prev3_max:
            score += 8
            reasons.append("突破前三日最大量")
        score = clamp100(min(VOL_GOOD_BAND[1], score))
        if above_prev3_max and expand_streak >= 2:
            tag = "放量突破"
        elif above_prev3_max:
            tag = "突破前三日高量"
        elif expand_streak >= 2:
            tag = f"连续放量{expand_streak}日"
        else:
            tag = "放量"
    else:
        regime = "neutral"
        bias = (ratio3 - 1.0) * 30
        if oscillating:
            bias *= 0.55
        score = clamp100(50 + bias)
        lo, hi = VOL_NEUTRAL_BAND
        score = max(lo, min(hi, score))
        tag = "量能震荡"
        if ratio3 >= 1.03:
            reasons.append("较近3日均略高")
        elif ratio3 <= 0.97:
            reasons.append("较近3日均略低")
        else:
            reasons.append("较近3日均持平")

    vol_wy = vol / 10000
    inc_s = f"，较前日{inc:+.1%}" if inc is not None else ""
    note = f"{vol_wy:.2f}万亿，近3日均{prev3_mean / 10000:.2f}万亿（{ratio3:.0%}）{inc_s}"
    if reasons:
        note += "；" + "，".join(reasons)

    return {
        "score": score,
        "regime": regime,
        "ratio3": ratio3,
        "ratio5": ratio3,
        "ratio2": ratio3,
        "inc": inc,
        "shrink_streak": shrink_streak,
        "expand_streak": expand_streak,
        "two_day_sharp": two_day_sharp,
        "two_day_cum": two_day_cum,
        "prev3_mean": prev3_mean,
        "prev3_max": prev3_max,
        "above_prev3_max": above_prev3_max,
        "oscillating": oscillating,
        "tag": tag,
        "note": note,
        "vol": vol,
    }


def emotion_index(row: pd.Series) -> int:
    zt = row.get("涨停", 0) or 0
    dt = row.get("跌停", 0) or 0
    fb = row.get("封板率", 0.5) or 0.5
    zb = row.get("炸板率", 0.3) or 0.3
    xg = row.get("新高", 0) or 0
    md = row.get("主赚差", 0) or 0
    mb = row.get("主爆赚率", 0) or 0
    parts = [
        min(100, zt / 150 * 100),
        min(100, zt / max(dt, 1) / 8 * 100),
        fb * 100,
        max(0, min(100, 50 + md * 200)),
        mb * 100 if pd.notna(mb) else 50,
        min(100, xg / 120 * 100),
        max(0, 100 - dt * 5),
        max(0, 100 - zb * 100),
    ]
    return int(sum(parts) / len(parts))


def next_trading_day(d: datetime) -> datetime:
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def fmt_md(d: datetime) -> str:
    return f"{d.month:02d}/{d.day:02d}"


def fmt_pct(v: float) -> str:
    return "—" if pd.isna(v) else f"{v:+.1%}"


def fmt_ratio(v: float) -> str:
    return "—" if pd.isna(v) else f"{v:.0%}"


def safe_int(v, default: int = 0) -> int:
    if pd.isna(v):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def normalize_ratio(v) -> float:
    """表格中的回落占比：支持 0–1 或 0–100。"""
    if pd.isna(v):
        return float("nan")
    x = float(v)
    return x / 100.0 if x > 1 else x


def build_env_callout(row: pd.Series, df: pd.DataFrame, dim: dict, dims_3d: list) -> dict:
    """环境区单一结论文案：结论先行 + 关键依据（一句）。"""
    synth = env_synthesis(dim, dims_3d)
    md = float(row.get("主赚差") or 0)
    cd = float(row.get("创赚差") or 0)
    zj = int(row.get("主追高数") or 0)
    hist = hist_tail(df)
    zj_med = float(hist["主追高数"].median()) if len(hist) else zj
    main_money = dim["main_money"]
    main_act = dim["main_act"]
    mc = dim["main_close"]["score"]
    cc = dim["chuang_close"]["score"]
    depth = dim["depth"]
    main_streak = payoff_streak(df, "main")
    vm = dim["vm"]

    if "偏弱" in synth or "防守" in synth:
        tag = "bad"
    elif "观望" in synth or "轻仓" in synth:
        tag = "warn"
    else:
        tag = "ok"

    parts: list[str] = []
    if main_money < 40 and main_act >= 50:
        parts.append(f"主追{zj}家仍热，昨追难兑现（{fmt_pct(md)}/{fmt_pct(cd)}）")
    elif main_money <= 43 or md < -0.05:
        parts.append(f"昨追赚钱效应 {fmt_pct(md)}/{fmt_pct(cd)}")
    if main_streak >= 2:
        parts.append(f"连续{main_streak}日偏弱")
    elif md < -0.05 and zj > zj_med and main_money < 40:
        parts.append("主追仍热")

    if mc >= 55 and cc >= 55 and main_money < 45:
        parts.append(f"今追效应偏强（主{mc}/创{cc}）")
    elif mc < 50 or cc < 50:
        parts.append(f"今追效应一般（主{mc}/创{cc}）")

    if depth < 35:
        parts.append(f"新高{safe_int(row.get('新高'))}家，趋势偏弱")
    if vm.get("regime") == "bad":
        parts.append("大盘量能偏弱")

    detail = "；".join(parts[:3])
    text = f"<strong>{synth}</strong>"
    if detail:
        text += f" {detail}。"

    return {"text": text, "tag": tag, "synth": synth}


def chase_close_quality(row: pd.Series, df: pd.DataFrame, board: str) -> dict:
    """当日今追赚钱效应：取表格「冲高未回落占比」，占比越高越好。"""
    if board == "main":
        ratio_col, cnt_col = "主冲高未回落占比", "主冲高回落"
        label = "主板"
    else:
        ratio_col, cnt_col = "创冲高未回落占比", "创冲高回落"
        label = "创板"

    ratio_raw = row.get(ratio_col)
    if pd.isna(ratio_raw):
        return {
            "score": 50,
            "tag": "—",
            "note": f"{label}暂无今追数据",
            "ratio": None,
            "count": None,
        }

    ratio = normalize_ratio(ratio_raw)
    hist = hist_tail(df)
    valid = (
        hist[ratio_col].dropna().map(normalize_ratio)
        if ratio_col in hist.columns
        else pd.Series(dtype=float)
    )
    h = hybrid_hist_score(valid, ratio)
    score = h["score"]
    hist_mean_ratio = float(valid.mean()) if len(valid) else float("nan")

    tag = score_badge(score)[1]
    cnt = int(row.get(cnt_col)) if cnt_col in row.index and pd.notna(row.get(cnt_col)) else None
    val_fmt = f"冲高未回落占比{fmt_ratio(ratio)}"
    if cnt is not None:
        val_fmt += f"（{cnt}家）"
    if len(valid) >= 5:
        note = hybrid_note(h, val_fmt, f"均值{fmt_ratio(hist_mean_ratio)}")
    else:
        note = f"{val_fmt}（历史样本少）"
    return {"score": score, "tag": tag, "note": note, "ratio": ratio, "count": cnt, "hist": h}


def payoff_streak(df: pd.DataFrame, board: str = "main", threshold: int = 40) -> int:
    """截至最新日，昨日追高赚钱效应连续低于阈值的天数。"""
    col = "主赚差" if board == "main" else "创赚差"
    streak = 0
    for i in range(len(df) - 1, -1, -1):
        sub = df.iloc[: i + 1]
        hist = hist_tail(sub)
        sc = hybrid_hist_score(hist[col], df.iloc[i][col])["score"]
        if sc < threshold:
            streak += 1
        else:
            break
    return streak


TREND_SCORE_KEYS = (
    "vol", "depth", "main_act", "chuang_act", "main_chase", "chuang_chase",
)


# 10日趋势 · 主追/创追效应：昨追赚钱效应 : 今追赚钱效应 = 4 : 6（固定）
CHASE_YDAY_W = 0.4
CHASE_TODAY_W = 0.6


def day_trend_scores(row: pd.Series, full_df: pd.DataFrame) -> dict[str, int]:
    """单日 10 日趋势用 6 项评分（与八维环境同一套算法）。

    主追/创追效应 = 昨追赚钱效应×0.4 + 今追赚钱效应×0.6（今追权重更重）。
    """
    sub = full_df[full_df["date"] <= row["date"]]
    dim = compute_six_dim(row, sub)
    main_chase = clamp100(
        int(round(dim["main_money"] * CHASE_YDAY_W + dim["main_close"]["score"] * CHASE_TODAY_W))
    )
    chuang_chase = clamp100(
        int(round(dim["chuang_money"] * CHASE_YDAY_W + dim["chuang_close"]["score"] * CHASE_TODAY_W))
    )
    return {
        "vol": int(dim["vol_score"]),
        "depth": int(dim["depth"]),
        "main_act": int(dim["main_act"]),
        "chuang_act": int(dim["chuang_act"]),
        "main_chase": main_chase,
        "chuang_chase": chuang_chase,
    }


def coalesce_trend_row(raw: pd.Series, prev: pd.Series | None) -> pd.Series:
    """10日趋势：缺失/半行数据用前一日补全（如 07/03 仅填部分列）。"""
    row = coalesce_row(raw, prev)
    if prev is None:
        return row
    incomplete = any(pd.isna(raw.get(c)) for c in ("主赚差", "新高", "主冲高未回落占比"))
    if not incomplete:
        return row
    for col in ("创活跃", "主追高数", "主冲高未回落占比", "创冲高未回落占比", "新高"):
        if col not in prev.index or pd.isna(prev[col]):
            continue
        cur = row.get(col)
        if pd.isna(cur) or (col == "创活跃" and float(cur or 0) == 0):
            row[col] = prev[col]
    return row


def analyze_10d(last10: pd.DataFrame, full_df: pd.DataFrame) -> tuple[list[dict], str]:
    """近10个交易日：6 项环境评分。"""
    days: list[dict] = []
    coalesced_rows: list[pd.Series] = []

    for i in range(len(last10)):
        raw = last10.iloc[i]
        gidx = full_df.index.get_loc(last10.index[i])
        prev_full = full_df.iloc[gidx - 1] if gidx > 0 else None
        row = coalesce_trend_row(raw, prev_full)
        coalesced_rows.append(row)
        scores = day_trend_scores(row, full_df)
        dt_py = row["date"].to_pydatetime() if hasattr(row["date"], "to_pydatetime") else row["date"]
        item: dict = {
            "date": fmt_md(dt_py),
            "weekday": WEEKDAY[dt_py.weekday()],
            "is_latest": i == len(last10) - 1,
        }
        for key in TREND_SCORE_KEYS:
            val = scores[key]
            item[key] = val
            item[f"{key}_tag"] = score_badge(val)[0]
        days.append(item)

    if days:
        days[-1]["is_latest"] = True

    first, last = coalesced_rows[0], coalesced_rows[-1]
    d0_py = first["date"].to_pydatetime() if hasattr(first["date"], "to_pydatetime") else first["date"]
    d1_py = last["date"].to_pydatetime() if hasattr(last["date"], "to_pydatetime") else last["date"]
    md0 = float(first["主赚差"]) if pd.notna(first.get("主赚差")) else 0.0
    md1 = float(last["主赚差"]) if pd.notna(last.get("主赚差")) else 0.0
    md_trend = md1 - md0
    xg0, xg1 = float(first.get("新高") or 0), float(last.get("新高") or 0)
    xg_ratio = xg1 / xg0 if xg0 else 1.0

    if md1 < -0.08:
        summary, position = "昨追赚钱效应偏弱", "退潮/弱势段"
    elif xg_ratio < 0.6:
        summary, position = "趋势强度明显回落", "趋势退潮段"
    elif md_trend > 0.05:
        summary, position = "昨追赚钱效应修复", "情绪修复段"
    elif md_trend < -0.05:
        summary, position = "昨追赚钱效应走弱", "震荡防守"
    else:
        summary, position = "涨跌互现，结构分化", "震荡轮动"

    headline = f"{fmt_md(d0_py)}→{fmt_md(d1_py)}：{summary}。当前处于<strong>{position}</strong>。"
    return days, headline


def analyze_3d(df: pd.DataFrame, row: pd.Series) -> list[tuple[str, str]]:
    """近3日方向标签，仅用于环境结论（不展示在八维卡片）。"""
    prev = df.iloc[-2] if len(df) >= 2 else row
    recent5 = df.tail(5)
    prior5 = df.iloc[-10:-5] if len(df) >= 10 else df.iloc[:-5]
    md, md_p = row["主赚差"], prev["主赚差"]

    if md < md_p - 0.05:
        s1 = "bad"
    elif md > md_p + 0.05:
        s1 = "ok" if md > 0 else "warn"
    else:
        s1 = "warn" if md < 0 else "ok"

    xg5 = recent5["新高"].mean() if len(recent5) else 0
    xg5p = prior5["新高"].mean() if len(prior5) else xg5
    if xg5 < xg5p * 0.7:
        s2 = "warn"
    elif xg5 > xg5p * 1.2:
        s2 = "ok"
    else:
        s2 = "warn"

    vm = volume_metrics(row, df)
    if vm["regime"] == "bad":
        s3 = "bad"
    elif vm["regime"] == "good":
        s3 = "ok"
    else:
        s3 = "warn"

    return [("主板昨追赚钱效应", s1), ("趋势强度", s2), ("大盘量能", s3)]


def merge_env_scores(dim: dict) -> list[tuple]:
    """八维环境卡片，固定顺序。"""
    vm = dim["vm"]
    main_close = dim["main_close"]
    chuang_close = dim["chuang_close"]
    return [
        ("大盘量能", dim["vol_score"], vm["tag"], dim["vol_note"]),
        ("趋势强度", dim["depth"], dim["depth_tag"], dim["depth_note"]),
        ("主板活跃度", dim["main_act"], score_badge(dim["main_act"])[1], dim["main_act_note"]),
        ("创业板活跃度", dim["chuang_act"], score_badge(dim["chuang_act"])[1], dim["chuang_act_note"]),
        ("主板昨追赚钱效应", dim["main_money"], score_badge(dim["main_money"])[1], dim["main_money_note"]),
        ("创板昨追赚钱效应", dim["chuang_money"], score_badge(dim["chuang_money"])[1], dim["chuang_money_note"]),
        ("主板今追赚钱效应", main_close["score"], main_close["tag"], main_close["note"]),
        ("创板今追赚钱效应", chuang_close["score"], chuang_close["tag"], chuang_close["note"]),
    ]


def env_synthesis(dim: dict, dims_3d: list) -> str:
    """环境一句话结论（基于八维，无综合分）。"""
    bad_n = sum(1 for d in dims_3d if d[1] == "bad")
    mm, depth = dim["main_money"], dim["depth"]
    if mm < 40 or bad_n >= 2:
        return "环境偏弱，宜防守精选"
    if mm <= 43:
        return "昨追赚钱效应弱，宜观望、不追"
    if depth < 35:
        return "趋势强度不足，宜防守"
    if all(d[1] == "ok" for d in dims_3d):
        return "趋势环境尚可，两种模式均可参与"
    return "结构分化，轻仓精选"


# 盘后公告精选：A业绩 B主线强化/证伪 C异动 D政策节点
CURATE_CAPS_OK = {"A": 5, "B": 4, "C": 3, "D": 2}
CURATE_CAPS_WEAK = {"A": 3, "B": 2, "C": 3, "D": 2}
CURATE_CAT_LABEL = {"A": "业绩", "B": "主线", "C": "异动", "D": "节点"}
TIER_RANK = {"龙头": 3, "中军": 2, "边缘": 1}
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1}


def env_is_weak(main_money: int, synth: str, dims_3d: list) -> bool:
    bad_n = sum(1 for d in dims_3d if d[1] == "bad")
    return main_money < 40 or bad_n >= 2 or "偏弱" in synth or "防守" in synth


# 盘面点评/旧闻余波，非上市公司盘后正式披露
_COMMENTARY_MARKERS = (
    "主题余温", "进展关注", "大幅回调", "概念走弱",
    "资金从", "需区分", "概念炒作", "当日PCB", "当日机器人",
)


def _is_formal_disclosure(item: dict) -> bool:
    if item.get("commentary") or item.get("market_note"):
        return False
    blob = f"{item.get('title', '')}{item.get('content', '')}"
    return not any(m in blob for m in _COMMENTARY_MARKERS)


def infer_announce_category(item: dict) -> str:
    if item.get("category") in CURATE_CAPS_OK:
        return item["category"]
    blob = f"{item.get('title', '')}{item.get('content', '')}"
    if item.get("type") == "预告" or any(k in blob for k in ("预增", "扭亏", "首亏", "下修", "业绩快报")):
        return "A"
    if any(k in blob for k in ("异动", "波动", "风险提示")) and "澄清" not in blob:
        return "C"
    if any(k in blob for k in ("澄清", "传闻", "撇清", "证伪", "无相关", "不足1")):
        return "B"
    if any(k in blob for k in ("收购", "订单", "涨价", "量产", "合作", "注册生效", "IPO")):
        return "B"
    if item.get("type") in ("政策", "事件") or any(
        k in blob for k in ("国务院", "商务部", "发改委", "工信部", "药监局", "央行", "出口管制", "国标")
    ):
        return "D"
    return "B"


def _sector_match(item: dict, sector_names: list[str]) -> bool:
    for sec in item.get("sectors") or []:
        for tn in sector_names:
            if sec in tn or tn in sec:
                return True
    return False


def _curate_sort_key(item: dict, sector_names: list[str], env_weak: bool) -> tuple:
    match = int(_sector_match(item, sector_names))
    tier = TIER_RANK.get(item.get("tier"), 1)
    impact = IMPACT_RANK.get(item.get("impact"), 2)
    tag = item.get("tag", "warn")
    if env_weak:
        tag_boost = {"bad": 2, "warn": 1, "ok": 0}.get(tag, 0)
    else:
        tag_boost = {"ok": 2, "warn": 1, "bad": 0}.get(tag, 0)
    return (match, impact + tag_boost, tier)


def _timing_matches_report_day(timing: str, as_of: datetime) -> bool:
    m, d = as_of.month, as_of.day
    same_day_refs = (
        f"{m}/{d}",
        f"{m:02d}/{d:02d}",
        as_of.strftime("%Y-%m-%d"),
        f"{m}月{d}日",
        f"{m}月{d}日晚间",
        f"{m}月{d}日晚上",
    )
    if any(ref in timing for ref in same_day_refs):
        return True
    for om, od in re.findall(r"(\d{1,2})/(\d{1,2})", timing):
        if int(om) == m and int(od) == d:
            return True
    return False


def _is_same_day_post_close(item: dict, as_of: datetime) -> bool:
    """保留报表当日上市公司盘后披露，或当日公开发布的重要国家政策。"""
    if item.get("pre_close") is True:
        return False
    timing = str(item.get("timing") or "").strip()
    if not timing:
        return False

    # 国家政策：当日发布即可（含盘中/盘后/印发/批复等），不要求 15:00 后
    if item.get("type") == "政策":
        if not _timing_matches_report_day(timing, as_of):
            return False
        policy_markers = ("发布", "印发", "实施", "批复", "出台", "公告", "施行", "盘后", "晚间", "收盘后")
        if timing in ("盘后", "收盘后"):
            return True
        return any(k in timing for k in policy_markers)

    # 上市公司公告：须当日收盘后披露
    if any(k in timing for k in ("盘前", "盘中", "午间", "早盘", "开盘")):
        return False
    if not any(k in timing for k in ("盘后", "晚间", "收盘后")):
        return False
    if not _timing_matches_report_day(timing, as_of):
        return False
    if timing in ("盘后", "收盘后"):
        return True
    return True


def curate_post_close(
    pool: list,
    top_sectors: list,
    env_weak: bool,
    as_of: datetime,
) -> tuple[list, dict]:
    """从候选池精选盘后披露（仅当日收盘后）。返回 (精选列表, 统计)。"""
    sector_names = [s.get("name", "") for s in top_sectors]
    caps = CURATE_CAPS_WEAK if env_weak else CURATE_CAPS_OK

    candidates = []
    for item in pool:
        if item.get("skip") or item.get("routine"):
            continue
        if not _is_same_day_post_close(item, as_of):
            continue
        if not (item.get("title") or item.get("content")):
            continue
        if not _is_formal_disclosure(item):
            continue
        cat = infer_announce_category(item)
        enriched = {**item, "category": cat}
        enriched["_sort"] = _curate_sort_key(enriched, sector_names, env_weak)
        candidates.append(enriched)

    by_cat: dict[str, list] = {k: [] for k in caps}
    for item in candidates:
        by_cat[item["category"]].append(item)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: x["_sort"], reverse=True)

    selected: list = []
    stats = {"pool": len(pool), "eligible": len(candidates), "by_cat": {}}
    for cat, limit in caps.items():
        picked = by_cat[cat][:limit]
        stats["by_cat"][cat] = len(picked)
        for it in picked:
            selected.append({k: v for k, v in it.items() if k != "_sort"})

    # 总排序：板块匹配 > 类别优先级 A,B,C,D
    cat_order = {"A": 4, "B": 3, "C": 2, "D": 1}
    selected.sort(
        key=lambda x: (
            int(_sector_match(x, sector_names)),
            cat_order.get(x.get("category"), 0),
            _curate_sort_key(x, sector_names, env_weak),
        ),
        reverse=True,
    )
    stats["selected"] = len(selected)
    return selected, stats



def normalize_stock_code(code) -> str:
    s = re.sub(r"\D", "", str(code or ""))
    return s[-6:].zfill(6) if s else ""


def stock_board(code: str) -> str:
    c = normalize_stock_code(code)
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("83", "87", "43", "92")):
        return "北交所"
    return "主板"


def is_st_stock(item: dict) -> bool:
    if item.get("st") is True:
        return True
    name = str(item.get("name") or "")
    return bool(re.search(r"\*?ST", name, re.I))


def parse_stock_item(raw) -> dict | None:
    if isinstance(raw, str):
        code = normalize_stock_code(raw)
        return {"code": code, "name": "", "days": 1} if code else None
    if isinstance(raw, dict):
        code = normalize_stock_code(raw.get("code"))
        if not code:
            return None
        return {
            "code": code,
            "name": str(raw.get("name") or "").strip(),
            "days": max(1, int(raw.get("days") or raw.get("lb") or 1)),
        }
    return None


def collect_sector_stocks(sector: dict) -> list[dict]:
    """汇总板块涨停股：含主板/创业板/科创板，剔除 ST。"""
    items: list[dict] = []
    if sector.get("ladder"):
        for tier in sector["ladder"]:
            days = max(1, int(tier.get("days") or tier.get("lb") or 1))
            for raw in tier.get("stocks") or tier.get("codes") or []:
                it = parse_stock_item(raw)
                if it:
                    it["days"] = days
                    items.append(it)
    for raw in sector.get("stocks") or []:
        it = parse_stock_item(raw)
        if it:
            items.append(it)
    for raw in sector.get("codes") or []:
        it = parse_stock_item(raw)
        if it:
            items.append(it)

    by_code: dict[str, dict] = {}
    for it in items:
        if is_st_stock(it):
            continue
        code = it["code"]
        if code not in by_code or it["days"] > by_code[code]["days"]:
            by_code[code] = it

    out = []
    for it in by_code.values():
        it["board"] = stock_board(it["code"])
        out.append(it)
    out.sort(key=lambda x: (-x["days"], x["board"], x["code"]))
    return out


def sync_sector_count(sector: dict) -> dict:
    """按全市场涨停（含创/科，剔除ST）重算板块涨停家数。"""
    stocks = collect_sector_stocks(sector)
    if stocks:
        sector["count"] = len(stocks)
    return sector


def _news_days_chronological(raw: dict) -> list[tuple[datetime, dict]]:
    days: list[tuple[datetime, dict]] = []
    for key, val in raw.items():
        d = parse_date(key)
        if pd.isna(d):
            continue
        if hasattr(d, "to_pydatetime"):
            d = d.to_pydatetime()
        days.append((d, val))
    days.sort(key=lambda x: x[0])
    return days


def sector_hot_streak(sector_name: str, as_of: datetime, raw: dict) -> int:
    """连续天数：板块涨停家数≥5（从 as_of 往前，仅统计 market_news.json 有记录的交易日）。"""
    if not sector_name or not raw:
        return 0
    end = as_of.date() if isinstance(as_of, datetime) else as_of
    days = [(d, v) for d, v in _news_days_chronological(raw) if d.date() <= end]
    days.sort(key=lambda x: x[0], reverse=True)
    streak = 0
    for _, day in days:
        count = None
        for sec in day.get("top_sectors") or []:
            if sec.get("name") != sector_name:
                continue
            synced = sync_sector_count(dict(sec))
            count = int(synced.get("count") or 0)
            break
        if count is not None and count >= 5:
            streak += 1
        else:
            break
    return streak


def enrich_sector_streaks(sectors: list, as_of: datetime, raw: dict) -> list:
    for s in sectors:
        s["streak_days"] = sector_hot_streak(s.get("name", ""), as_of, raw)
    return sectors


def load_market_news(
    as_of: datetime,
    *,
    env_weak: bool = False,
) -> dict:
    """读取消息面并精选盘后公告。"""
    key = as_of.strftime("%Y-%m-%d")
    empty = {
        "top_sectors": [],
        "post_close": [],
        "curate_stats": {},
        "has_data": False,
        "hint": f"请在 market_news.json 中补充 {key}（top_sectors + post_close_pool + direction_analysis；codes/stocks 含创业板/科创板，剔除ST）",
    }
    if not NEWS_FILE.exists():
        empty["hint"] = "尚未创建 market_news.json，可在项目目录新建并填写当日消息"
        return empty
    try:
        raw = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        empty["hint"] = "market_news.json 格式有误，请检查 JSON 语法"
        return empty
    day = raw.get(key)
    if not day:
        return empty
    sectors = [sync_sector_count(dict(s)) for s in (day.get("top_sectors") or [])]
    sectors = [s for s in sectors if int(s.get("count") or 0) >= 4][:5]
    pool = day.get("post_close_pool") or day.get("post_close") or []
    curated, stats = curate_post_close(pool, sectors, env_weak, as_of)
    return {
        "top_sectors": sectors,
        "post_close": curated,
        "curate_stats": stats,
        "direction_analysis": day.get("direction_analysis"),
        "has_data": True,
        "hint": "",
    }


# 种子事件库（含人工撰写的 brief；抓取结果会与之合并）
SEED_EVENT_CATALOG: list[dict] = [
    {
        "month": 7,
        "day": 15,
        "dot": "15",
        "label": "7/15",
        "title": "中报预告截止",
        "short": "预告截止",
        "brief": (
            "沪深交易所规定：触及披露标准的上市公司须在此时限前发布2026上半年业绩预告。"
            "截止日后转入验牌分化——真预增与蹭概念分化加剧，资金从预期切换到兑现。"
        ),
        "hot": True,
        "source": "seed",
    },
    {
        "month": 7,
        "day": 17,
        "dot": "AI",
        "label": "7/17–20",
        "title": "WAIC 世界人工智能大会",
        "short": "WAIC",
        "brief": (
            "上海 WAIC 2026（7/17–20）升格为人工智能全球治理高级别会议；"
            "外交部确认最高领导人出席开幕式并阐述 AI 治理立场。"
            "会中看点：华为 Atlas 950 真机首展、具身/大模型新品、拟人化互动新规落地后的官方解读；"
            "应用与政策叙事强于纯硬件二波。"
        ),
        "hot": True,
        "span_end": (7, 20),
        "source": "seed",
    },
    {
        "month": 7,
        "day": 22,
        "dot": "药",
        "label": "7/22–24",
        "title": "CPIC 中国国际医药创新大会",
        "short": "CPIC",
        "brief": (
            "国家会展中心（上海）举办，覆盖创新药、CXO、BD 授权与审评政策。"
            "关注临床数据、合作签约与医保/集采预期；与同周低空展并行，医药线仍看 BD/业绩锚。"
        ),
        "hot": False,
        "span_end": (7, 24),
        "source": "seed",
    },
    {
        "month": 7,
        "day": 22,
        "dot": "空",
        "label": "7/22–25",
        "title": "国际低空经济博览会",
        "short": "低空展",
        "brief": (
            "国家会展中心（上海）7/22–25，低空经济全产业链专业展（约 10 万㎡）。"
            "关注整机/eVTOL、基础设施与地方政策落地叙事，属板块级会议催化，谨防一日游。"
        ),
        "hot": False,
        "span_end": (7, 25),
        "source": "seed",
    },
    {
        "month": 7,
        "day": 25,
        "dot": "政",
        "label": "7/25–31",
        "title": "年中政治局会议窗口",
        "short": "政治局",
        "brief": (
            "历年 7 月下旬召开，分析上半年经济形势、部署下半年工作；具体日期待新华社通稿。"
            "定调稳增长与资本市场表述，影响消费、高股息与总量预期。"
        ),
        "hot": True,
        "span_end": (7, 31),
        "source": "seed",
    },
]

# 事件库：只保留未来2周可能形成板块级炒作的节点（会议/政策/财报窗口），不要单股解禁/IPO
EVENT_STRONG_THEME_KW = (
    "WAIC", "世界人工智能", "人工智能大会",
    "具身", "人形机器人", "机器人+", "智能机器人", "机器人产业",
    "CPIC", "创新药", "细胞与基因", "CGT", "医药创新",
    "中报", "业绩预告", "预告截止", "业绩快报", "财报",
    "政治局", "国务院常务", "证监会", "工信部", "药监局", "发改委",
    "低空经济", "半导体", "自主可控", "国产替代", "制裁", "突发",
)
EVENT_POLICY_KW = (
    "政策", "意见", "方案", "规划", "试点", "通知", "征求", "条例", "办法",
)
# 区域车展、厂商峰会、通用装备展等——历年难独立带起 A 股板块炒作
EVENT_WEAK_EXPO_KW = (
    "国际汽车博览会", "汽车博览会", "国际车展",
    "光合组织", "Salesforce",
    "智能计算应用大会", "智能装备博览会", "APIE", "亚太国际智能装备",
    "中国互联网大会",
)
EVENT_SUPPLY_KW = (
    "限售股解禁", "基石限售", "首批IPO", "IPO基石", "港股上市", "港股IPO",
    "预计7月", "预计8月", "基石投资者", "限售股", "上市仪式",
)
EVENT_EXCLUDE_KW = (
    "M0货币", "M1货币", "M2货币", "M2", "新增人民币贷款", "新增信贷", "社融",
    "贸易帐", "进口同比", "出口同比", "外汇储备", "顺差", "逆差",
    "逆回购", "买断式", "央行将开展", "央行开展", "央行公布",
    "1至6月", "1至5月", "1-6月", "1-5月",
    "数字经济论坛", "货币论坛", "固定收益", "将出席",
    "CPI", "PPI", "GDP", "PMI", "LPR",
)


def _event_has_strong_theme(title: str) -> bool:
    return any(k in title for k in EVENT_STRONG_THEME_KW)


def _event_is_waic_prep_noise(title: str) -> bool:
    """WAIC 会前吹风/筹备/亮相预告/闭幕日，不单列，说明并入 WAIC seed。"""
    if not any(k in title for k in ("世界人工智能", "WAIC", "人工智能大会")):
        return False
    if any(k in title for k in ("筹备", "预热", "吹风", "闭幕", "亮相", "即将")):
        return True
    return "发布会" in title and "介绍" in title


def _event_is_weak_expo(title: str) -> bool:
    """区域车展、厂商峰会、通用展会等弱催化。"""
    if any(k in title for k in EVENT_WEAK_EXPO_KW):
        return True
    if ("博览会" in title or "展会" in title) and not _event_has_strong_theme(title):
        return True
    if "年度峰会" in title and not _event_has_strong_theme(title):
        return True
    if ("大会" in title or "论坛" in title) and not _event_has_strong_theme(title):
        if not any(k in title for k in EVENT_POLICY_KW):
            return True
    return False


def _event_is_supply_only(title: str) -> bool:
    """单股供给冲击（解禁/IPO），难形成板块规模炒作。"""
    return any(k in title for k in EVENT_SUPPLY_KW)


def _event_is_catalyst(title: str, ev: dict | None = None) -> bool:
    """是否属于可炒作节点（强主题会议/政策/财报窗口），排除 routine 宏观、单股解禁、弱展会。"""
    if ev and ev.get("source") == "seed":
        return True
    if not title:
        return False
    if _event_is_supply_only(title):
        return False
    if any(k in title for k in EVENT_EXCLUDE_KW):
        return False
    if _event_is_weak_expo(title):
        return False
    if _event_is_waic_prep_noise(title):
        return False
    if _event_has_strong_theme(title):
        return True
    if any(k in title for k in EVENT_POLICY_KW):
        return True
    return False


def _http_json(url: str, params: dict | None = None, timeout: int = 15) -> dict:
    """GET JSON；默认不走系统代理（避免 Cursor/IDE 注入的本地代理对 HTTPS CONNECT 返回 403）。"""
    qs = urllib.parse.urlencode(params or {})
    full = f"{url}?{qs}" if qs else url
    req = urllib.request.Request(
        full,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    use_proxy = os.environ.get("REPORT_HTTP_USE_PROXY", "").lower() in ("1", "true", "yes")
    if use_proxy:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    else:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx),
        )
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _as_cn_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ_CN)
    return value.astimezone(TZ_CN)


def _event_end_dt(ev: dict, year: int) -> datetime:
    span = ev.get("span_end")
    if span:
        return datetime(year, int(span[0]), int(span[1]), tzinfo=TZ_CN)
    return datetime(year, ev["month"], ev["day"], tzinfo=TZ_CN)


def _parse_span_from_title(title: str, month: int, day: int) -> tuple[int, int] | None:
    m = re.search(
        rf"{month}月{day}日(?:至|到|-)(\d{{1,2}})月(\d{{1,2}})日",
        title,
    )
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(rf"{month}月{day}日(?:至|到|-)(\d{{1,2}})日", title)
    if m:
        return month, int(m.group(1))
    m = re.search(r"(\d{1,2})月(\d{1,2})日(?:至|到|-)(\d{1,2})月(\d{1,2})日", title)
    if m:
        return int(m.group(3)), int(m.group(4))
    return None


def _event_dot(title: str, day: int) -> str:
    if "WAIC" in title or "人工智能" in title:
        return "AI"
    if any(k in title for k in ("医药", "CPIC", "创新药")):
        return "药"
    if "机器人" in title or "具身" in title:
        return "机"
    if "LPR" in title:
        return "LPR"
    if "GDP" in title or "CPI" in title or "PPI" in title:
        return "数"
    if "预告" in title or "中报" in title:
        return str(day)
    return str(day)


def _event_short(title: str) -> str:
    for kw, short in (
        ("WAIC", "WAIC"),
        ("世界人工智能大会", "WAIC"),
        ("中报", "预告"),
        ("业绩预告", "预告"),
        ("LPR", "LPR"),
        ("GDP", "GDP"),
        ("CPI", "CPI"),
        ("具身智能", "具身"),
        ("机器人", "机器人"),
        ("医药", "医药"),
    ):
        if kw in title:
            return short
    return title[:8] + ("…" if len(title) > 8 else "")


def _event_brief(title: str) -> str:
    if "WAIC" in title or "世界人工智能大会" in title:
        return (
            "上海 WAIC 2026（7/17–20）升格为人工智能全球治理高级别会议；"
            "最高领导人出席开幕式。会中看 Atlas 950、具身/大模型新品与治理政策叙事。"
        )
    if "预告" in title or "中报" in title:
        return "财报验证节点，预增抢跑与蹭概念分化，截止日前后波动通常放大。"
    if "LPR" in title:
        return "利率政策信号，影响金融、地产链及高股息资产定价。"
    if any(k in title for k in ("GDP", "CPI", "PPI")):
        return "宏观数据公布，影响总量预期与顺周期/消费板块情绪。"
    if any(k in title for k in ("机器人", "具身", "人工智能")):
        return "产业顶会/强主题展会，关注主题龙头竞价强度与分化。"
    if "解禁" in title or "IPO" in title:
        return "供给端事件，关注解禁规模、筹码结构与短线承接。"
    return f"{title.rstrip('。')}。关注相关板块竞价与持续性。"


def _wscn_relevant(item: dict) -> bool:
    title = item.get("title") or ""
    country = item.get("country") or ""
    if country not in ("中国", "中国香港"):
        return False
    imp = int(item.get("importance") or 0)
    return _event_is_catalyst(title, {"importance": imp, "source": "wscn"})


def _wscn_to_catalog(item: dict) -> dict | None:
    ts = item.get("public_date")
    if not ts:
        return None
    dt = datetime.fromtimestamp(int(ts), TZ_CN)
    title = (item.get("title") or "").strip().rstrip("。")
    if not title:
        return None
    month, day = dt.month, dt.day
    span = _parse_span_from_title(title, month, day)
    imp = int(item.get("importance") or 0)
    hot = any(k in title for k in ("WAIC", "中报", "业绩预告", "世界人工智能", "人工智能大会", "政治局"))
    label = f"{month}/{day}"
    if span:
        label = f"{month}/{day}–{span[1]}" if span[0] == month else f"{span[0]}/{span[1]}"
    ev = {
        "month": month,
        "day": day,
        "dot": _event_dot(title, day),
        "label": label,
        "title": title,
        "short": _event_short(title),
        "brief": _event_brief(title),
        "hot": hot,
        "importance": imp,
        "source": "wscn",
        "fetched_at": datetime.now(TZ_CN).strftime("%Y-%m-%d"),
    }
    if span:
        ev["span_end"] = span
    return ev


def _events_match(a: dict, b: dict) -> bool:
    if a["month"] != b["month"] or a["day"] != b["day"]:
        return False
    ta, tb = a.get("title", ""), b.get("title", "")
    if ta in tb or tb in ta:
        return True
    keys = ("WAIC", "人工智能大会", "中报", "预告", "LPR", "GDP", "CPI", "具身", "机器人", "互联网大会")
    for kw in keys:
        if kw in ta and kw in tb:
            return True
    return False


def _merge_event(base: dict, incoming: dict) -> dict:
    merged = dict(incoming)
    if base.get("source") == "seed" and base.get("brief"):
        merged["brief"] = base["brief"]
        merged["title"] = base.get("title", merged["title"])
        merged["short"] = base.get("short", merged.get("short"))
        merged["dot"] = base.get("dot", merged.get("dot"))
        merged["label"] = base.get("label", merged.get("label"))
        merged["hot"] = base.get("hot", merged.get("hot"))
        if base.get("span_end"):
            merged["span_end"] = base["span_end"]
        merged["source"] = "seed"
    elif incoming.get("source") == "wscn" and base.get("brief") and len(base["brief"]) > len(incoming.get("brief", "")):
        merged["brief"] = base["brief"]
    merged["importance"] = max(int(base.get("importance") or 0), int(incoming.get("importance") or 0))
    merged["hot"] = bool(base.get("hot") or incoming.get("hot"))
    return merged


def fetch_wscn_events(as_of: datetime, days: int = 14, retries: int = 3) -> list[dict]:
    """联网抓取未来日历。失败自动重试；全部失败则抛错——禁止静默沿用本地事件库。"""
    start = _as_cn_dt(as_of).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days, hours=23, minutes=59, seconds=59)
    params = {"start": int(start.timestamp()), "end": int(end.timestamp())}
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            data = _http_json(WSCN_CAL_URL, params)
            items = (data.get("data") or {}).get("items") or []
            out: list[dict] = []
            seen: set[str] = set()
            for item in items:
                if not _wscn_relevant(item):
                    continue
                ev = _wscn_to_catalog(item)
                if not ev:
                    continue
                key = f"{ev['month']:02d}-{ev['day']:02d}-{ev['title'][:16]}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(ev)
            return out
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError) as exc:
            last_exc = exc
            print(f"事件抓取失败({attempt}/{retries}): {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(
        f"事件日历抓取失败（已重试 {retries} 次），禁止沿用本地事件库，请检查网络后重新生成: {last_exc}"
    )


def save_event_catalog(catalog: list[dict]) -> None:
    """仅写出本次同步结果，不作下次输入源。"""
    payload = []
    for ev in catalog:
        payload.append({k: v for k, v in ev.items() if not str(k).startswith("_")})
    EVENT_CATALOG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_covers_event(seed: dict, ev: dict) -> bool:
    """同日或落在 seed 跨度内的同主题事件，并入 seed，不单列。"""
    if seed.get("source") != "seed":
        return False
    sm, sd = seed["month"], seed["day"]
    em, ed = ev["month"], ev["day"]
    span = seed.get("span_end")
    if span:
        start = (sm, sd)
        end = (int(span[0]), int(span[1]))
        point = (em, ed)
        if not (start <= point <= end):
            return False
    elif sm != em or sd != ed:
        return False
    st, et = seed.get("title", ""), ev.get("title", "")
    keys = (
        "WAIC", "人工智能大会", "中报", "预告", "CPIC", "医药创新",
        "低空", "政治局", "具身", "机器人",
    )
    return any(k in st and k in et for k in keys) or st in et or et in st


def sync_event_catalog(as_of: datetime) -> tuple[list[dict], str]:
    """每次必须联网抓取：seed + 当日抓取；禁止读取/回退 event_catalog.json。"""
    as_of = _as_cn_dt(as_of).replace(tzinfo=None)
    year = as_of.year
    fetched = fetch_wscn_events(as_of)  # 失败会抛错并中断报表生成
    catalog: list[dict] = [dict(ev) for ev in SEED_EVENT_CATALOG]

    def _merge_into(catalog_list: list[dict], inc: dict) -> None:
        for i, cur in enumerate(catalog_list):
            if _events_match(cur, inc) or _seed_covers_event(cur, inc):
                catalog_list[i] = _merge_event(cur, inc)
                return
        catalog_list.append(inc)

    for inc in fetched:
        if _event_is_waic_prep_noise(inc.get("title", "")):
            for i, cur in enumerate(catalog):
                if _seed_covers_event(cur, inc) or (
                    cur.get("source") == "seed" and "WAIC" in cur.get("title", "")
                ):
                    catalog[i]["hot"] = bool(catalog[i].get("hot") or inc.get("hot"))
                    break
            continue
        _merge_into(catalog, inc)

    cutoff = as_of - timedelta(days=3)
    pruned: list[dict] = []
    for ev in catalog:
        if _event_end_dt(ev, year).replace(tzinfo=None) >= cutoff:
            pruned.append(ev)
    pruned.sort(key=lambda e: (e["month"], e["day"], e.get("title", "")))
    pruned = [ev for ev in pruned if _event_is_catalyst(ev.get("title", ""), ev)]
    save_event_catalog(pruned)
    note = f"已同步 {len(fetched)} 条日历线索"
    return pruned, note


def _pick_event_nodes(nodes: list[dict], as_of: datetime, limit: int = 10) -> list[dict]:
    """日期正序；优先保留 seed/hot，再按时间补齐至 limit。"""
    by_date = sorted(nodes, key=lambda ev: (ev["month"], ev["day"]))
    if len(by_date) <= limit:
        return by_date
    must = [n for n in by_date if n.get("hot") or n.get("source") == "seed"]
    must_ids = {id(n) for n in must}
    selected = list(must)
    for n in by_date:
        if len(selected) >= limit:
            break
        if id(n) not in must_ids:
            selected.append(n)
    return sorted(selected, key=lambda ev: (ev["month"], ev["day"]))


STANCE_SORT = {"优先": 0, "重点": 0, "可做": 1, "观察": 2, "回避": 3}


def _stance_tag(stance: str) -> str:
    if stance in ("优先", "重点", "可做"):
        return "ok"
    if stance == "观察":
        return "warn"
    return "bad"


def load_direction_analysis(block: dict | None) -> tuple[list[dict], str, str, str]:
    """读取 market_news.json 中的主观题材研判（非算法打分）。"""
    block = block or {}
    themes_out: list[dict] = []
    for t in block.get("themes") or []:
        stance = t.get("stance", "观察")
        themes_out.append({
            "name": t.get("name", ""),
            "stance": stance,
            "tag": t.get("tag") or _stance_tag(stance),
            "logic": t.get("logic", ""),
            "drivers": t.get("drivers") or [],
        })
    themes_out.sort(key=lambda t: STANCE_SORT.get(t["stance"], 9))
    return (
        themes_out,
        block.get("summary", ""),
        block.get("rhythm", ""),
        block.get("peak", ""),
    )


def build_events_window(as_of: datetime) -> tuple:
    if hasattr(as_of, "to_pydatetime"):
        as_of = as_of.to_pydatetime()
    if getattr(as_of, "tzinfo", None) is not None:
        as_of = as_of.replace(tzinfo=None)
    catalog, sync_note = sync_event_catalog(as_of)
    end = as_of + timedelta(days=14)
    label = f"{as_of.month}/{as_of.day}–{end.month}/{end.day}"
    nodes: list[dict] = []
    for ev in catalog:
        em, ed = ev["month"], ev["day"]
        start = datetime(as_of.year, em, ed)
        span = ev.get("span_end")
        if span:
            ev_end = datetime(as_of.year, int(span[0]), int(span[1]))
        else:
            ev_end = start
        if as_of <= ev_end and start <= end + timedelta(days=7):
            if not _event_is_catalyst(ev.get("title", ""), ev):
                continue
            nodes.append({
                "month": em,
                "day": ed,
                "dot": ev["dot"],
                "label": ev["label"],
                "title": ev["title"],
                "sub": ev.get("short", ev["title"]),
                "brief": ev["brief"],
                "hot": ev.get("hot", False),
                "importance": ev.get("importance", 0),
                "source": ev.get("source", ""),
            })
    nodes = _pick_event_nodes(nodes, as_of)
    if not nodes:
        nodes = [{
            "dot": "—",
            "label": "暂无大节点",
            "title": "关注业绩披露",
            "sub": "关注业绩披露",
            "brief": "未来两周暂无预设大事件，重点跟踪中报预告与行业政策动态。",
            "hot": False,
        }]
    return label, nodes, sync_note


# 板块 → 2周方向表关键词（用于事件/评级匹配）
SECTOR_DIR_KEYS: dict[str, list[str]] = {
    "机器人": ["机器人", "WAIC", "具身"],
    "通用设备": ["机器人", "WAIC", "机床"],
    "化学制品": ["化工", "预增", "出海"],
    "汽车零部件": ["汽车", "智能驾驶", "零部件"],
    "黄金": ["黄金", "避险"],
    "存储": ["存储", "AI硬件"],
    "通信": ["通信", "光通信"],
    "创新药": ["创新药", "CXO", "CPIC"],
    "券商": ["券商", "金融"],
}


def _find_sector_direction(sector_name: str, directions: list) -> tuple[str, str]:
    """返回 (dir_tag, dir_hint)。匹配题材研判中的主题。"""
    keys = SECTOR_DIR_KEYS.get(sector_name, [sector_name])
    for d in directions:
        if isinstance(d, dict):
            dname, tag, hint = d.get("name", ""), d.get("tag", "warn"), d.get("logic", "")
        else:
            dname, _rating, hint, tag = d[0], d[1], d[2], d[3]
        if sector_name in dname.split("/")[0] or dname.startswith(sector_name):
            return tag, hint[:48] if hint else ""
    for d in directions:
        if isinstance(d, dict):
            dname, tag, hint = d.get("name", ""), d.get("tag", "warn"), d.get("logic", "")
        else:
            dname, _rating, hint, tag = d[0], d[1], d[2], d[3]
        if any(k in dname for k in keys):
            return tag, hint[:48] if hint else ""
    return "warn", ""


def _sector_near_events(sector_name: str, as_of: datetime) -> list[str]:
    """近 10 个交易日内与板块相关的事件节点。"""
    events: list[str] = []
    horizon = as_of + timedelta(days=14)
    catalog = [
        (7, 15, "7/15 预告截止", ["化学制品", "化工", "预增", "存储", "通信"]),
        (7, 17, "7/17 WAIC 开幕", ["机器人", "通用设备", "WAIC", "AI"]),
        (7, 22, "7/22 CPIC 开幕", ["创新药", "CXO", "医药"]),
    ]
    keys = SECTOR_DIR_KEYS.get(sector_name, [sector_name])
    for em, ed, label, tags in catalog:
        ev = datetime(as_of.year, em, ed)
        if not (as_of < ev <= horizon):
            continue
        days = (ev - as_of).days
        if any(t in sector_name or any(k in t for k in keys) for t in tags):
            events.append(f"{label}（+{days}天）")
    if sector_name == "黄金" and not events:
        events.append("无固定节点·看金价/避险")
    return events[:2]


def _sector_post_close_boost(sector_name: str, post_close: list) -> tuple[int, str]:
    """盘后利好/澄清对板块持续性评分的修正。"""
    ok_n = warn_n = bad_n = 0
    for it in post_close:
        if not _sector_match(it, [sector_name]):
            continue
        tag = it.get("tag", "warn")
        if tag == "ok":
            ok_n += 1
        elif tag == "bad":
            bad_n += 1
        else:
            warn_n += 1
    score = ok_n * 10 - bad_n * 8 + min(warn_n, 2) * 2
    if ok_n:
        note = f"盘后{ok_n}条利好"
    elif bad_n:
        note = f"澄清/证伪{bad_n}条"
    else:
        note = ""
    return score, note


def forecast_sector_persistence(
    sectors: list,
    *,
    as_of: datetime,
    row: pd.Series,
    df: pd.DataFrame,
    directions: list,
    env_weak: bool,
    post_close: list,
) -> list[dict]:
    """结合涨停广度 + 近端事件 + 环境，预判板块炒作持续性。

    自报表次一交易日起算：
    - 不认可持续：不看好次日进一步走强
    - 预估持续2天：看好次日延续，预计还能走 2 个交易日（含次日）
    - 预估持续3天及以上：看好多日延续，可沿主线缩圈跟龙头
    """
    if not sectors:
        return []
    vm = volume_metrics(row, df)
    md = float(row.get("主赚差") or 0)
    zt_total = int(row.get("涨停") or 0) or sum(int(s.get("count") or 0) for s in sectors)
    top5_sum = sum(int(s.get("count") or 0) for s in sectors)
    weekday = as_of.weekday()  # 4=周五

    enriched = []
    for s in sectors:
        name = s.get("name", "")
        count = int(s.get("count") or 0)

        # ── 热度基准（仅用于持续性打分，不单独展示）──
        if count >= 15:
            heat_score = 82
        elif count >= 8:
            heat_score = 62
        else:
            heat_score = 42

        share = count / top5_sum if top5_sum else 0
        if share >= 0.32:
            heat_score += 6
        if zt_total and count / zt_total >= 0.12:
            heat_score += 5

        if vm["shrink_streak"] >= 3:
            heat_score -= 14
        elif vm["shrink_streak"] >= 2:
            heat_score -= 8
        if vm["ratio2"] < 0.93:
            heat_score -= 6
        if count >= 12 and md < -0.05:
            heat_score -= 16  # 高潮但承接弱
        elif count >= 8 and md > 0.02:
            heat_score += 8

        pc_score, _pc_note = _sector_post_close_boost(name, post_close)
        heat_score += pc_score
        heat_score = max(20, min(95, heat_score))

        # ── 近端事件 + 方向评级 ──
        events = _sector_near_events(name, as_of)
        dir_tag, dir_hint = _find_sector_direction(name, directions)
        events_near = "；".join(events) if events else "暂无直接催化"

        # ── 持续性得分 ──
        persist_score = heat_score
        if dir_tag == "ok":
            persist_score += 18
        elif dir_tag == "bad":
            persist_score -= 22
        if events and "7/15" in events[0]:
            persist_score += 10
        if events and any("WAIC" in e for e in events):
            persist_score += 6 if dir_tag != "bad" else -4
        if env_weak and count >= 14:
            persist_score -= 18
        if weekday == 4 and count >= 10:
            persist_score -= 10  # 周五高潮
        if s.get("reason") and any(k in s["reason"] for k in ("IPO", "收购", "注册生效")):
            persist_score += 4

        if persist_score >= 72:
            persist_label, persist_tag = "预估持续3天及以上", "ok"
            persist_note = "看好次日延续，题材预计还能走3个交易日及以上，可沿主线缩圈跟龙头"
        elif persist_score >= 52:
            persist_label, persist_tag = "预估持续2天", "warn"
            persist_note = "看好次日延续，题材预计还能走2个交易日（含次日），分化中只做核心"
        else:
            persist_label, persist_tag = "不认可持续", "weak"
            persist_note = "不看好次日进一步走强，谨防一日游或快速退潮"

        reason_blob = s.get("reason") or ""
        has_hard_catalyst = any(k in reason_blob for k in ("IPO", "收购", "注册生效", "订单", "预增"))

        if name == "机器人" and env_weak and count >= 15:
            persist_label, persist_tag = "预估持续2天", "warn"
            persist_note = "宇树IPO已定价+埃斯顿收购，逻辑在但环境弱，次日或有惯性、随后看分化"
        elif name == "化学制品":
            persist_note = "7/15预告窗口支撑，低位预增可轮动，追高慎"
        elif name == "黄金":
            persist_note = "避险+金价驱动，偏独立节奏，看期货不追板"
        elif name in ("通用设备", "汽车零部件") and count < 10:
            if persist_tag == "ok":
                persist_label, persist_tag = "预估持续2天", "warn"
            persist_note = "主线扩散补涨，弹性弱于龙头，跟随不领涨"
        elif has_hard_catalyst and pc_score > 0 and persist_tag == "weak":
            persist_label, persist_tag = "预估持续2天", "warn"
            persist_note = "有硬催化但环境/量能拖累，次日或有惯性，缩圈做核心"

        enriched.append({
            **s,
            "events_near": events_near,
            "dir_hint": dir_hint or "",
            "persist": persist_label,
            "persist_tag": persist_tag,
            "persist_note": persist_note,
        })
    return enriched


def analyze_trading_modes(row: pd.Series, df: pd.DataFrame, emo: int, dim: dict = None) -> dict:
    """判断 5/10日线试单 & 新高加仓；模式得分/承接得分均为 0–100。"""
    md = row.get("主赚差", 0) or 0
    cd = row.get("创赚差", 0) or 0
    zj = int(row.get("主追高数") or 0)
    xg = row.get("新高", 0) or 0
    vm = volume_metrics(row, df)
    hist = hist_tail(df)
    zj_med = float(hist["主追高数"].median()) if len(hist) else zj
    main_close = (dim or {}).get("main_close") or chase_close_quality(row, df, "main")
    chuang_close = (dim or {}).get("chuang_close") or chase_close_quality(row, df, "chuang")

    recent5 = df.tail(5)
    prior5 = df.iloc[-10:-5] if len(df) >= 10 else recent5
    xg5 = recent5["新高"].mean() if len(recent5) else xg
    xg5p = prior5["新高"].mean() if len(prior5) else xg5
    xg_expanding = xg5 > xg5p * 1.1 and xg >= 60

    carry_reasons, trend_reasons = [], []

    # ── 承接得分：昨追赚钱效应 + 活跃度背离 + 今追冲高未回落 ──
    carry_100 = 0
    if md > 0.03:
        carry_100 += 32
        carry_reasons.append(f"主板昨追赚钱效应{fmt_pct(md)}，效应强")
    elif md > -0.05:
        carry_100 += 18
        carry_reasons.append(f"主板昨追赚钱效应{fmt_pct(md)}，效应一般")
    elif md > -0.08:
        carry_100 += 8
        carry_reasons.append(f"主板昨追赚钱效应{fmt_pct(md)}，效应偏弱")
    else:
        carry_reasons.append(f"主板昨追赚钱效应{fmt_pct(md)}，效应枯竭")

    carry_100 += max(0, min(12, int(12 + cd * 45)))
    if cd < -0.12:
        carry_reasons.append(f"创板昨追赚钱效应{fmt_pct(cd)}，20cm效应更差")

    if md < -0.05 and zj > zj_med:
        pen = min(14, int(6 + (zj / max(zj_med, 1) - 1) * 12))
        carry_100 -= pen
        carry_reasons.append(f"主追{zj}家仍高，背离赚钱效应")

    close_blend = main_close["score"] * 0.6 + chuang_close["score"] * 0.4
    carry_100 += int((close_blend - 50) / 50 * 12)
    mr, cr = main_close.get("ratio"), chuang_close.get("ratio")
    if mr is not None and mr <= 0.35:
        carry_100 -= 6
        carry_reasons.append(f"主板今追效应偏弱，冲高未回落{fmt_ratio(mr)}")
    elif mr is not None and mr >= 0.55:
        carry_reasons.append(f"主板今追效应尚可，冲高未回落{fmt_ratio(mr)}")
    if cr is not None and cr <= 0.35:
        carry_100 -= 4
        carry_reasons.append(f"创板今追效应偏弱，冲高未回落{fmt_ratio(cr)}")

    carry_100 += int(min(vm["ratio5"] / 1.05, 1) * 18)
    if vm["ratio5"] < 0.93:
        carry_reasons.append(f"成交额较前2日均缩量{(1 - vm['ratio5']):.0%}")
    if vm["shrink_streak"] >= 3:
        carry_reasons.append(f"连续{vm['shrink_streak']}日缩量，承接环境差")
    carry_100 += int(min(xg / 120, 1) * 15)
    if xg < 40:
        carry_reasons.append(f"新高{int(xg)}家，深度不足")
    carry_100 = clamp100(carry_100)

    # ── 模式得分 0–100：5/10日线+新高加仓结构是否成立 ──
    trend_100 = 0
    if xg_expanding:
        trend_100 += 30
        trend_reasons.append(f"新高扩张（5日均{int(xg5)}）")
    else:
        trend_100 += int(min(xg / 120, 1) * 12)
        if xg < 40:
            trend_reasons.append(f"新高仅{int(xg)}家")
    if md > 0.03:
        trend_100 += 25
        trend_reasons.append(f"主板昨追赚钱效应{fmt_pct(md)}，试单胜率高")
    elif md > -0.05:
        trend_100 += 15
    elif md > -0.08:
        trend_100 += 5
    if vm["ratio5"] >= 0.98:
        trend_100 += 12
    elif vm["ratio5"] >= 0.93:
        trend_100 += 6
    elif vm["ratio5"] < 0.93:
        trend_reasons.append("市场缩量，趋势难续航")
    if vm["shrink_streak"] >= 3:
        trend_100 -= 12
        trend_reasons.append(f"连续{vm['shrink_streak']}日缩量，危险信号")
    if emo >= 50:
        trend_100 += 15
    elif emo >= 35:
        trend_100 += 8
    if emo < 35 or md < -0.08:
        trend_100 -= 25
        trend_reasons.append("情绪弱或昨追赚钱效应偏弱")
    if xg < 40:
        trend_100 -= 20
    trend_100 = clamp100(trend_100)

    carry_ok = carry_100 >= 55
    TREND_TRY, TREND_FULL = 40, 60

    if trend_100 >= TREND_FULL and carry_ok:
        status, pill = "主用", "ok"
        pos = "试单1成，新高确认后加至2–3成"
        entry = "趋势确立后，回踩5日/10日线缩量试单"
        add = "个股/板块放量创新高且主板昨追赚钱效应≥0时加仓"
        stop = "破10日线或趋势结构坏则减/清"
        primary_label = "5/10日线试单 & 新高加仓"
        summary = "模式与承接均达标，可按策略试单并新高加仓。"
    elif trend_100 >= TREND_TRY:
        status, pill = "仅试单", "warn"
        pos = "≤1成试单，不加仓"
        entry = "仅最强主线龙头，回踩5日线小仓试"
        add = "新高暂不加仓，等新高家数回升"
        stop = "破5日线走"
        primary_label = "5/10日线试单（极小仓）"
        summary = "模式分达标但承接一般，极小仓试单，不加仓。"
    else:
        status, pill = "空仓", "bad"
        pos = entry = add = stop = "不做"
        primary_label = "空仓观望"
        summary = "模式或承接不足，空仓等待昨追赚钱效应修复、新高回升。"
        trend_reasons = (trend_reasons + carry_reasons)[:4]

    return {
        "summary": summary,
        "primary_label": primary_label,
        "active": trend_100 >= TREND_TRY,
        "status": status,
        "pill": pill,
        "pos": pos,
        "entry": entry,
        "add": add,
        "stop": stop,
        "reasons": (trend_reasons + carry_reasons if status == "空仓" else trend_reasons)[:4] or ["暂无明确信号"],
        "carry_score": carry_100,
        "trend_score": trend_100,
        "carry_ok": carry_ok,
        "threshold_try": TREND_TRY,
        "threshold_full": TREND_FULL,
    }


def opening_advice(row: pd.Series, emo: int, df: pd.DataFrame, nxt: datetime, modes: dict) -> dict:
    vm = volume_metrics(row, df)
    vol_chg = vm["inc"] if pd.notna(vm["inc"]) else 0.0
    if vol_chg == 0 and len(df) >= 2 and pd.notna(row.get("成交额")) and pd.notna(df.iloc[-2].get("成交额")):
        vol_chg = row["成交额"] / df.iloc[-2]["成交额"] - 1
    emo3 = emo - emotion_index(df.iloc[-4]) if len(df) >= 4 else 0
    xg = row.get("新高", 0) or 0

    alerts = []
    if emo < 35:
        alerts.append(f"情绪{emo}分（弱）")
    if emo3 <= -20:
        alerts.append(f"3日急降{abs(emo3)}分")
    if xg < 40:
        alerts.append(f"新高{int(xg)}家（趋势强度不足）")
    if vm["shrink_streak"] >= 3:
        alerts.append(f"连续{vm['shrink_streak']}日缩量（危险）")
    elif vol_chg < -0.05:
        alerts.append(f"成交额缩量{abs(vol_chg):.1%}")

    return {
        "modes": modes,
        "alerts": "｜".join(alerts) if alerts else "暂无极端信号",
        "nxt_label": f"{fmt_md(nxt)} 周{WEEKDAY[nxt.weekday()]}",
    }


def compute_six_dim(row: pd.Series, df: pd.DataFrame) -> dict:
    """计算环境分数：活跃度、昨追赚钱效应、今追冲高未回落等。"""
    hist = hist_tail(df)
    ha = hybrid_hist_score(hist["主追高数"], row.get("主追高数", 0))
    hca = hybrid_hist_score(hist["创活跃"], row.get("创活跃", 0))
    hm = hybrid_hist_score(hist["主赚差"], row.get("主赚差", 0))
    hcm = hybrid_hist_score(hist["创赚差"], row.get("创赚差", 0))
    main_act, chuang_act = ha["score"], hca["score"]
    main_money, chuang_money = hm["score"], hcm["score"]
    main_close = chase_close_quality(row, df, "main")
    chuang_close = chase_close_quality(row, df, "chuang")
    xg = safe_int(row.get("新高"))
    vm = volume_metrics(row, df)
    hd = hybrid_hist_score(hist["新高"], row.get("新高", 0))
    depth = hd["score"]
    depth_tag = score_badge(depth)[1]
    depth_note = hybrid_note(
        hd, f"当日新高{xg}家", f"均值{int(round(hd['mean']))}家"
    )
    recent5_xg = df.tail(5)["新高"].mean()
    prior5_xg = df.iloc[-10:-5]["新高"].mean() if len(df) >= 10 else recent5_xg
    if recent5_xg < prior5_xg * 0.7:
        depth_note += "；近5日新高收缩"
    elif recent5_xg > prior5_xg * 1.15:
        depth_note += "；近5日新高扩张"
    vol_score = vm["score"]
    vol_note = vm["note"]
    if vol_score < 40:
        vol_note += "，资金在场偏弱"
    return {
        "vol_score": vol_score,
        "depth": depth,
        "main_act": main_act,
        "chuang_act": chuang_act,
        "main_money": main_money,
        "chuang_money": chuang_money,
        "main_act_note": hybrid_note(
            ha, f"当日主追{int(row.get('主追高数') or 0)}家", f"均值{int(round(ha['mean']))}家"
        ),
        "chuang_act_note": hybrid_note(
            hca, f"当日创活跃{int(row.get('创活跃') or 0)}", f"均值{int(round(hca['mean']))}"
        ),
        "main_money_note": hybrid_note(
            hm, f"赚差{fmt_pct(row.get('主赚差'))}", f"均值{fmt_pct(hm['mean'])}"
        ),
        "chuang_money_note": hybrid_note(
            hcm, f"赚差{fmt_pct(row.get('创赚差'))}", f"均值{fmt_pct(hcm['mean'])}"
        ),
        "main_close": main_close,
        "chuang_close": chuang_close,
        "vm": vm,
        "depth_tag": depth_tag,
        "depth_note": depth_note,
        "vol_note": vol_note,
    }


def _parse_cal_sort_key(label: str) -> tuple[int, int]:
    m = re.search(r"(\d{1,2})[/月](\d{1,2})", label or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return 99, 99


def _peak_tags_for_label(label: str, peak: str) -> list[str]:
    """从 peak 文案为日历节点匹配标注（催化峰值 / 情绪高点）。"""
    if not peak or not label:
        return []
    tags: list[str] = []
    lbl = label.replace("–", "-").replace("—", "-")
    title_blob = lbl
    if ("催化" in peak or "轮动" in peak) and re.search(r"7[-/]1[4-8]", lbl):
        tags.append("催化峰值")
    if ("WAIC" in peak or "情绪高点" in peak) and (
        "17" in lbl or "18" in lbl or "19" in lbl or "20" in lbl
    ):
        tags.append("情绪高点")
    return tags


def _extra_milestones_from_rhythm(rhythm: str) -> list[dict]:
    """节奏里有关键节点但事件库未收录时，补入日历。"""
    extras: list[dict] = []
    if rhythm and "长鑫" in rhythm:
        extras.append({
            "label": "7/16",
            "title": "长鑫科技 IPO 申购",
            "short": "长鑫IPO",
            "brief": "存储链事件性兑现窗口，申购日前后波动加大。",
            "hot": True,
            "dot": "存",
        })
    return extras


def _filter_summary_points(summary: str, rhythm: str) -> list[str]:
    """去掉与日历重复的 summary 要点。"""
    points = [s.strip() for s in re.split(r"[。！？]", summary or "") if s.strip()]
    out: list[str] = []
    for p in points:
        if p.count("→") >= 2:
            continue
        if "→" in p and rhythm:
            arrow_parts = [part.strip() for part in p.split("→")]
            if all(part in rhythm for part in arrow_parts if part):
                continue
        if p.startswith("未来2周日历") or p.startswith("未来两周日历"):
            continue
        out.append(p)
    return out


def _parse_summary_sections(summary: str, rhythm: str) -> tuple[str, str]:
    """拆成 (2周主线, 当日背景)。"""
    s = (summary or "").strip()
    for sep in ("当日背景：", "今日背景：", "当日背景:", "今日背景:"):
        if sep in s:
            left, right = s.split(sep, 1)
            outlook = left.strip().rstrip("。；;")
            today = right.strip().rstrip("。；;")
            for prefix in ("2周主线：", "未来2周主线：", "未来2周：", "未来两周："):
                if outlook.startswith(prefix):
                    outlook = outlook[len(prefix) :].strip()
            return outlook, today
    points = _filter_summary_points(s, rhythm)
    if len(points) >= 2 and any(
        k in points[-1] for k in ("普跌", "大涨", "当日", "今日", "不改变上述")
    ):
        return "。".join(points[:-1]), points[-1]
    if points:
        return "。".join(points), ""
    return s, ""


def _render_fwd_lead_html(peak: str, summary: str, rhythm: str) -> str:
    """研判摘要：节奏峰值 + 2周主线 + 当日背景（置于模块末尾）。"""
    outlook, today_note = _parse_summary_sections(summary, rhythm)
    rows: list[str] = []
    if peak:
        peak_line = re.sub(r"[;；]", " · ", peak)
        rows.append(
            '<div class="fwd-lead-row">'
            '<span class="fwd-lead-tag peak">节奏峰值</span>'
            f'<p class="fwd-lead-text peak">{peak_line}</p></div>'
        )
    if outlook:
        rows.append(
            '<div class="fwd-lead-row">'
            '<span class="fwd-lead-tag">2周主线</span>'
            f'<p class="fwd-lead-text">{outlook}</p></div>'
        )
    if today_note:
        rows.append(
            '<div class="fwd-lead-row">'
            '<span class="fwd-lead-tag muted">当日背景</span>'
            f'<p class="fwd-lead-text muted">{today_note}</p></div>'
        )
    if not rows:
        return ""
    return (
        '<div class="fwd-lead-wrap">'
        '<div class="fwd-block-label">研判摘要 '
        '<span class="fwd-block-hint">未来2周总览</span></div>'
        f'<div class="fwd-lead">{"".join(rows)}</div></div>'
    )


def _timeline_nodes_from_cal(cal_items: list[dict], peak: str) -> list[dict]:
    """按日期合并节点，供横向时间轴展示（每日期一个点）。"""
    by_label: dict[str, list[dict]] = {}
    for n in cal_items:
        label = n.get("label", "")
        by_label.setdefault(label, []).append(n)
    nodes: list[dict] = []
    for label in sorted(by_label.keys(), key=_parse_cal_sort_key):
        items = by_label[label]
        primary = sorted(
            items,
            key=lambda x: (
                not x.get("hot"),
                len(x.get("short") or x.get("title") or ""),
            ),
        )[0]
        shorts = []
        for it in items:
            s = it.get("short") or it.get("title", "")
            if s and s not in shorts:
                shorts.append(s)
        if len(shorts) == 1:
            sub = shorts[0]
        elif len(shorts) == 2:
            sub = f"{shorts[0]}·{shorts[1]}"
        else:
            sub = f"{shorts[0]}+{len(shorts) - 1}"
        dot = primary.get("dot") or re.sub(r"[^\dA-Za-z\u4e00-\u9fff]", "", label)[:2] or "·"
        hot = any(it.get("hot") for it in items) or bool(_peak_tags_for_label(label, peak))
        nodes.append({
            "label": label,
            "sub": sub,
            "dot": dot,
            "hot": hot,
        })
    return nodes


def _render_fwd_timeline_html(nodes: list[dict]) -> str:
    if not nodes:
        return ""
    cells = []
    for n in nodes:
        hot = " hot" if n.get("hot") else ""
        cells.append(
            f'<div class="fwd-tnode{hot}">'
            f'<div class="fwd-tdot{hot}">{n.get("dot", "·")}</div>'
            f'<div class="fwd-tlabel">{n.get("label", "")}</div>'
            f'<div class="fwd-tsub">{n.get("sub", "")}</div>'
            f"</div>"
        )
    return (
        '<div class="fwd-track-wrap">'
        '<div class="fwd-track-scroll"><div class="fwd-track">'
        + "".join(cells)
        + "</div></div></div>"
    )


def render_fwd_section_html(
    event_nodes: list,
    peak: str,
    summary: str,
    rhythm: str,
    directions: list,
    as_of: datetime | None = None,
) -> tuple[str, str]:
    """未来2周模块：时间轴 → 事件详情 → 题材卡片 → 研判摘要。"""
    lead_html = _render_fwd_lead_html(peak, summary, rhythm)

    # ── 融合日历（事件库 + 节奏补点；跳过已过期节点）──
    cal_items: list[dict] = []
    seen: set[str] = set()
    cutoff = None
    if as_of is not None:
        cutoff = as_of.date() if hasattr(as_of, "date") else as_of
    for n in event_nodes or []:
        if cutoff:
            em, ed = _parse_cal_sort_key(n.get("label", ""))
            if 1 <= em <= 12 and 1 <= ed <= 31:
                if datetime(as_of.year if as_of else 2026, em, ed).date() < cutoff:
                    continue
        key = f"{n.get('label', '')}|{n.get('title', '')}"
        if key in seen:
            continue
        seen.add(key)
        cal_items.append(n)
    for ex in _extra_milestones_from_rhythm(rhythm):
        key = f"{ex.get('label', '')}|{ex.get('title', '')}"
        if key not in seen:
            seen.add(key)
            cal_items.append(ex)
    cal_items.sort(key=lambda x: _parse_cal_sort_key(x.get("label", "")))

    timeline_html = _render_fwd_timeline_html(
        _timeline_nodes_from_cal(cal_items, peak)
    )

    cal_rows: list[str] = []
    for n in cal_items:
        label = n.get("label", "")
        title = n.get("title", n.get("short", ""))
        note = n.get("brief", "")
        if len(note) > 88:
            note = note[:85].rstrip() + "…"
        tags = _peak_tags_for_label(label, peak)
        tag_html = "".join(f'<span class="fwd-cal-badge">{t}</span>' for t in tags)
        hot = n.get("hot") or bool(tags)
        cal_rows.append(
            f'<div class="fwd-cal-item{" hot" if hot else ""}">'
            f'<div class="fwd-cal-date">{label}</div>'
            f'<div class="fwd-cal-body">'
            f'<div class="fwd-cal-top">'
            f'<span class="fwd-cal-title">{title}</span>'
            f'{tag_html}</div>'
            f'<p class="fwd-cal-note">{note}</p>'
            f"</div></div>"
        )
    cal_html = (
        f'<div class="fwd-cal-wrap">'
        f'<div class="fwd-block-label">事件详情</div>'
        f'<div class="fwd-cal">{"".join(cal_rows)}</div></div>'
        if cal_rows else ""
    )

    # ── 题材方向 ──
    dir_card_parts: list[str] = []
    for t in directions:
        drivers = t.get("drivers") or []
        drivers_html = (
            '<div class="dir-drivers">'
            + "".join(f'<span class="dir-driver">{d}</span>' for d in drivers)
            + "</div>"
            if drivers else ""
        )
        dir_card_parts.append(
            f'<div class="dir-card {t["tag"]}">'
            f'<div class="dir-card-top"><span class="dir-name">{t["name"]}</span>'
            f'<span class="pill {t["tag"]}">{t["stance"]}</span></div>'
            f"{drivers_html}"
            f'<div class="dir-logic">{t.get("logic", "")}</div></div>'
        )
    themes_html = (
        f'<div class="fwd-themes-wrap">'
        f'<div class="fwd-block-label">题材方向 <span class="fwd-block-hint">主观判断 · 非算法加权</span></div>'
        f'<div class="dir-grid">{"".join(dir_card_parts)}</div></div>'
        if dir_card_parts else
        '<div class="news-empty">暂无题材研判。请在 market_news.json 写入 direction_analysis。</div>'
    )

    body = f"{timeline_html}{cal_html}{themes_html}{lead_html}"
    return body, lead_html


def render_direction_overview_html(peak: str, summary: str, rhythm: str) -> str:
    """已并入 render_fwd_section_html，保留兼容。"""
    return ""


def render_html(ctx: dict) -> str:
    def score_card(s: tuple) -> str:
        n, sc, t, note = s
        return f'''<div class="score-card">
      <div class="score-card-top">
        <span class="score-name">{n}</span>
        <span class="score-val" style="color:{score_bar(sc)}">{sc}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:{sc}%;background:{score_bar(sc)}"></div></div>
      <div class="score-foot"><span class="pill {score_badge(sc)[0]}">{t}</span><span>{note}</span></div>
    </div>'''

    scores_html = (
        f'<div class="score-grid score-grid-8">'
        f'{"".join(score_card(s) for s in ctx["scores"])}'
        f"</div>"
    )

    def trend_score_cell(key: str, d: dict) -> str:
        val = d[key]
        tag = d[f"{key}_tag"]
        return f'<td class="trend-cell {tag}">{val}</td>'

    trend_cols = (
        ("vol", "大盘量能"),
        ("depth", "趋势强度"),
        ("main_act", "主板活跃"),
        ("chuang_act", "创板活跃"),
        ("main_chase", "主追效应"),
        ("chuang_chase", "创追效应"),
    )
    trend_days = ctx["trend_days"]
    trend_head_cells = "".join(f"<th>{label}</th>" for _, label in trend_cols)
    trend_rows = "".join(
        f'''<tr class="{"trend-row-latest" if d["is_latest"] else ""}">
      <th class="trend-date-cell">
        <span class="trend-date-md">{d["date"]}</span>
        <span class="trend-date-wd">周{d["weekday"]}</span>
      </th>
      {"".join(trend_score_cell(k, d) for k, _ in trend_cols)}
    </tr>'''
        for d in trend_days
    )
    trend_html = f'''<div class="trend-matrix-wrap">
    <table class="trend-matrix">
      <thead>
        <tr>
          <th class="trend-corner">日期</th>
          {trend_head_cells}
        </tr>
      </thead>
      <tbody>{trend_rows}</tbody>
    </table>
  </div>'''
    def post_close_html(items: list) -> str:
        if not items:
            return '<div class="news-empty">暂无盘后披露</div>'
        return (
            '<div class="post-list">'
            + "".join(
                f'''<div class="post-row {it.get("tag", "warn")}">
      <div class="post-type">{it.get("type", "公告")}</div>
      <div class="post-title">{it.get("title", "")}</div>
      <p class="post-summary">{it.get("content", it.get("text", ""))}</p>
    </div>'''
                for it in items
            )
            + "</div>"
        )

    news = ctx["market_news"]
    sectors = news["top_sectors"]
    if sectors:
        sector_cards = "".join(
            f'''<div class="sector-row">
      <div class="sector-head">
        <div class="sector-head-left">
          <span class="sector-rank">{i}</span>
          <span class="sector-name">{s.get("name", "")}</span>
          <span class="sector-stat hot">{s.get("count", "—")} 涨停</span>
          <span class="sector-stat{" hot" if int(s.get("streak_days") or 0) >= 3 else ""}">已持续 {int(s.get("streak_days") or 0)} 天</span>
        </div>
        <div class="sector-forecast-corner">
          <span class="pill {s.get("persist_tag", "warn")} sector-persist-pill">{s.get("persist", "—")}</span>
        </div>
      </div>
      <div class="sector-cols">
        <div class="sector-col-left">
          {f'<div class="sector-block"><div class="sector-label sector-label-logic">逻辑</div><div class="sector-text">{s.get("reason", "")}</div></div>' if s.get("reason") else '<div class="sector-block sector-block-empty"><div class="sector-text sector-muted">—</div></div>'}
        </div>
        <div class="sector-col-right">
          <div class="sector-block"><div class="sector-label sector-label-event">后续事件</div><div class="sector-text">{s.get("events_near", "—")}{("｜" + s.get("dir_hint")) if s.get("dir_hint") and s.get("dir_hint") not in (s.get("events_near") or "") else ""}</div></div>
          <div class="sector-block"><div class="sector-label sector-label-forecast">预判</div><div class="sector-text">{s.get("persist_note", "")}</div></div>
        </div>
      </div>
    </div>'''
            for i, s in enumerate(sectors, 1)
        )
        sector_block = f'<div class="sector-list">{sector_cards}</div>'
    else:
        sector_block = '<div class="news-empty">暂无板块数据，请在 market_news.json 填写 top_sectors</div>'
    sector_summary = news.get("sector_summary", "")
    post_summary = news.get("post_summary", "")
    post_block = post_close_html(news["post_close"])

    fwd_section_html, _ = render_fwd_section_html(
        ctx.get("event_nodes") or [],
        ctx.get("event_peak") or "",
        ctx.get("dir_summary") or "",
        ctx.get("rhythm") or "",
        ctx.get("directions") or [],
        ctx.get("as_of"),
    )

    m = ctx["modes"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="复">
<meta name="theme-color" content="#f5f5f7">
<title>复盘报告 · {ctx['title_date']}</title>
<style>
  :root {{
    --bg: #f5f5f7;
    --surface: #ffffff;
    --text: #1c1c1e;
    --sub: #636366;
    --muted: #8e8e93;
    --ok: #30d158;
    --ok-bg: #30d15812;
    --warn: #ff9f0a;
    --warn-bg: #ff9f0a12;
    --bad: #ff453a;
    --bad-bg: #ff453a12;
    --accent: #0a84ff;
    --accent-bg: #0a84ff0d;
    --bar-ok: #E53935;
    --bar-warn: #ff9f0a;
    --bar-bad: #34C759;
    --border: rgba(0,0,0,.06);
    --shadow: 0 1px 2px rgba(0,0,0,.03), 0 4px 16px rgba(0,0,0,.04);
    --radius: 14px;
    --radius-sm: 10px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 12px 12px 40px;
    -webkit-font-smoothing: antialiased;
  }}
  .page {{ max-width: 1120px; margin: 0 auto; }}

  /* ── 页头 Hero ── */
  .hero {{
    background: var(--surface);
    border-radius: var(--radius);
    padding: 14px 16px 12px;
    margin-bottom: 10px;
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
  }}
  .hero-top {{
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; flex-wrap: wrap;
  }}
  .hero h1 {{ font-size: 22px; font-weight: 800; letter-spacing: -0.4px; }}
  .hero-meta {{ font-size: 12px; color: var(--sub); margin-top: 4px; }}
  .hero-tag {{
    display: inline-block; margin-top: 6px; font-size: 11px; font-weight: 600;
    color: var(--accent); background: var(--accent-bg); padding: 4px 10px; border-radius: 6px;
  }}
  .hero-decision {{
    text-align: right; padding: 12px 18px; border-radius: var(--radius-sm);
    border: 1px solid var(--border); min-width: 140px;
  }}
  .hero-decision.ok {{ background: linear-gradient(135deg, #f0fdf4, #fff); border-color: #30d15840; }}
  .hero-decision.warn {{ background: linear-gradient(135deg, #fffbf0, #fff); border-color: #ff9f0a40; }}
  .hero-decision.bad {{ background: linear-gradient(135deg, #fff5f5, #fff); border-color: #ff453a40; }}
  .hero-status {{
    display: block; font-size: 10px; font-weight: 700; letter-spacing: .6px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 4px;
  }}
  .hero-mode {{ font-size: 20px; font-weight: 800; letter-spacing: -0.3px; }}
  .hero-decision.ok .hero-mode {{ color: #1a7a34; }}
  .hero-decision.warn .hero-mode {{ color: #b25000; }}
  .hero-decision.bad .hero-mode {{ color: #c41e16; }}
  .hero-summary {{
    margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border);
    font-size: 13px; color: var(--sub); line-height: 1.55;
  }}

  .page-nav {{
    display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px;
  }}
  .page-nav a {{
    font-size: 11px; font-weight: 600; color: var(--sub); text-decoration: none;
    padding: 4px 10px; border-radius: 999px; background: #fff; border: 1px solid var(--border);
    transition: background .15s, color .15s;
  }}
  .page-nav a:hover {{ background: var(--accent-bg); color: var(--accent); border-color: #0a84ff30; }}

  /* ── 区块 ── */
  .section {{
    background: var(--surface);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 10px;
    box-shadow: var(--shadow);
    border: 1px solid var(--border);
  }}
  .section-head {{
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
  }}
  .section-num {{
    width: 22px; height: 22px; border-radius: 6px; background: var(--accent); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; flex-shrink: 0;
  }}
  .section-title {{ font-size: 15px; font-weight: 700; }}
  .section-sub {{ font-size: 12px; color: var(--muted); margin-left: auto; white-space: nowrap; }}
  .section-sub.event-meta {{
    display: flex; flex-direction: column; align-items: flex-end; gap: 2px;
    white-space: normal; text-align: right; line-height: 1.45; max-width: 52%;
  }}
  .section-sub.event-meta .event-sync-note {{ font-size: 11px; color: var(--sub); }}

  .sub-block {{ margin-top: 4px; }}
  .sub-block + .sub-block {{ margin-top: 20px; padding-top: 18px; border-top: 1px dashed var(--border); }}
  .sub-block-head {{
    font-size: 12px; font-weight: 700; color: var(--sub); letter-spacing: .2px;
    margin-bottom: 12px; display: flex; align-items: center; gap: 8px;
  }}
  .sub-block-head::before {{
    content: ''; width: 3px; height: 14px; background: var(--accent); border-radius: 2px;
  }}
  .sub-note {{ font-size: 11px; font-weight: 500; color: var(--muted); }}

  .callout {{
    background: var(--accent-bg); border-left: 3px solid var(--accent);
    padding: 8px 12px; border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    font-size: 12px; line-height: 1.55; margin-top: 10px;
  }}
  .callout.warn {{ background: var(--warn-bg); border-color: var(--warn); }}
  .callout.bad {{ background: var(--bad-bg); border-color: var(--bad); }}
  .callout-row {{ display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }}

  /* ── 八维 ── */
  .score-groups {{ display: flex; flex-direction: column; gap: 16px; }}
  .score-grid-8 {{ grid-template-columns: repeat(4, 1fr); }}
  @media (max-width: 900px) {{ .score-grid-8 {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 520px) {{ .score-grid-8 {{ grid-template-columns: 1fr; }} }}
  .score-group-label {{
    font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase;
    letter-spacing: .5px; margin-bottom: 4px;
  }}
  .score-group-hint {{
    font-size: 11px; color: var(--muted); line-height: 1.45; margin-bottom: 8px;
  }}
  .score-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  @media (max-width: 520px) {{ .score-grid {{ grid-template-columns: 1fr; }} }}
  .score-card {{
    background: #fff; border-radius: var(--radius-sm); padding: 10px 11px;
    border: 1px solid var(--border);
  }}
  .score-card-top {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }}
  .score-name {{ font-size: 11px; font-weight: 600; color: var(--sub); }}
  .score-val {{ font-size: 20px; font-weight: 800; letter-spacing: -0.5px; }}
  .bar-track {{ height: 4px; background: #e5e5ea; border-radius: 99px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 99px; }}
  .score-foot {{
    display: flex; align-items: center; gap: 5px; margin-top: 6px;
    font-size: 10px; color: var(--muted); flex-wrap: wrap; line-height: 1.45;
  }}

  /* ── 10日趋势表格（日期竖轴） ── */
  .trend-matrix-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  .trend-matrix {{
    width: 90%; border-collapse: collapse; font-size: 12px; min-width: 468px;
  }}
  .trend-matrix th, .trend-matrix td {{
    border: 1px solid var(--border); padding: 8px 10px; text-align: center;
  }}
  .trend-matrix thead th {{
    font-size: 12px; font-weight: 800; color: var(--text);
    background: #ececf1; border-bottom: 2px solid #c8c8d0;
    padding: 10px 10px; letter-spacing: 0.02em; white-space: nowrap;
  }}
  .trend-corner {{
    text-align: left; color: var(--text);
  }}
  .trend-date-cell {{
    text-align: left; white-space: nowrap; background: #f7f7fa;
    font-weight: 600; color: var(--sub);
  }}
  .trend-date-md {{ display: block; font-weight: 700; color: var(--text); line-height: 1.2; }}
  .trend-date-wd {{ display: block; font-size: 10px; color: var(--muted); margin-top: 1px; }}
  .trend-row-latest {{ background: #fafbff; }}
  .trend-row-latest .trend-date-md {{ color: var(--accent); }}
  .trend-cell {{
    font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums;
  }}
  .trend-cell.ok {{ color: #c62828; }}
  .trend-cell.warn {{ color: #e65100; }}
  .trend-cell.bad {{ color: #2e7d32; }}

  /* ── 涨停板块 ── */
  .sector-list {{ display: flex; flex-direction: column; gap: 10px; }}
  .sector-row {{
    position: relative;
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 12px 14px; background: #fff;
  }}
  .sector-head {{
    display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
    padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid #f0f0f5;
  }}
  .sector-head-left {{
    display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
    flex: 1; min-width: 0; padding-right: 8px;
  }}
  .sector-forecast-corner {{
    flex-shrink: 0; text-align: right;
  }}
  .sector-persist-pill {{ font-size: 10px; font-weight: 600; padding: 3px 9px; white-space: nowrap; }}
  .sector-rank {{
    width: 22px; height: 22px; border-radius: 5px; background: var(--accent); color: #fff;
    font-size: 11px; font-weight: 800; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
  }}
  .sector-name {{ font-size: 15px; font-weight: 800; letter-spacing: -0.2px; margin-right: 4px; }}
  .sector-stat {{
    font-size: 11px; font-weight: 700; color: var(--sub);
    background: #f2f2f7; padding: 3px 9px; border-radius: 999px; white-space: nowrap;
  }}
  .sector-stat.hot {{ color: var(--bad); background: var(--bad-bg); }}
  .sector-cols {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px;
  }}
  @media (max-width: 640px) {{ .sector-cols {{ grid-template-columns: 1fr; gap: 10px; }} }}
  .sector-col-left {{ min-width: 0; flex: 1; }}
  .sector-col-right {{
    min-width: 0; flex: 1; padding-left: 16px; border-left: 1px solid #f0f0f5;
  }}
  @media (max-width: 640px) {{
    .sector-col-right {{ padding-left: 0; border-left: none; padding-top: 10px; border-top: 1px solid #f0f0f5; }}
  }}
  .sector-block {{ margin-bottom: 8px; }}
  .sector-block:last-child {{ margin-bottom: 0; }}
  .sector-block-empty {{ opacity: .5; }}
  .sector-muted {{ color: var(--muted); font-size: 12px; }}
  .sector-label {{
    display: inline-block;
    font-size: 11px; font-weight: 800; letter-spacing: 0.02em;
    margin-bottom: 4px;
  }}
  .sector-label-logic {{ color: #b25000; }}
  .sector-label-event {{ color: #b25000; }}
  .sector-label-forecast {{ color: #b25000; }}
  .sector-text {{ font-size: 12px; color: var(--sub); line-height: 1.55; }}

  /* ── 盘后公告 ── */
  .post-list {{
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
  }}
  @media (max-width: 720px) {{ .post-list {{ grid-template-columns: 1fr; }} }}
  .post-row {{
    display: flex; flex-direction: column; gap: 6px; height: 100%;
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 11px 13px; background: #fff;
    border-left: 2px solid var(--border);
  }}
  .post-row.ok {{ border-left-color: var(--bar-ok); }}
  .post-row.warn {{ border-left-color: var(--bar-warn); }}
  .post-row.bad {{ border-left-color: var(--bar-bad); }}
  .post-type {{
    font-size: 11px; font-weight: 700; color: var(--muted);
    align-self: flex-start;
  }}
  .post-title {{ font-size: 13px; font-weight: 700; color: var(--text); line-height: 1.5; }}
  .post-summary {{ font-size: 12px; color: var(--sub); line-height: 1.55; margin: 0; }}
  .news-empty {{ font-size: 12px; color: var(--muted); padding: 4px 0; }}
  .module-summary {{
    font-size: 11px; color: var(--sub); margin-top: 8px; padding: 6px 9px;
    background: #fff; border-radius: var(--radius-sm); border: 1px solid var(--border); line-height: 1.45;
  }}

  /* ── 未来2周 · 事件与方向（融合版）── */
  .fwd-track-wrap {{
    background: #fff; border-radius: var(--radius-sm); border: 1px solid var(--border);
    padding: 14px 12px 16px; margin-bottom: 12px;
  }}
  .fwd-track-scroll {{
    overflow-x: auto; -webkit-overflow-scrolling: touch; padding-bottom: 2px;
  }}
  .fwd-track {{
    display: flex; align-items: flex-start; position: relative; gap: 0;
    min-width: max-content; padding: 0 4px;
  }}
  .fwd-track::before {{
    content: ''; position: absolute; top: 18px; left: 28px; right: 28px;
    height: 2px; background: linear-gradient(90deg, #d1d1d6, #0a84ff44, #d1d1d6);
    z-index: 0; border-radius: 1px;
  }}
  .fwd-tnode {{
    flex: 0 0 auto; width: 100px; text-align: center; position: relative; z-index: 1;
    padding: 0 4px;
  }}
  .fwd-tdot {{
    width: 36px; height: 36px; border-radius: 50%; background: var(--accent); color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 800; box-shadow: 0 1px 4px rgba(10,132,255,.2);
    margin: 0 auto;
  }}
  .fwd-tdot.hot {{ background: #ff6b63; box-shadow: 0 1px 4px rgba(255,69,58,.25); }}
  .fwd-tlabel {{
    margin-top: 10px; font-size: 12px; font-weight: 700; color: var(--text); line-height: 1.35;
  }}
  .fwd-tsub {{
    margin-top: 3px; font-size: 10px; color: var(--muted); line-height: 1.4;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }}

  .fwd-lead-wrap {{ margin-top: 16px; }}
  .fwd-lead {{
    padding: 12px 14px;
    background: linear-gradient(180deg, #f8f9fc 0%, #fff 100%);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    display: flex; flex-direction: column; gap: 12px;
  }}
  .fwd-lead-row {{
    display: grid; grid-template-columns: 68px 1fr; gap: 8px 10px; align-items: start;
  }}
  .fwd-lead-tag {{
    font-size: 10px; font-weight: 700; letter-spacing: .2px;
    color: var(--muted); padding-top: 2px; line-height: 1.4;
  }}
  .fwd-lead-tag.peak {{ color: #b25000; }}
  .fwd-lead-tag.muted {{ color: #8e8e93; }}
  .fwd-lead-text {{
    margin: 0; font-size: 13px; line-height: 1.7; color: var(--text);
  }}
  .fwd-lead-text.peak {{ font-weight: 600; color: #b25000; }}
  .fwd-lead-text.muted {{ color: var(--sub); }}
  .fwd-block-label {{
    font-size: 11px; font-weight: 700; color: var(--muted);
    letter-spacing: .3px; margin-bottom: 10px;
  }}
  .fwd-block-hint {{
    font-size: 10px; font-weight: 500; color: var(--muted); margin-left: 6px;
  }}
  .fwd-cal-wrap {{ margin-top: 14px; }}
  .fwd-cal {{
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    background: #fff; overflow: hidden;
  }}
  .fwd-cal-item {{
    display: grid; grid-template-columns: 76px 1fr; gap: 10px 12px;
    padding: 12px 14px; border-bottom: 1px solid #f0f0f5;
    position: relative;
  }}
  .fwd-cal-item:last-child {{ border-bottom: none; }}
  .fwd-cal-date {{
    font-size: 12px; font-weight: 800; color: var(--accent);
    line-height: 1.4; padding-top: 2px;
  }}
  .fwd-cal-top {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px;
  }}
  .fwd-cal-title {{ font-size: 13px; font-weight: 700; color: var(--text); line-height: 1.45; }}
  .fwd-cal-badge {{
    font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
    background: #fff3e0; color: #b25000; border: 1px solid #ffe0b2;
  }}
  .fwd-cal-note {{
    font-size: 12px; line-height: 1.6; color: var(--sub); margin: 4px 0 0;
  }}
  .fwd-themes-wrap {{ margin-top: 16px; }}

  .dir-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 0; }}
  @media (max-width: 560px) {{ .dir-grid {{ grid-template-columns: 1fr; }} }}
  .dir-card {{
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 11px 13px; background: #fff;
  }}
  /* 题材方向：A股配色 红=好 绿=不好 */
  .dir-card.ok {{ border-left: 3px solid #E53935; }}
  .dir-card.warn {{ border-left: 3px solid var(--warn); }}
  .dir-card.bad {{ border-left: 3px solid #34C759; }}
  .dir-grid .pill.ok {{ background: #ffebee; color: #c62828; }}
  .dir-grid .pill.warn {{ background: var(--warn-bg); color: #b25000; }}
  .dir-grid .pill.bad {{ background: #e8f5e9; color: #2e7d32; }}
  .dir-card-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 4px; }}
  .dir-name {{ font-size: 13px; font-weight: 700; }}
  .dir-drivers {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0 8px; }}
  .dir-driver {{
    font-size: 10px; padding: 3px 8px; border-radius: 6px;
    background: #f0f4ff; color: #3a5a9a; border: 1px solid #d8e2f4;
  }}
  .dir-logic {{ font-size: 12px; color: var(--sub); line-height: 1.65; }}

  .pill {{
    display: inline-block; font-size: 11px; padding: 3px 9px; border-radius: 999px;
    font-weight: 600; white-space: nowrap;
  }}
  .pill.ok {{ background: var(--ok-bg); color: #1a7a34; }}
  .pill.warn {{ background: var(--warn-bg); color: #b25000; }}
  .pill.bad {{ background: var(--bad-bg); color: #c41e16; }}
  .pill.weak {{ background: #eef2f6; color: #5a6472; }}

  .footer {{
    text-align: center; color: var(--muted); font-size: 11px;
    margin-top: 16px; padding-top: 14px;
  }}

  /* ── 手机端字号（固定标准，勿随意改动；用户确认 2026-07-07）── */
  @media (max-width: 640px) {{
    body {{
      font-size: 15px;
      line-height: 1.55;
      padding: 8px 8px calc(72px + env(safe-area-inset-bottom, 0px));
      padding-top: max(8px, env(safe-area-inset-top, 0px));
    }}
    .hero {{
      position: sticky; top: 0; z-index: 200;
      margin-bottom: 8px; padding: 12px 12px 10px;
      box-shadow: 0 2px 12px rgba(0,0,0,.06);
    }}
    .hero-top {{ flex-direction: column; gap: 10px; }}
    .hero h1 {{ font-size: 24px; }}
    .hero-meta {{ font-size: 14px; }}
    .hero-tag {{ font-size: 13px; }}
    .hero-status {{ font-size: 12px; }}
    .hero-decision {{ width: 100%; text-align: left; min-width: 0; }}
    .hero-mode {{ font-size: 22px; }}
    .hero-summary {{ font-size: 15px; }}
    .page-nav {{
      flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
      gap: 6px; padding-bottom: 2px; margin-top: 8px;
      scrollbar-width: none;
    }}
    .page-nav::-webkit-scrollbar {{ display: none; }}
    .page-nav a {{
      flex-shrink: 0; padding: 8px 14px; font-size: 14px;
      min-height: 38px; display: inline-flex; align-items: center;
    }}
    .section {{ padding: 12px; margin-bottom: 8px; }}
    .section-head {{ flex-wrap: wrap; gap: 6px; }}
    .section-num {{ font-size: 14px; }}
    .section-title {{ font-size: 18px; }}
    .section-sub {{ font-size: 14px; }}
    .section-sub.event-meta .event-sync-note {{ font-size: 13px; }}
    .sub-heading {{ font-size: 14px; }}
    .sub-note {{ font-size: 13px; }}
    .callout {{ font-size: 15px; }}
    .score-label {{ font-size: 13px; }}
    .score-hint {{ font-size: 13px; }}
    .score-name {{ font-size: 13px; }}
    #sec-env .score-val {{ font-size: 22px; letter-spacing: -0.3px; }}
    .score-foot {{ font-size: 13px; }}
    .section-sub {{
      margin-left: 0; white-space: normal; width: 100%;
      text-align: left; order: 3;
    }}
    .section-sub.event-meta {{
      align-items: flex-start; text-align: left; max-width: 100%;
    }}
    .trend-matrix {{ width: 100%; min-width: 520px; font-size: 13px; }}
    .trend-matrix th, .trend-matrix td {{ padding: 7px 9px; }}
    .trend-date-wd {{ font-size: 12px; }}
    #sec-trend .trend-cell {{ font-size: 13px; }}
    .sector-rank {{ font-size: 13px; }}
    .sector-name {{ font-size: 17px; }}
    .sector-stat {{ font-size: 13px; }}
    .sector-persist-pill {{ font-size: 12px; }}
    .sector-label {{ font-size: 13px; }}
    .sector-text {{ font-size: 14px; }}
    .sector-muted {{ font-size: 14px; }}
    .module-summary {{ font-size: 13px; }}
    .post-meta {{ font-size: 13px; }}
    .post-title {{ font-size: 15px; }}
    .post-summary {{ font-size: 14px; }}
    .news-empty {{ font-size: 14px; }}
    .event-month {{ font-size: 13px; }}
    .event-day {{ font-size: 13px; }}
    .event-label {{ font-size: 14px; }}
    .event-note {{ font-size: 13px; }}
    .event-block-title {{ font-size: 16px; }}
    .event-block-body {{ font-size: 14px; }}
    .dir-name {{ font-size: 15px; }}
    .dir-badge {{ font-size: 12px; }}
    .dir-logic {{ font-size: 14px; line-height: 1.7; }}
    .fwd-lead-text {{ font-size: 15px; line-height: 1.75; }}
    .fwd-lead-row {{ grid-template-columns: 72px 1fr; }}
    .fwd-tdot {{ width: 40px; height: 40px; font-size: 11px; }}
    .fwd-tlabel {{ font-size: 13px; }}
    .fwd-tsub {{ font-size: 11px; }}
    .fwd-cal-title {{ font-size: 14px; }}
    .fwd-cal-note {{ font-size: 14px; line-height: 1.7; }}
    .fwd-cal-date {{ font-size: 13px; }}
    .footer {{ font-size: 13px; }}
  }}

  @media print {{
    body {{ background: #fff; padding: 0; }}
    .page-nav {{ display: none; }}
    .section {{ box-shadow: none; break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="page">

  <header class="hero">
    <div class="hero-top">
      <div>
        <h1>复盘报告 · {ctx['title_date']}</h1>
        <div class="hero-meta">
          数据 {ctx['data_date']}（周{ctx['data_weekday']}）｜下一交易日 {ctx['next_date']}（周{ctx['next_weekday']}）
        </div>
        <span class="hero-tag">5/10日线试单 & 新高加仓</span>
      </div>
      <div class="hero-decision {m['pill']}">
        <span class="hero-status">{m['status']}</span>
        <div class="hero-mode">{m['primary_label']}</div>
      </div>
    </div>
    <div class="hero-summary">{m['summary']}</div>
    <nav class="page-nav">
      <a href="index.html">首页</a>
      <a href="#sec-trend">10日趋势</a>
      <a href="#sec-env">环境评分</a>
      <a href="#sec-sectors">涨停板块</a>
      <a href="#sec-post">公告与政策</a>
      <a href="#sec-event">事件方向</a>
    </nav>
  </header>

  <!-- 1 10日 -->
  <div class="section" id="sec-trend">
    <div class="section-head">
      <div class="section-num">1</div>
      <div class="section-title">10日趋势</div>
      <div class="section-sub">{ctx['trend_range']}</div>
    </div>
    {trend_html}
    <div class="callout">{ctx['trend_headline']}</div>
  </div>

  <!-- 2 环境 -->
  <div class="section" id="sec-env">
    <div class="section-head">
      <div class="section-num">2</div>
      <div class="section-title">环境评分 · 趋势与承接</div>
      <div class="section-sub">八维评分</div>
    </div>
    <div class="score-groups">{scores_html}</div>
    <div class="callout {ctx['env_callout']['tag']}">{ctx['env_callout']['text']}</div>
  </div>

  <!-- 3 涨停板块 -->
  <div class="section" id="sec-sectors">
    <div class="section-head">
      <div class="section-num">3</div>
      <div class="section-title">涨停板块 Top5</div>
      <div class="section-sub">{ctx['data_date']}</div>
    </div>
    {sector_block}
    {f'<div class="module-summary">{sector_summary}</div>' if sector_summary else ''}
    {f'<div class="news-empty" style="margin-top:10px">{news["hint"]}</div>' if not news["has_data"] else ''}
  </div>

  <!-- 4 公告与政策 -->
  <div class="section" id="sec-post">
    <div class="section-head">
      <div class="section-num">4</div>
      <div class="section-title">公告与政策</div>
      <div class="section-sub">上市公司盘后披露 + 当日重要国家政策 · 精选摘要</div>
    </div>
    {post_block}
    {f'<div class="module-summary">{post_summary}</div>' if post_summary else ''}
  </div>

  <!-- 5 事件 -->
  <div class="section" id="sec-event">
    <div class="section-head">
      <div class="section-num">5</div>
      <div class="section-title">未来2周 · 事件与方向</div>
      <div class="section-sub event-meta">
        <span>{ctx['event_window']}</span>
        <span class="event-sync-note">{ctx['event_sync']}</span>
      </div>
    </div>
    {fwd_section_html}
  </div>

  <div class="footer">大盘数据.numbers · 生成于 {ctx['generated']}</div>
</div>
</body>
</html>"""


def coalesce_row(row: pd.Series, prev: pd.Series | None) -> pd.Series:
    """历史日生成：缺失字段用前一交易日同列值补全。"""
    if prev is None:
        return row
    out = row.copy()
    for col in out.index:
        if pd.isna(out[col]) and col in prev.index and pd.notna(prev[col]):
            out[col] = prev[col]
    if pd.isna(out.get("主赚差")) and pd.notna(out.get("主爆赚率")) and pd.notna(out.get("主爆亏率")):
        out["主赚差"] = out["主爆赚率"] - out["主爆亏率"]
    if pd.isna(out.get("创赚差")) and pd.notna(out.get("创爆赚率")) and pd.notna(out.get("创爆亏率")):
        out["创赚差"] = out["创爆赚率"] - out["创爆亏率"]
    return out


def build_context(df: pd.DataFrame, as_of: datetime | pd.Timestamp | None = None) -> dict:
    if as_of is not None:
        if hasattr(as_of, "to_pydatetime"):
            as_of = as_of.to_pydatetime()
        work = df[df["date"] <= as_of].copy()
        if work.empty:
            raise ValueError(f"无 {as_of.date()} 及之前的交易日数据")
    else:
        work = df
    prev_row = work.iloc[-2] if len(work) >= 2 else None
    row = coalesce_row(work.iloc[-1], prev_row)
    dt = row["date"].to_pydatetime() if hasattr(row["date"], "to_pydatetime") else row["date"]
    nxt = next_trading_day(dt)
    last10 = work.tail(10)
    dim = compute_six_dim(row, work)
    trend_days, trend_headline = analyze_10d(last10, work)
    trend_range = f"{fmt_md(last10.iloc[0]['date'])}–{fmt_md(last10.iloc[-1]['date'])}"
    dims = analyze_3d(work, row)
    env_callout = build_env_callout(row, work, dim, dims)
    synth = env_callout["synth"]
    scores = merge_env_scores(dim)
    main_money = dim["main_money"]

    market_news = load_market_news(dt, env_weak=env_is_weak(main_money, synth, dims))
    news_raw = {}
    if NEWS_FILE.exists():
        try:
            news_raw = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            news_raw = {}
    if market_news["has_data"]:
        n_sec = len(market_news["top_sectors"])
        n_post = len(market_news["post_close"])
        st = market_news.get("curate_stats") or {}
        pool_n = st.get("pool", n_post)
        zt = int(row.get("涨停") or 0)
        mode = "偏弱" if env_is_weak(main_money, synth, dims) else "尚可"
        market_news["sector_summary"] = f"当日涨停 {zt} 家 · Top5 板块 {n_sec} 项"
        market_news["post_summary"] = (
            f"候选池 {pool_n} 条 → 精选 {n_post} 条（环境{mode}）"
        )
    else:
        market_news["sector_summary"] = ""
        market_news["post_summary"] = ""
    event_window, event_nodes, event_sync = build_events_window(dt)
    directions, dir_summary, rhythm, event_peak = load_direction_analysis(
        market_news.get("direction_analysis"),
    )
    if market_news["has_data"] and market_news["top_sectors"]:
        market_news["top_sectors"] = enrich_sector_streaks(market_news["top_sectors"], dt, news_raw)
        market_news["top_sectors"] = forecast_sector_persistence(
            market_news["top_sectors"],
            as_of=dt,
            row=row,
            df=df,
            directions=directions,
            env_weak=env_is_weak(main_money, synth, dims),
            post_close=market_news["post_close"],
        )
    emo = emotion_index(row)
    modes = analyze_trading_modes(row, work, emo, dim)
    advice = opening_advice(row, emo, work, nxt, modes)

    return {
        "title_date": fmt_md(dt),
        "data_date": dt.strftime("%Y-%m-%d"),
        "data_weekday": WEEKDAY[dt.weekday()],
        "next_date": nxt.strftime("%Y-%m-%d"),
        "next_weekday": WEEKDAY[nxt.weekday()],
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scores": scores,
        "env_callout": env_callout,
        "trend_days": trend_days,
        "trend_range": trend_range,
        "trend_headline": trend_headline,
        "synth": synth,
        "market_news": market_news,
        "event_window": event_window,
        "event_nodes": event_nodes,
        "directions": directions,
        "dir_summary": dir_summary,
        "rhythm": rhythm,
        "event_peak": event_peak,
        "event_sync": event_sync,
        "as_of": dt,
        "modes": modes,
        "advice": advice,
        "emo": emo,
    }


def write_index_html() -> Path:
    """生成手机入口页 index.html，列出全部复盘报告。"""
    reports: list[tuple[str, str, str]] = []
    for p in sorted(BASE.glob("复盘概览-*.html"), reverse=True):
        m = re.match(r"复盘概览-(\d{2})-(\d{2})\.html", p.name)
        if not m:
            continue
        label = f"{m.group(1)}/{m.group(2)}"
        mode = ""
        try:
            text = p.read_text(encoding="utf-8")
            mm = re.search(r'<div class="hero-mode">([^<]+)</div>', text)
            if mm:
                mode = mm.group(1).strip()
        except OSError:
            pass
        reports.append((label, p.name, mode))

    latest = reports[0] if reports else None
    cards = []
    for i, (label, fname, mode) in enumerate(reports):
        badge = '<span class="idx-badge latest">最新</span>' if i == 0 else ""
        mode_html = f'<div class="idx-mode">{mode}</div>' if mode else ""
        cards.append(
            f'''<a class="idx-card{" featured" if i == 0 else ""}" href="{fname}">
      <div class="idx-card-top"><span class="idx-date">{label}</span>{badge}</div>
      {mode_html}
      <div class="idx-arrow">查看 ›</div>
    </a>'''
        )
    cards_html = "".join(cards) if cards else '<p class="idx-empty">暂无报告，请先运行 generate_report.py</p>'
    latest_btn = (
        f'<a class="idx-hero-btn" href="{latest[1]}">打开最新复盘 · {latest[0]}</a>'
        if latest else ""
    )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="复">
<meta name="theme-color" content="#0a84ff">
<title>复盘报告</title>
<style>
  :root {{
    --bg: #f5f5f7; --surface: #fff; --text: #1c1c1e; --sub: #636366;
    --muted: #8e8e93; --accent: #0a84ff; --border: rgba(0,0,0,.06);
    --radius: 14px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    padding: max(12px, env(safe-area-inset-top)) 12px calc(24px + env(safe-area-inset-bottom));
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 480px; margin: 0 auto; }}
  .head {{
    background: linear-gradient(135deg, #0a84ff, #0066cc);
    color: #fff; border-radius: var(--radius); padding: 20px 18px; margin-bottom: 14px;
  }}
  .head h1 {{ font-size: 22px; font-weight: 800; letter-spacing: -0.3px; }}
  .head p {{ font-size: 13px; opacity: .9; margin-top: 6px; }}
  .idx-hero-btn {{
    display: block; margin-top: 14px; background: #fff; color: var(--accent);
    text-align: center; text-decoration: none; font-weight: 700; font-size: 15px;
    padding: 12px; border-radius: 10px;
  }}
  .idx-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .idx-card {{
    display: block; background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px 16px; text-decoration: none; color: inherit;
  }}
  .idx-card.featured {{ border-color: #0a84ff40; background: #f8fbff; }}
  .idx-card-top {{ display: flex; align-items: center; gap: 8px; }}
  .idx-date {{ font-size: 18px; font-weight: 800; }}
  .idx-badge {{
    font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 999px;
    background: #eef2f6; color: var(--sub);
  }}
  .idx-badge.latest {{ background: #ffebee; color: #c62828; }}
  .idx-mode {{ font-size: 13px; color: var(--sub); margin-top: 4px; }}
  .idx-arrow {{ font-size: 12px; color: var(--accent); margin-top: 8px; font-weight: 600; }}
  .idx-empty {{ color: var(--muted); font-size: 14px; padding: 20px; text-align: center; }}
  .tip {{
    margin-top: 16px; padding: 12px 14px; background: var(--surface);
    border-radius: var(--radius); border: 1px solid var(--border);
    font-size: 12px; color: var(--sub); line-height: 1.6;
  }}
  .tip code {{
    background: #f2f2f7; padding: 2px 6px; border-radius: 4px; font-size: 11px;
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1>复盘报告</h1>
    <p>手机端浏览 · 同一 WiFi 下可局域网访问</p>
    {latest_btn}
  </div>
  <div class="idx-list">{cards_html}</div>
  <div class="tip">
    <strong>外网：</strong><a href="https://mcxdcyy.github.io/market-report/">mcxdcyy.github.io/market-report</a>
    <br><strong>更新：</strong><code>python3 generate_report.py</code> → <code>git add docs && git push</code>
  </div>
</div>
</body>
</html>"""
    out = BASE / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def sync_pages_site() -> Path:
    """同步静态页到 docs/，供 GitHub Pages 发布。"""
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch()
    for old in DOCS_DIR.glob("复盘概览-*.html"):
        old.unlink()
    n = 0
    for p in sorted(BASE.glob("复盘概览-*.html")):
        shutil.copy2(p, DOCS_DIR / p.name)
        n += 1
    idx = BASE / "index.html"
    if idx.exists():
        shutil.copy2(idx, DOCS_DIR / "index.html")
        n += 1
    print(f"已同步 GitHub Pages: {DOCS_DIR}（{n} 个文件）")
    return DOCS_DIR


def write_report(df: pd.DataFrame, as_of: datetime | pd.Timestamp | None = None) -> Path:
    ctx = build_context(df, as_of)
    out = BASE / f"复盘概览-{ctx['title_date'].replace('/', '-')}.html"
    out.write_text(render_html(ctx), encoding="utf-8")
    print(f"已生成: {out}")
    print(f"主模式: {ctx['modes']['primary_label']}")
    return out


def main() -> None:
    if not DATA_FILE.exists():
        print(f"找不到: {DATA_FILE}", file=sys.stderr)
        sys.exit(1)
    df = load_market_data()
    write_report(df)
    # 最新日不是 07-03 时，同步更新 07-03 报告（便于对照历史样式）
    anchor = pd.Timestamp("2026-07-03")
    latest = df.iloc[-1]["date"]
    latest_dt = latest.to_pydatetime() if hasattr(latest, "to_pydatetime") else latest
    anchor_dt = anchor.to_pydatetime()
    if (df["date"] == anchor).any() and latest_dt.date() != anchor_dt.date():
        write_report(df, anchor)
    idx = write_index_html()
    print(f"已生成入口: {idx}")
    sync_pages_site()


if __name__ == "__main__":
    main()
