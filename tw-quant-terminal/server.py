#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TW-Quant Alpha — 台股全市場即時量化交易與條件警示系統後端引擎
涵蓋：
  1. 臺灣證券交易所 (TWSE 上市) 全數個股與 ETF 即時/收盤行情、本益比殖利率、三大法人買賣超
  2. 證券櫃檯買賣中心 (TPEx 上櫃 + ESB 興櫃) 全數個股與 ETF 行情、估值與三大法人籌碼
  3. 證交所 MIS 即時五秒撮合報價與最佳五檔委買委賣深度 (mis.twse.com.tw)
  4. Yahoo Finance 台股日K / 5分K 歷史序列與量化技術指標 (MA5/20/60, RSI14, KD9, MACD, 布林通道)
  5. 八大台股量化條件即時掃描引擎 + SSE (Server-Sent Events) 即時推播 + 盤中模擬壓力測試
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import math
import os
import queue
import random
import re
import ssl
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = int(os.environ.get("PORT", "8765"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 核心焦點量化池（啟動時預先載入日K計算 KD/RSI/MACD 與優先輪詢 MIS 五檔）
CORE_WATCH_CODES = [
    "2330", "2317", "2454", "2382", "3231", "2308", "2881", "2882", "2603", "3711",
    "2303", "3017", "2345", "6669", "3661", "2357", "2412", "2891", "2886", "1301",
    "2002", "2609", "2615", "2618", "1519", "1513", "2379", "3034", "3008", "2327",
    "6488", "8299", "5274", "3529", "8069", "6446", "3324", "5347", "6223", "3105",
    "0050", "0056", "00878", "00919", "00929", "00679B",
]


def to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").replace("X", "").replace("x", "").strip()
    if s in ("", "-", "--", "---", "----", "N/A", "nan", "None", "除權息", "除息", "除權"):
        return None
    try:
        v = float(s)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except ValueError:
        return None


def tw_tick_size(price: float) -> float:
    """台股標準升降單位 (Tick Size)"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_tw_price(price: float) -> float:
    tick = tw_tick_size(max(price, 0.01))
    rounded = round(round(price / tick) * tick, 2)
    return max(0.01, rounded)


def compute_tw_limits(prev_close: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if not prev_close or prev_close <= 0:
        return None, None
    up_raw = prev_close * 1.10
    dn_raw = prev_close * 0.90
    tick_u = tw_tick_size(up_raw)
    tick_d = tw_tick_size(dn_raw)
    limit_up = round(math.floor((up_raw + 1e-9) / tick_u) * tick_u, 2)
    limit_dn = round(math.ceil((dn_raw - 1e-9) / tick_d) * tick_d, 2)
    return limit_up, max(0.01, limit_dn)


def taipei_now() -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz)


def is_tw_market_open() -> bool:
    now = taipei_now()
    if now.weekday() >= 5:  # 週六、週日休市
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1335


def normalize_search_text(s: str) -> str:
    """正規化搜尋字串：全形轉半形、臺→台、移除空白與注音未選字干擾，徹底解決手機輸入法搜尋問題"""
    if not s:
        return ""
    out_chars: List[str] = []
    for ch in s.strip():
        code = ord(ch)
        if code == 0x3000:
            continue
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if ch == "臺":
            ch = "台"
        if ch.isspace():
            continue
        out_chars.append(ch.upper())
    return "".join(out_chars)


def compute_technical_indicators(
    timestamps: List[int],
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
) -> Dict[str, Any]:
    """
    計算台股技術指標與【自研核心量化指標】：
      - ★ α-量價多空生命線 (Lifeline 18): 0.6 * VWMA18(TypicalPrice, Vol) + 0.4 * EMA18(Close)
      - 乾坤三線系統: EMA5 (短攻線) + EMA10 (輔攻線) + Lifeline18 (多空生命線) 與三線糾結壓縮率
      - 多空九轉序列 (TD 9-Turn Exhaustion Count) 與神針下影線承接比
      - 逐根 K 線自研戰法買賣訊號標記 (★翻多 / ★回踩 / ★拔蔥 / ▲九轉 / ▼破線)
      - 標準指標: MA20 / MA60 / 布林通道 / RSI(14) / KD(9,3,3) / MACD(12,26,9)
    """
    n = len(closes)
    if n == 0:
        return {"series": {}, "latest": {}, "signals": []}

    o = np.array(opens, dtype=float)
    c = np.array(closes, dtype=float)
    h = np.array(highs, dtype=float)
    l = np.array(lows, dtype=float)
    v = np.maximum(np.array(volumes, dtype=float), 1.0)
    typ = (h + l + 2.0 * c) / 4.0

    def sma(arr: np.ndarray, period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(arr)
        if len(arr) < period:
            return out
        csum = np.cumsum(np.insert(arr, 0, 0.0))
        vals = (csum[period:] - csum[:-period]) / float(period)
        for idx, val in enumerate(vals):
            out[idx + period - 1] = round(float(val), 2)
        return out

    def ema_series(arr: np.ndarray, span: int) -> np.ndarray:
        alpha = 2.0 / (span + 1.0)
        out = np.zeros_like(arr)
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
        return out

    ma5 = sma(c, 5)
    ma10 = sma(c, 10)
    ma20 = sma(c, 20)
    ma60 = sma(c, 60)

    ema5_np = ema_series(c, 5)
    ema10_np = ema_series(c, 10)
    ema18_np = ema_series(c, 18)

    # ★ 自研【α-量價多空生命線 (Lifeline 18)】= 0.6 * VWMA18(TypicalPrice, Vol) + 0.4 * EMA18
    ll_np = np.zeros_like(c)
    for i in range(n):
        w0 = max(0, i - 17)
        vol_sum = float(np.sum(v[w0 : i + 1]))
        vwma18 = float(np.sum(typ[w0 : i + 1] * v[w0 : i + 1])) / vol_sum if vol_sum > 0 else float(c[i])
        ll_np[i] = 0.6 * vwma18 + 0.4 * float(ema18_np[i])

    lifeline_list: List[Optional[float]] = [round(float(x), 2) if idx >= 4 else None for idx, x in enumerate(ll_np)]
    ema5_list: List[Optional[float]] = [round(float(x), 2) for x in ema5_np]
    ema10_list: List[Optional[float]] = [round(float(x), 2) if idx >= 2 else None for idx, x in enumerate(ema10_np)]

    # 計算多空九轉序列 (TD Sequential 9-Count: 負數為連續空方下跌轉數 -1~-9，正數為多方上攻轉數 +1~+9)
    td_counts: List[int] = [0] * n
    up_cnt = 0
    dn_cnt = 0
    for i in range(n):
        if i >= 4:
            if c[i] > c[i - 4]:
                up_cnt = (up_cnt % 9) + 1
                dn_cnt = 0
                td_counts[i] = up_cnt
            elif c[i] < c[i - 4]:
                dn_cnt = (dn_cnt % 9) + 1
                up_cnt = 0
                td_counts[i] = -dn_cnt
            else:
                up_cnt = 0
                dn_cnt = 0

    # Bollinger Bands (20, 2)
    bb_upper: List[Optional[float]] = [None] * n
    bb_lower: List[Optional[float]] = [None] * n
    bb_pct_b: Optional[float] = None
    if n >= 20:
        for i in range(19, n):
            win = c[i - 19 : i + 1]
            mean = float(np.mean(win))
            std = float(np.std(win, ddof=0))
            bb_upper[i] = round(mean + 2.0 * std, 2)
            bb_lower[i] = round(mean - 2.0 * std, 2)
        if bb_upper[-1] is not None and bb_lower[-1] is not None and bb_upper[-1] > bb_lower[-1]:
            bb_pct_b = round((c[-1] - bb_lower[-1]) / (bb_upper[-1] - bb_lower[-1]), 3)

    # RSI (14)
    rsi14: List[Optional[float]] = [None] * n
    if n >= 15:
        deltas = np.diff(c)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[:14]))
        avg_loss = float(np.mean(losses[:14]))
        rs = avg_gain / avg_loss if avg_loss > 1e-9 else 999.0
        rsi14[14] = round(100.0 - (100.0 / (1.0 + rs)), 2)
        for i in range(14, len(deltas)):
            avg_gain = (avg_gain * 13.0 + float(gains[i])) / 14.0
            avg_loss = (avg_loss * 13.0 + float(losses[i])) / 14.0
            if avg_loss < 1e-9:
                rsi14[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi14[i + 1] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    # KD (9, 3, 3)
    k_vals: List[Optional[float]] = [None] * n
    d_vals: List[Optional[float]] = [None] * n
    if n >= 9:
        k_cur = 50.0
        d_cur = 50.0
        for i in range(8, n):
            hh = float(np.max(h[i - 8 : i + 1]))
            ll_low = float(np.min(l[i - 8 : i + 1]))
            rsv = ((float(c[i]) - ll_low) / (hh - ll_low) * 100.0) if (hh - ll_low) > 1e-9 else 50.0
            k_cur = (2.0 / 3.0) * k_cur + (1.0 / 3.0) * rsv
            d_cur = (2.0 / 3.0) * d_cur + (1.0 / 3.0) * k_cur
            k_vals[i] = round(k_cur, 2)
            d_vals[i] = round(d_cur, 2)

    # MACD (12, 26, 9)
    dif_list: List[Optional[float]] = [None] * n
    dea_list: List[Optional[float]] = [None] * n
    hist_list: List[Optional[float]] = [None] * n
    if n >= 26:
        ema12 = ema_series(c, 12)
        ema26 = ema_series(c, 26)
        dif = ema12 - ema26
        dea = ema_series(dif, 9)
        hist = dif - dea
        for i in range(25, n):
            dif_list[i] = round(float(dif[i]), 3)
            dea_list[i] = round(float(dea[i]), 3)
            hist_list[i] = round(float(hist[i]), 3)

    # ATR(14) 波動率序列（佔股價百分比，用於自適應過濾雜訊）
    tr_arr = np.zeros(n, dtype=float)
    tr_arr[0] = max(0.01, float(h[0] - l[0]))
    for i in range(1, n):
        tr_arr[i] = max(
            float(h[i] - l[i]),
            abs(float(h[i] - c[i - 1])),
            abs(float(l[i] - c[i - 1])),
        )
    atr14_np = ema_series(tr_arr, 14)

    # 逐根 K 線掃描自研戰法 2.0 買賣點標記（含上影線假突破過濾、波段冷卻去重與詳盡觸發原因）
    chart_signals: List[Dict[str, Any]] = []
    last_sig_bar: Dict[str, int] = {}

    for i in range(18, n):
        ll_cur = float(ll_np[i])
        ll_prev = float(ll_np[i - 1])
        ll_prev3 = float(ll_np[max(0, i - 3)])
        if ll_cur <= 0 or ll_prev <= 0:
            continue

        slope_i = (ll_cur - ll_prev) / ll_prev * 100.0
        ll_rising_3d = (ll_cur > ll_prev) and (ll_prev >= ll_prev3)
        bias_i = (float(c[i]) - ll_cur) / ll_cur * 100.0
        pct_i = (float(c[i]) - float(c[i - 1])) / float(c[i - 1]) * 100.0 if c[i - 1] > 0 else 0.0
        atr_pct_i = (float(atr14_np[i]) / float(c[i]) * 100.0) if c[i] > 0 else 2.5

        avg_v = float(np.mean(v[max(0, i - 19) : i])) if i >= 5 else float(v[i])
        vr_i = float(v[i]) / avg_v if avg_v > 0 else 1.0

        # K 棒實體與上下影線結構品質（過濾隔日沖避雷針假突破）
        rng_i = max(0.01, float(h[i] - l[i]))
        close_pos_i = (float(c[i]) - float(l[i])) / rng_i          # 1.0=收最高, 0.0=收最低
        upper_wick_i = (float(h[i]) - max(float(o[i]), float(c[i]))) / rng_i
        wick_dn_i = (min(float(o[i]), float(c[i])) - float(l[i])) / rng_i

        # 乾坤三線前 3 日平均糾結壓縮率
        sp_vals = []
        for k in range(max(4, i - 3), i):
            hi_k = max(float(ema5_np[k]), float(ema10_np[k]), float(ll_np[k]))
            lo_k = min(float(ema5_np[k]), float(ema10_np[k]), float(ll_np[k]))
            if ll_np[k] > 0:
                sp_vals.append((hi_k - lo_k) / float(ll_np[k]) * 100.0)
        spread_prev = float(np.mean(sp_vals)) if sp_vals else 99.0

        # 主力口袋支點量比：當日成交量 vs 前 10 日最大下跌黑 K 量
        down_vols_10 = [float(v[k]) for k in range(max(1, i - 10), i) if c[k] < c[k - 1]]
        max_dn_v10 = max(down_vols_10) if down_vols_10 else avg_v
        pocket_ratio_i = float(v[i]) / max_dn_v10 if max_dn_v10 > 0 else 1.0
        high_close_10 = float(np.max(c[max(0, i - 10) : i]))

        def can_fire(sig_code: str, cooldown: int) -> bool:
            prev_b = last_sig_bar.get(sig_code, -999)
            return (i - prev_b) >= cooldown

        # 1. 買 ★ 拔蔥（三線麻花壓縮後帶量紅 K 貫穿三線，且收盤飽滿無長上影）
        squeeze_limit = max(2.4, min(3.4, atr_pct_i * 0.95))
        if (
            spread_prev <= squeeze_limit
            and c[i] > max(float(ema5_np[i]), float(ema10_np[i]), ll_cur)
            and c[i - 1] <= max(float(ema5_np[i - 1]), float(ema10_np[i - 1]), ll_prev) * 1.008
            and pct_i >= max(2.0, atr_pct_i * 0.65)
            and vr_i >= 1.3
            and close_pos_i >= 0.62
            and upper_wick_i <= 0.35
            and can_fire("SQUEEZE", 4)
        ):
            last_sig_bar["SQUEEZE"] = i
            chart_signals.append({
                "idx": i,
                "type": "BUY",
                "code": "SQUEEZE",
                "tag": "買★拔蔥",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"三線糾結僅 {spread_prev:.1f}%，出量 {vr_i:.1f}x 長紅貫穿三線起漲",
            })

        # 2. 買 ★ 翻多（昨日位於生命線下，今日帶量實體紅 K 正式躍過生命線）
        elif (
            c[i - 1] <= ll_prev * 1.002
            and c[i] > ll_cur * 1.004
            and pct_i >= max(1.6, atr_pct_i * 0.55)
            and vr_i >= 1.2
            and c[i] > o[i]
            and close_pos_i >= 0.60
            and upper_wick_i <= 0.36
            and can_fire("FLIP", 4)
        ):
            last_sig_bar["FLIP"] = i
            chart_signals.append({
                "idx": i,
                "type": "BUY",
                "code": "FLIP",
                "tag": "買★翻多",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"帶量 {vr_i:.1f}x 紅K躍過生命線 ({ll_cur:.2f}元)，空翻多確認",
            })

        # 3. 買 ★ 守線（生命線連3日上揚，股價回踩生命線量縮惜售或下影收腳守穩）
        elif (
            ll_rising_3d
            and slope_i >= 0.05
            and -0.3 <= bias_i <= min(2.2, max(1.3, atr_pct_i * 0.7))
            and l[i] <= ll_cur * 1.012
            and c[i] >= ll_cur
            and (c[i] > o[i] or wick_dn_i >= 0.38)
            and close_pos_i >= 0.52
            and vr_i <= 1.35
            and can_fire("HOLD", 6)
        ):
            last_sig_bar["HOLD"] = i
            chart_signals.append({
                "idx": i,
                "type": "BUY",
                "code": "HOLD",
                "tag": "買★守線",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"回踩上揚生命線 ({ll_cur:.2f}元) 量縮收腳守穩，低風險買點",
            })

        # 4. 買 ★ 支點（線上強勢整理後創 10 日收盤高，且量能勝過 10 日最大黑 K 量）
        elif (
            c[i] > ll_cur
            and ll_rising_3d
            and c[i] > high_close_10
            and c[i - 1] <= high_close_10
            and pocket_ratio_i >= 1.35
            and pct_i >= 2.0
            and close_pos_i >= 0.65
            and can_fire("POCKET", 6)
        ):
            last_sig_bar["POCKET"] = i
            chart_signals.append({
                "idx": i,
                "type": "BUY",
                "code": "POCKET",
                "tag": "買★支點",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"主力口袋支點！創10日收盤高且量勝最大黑K量 {pocket_ratio_i:.1f}x",
            })

        # 5. 買 ▲ 九轉（空方九轉衰竭 ≥7 轉或負乖離極值＋神針下影線／紅K止跌）
        elif (
            (td_counts[i] <= -7 or (bias_i <= -max(7.0, atr_pct_i * 2.4) and (rsi14[i] or 50) <= 32))
            and (wick_dn_i >= 0.45 or (c[i] > o[i] and close_pos_i >= 0.62))
            and can_fire("TD_BOT", 6)
        ):
            last_sig_bar["TD_BOT"] = i
            chart_signals.append({
                "idx": i,
                "type": "BUY",
                "code": "TD_BOT",
                "tag": "買▲九轉",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"空方衰竭 ({abs(td_counts[i])}轉 / 乖離{bias_i:+.1f}%) 浮現神針低接買盤",
            })

        # 6. 賣 ▼ 破線（昨日在生命線上，今日帶量長黑收低實質跌破生命線）
        elif (
            c[i - 1] >= ll_prev * 0.998
            and bias_i <= -max(1.4, atr_pct_i * 0.45)
            and pct_i <= -2.6
            and close_pos_i <= 0.40
            and vr_i >= 1.05
            and can_fire("BREAK", 4)
        ):
            last_sig_bar["BREAK"] = i
            chart_signals.append({
                "idx": i,
                "type": "SELL",
                "code": "BREAK",
                "tag": "賣▼破線",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"帶量長黑實質失守生命線 ({ll_cur:.2f}元)，嚴格執行破線停損",
            })

        # 7. 賣 ▼ 高九（多方高檔九轉 ≥8 轉且正乖離過熱，爆量長上影或收黑結帳警訊）
        elif (
            td_counts[i] >= 8
            and bias_i >= max(6.5, atr_pct_i * 2.2)
            and (upper_wick_i >= 0.42 or (c[i] < o[i] and pct_i <= -1.5))
            and vr_i >= 1.2
            and can_fire("TD_TOP", 6)
        ):
            last_sig_bar["TD_TOP"] = i
            chart_signals.append({
                "idx": i,
                "type": "SELL",
                "code": "TD_TOP",
                "tag": "賣▼高九",
                "price": round(float(c[i]), 2),
                "lifeline": round(ll_cur, 2),
                "detail": f"高檔多方第 +{td_counts[i]} 轉正乖離 {bias_i:+.1f}% 爆量上影結帳警訊",
            })

    # 最新一根 K 線的自研指標 2.0 精確讀數
    vol_ratio_20 = None
    if n >= 20:
        avg_v20 = float(np.mean(v[-20:]))
        if avg_v20 > 0:
            vol_ratio_20 = round(float(v[-1]) / avg_v20, 2)

    kd_cross = None
    if n >= 10 and k_vals[-1] is not None and d_vals[-1] is not None and k_vals[-2] is not None and d_vals[-2] is not None:
        if k_vals[-2] <= d_vals[-2] and k_vals[-1] > d_vals[-1]:
            kd_cross = "GOLDEN"
        elif k_vals[-2] >= d_vals[-2] and k_vals[-1] < d_vals[-1]:
            kd_cross = "DEATH"

    macd_cross = None
    if n >= 27 and hist_list[-1] is not None and hist_list[-2] is not None:
        if hist_list[-2] <= 0 < hist_list[-1]:
            macd_cross = "GOLDEN"
        elif hist_list[-2] >= 0 > hist_list[-1]:
            macd_cross = "DEATH"

    lifeline_val = lifeline_list[-1]
    lifeline_slope = None
    lifeline_bias = None
    lifeline_signal = "NEUTRAL"
    ribbon_spread_prev = None
    pocket_pivot_ratio = None
    breakout_10d = False
    atr_pct_latest = round(float(atr14_np[-1]) / float(c[-1]) * 100.0, 2) if c[-1] > 0 else 2.5
    rng_last = max(0.01, float(h[-1] - l[-1]))
    close_pos_last = round((float(c[-1]) - float(l[-1])) / rng_last, 2)
    upper_wick_pct = round(((float(h[-1]) - max(float(o[-1]), float(c[-1]))) / rng_last) * 100.0, 1)
    lower_wick_pct = round(((min(float(o[-1]), float(c[-1])) - float(l[-1])) / rng_last) * 100.0, 1)

    if n >= 12:
        dn_v10 = [float(v[k]) for k in range(max(1, n - 11), n - 1) if c[k] < c[k - 1]]
        base_dn = max(dn_v10) if dn_v10 else float(np.mean(v[max(0, n - 11) : n - 1]))
        if base_dn > 0:
            pocket_pivot_ratio = round(float(v[-1]) / base_dn, 2)
        prev_hi10 = float(np.max(c[max(0, n - 11) : n - 1]))
        breakout_10d = bool(c[-1] > prev_hi10 and c[-2] <= prev_hi10 * 1.005)

    if n >= 18 and ll_np[-1] > 0 and ll_np[-2] > 0:
        lifeline_slope = round(float((ll_np[-1] - ll_np[-2]) / ll_np[-2] * 100.0), 3)
        lifeline_bias = round(float((c[-1] - ll_np[-1]) / ll_np[-1] * 100.0), 2)
        sp_3d = []
        for k in range(max(4, n - 4), n - 1):
            hi_k = max(float(ema5_np[k]), float(ema10_np[k]), float(ll_np[k]))
            lo_k = min(float(ema5_np[k]), float(ema10_np[k]), float(ll_np[k]))
            sp_3d.append((hi_k - lo_k) / float(ll_np[k]) * 100.0)
        ribbon_spread_prev = round(float(np.mean(sp_3d)), 2) if sp_3d else None

        pct_last = (float(c[-1]) - float(c[-2])) / float(c[-2]) * 100.0 if c[-2] > 0 else 0.0
        ll_up3 = (ll_np[-1] > ll_np[-2]) and (ll_np[-2] >= ll_np[-4])
        if c[-2] <= ll_np[-2] * 1.002 and c[-1] > ll_np[-1] * 1.003 and pct_last >= 1.5 and close_pos_last >= 0.58:
            lifeline_signal = "FLIP_BULL"         # 帶量實體紅K躍過生命線（破線翻多）
        elif ll_up3 and lifeline_slope > 0.04 and -0.4 <= lifeline_bias <= 2.3 and l[-1] <= ll_np[-1] * 1.015 and c[-1] >= ll_np[-1] and close_pos_last >= 0.50:
            lifeline_signal = "PULLBACK_HOLD"     # 上揚生命線縮量回踩守穩（黃金買點）
        elif c[-2] >= ll_np[-2] * 0.998 and lifeline_bias <= -1.4 and pct_last <= -2.5 and close_pos_last <= 0.42:
            lifeline_signal = "BREAKDOWN"         # 帶量長黑實質跌破生命線（破位撤退）
        elif c[-1] >= ll_np[-1]:
            lifeline_signal = "ABOVE_BULL"        # 線上多頭控盤
        else:
            lifeline_signal = "BELOW_BEAR"        # 線下空方壓制

    latest = {
        "lifeline": lifeline_val,
        "lifelineSlope": lifeline_slope,
        "lifelineBias": lifeline_bias,
        "lifelineSignal": lifeline_signal,
        "atrPct": atr_pct_latest,
        "closePos": close_pos_last,
        "upperWickPct": upper_wick_pct,
        "pocketPivotRatio": pocket_pivot_ratio,
        "breakout10d": breakout_10d,
        "ema5": ema5_list[-1],
        "ema10": ema10_list[-1],
        "ribbonSpreadPrev": ribbon_spread_prev,
        "ribbonBreakout": bool(
            ribbon_spread_prev is not None
            and ribbon_spread_prev <= 3.0
            and c[-1] > max(float(ema5_np[-1]), float(ema10_np[-1]), float(ll_np[-1]))
            and close_pos_last >= 0.60
        ),
        "tdCount": td_counts[-1],
        "lowerWickPct": lower_wick_pct,
        "ma5": ma5[-1],
        "ma10": ma10[-1],
        "ma20": ma20[-1],
        "ma60": ma60[-1],
        "bbUpper": bb_upper[-1],
        "bbLower": bb_lower[-1],
        "bbPctB": bb_pct_b,
        "rsi14": rsi14[-1],
        "k9": k_vals[-1],
        "d9": d_vals[-1],
        "kdCross": kd_cross,
        "macdDif": dif_list[-1],
        "macdDea": dea_list[-1],
        "macdHist": hist_list[-1],
        "macdCross": macd_cross,
        "volRatio20": vol_ratio_20,
    }
    series = {
        "lifeline": lifeline_list,
        "ema5": ema5_list,
        "ema10": ema10_list,
        "tdCount": td_counts,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "bbUpper": bb_upper,
        "bbLower": bb_lower,
        "rsi14": rsi14,
        "k9": k_vals,
        "d9": d_vals,
        "macdDif": dif_list,
        "macdDea": dea_list,
        "macdHist": hist_list,
    }
    return {"series": series, "latest": latest, "signals": chart_signals}


# ==================== 六大自研台股量化戰法 2.0 (Proprietary Alpha Suite v2) ====================
DEFAULT_QUANT_RULES: List[Dict[str, Any]] = [
    {
        "id": "alpha_lifeline",
        "name": "自研【α-生命線 2.0】實體躍線翻多＆縮量守線戰法",
        "category": "LIFELINE",
        "badge": "🧬 生命線戰法",
        "enabled": True,
        "description": "融合 60% 主力成交量加權成本 VWMA18 與 40% 指數動能 EMA18，並加入「上影線避雷針過濾」與「連3日上揚縮量收腳確認」，精準鎖定破線翻多與黃金回踩守線買點。",
        "params": {
            "maxPullbackBias": {"label": "回踩生命線容許乖離 (%)", "value": 2.2, "step": 0.2, "min": 0.5, "max": 5.0},
            "minFlipPct": {"label": "躍過生命線最低漲幅 (%)", "value": 1.6, "step": 0.2, "min": 0.5, "max": 8.0},
        },
    },
    {
        "id": "ribbon_squeeze",
        "name": "自研【乾坤三線麻花】連3日波動壓縮旱地拔蔥戰法",
        "category": "BULL",
        "badge": "🌊 三線拔蔥",
        "enabled": True,
        "description": "偵測 EMA5、EMA10 與 α-生命線連續 3 日極度麻花收斂（浮額洗淨），當日出量紅 K 收高（收盤位階 ≥60%）一舉貫穿三線時發出主升段起漲訊號！",
        "params": {
            "maxSpreadPct": {"label": "三線連3日壓縮門檻 ≤ (%)", "value": 3.0, "step": 0.2, "min": 1.0, "max": 6.0},
            "minBreakPct": {"label": "突破實體紅K漲幅 ≥ (%)", "value": 2.2, "step": 0.2, "min": 0.8, "max": 9.5},
        },
    },
    {
        "id": "smart_moat_lock",
        "name": "自研【主力口袋支點】勝10日最大黑K量＋箱頂創高戰法",
        "category": "CHIP",
        "badge": "🚀 口袋支點",
        "enabled": True,
        "description": "純價量即時偵測主力真金白銀吃貨！當股價站穩 α-生命線創 10 日新高，且當日成交量超越近 10 日最大下跌黑 K 量（口袋支點量比 ≥1.3x）並強於大盤 Alpha 時觸發。",
        "params": {
            "minPocketRatio": {"label": "勝10日最大黑K量倍數 ≥", "value": 1.3, "step": 0.1, "min": 1.0, "max": 5.0},
            "minAlphaPct": {"label": "領先大盤超額報酬 ≥ (%)", "value": 2.0, "step": 0.5, "min": 0.0, "max": 10.0},
        },
    },
    {
        "id": "nine_turn_bottom",
        "name": "自研【九轉極值神針】恐慌衰竭＋下影承接抄底戰法",
        "category": "TECH",
        "badge": "⚡ 九轉神針",
        "enabled": True,
        "description": "結合 TD 九轉連續空方衰竭（≥7轉）或 ATR 波動率極限負乖離，當低檔拉出長下影線「定海神針」或紅 K 吞噬止跌時，精準捕捉恐慌錯殺反轉點！",
        "params": {
            "minTdDown": {"label": "空方九轉衰竭計數 ≥", "value": 7, "step": 1, "min": 5, "max": 9},
            "minWickPct": {"label": "神針下影線佔比 ≥ (%)", "value": 42.0, "step": 2.0, "min": 20.0, "max": 80.0},
        },
    },
    {
        "id": "whale_orderbook",
        "name": "自研【盤口巨鯨點火】五檔近端加權買壓＋紅K攻擊戰法",
        "category": "MICRO",
        "badge": "🐋 巨鯨點火",
        "enabled": True,
        "description": "對即時五檔賦予近端衰減權重 [1.6, 1.3, 1.0, 0.7, 0.5] 識破遠端假墊單，且要求股價維持紅盤或開高走高（拒絕下跌接刀），鎖定真巨鯨向上點火。",
        "params": {
            "minWeightedBid": {"label": "近端加權買壓佔比 ≥ (%)", "value": 66.0, "step": 1.0, "min": 53.0, "max": 95.0},
            "minBidLots": {"label": "五檔委買總量 ≥ (張)", "value": 500, "step": 50, "min": 50, "max": 20000},
        },
    },
    {
        "id": "lifeline_breakdown",
        "name": "自研【破線／高九警報】實質失守生命線＆高檔結帳風控",
        "category": "RISK",
        "badge": "☠️ 破線警報",
        "enabled": True,
        "description": "「線上持股續抱、破線嚴格保命」！過濾盤中洗盤假跌破，僅在帶量長黑收低實質失守 α-生命線、高檔九轉爆量長上影或單日重挫破位時發出撤退警報。",
        "params": {
            "breakBiasPct": {"label": "實質破線負乖離 ≤ (%)", "value": -1.5, "step": 0.2, "min": -6.0, "max": -0.5},
            "crashPct": {"label": "全市場爆量重挫門檻 ≤ (%)", "value": -7.5, "step": 0.5, "min": -9.9, "max": -3.0},
        },
    },
]


class MarketEngine:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.stocks: Dict[str, Dict[str, Any]] = {}
        self.base_snapshot: Dict[str, Dict[str, Any]] = {}
        self.indices: Dict[str, Dict[str, Any]] = {
            "TAIEX": {"code": "TAIEX", "name": "發行量加權股價指數", "price": 0.0, "change": 0.0, "changePct": 0.0, "high": 0.0, "low": 0.0, "updatedAt": "--:--:--"},
            "TPEX": {"code": "TPEX", "name": "櫃買市場指數", "price": 0.0, "change": 0.0, "changePct": 0.0, "high": 0.0, "low": 0.0, "updatedAt": "--:--:--"},
        }
        self.rules: List[Dict[str, Any]] = json.loads(json.dumps(DEFAULT_QUANT_RULES))
        self.custom_price_alerts: List[Dict[str, Any]] = [
            # 預設提供兩組示範自訂價位監控，使用者可隨時新增或刪除
        ]
        self.alerts_map: Dict[str, List[Dict[str, Any]]] = {}  # code -> list of triggered alert dicts
        self.recent_alert_events: List[Dict[str, Any]] = []   # chronological feed (max 250)
        self.seen_alert_keys: set = set()

        self.kline_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.hot_codes: List[str] = []
        self.focus_codes: List[str] = ["2330", "2317", "2454", "2603", "6488"]
        self.mis_cursor: int = 0

        self.sim_mode: bool = False
        self.last_full_sync: str = "--"
        self.last_quote_sync: str = "--"
        self.data_date: str = "--"
        self.sse_clients: List[queue.Queue] = []
        self.ready_event = threading.Event()

    # -------------------- SSE Broadcast --------------------
    def register_sse(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self.lock:
            self.sse_clients.append(q)
        return q

    def unregister_sse(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.sse_clients:
                self.sse_clients.remove(q)

    def broadcast(self, event: str, payload: Any) -> None:
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        with self.lock:
            dead = []
            for q in self.sse_clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                if q in self.sse_clients:
                    self.sse_clients.remove(q)

    # -------------------- HTTP Helper --------------------
    def _get_json(self, url: str, timeout: float = 14.0) -> Any:
        import csv
        import io
        import urllib.request

        ctx = ssl._create_unverified_context()
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": HEADERS["User-Agent"],
                        "Accept": "application/json",
                        "Connection": "close",
                    },
                )
                with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8-sig", errors="replace").strip()
                if not raw:
                    raise ValueError("Empty response body")
                if raw[0] in ("[", "{"):
                    return json.loads(raw)
                # TWSE OpenAPI CDN 偶爾回傳 UTF-8 CSV 格式，自動無縫解析為 list[dict]
                return list(csv.DictReader(io.StringIO(raw)))
            except Exception as exc:
                last_err = exc
                time.sleep(0.45 * (attempt + 1))
        raise last_err if last_err else RuntimeError(f"Failed fetching {url}")

    # -------------------- Full Universe Load --------------------
    def load_all_taiwan_stocks(self) -> None:
        """載入全台股上市(TWSE)、上櫃(TPEx)、興櫃(ESB)所有個股行情、估值與三大法人籌碼（依網域禮貌佇列避免政府API連線重置）"""
        twse_group = [
            ("twse_day", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"),
            ("twse_pe", "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"),
            ("twse_inst", "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALLBUT0999"),
        ]
        tpex_group = [
            ("tpex_day", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"),
            ("tpex_pe", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"),
            ("tpex_inst", "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"),
            ("esb_day", "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"),
        ]

        results: Dict[str, Any] = {}

        def fetch_group(items: List[Tuple[str, str]]) -> None:
            for key, url in items:
                try:
                    results[key] = self._get_json(url, timeout=16.0)
                except Exception as exc:
                    print(f"[WARN] Failed loading {key}: {exc}", file=sys.stderr)
                    results[key] = None
                time.sleep(0.18)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(fetch_group, twse_group)
            f2 = pool.submit(fetch_group, tpex_group)
            concurrent.futures.wait([f1, f2])

        new_stocks: Dict[str, Dict[str, Any]] = {}
        latest_date = ""

        # 1. 解析 TWSE 本益比/殖利率/淨值比（相容 JSON 英文欄位與 CSV 中文欄位）
        twse_pe_map: Dict[str, Dict[str, Any]] = {}
        for row in results.get("twse_pe") or []:
            c = str(row.get("Code") or row.get("證券代號") or "").strip()
            if c:
                twse_pe_map[c] = row

        # 2. 解析 TWSE 三大法人買賣超
        twse_inst_map: Dict[str, Tuple[float, float, float, float]] = {}
        twse_inst_raw = results.get("twse_inst")
        if isinstance(twse_inst_raw, dict) and twse_inst_raw.get("data"):
            for row in twse_inst_raw["data"]:
                if len(row) >= 19:
                    c = str(row[0]).strip()
                    f_lots = round(((to_float(row[4]) or 0.0) + (to_float(row[7]) or 0.0)) / 1000.0, 1)
                    t_lots = round((to_float(row[10]) or 0.0) / 1000.0, 1)
                    d_lots = round((to_float(row[11]) or 0.0) / 1000.0, 1)
                    tot_lots = round((to_float(row[18]) or 0.0) / 1000.0, 1)
                    twse_inst_map[c] = (f_lots, t_lots, d_lots, tot_lots)

        # 3. 解析 TWSE 上市所有個股與 ETF（相容 JSON 與 CSV 欄位）
        for row in results.get("twse_day") or []:
            code = str(row.get("Code") or row.get("證券代號") or "").strip()
            name = str(row.get("Name") or row.get("證券名稱") or "").strip()
            if not code or not name:
                continue
            r_date = row.get("Date") or row.get("日期")
            if not latest_date and r_date:
                latest_date = str(r_date).strip()

            close = to_float(row.get("ClosingPrice") if "ClosingPrice" in row else row.get("收盤價"))
            open_p = to_float(row.get("OpeningPrice") if "OpeningPrice" in row else row.get("開盤價"))
            high = to_float(row.get("HighestPrice") if "HighestPrice" in row else row.get("最高價"))
            low = to_float(row.get("LowestPrice") if "LowestPrice" in row else row.get("最低價"))
            chg = to_float(row.get("Change") if "Change" in row else row.get("漲跌價差"))
            vol_shares = to_float(row.get("TradeVolume") if "TradeVolume" in row else row.get("成交股數")) or 0.0
            val_twd = to_float(row.get("TradeValue") if "TradeValue" in row else row.get("成交金額")) or 0.0

            prev_close = (close - chg) if (close is not None and chg is not None) else close
            chg_pct = round((chg / prev_close) * 100.0, 2) if (chg is not None and prev_close and prev_close > 0) else 0.0
            amp = round(((high - low) / prev_close) * 100.0, 2) if (high and low and prev_close and prev_close > 0) else 0.0
            limit_up, limit_dn = compute_tw_limits(prev_close)

            pe_row = twse_pe_map.get(code, {})
            f_lots, t_lots, d_lots, tot_lots = twse_inst_map.get(code, (0.0, 0.0, 0.0, 0.0))

            item = {
                "code": code,
                "name": name,
                "market": "TWSE",
                "marketLabel": "上市",
                "isEtf": code.startswith("00") or code.startswith("01") or code.startswith("02"),
                "price": close,
                "prevClose": round(prev_close, 2) if prev_close else None,
                "open": open_p,
                "high": high,
                "low": low,
                "change": round(chg, 2) if chg is not None else 0.0,
                "changePct": chg_pct,
                "amplitude": amp,
                "volumeLots": int(round(vol_shares / 1000.0)),
                "turnoverM": round(val_twd / 1_000_000.0, 2),
                "pe": to_float(pe_row.get("PEratio") if "PEratio" in pe_row else pe_row.get("本益比")),
                "pb": to_float(pe_row.get("PBratio") if "PBratio" in pe_row else pe_row.get("股價淨值比")),
                "dividendYield": to_float(pe_row.get("DividendYield") if "DividendYield" in pe_row else pe_row.get("殖利率(%)")),
                "foreignNetLots": f_lots,
                "trustNetLots": t_lots,
                "dealerNetLots": d_lots,
                "instTotalLots": tot_lots,
                "bidPrice": None,
                "askPrice": None,
                "limitUp": limit_up,
                "limitDown": limit_dn,
                "bidRatio": None,
                "bidTotalLots": 0,
                "askTotalLots": 0,
                "orderbook": None,
                "tech": {},
                "quantScore": 50,
                "updatedAt": "13:30:00",
                "date": latest_date,
            }
            new_stocks[code] = item

        # 4. 解析 TPEx 本益比與殖利率
        tpex_pe_map: Dict[str, Dict[str, Any]] = {}
        for row in results.get("tpex_pe") or []:
            c = str(row.get("SecuritiesCompanyCode", "")).strip()
            if c:
                tpex_pe_map[c] = row

        # 5. 解析 TPEx 三大法人買賣超
        tpex_inst_map: Dict[str, Tuple[float, float, float, float]] = {}
        for row in results.get("tpex_inst") or []:
            c = str(row.get("SecuritiesCompanyCode", "")).strip()
            if not c:
                continue
            f_diff = (
                to_float(row.get("ForeignInvestorsInclude MainlandAreaInvestors-Difference"))
                or to_float(row.get("Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference"))
                or 0.0
            )
            t_diff = to_float(row.get("SecuritiesInvestmentTrustCompanies-Difference")) or 0.0
            d_diff = to_float(row.get("Dealers-Difference")) or 0.0
            tot_diff = to_float(row.get("TotalDifference")) or (f_diff + t_diff + d_diff)
            tpex_inst_map[c] = (
                round(f_diff / 1000.0, 1),
                round(t_diff / 1000.0, 1),
                round(d_diff / 1000.0, 1),
                round(tot_diff / 1000.0, 1),
            )

        # 6. 解析 TPEx 上櫃所有個股與 ETF（排除 6 碼 7 開頭權證）
        for row in results.get("tpex_day") or []:
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            name = str(row.get("CompanyName", "")).strip()
            if not code or not name:
                continue
            if len(code) == 6 and code.startswith("7"):
                continue
            if code in new_stocks:
                continue

            close = to_float(row.get("Close"))
            open_p = to_float(row.get("Open"))
            high = to_float(row.get("High"))
            low = to_float(row.get("Low"))
            chg = to_float(row.get("Change"))
            vol_shares = to_float(row.get("TradingShares")) or 0.0
            val_twd = to_float(row.get("TransactionAmount")) or 0.0
            bid_p = to_float(row.get("LatestBidPrice"))
            ask_p = to_float(row.get("LatesAskPrice"))

            if close is None and bid_p is not None:
                close = bid_p
            prev_close = (close - chg) if (close is not None and chg is not None) else close
            chg_pct = round((chg / prev_close) * 100.0, 2) if (chg is not None and prev_close and prev_close > 0) else 0.0
            amp = round(((high - low) / prev_close) * 100.0, 2) if (high and low and prev_close and prev_close > 0) else 0.0
            limit_up, limit_dn = compute_tw_limits(prev_close)

            pe_row = tpex_pe_map.get(code, {})
            f_lots, t_lots, d_lots, tot_lots = tpex_inst_map.get(code, (0.0, 0.0, 0.0, 0.0))

            item = {
                "code": code,
                "name": name,
                "market": "TPEX",
                "marketLabel": "上櫃",
                "isEtf": code.startswith("00") or code.startswith("02"),
                "price": close,
                "prevClose": round(prev_close, 2) if prev_close else None,
                "open": open_p,
                "high": high,
                "low": low,
                "change": round(chg, 2) if chg is not None else 0.0,
                "changePct": chg_pct,
                "amplitude": amp,
                "volumeLots": int(round(vol_shares / 1000.0)),
                "turnoverM": round(val_twd / 1_000_000.0, 2),
                "pe": to_float(pe_row.get("PriceEarningRatio")),
                "pb": to_float(pe_row.get("PriceBookRatio")),
                "dividendYield": to_float(pe_row.get("YieldRatio")),
                "foreignNetLots": f_lots,
                "trustNetLots": t_lots,
                "dealerNetLots": d_lots,
                "instTotalLots": tot_lots,
                "bidPrice": bid_p,
                "askPrice": ask_p,
                "limitUp": limit_up,
                "limitDown": limit_dn,
                "bidRatio": None,
                "bidTotalLots": 0,
                "askTotalLots": 0,
                "orderbook": None,
                "tech": {},
                "quantScore": 50,
                "updatedAt": "13:30:00",
                "date": str(row.get("Date") or latest_date).strip(),
            }
            new_stocks[code] = item

        # 7. 解析 ESB 興櫃所有個股
        for row in results.get("esb_day") or []:
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            name = str(row.get("CompanyName", "")).strip()
            if not code or not name or code in new_stocks:
                continue

            close = to_float(row.get("LatestPrice")) or to_float(row.get("Average"))
            prev_avg = to_float(row.get("PreviousAveragePrice"))
            high = to_float(row.get("Highest"))
            low = to_float(row.get("Lowest"))
            bid_p = to_float(row.get("BuyingPrice"))
            ask_p = to_float(row.get("SellingPrice"))
            vol_shares = to_float(row.get("TransactionVolume")) or 0.0

            chg = (close - prev_avg) if (close is not None and prev_avg is not None) else 0.0
            chg_pct = round((chg / prev_avg) * 100.0, 2) if (prev_avg and prev_avg > 0 and close is not None) else 0.0
            amp = round(((high - low) / prev_avg) * 100.0, 2) if (high and low and prev_avg and prev_avg > 0) else 0.0

            item = {
                "code": code,
                "name": name,
                "market": "ESB",
                "marketLabel": "興櫃",
                "isEtf": False,
                "price": close,
                "prevClose": prev_avg,
                "open": prev_avg,
                "high": high,
                "low": low,
                "change": round(chg, 2),
                "changePct": chg_pct,
                "amplitude": amp,
                "volumeLots": int(round(vol_shares / 1000.0)),
                "turnoverM": round(((close or 0.0) * vol_shares) / 1_000_000.0, 2),
                "pe": None,
                "pb": None,
                "dividendYield": None,
                "foreignNetLots": 0.0,
                "trustNetLots": 0.0,
                "dealerNetLots": 0.0,
                "instTotalLots": 0.0,
                "bidPrice": bid_p,
                "askPrice": ask_p,
                "limitUp": None,  # 興櫃無漲跌停限制
                "limitDown": None,
                "bidRatio": None,
                "bidTotalLots": 0,
                "askTotalLots": 0,
                "orderbook": None,
                "tech": {},
                "quantScore": 50,
                "updatedAt": "15:00:00",
                "date": str(row.get("Date") or latest_date).strip(),
            }
            new_stocks[code] = item

        # 計算所有個股初始量化評分，建立熱門股輪詢清單
        for st in new_stocks.values():
            st["quantScore"] = self._compute_quant_score(st)

        # 取成交金額前 90 檔上市櫃個股 + 核心焦點股作為即時 MIS 輪詢對象
        tradable = [s for s in new_stocks.values() if s["market"] in ("TWSE", "TPEX")]
        tradable.sort(key=lambda x: x.get("turnoverM") or 0.0, reverse=True)
        hot_set: List[str] = []
        for c in CORE_WATCH_CODES + [s["code"] for s in tradable[:90]]:
            if c in new_stocks and new_stocks[c]["market"] in ("TWSE", "TPEX") and c not in hot_set:
                hot_set.append(c)

        with self.lock:
            # 保留已計算好的技術指標或五檔
            for code, st in new_stocks.items():
                old = self.stocks.get(code)
                if old:
                    if old.get("tech"):
                        st["tech"] = old["tech"]
                    if old.get("orderbook"):
                        st["orderbook"] = old["orderbook"]
                        st["bidRatio"] = old.get("bidRatio")
                        st["bidTotalLots"] = old.get("bidTotalLots", 0)
                        st["askTotalLots"] = old.get("askTotalLots", 0)
                    st["quantScore"] = self._compute_quant_score(st)

            self.stocks = new_stocks
            self.base_snapshot = json.loads(json.dumps(new_stocks))
            self.hot_codes = hot_set
            self.data_date = latest_date
            self.last_full_sync = taipei_now().strftime("%H:%M:%S")

        # 立即拉取指數與首輪 MIS 即時五檔
        self.refresh_indices_and_mis_batch(codes_override=hot_set[:45])
        self.evaluate_all_alerts(emit_sse=False)
        print(f"[INFO] Loaded {len(new_stocks)} Taiwan stocks (Date={latest_date}). Alerts active={sum(len(v) for v in self.alerts_map.values())}")

    # -------------------- MIS Real-Time Quotes & Orderbook --------------------
    def _build_mis_channel(self, code: str) -> Optional[str]:
        with self.lock:
            st = self.stocks.get(code)
        if not st:
            return None
        if st["market"] == "TWSE":
            return f"tse_{code}.tw"
        if st["market"] == "TPEX":
            return f"otc_{code}.tw"
        return None

    def fetch_mis_quotes(self, codes: List[str], include_indices: bool = False) -> List[Dict[str, Any]]:
        channels: List[str] = []
        if include_indices:
            channels.extend(["tse_t00.tw", "otc_o00.tw"])
        for c in codes:
            ch = self._build_mis_channel(c)
            if ch and ch not in channels:
                channels.append(ch)
        if not channels:
            return []

        ex_ch = "|".join(channels[:60])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time() * 1000)}"
        try:
            data = self._get_json(url, timeout=8.0)
        except Exception as exc:
            print(f"[WARN] MIS fetch error: {exc}", file=sys.stderr)
            return []

        updated_stocks: List[Dict[str, Any]] = []
        msg_array = data.get("msgArray") or []

        with self.lock:
            for m in msg_array:
                c = str(m.get("c") or "").strip()
                if not c:
                    continue

                # 大盤與櫃買指數
                if c in ("t00", "o00"):
                    idx_key = "TAIEX" if c == "t00" else "TPEX"
                    z = to_float(m.get("z"))
                    y = to_float(m.get("y"))
                    h = to_float(m.get("h"))
                    l = to_float(m.get("l"))
                    price = z if z is not None else y
                    if price is not None:
                        chg = round(price - y, 2) if y else 0.0
                        pct = round((chg / y) * 100.0, 2) if (y and y > 0) else 0.0
                        self.indices[idx_key].update({
                            "price": round(price, 2),
                            "prevClose": y,
                            "change": chg,
                            "changePct": pct,
                            "high": h or price,
                            "low": l or price,
                            "updatedAt": str(m.get("t") or taipei_now().strftime("%H:%M:%S")),
                        })
                    continue

                st = self.stocks.get(c)
                if not st or self.sim_mode:
                    continue

                # 解析五檔委買委賣
                bids_p = [to_float(x) for x in str(m.get("b") or "").split("_") if x.strip()]
                bids_v = [int(to_float(x) or 0) for x in str(m.get("g") or "").split("_") if x.strip()]
                asks_p = [to_float(x) for x in str(m.get("a") or "").split("_") if x.strip()]
                asks_v = [int(to_float(x) or 0) for x in str(m.get("f") or "").split("_") if x.strip()]

                bids = [{"price": bids_p[i], "lots": bids_v[i] if i < len(bids_v) else 0} for i in range(min(5, len(bids_p))) if bids_p[i] is not None]
                asks = [{"price": asks_p[i], "lots": asks_v[i] if i < len(asks_v) else 0} for i in range(min(5, len(asks_p))) if asks_p[i] is not None]

                bid_sum = sum(x["lots"] for x in bids)
                ask_sum = sum(x["lots"] for x in asks)
                tot_ob = bid_sum + ask_sum
                bid_ratio = round((bid_sum / tot_ob) * 100.0, 1) if tot_ob > 0 else None

                # 自研【近端衰減加權買壓佔比】：第一、二檔權重加重 [1.6, 1.3, 1.0, 0.7, 0.5] 過濾遠端假掛單
                ob_weights = [1.6, 1.3, 1.0, 0.7, 0.5]
                w_bid = sum(bids[i]["lots"] * ob_weights[i] for i in range(len(bids)))
                w_ask = sum(asks[i]["lots"] * ob_weights[i] for i in range(len(asks)))
                w_tot = w_bid + w_ask
                weighted_bid_ratio = round((w_bid / w_tot) * 100.0, 1) if w_tot > 0 else bid_ratio

                z = to_float(m.get("z"))
                y = to_float(m.get("y")) or st.get("prevClose")
                o = to_float(m.get("o"))
                h = to_float(m.get("h"))
                l = to_float(m.get("l"))
                u = to_float(m.get("u"))
                w = to_float(m.get("w"))
                v_lots = to_float(m.get("v"))
                tv_lots = to_float(m.get("tv"))

                # MIS 盤中偶爾最新一筆撮合 z 為 "-"，此時保留有效現價或取最佳買賣價
                eff_price = z
                if eff_price is None:
                    eff_price = st.get("price")
                if eff_price is None and bids:
                    eff_price = bids[0]["price"]
                if eff_price is None and asks:
                    eff_price = asks[0]["price"]
                if eff_price is None:
                    eff_price = y

                if eff_price is not None:
                    st["price"] = round(eff_price, 2)
                if y is not None and y > 0:
                    st["prevClose"] = round(y, 2)
                    if st["price"] is not None:
                        st["change"] = round(st["price"] - y, 2)
                        st["changePct"] = round((st["change"] / y) * 100.0, 2)
                if o is not None:
                    st["open"] = o
                if h is not None:
                    st["high"] = h
                if l is not None:
                    st["low"] = l
                if st.get("high") and st.get("low") and st.get("prevClose"):
                    st["amplitude"] = round(((st["high"] - st["low"]) / st["prevClose"]) * 100.0, 2)
                if u is not None:
                    st["limitUp"] = u
                if w is not None:
                    st["limitDown"] = w
                if v_lots is not None and v_lots > 0:
                    st["volumeLots"] = int(v_lots)
                if tv_lots is not None:
                    st["lastTickLots"] = int(tv_lots)
                if bids:
                    st["bidPrice"] = bids[0]["price"]
                if asks:
                    st["askPrice"] = asks[0]["price"]

                st["orderbook"] = {"bids": bids, "asks": asks}
                st["bidTotalLots"] = bid_sum
                st["askTotalLots"] = ask_sum
                st["bidRatio"] = bid_ratio
                st["weightedBidRatio"] = weighted_bid_ratio
                if m.get("t"):
                    st["updatedAt"] = str(m["t"])

                st["quantScore"] = self._compute_quant_score(st)
                # 同步回 base_snapshot 以便關閉模擬時還原最新真實值
                self.base_snapshot[c] = json.loads(json.dumps(st))
                updated_stocks.append(st)

            self.last_quote_sync = taipei_now().strftime("%H:%M:%S")

        return updated_stocks

    def refresh_indices_and_mis_batch(self, codes_override: Optional[List[str]] = None) -> None:
        if self.sim_mode:
            return
        with self.lock:
            if codes_override is not None:
                batch = list(codes_override)
            else:
                batch = list(self.focus_codes[:10])
                if self.hot_codes:
                    start = self.mis_cursor % len(self.hot_codes)
                    sliced = (self.hot_codes + self.hot_codes)[start : start + 38]
                    self.mis_cursor = (start + 38) % len(self.hot_codes)
                    for c in sliced:
                        if c not in batch:
                            batch.append(c)

        updated = self.fetch_mis_quotes(batch, include_indices=True)
        new_events = self.evaluate_codes_alerts([s["code"] for s in updated])
        if updated or new_events:
            self.broadcast(
                "tick",
                {
                    "mode": "SIM" if self.sim_mode else "LIVE",
                    "marketOpen": is_tw_market_open(),
                    "lastQuoteSync": self.last_quote_sync,
                    "indices": self.indices,
                    "stocks": [self._compact_stock(s) for s in updated[:50]],
                    "newAlerts": new_events,
                },
            )

    # -------------------- K-Line & Technicals (Anue cnyes Primary + Yahoo Fallback) --------------------
    def fetch_stock_kline(self, code: str, interval: str = "1d", force: bool = False) -> Dict[str, Any]:
        cache_key = f"{code}:{interval}"
        now_ts = time.time()
        if not force and cache_key in self.kline_cache:
            ts, cached = self.kline_cache[cache_key]
            if now_ts - ts < 35.0:
                return cached

        with self.lock:
            st = self.stocks.get(code)
        if not st:
            return {"candles": [], "series": {}, "latest": {}}

        yf_interval = "5m" if interval == "5m" else "1d"
        tz = datetime.timezone(datetime.timedelta(hours=8))

        candles: List[Dict[str, Any]] = []
        ts_clean: List[int] = []
        o_clean: List[float] = []
        h_clean: List[float] = []
        l_clean: List[float] = []
        c_clean: List[float] = []
        v_clean: List[float] = []

        # 1. 優先透過鉅亨網台股原生 K 線 API (極速、無 429 頻率限制)
        if st["market"] in ("TWSE", "TPEX"):
            try:
                res_code = "5" if yf_interval == "5m" else "D"
                lookback = 86400 * 8 if yf_interval == "5m" else 86400 * 190
                now_i = int(now_ts)
                cnyes_url = (
                    f"https://ws.api.cnyes.com/ws/api/v1/charting/history"
                    f"?resolution={res_code}&symbol={urllib.parse.quote(f'TWS:{code}:STOCK')}"
                    f"&from={now_i}&to={now_i - lookback}"
                )
                c_raw = self._get_json(cnyes_url, timeout=8.0)
                c_data = (c_raw.get("data") if isinstance(c_raw, dict) else None) or {}
                t_arr = c_data.get("t") or []
                o_arr = c_data.get("o") or []
                h_arr = c_data.get("h") or []
                l_arr = c_data.get("l") or []
                cl_arr = c_data.get("c") or []
                v_arr = c_data.get("v") or []

                rows_tmp = []
                for i in range(len(t_arr)):
                    tv = t_arr[i]
                    ov = o_arr[i] if i < len(o_arr) else None
                    hv = h_arr[i] if i < len(h_arr) else None
                    lv = l_arr[i] if i < len(l_arr) else None
                    cv = cl_arr[i] if i < len(cl_arr) else None
                    vv = v_arr[i] if i < len(v_arr) else 0.0
                    if tv is None or ov is None or hv is None or lv is None or cv is None:
                        continue
                    rows_tmp.append((int(tv), float(ov), float(hv), float(lv), float(cv), float(vv or 0.0)))

                # 依時間由舊到新排序
                rows_tmp.sort(key=lambda x: x[0])
                for tv, ov, hv, lv, cv, vv in rows_tmp:
                    dt = datetime.datetime.fromtimestamp(tv, tz)
                    label = dt.strftime("%m/%d %H:%M") if yf_interval == "5m" else dt.strftime("%Y-%m-%d")
                    lots = int(round(vv))
                    candles.append({
                        "ts": tv,
                        "time": label,
                        "open": round(ov, 2),
                        "high": round(hv, 2),
                        "low": round(lv, 2),
                        "close": round(cv, 2),
                        "volumeLots": lots,
                    })
                    ts_clean.append(tv)
                    o_clean.append(ov)
                    h_clean.append(hv)
                    l_clean.append(lv)
                    c_clean.append(cv)
                    v_clean.append(float(lots))
            except Exception as exc:
                print(f"[WARN] cnyes K-line fallback for {code}: {exc}", file=sys.stderr)

        # 2. 若為興櫃 (ESB) 或主源無資料，自動降級使用 Yahoo Finance
        if not candles:
            suffix = ".TW" if st["market"] == "TWSE" else ".TWO"
            yf_range = "5d" if yf_interval == "5m" else "6mo"
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(code + suffix)}?interval={yf_interval}&range={yf_range}"
            try:
                raw = self._get_json(url, timeout=9.0)
                result = (raw.get("chart") or {}).get("result")
                if result:
                    r0 = result[0]
                    timestamps = r0.get("timestamp") or []
                    quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
                    opens_raw = quote.get("open") or []
                    highs_raw = quote.get("high") or []
                    lows_raw = quote.get("low") or []
                    closes_raw = quote.get("close") or []
                    vols_raw = quote.get("volume") or []

                    for i in range(len(timestamps)):
                        c_val = closes_raw[i] if i < len(closes_raw) else None
                        o_val = opens_raw[i] if i < len(opens_raw) else None
                        h_val = highs_raw[i] if i < len(highs_raw) else None
                        l_val = lows_raw[i] if i < len(lows_raw) else None
                        v_val = vols_raw[i] if i < len(vols_raw) else 0
                        if c_val is None or o_val is None or h_val is None or l_val is None:
                            continue
                        dt = datetime.datetime.fromtimestamp(timestamps[i], tz)
                        label = dt.strftime("%m/%d %H:%M") if yf_interval == "5m" else dt.strftime("%Y-%m-%d")
                        lots = int(round(float(v_val or 0) / 1000.0))
                        candles.append({
                            "ts": int(timestamps[i]),
                            "time": label,
                            "open": round(float(o_val), 2),
                            "high": round(float(h_val), 2),
                            "low": round(float(l_val), 2),
                            "close": round(float(c_val), 2),
                            "volumeLots": lots,
                        })
                        ts_clean.append(int(timestamps[i]))
                        o_clean.append(float(o_val))
                        h_clean.append(float(h_val))
                        l_clean.append(float(l_val))
                        c_clean.append(float(c_val))
                        v_clean.append(float(lots))
            except Exception as exc:
                print(f"[WARN] Yahoo Finance K-line error for {code}: {exc}", file=sys.stderr)

        if not candles:
            return {"candles": [], "series": {}, "latest": {}}

        tech = compute_technical_indicators(ts_clean, o_clean, h_clean, l_clean, c_clean, v_clean)
        payload = {
            "code": code,
            "interval": yf_interval,
            "candles": candles,
            "series": tech["series"],
            "latest": tech["latest"],
            "signals": tech.get("signals", []),
        }
        if len(self.kline_cache) > 320:
            oldest_keys = sorted(self.kline_cache.keys(), key=lambda k: self.kline_cache[k][0])[:70]
            for ok in oldest_keys:
                self.kline_cache.pop(ok, None)
        self.kline_cache[cache_key] = (now_ts, payload)

        # 若為日K，將最新自研生命線與技術指標寫回個股主表並重評分與警示
        if yf_interval == "1d" and tech["latest"]:
            with self.lock:
                if code in self.stocks:
                    self.stocks[code]["tech"] = tech["latest"]
                    self.stocks[code]["quantScore"] = self._compute_quant_score(self.stocks[code])
                    if code in self.base_snapshot:
                        self.base_snapshot[code]["tech"] = tech["latest"]
                        self.base_snapshot[code]["quantScore"] = self.stocks[code]["quantScore"]
            self.evaluate_codes_alerts([code])

        return payload

    def preload_core_technicals(self) -> None:
        """背景高速預載全台股前 220 大高成交值、強勢創高與弱勢異動股之日K，運算自研【α-生命線 2.0】、口袋支點與乾坤三線"""
        with self.lock:
            tradable = [s for s in self.stocks.values() if s["market"] in ("TWSE", "TPEX")]
        by_turn = sorted(tradable, key=lambda x: x.get("turnoverM") or 0.0, reverse=True)[:145]
        by_gain = sorted(
            [s for s in tradable if (s.get("volumeLots") or 0) >= 800 and not s["isEtf"]],
            key=lambda x: x.get("changePct") or 0.0,
            reverse=True,
        )[:45]
        by_drop = sorted(
            [s for s in tradable if (s.get("volumeLots") or 0) >= 1000 and not s["isEtf"]],
            key=lambda x: x.get("changePct") or 0.0,
        )[:30]
        pool_codes: List[str] = []
        for c in CORE_WATCH_CODES + [s["code"] for s in by_turn] + [s["code"] for s in by_gain] + [s["code"] for s in by_drop]:
            if c in self.stocks and c not in pool_codes:
                pool_codes.append(c)
        pool_codes = pool_codes[:220]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda c: self.fetch_stock_kline(c, "1d"), pool_codes))
        self.evaluate_all_alerts(emit_sse=True)
        print(f"[INFO] Preloaded Lifeline 2.0 & proprietary R&D indicators for {len(pool_codes)} active Taiwan stocks.")

    # -------------------- Quant Multi-Factor Scoring & Alert Evaluation (v2.0) --------------------
    def _compute_quant_score(self, st: Dict[str, Any]) -> int:
        """計算 0–100 自研量化評分 2.0（聚焦 α-生命線位階、K棒收盤實體品質、主力口袋支點量比、三線結構與五檔近端壓差）"""
        score = 50.0
        pct = st.get("changePct") or 0.0
        vol = max(1, st.get("volumeLots") or 0)

        # 1. 相對加權指數超額動能 RS-Alpha (-14 ~ +16)
        taiex_pct = (self.indices.get("TAIEX") or {}).get("changePct") or 0.0
        rs_alpha = pct - taiex_pct
        score += max(-14.0, min(16.0, rs_alpha * 2.0))

        # 2. 自研【α-量價生命線 2.0】與乾坤三線位階 (-16 ~ +20)
        tech = st.get("tech") or {}
        price = st.get("price")
        lifeline = tech.get("lifeline")
        ll_sig = tech.get("lifelineSignal")
        ll_slope = tech.get("lifelineSlope") or 0.0
        if price and lifeline:
            if ll_sig in ("FLIP_BULL", "PULLBACK_HOLD"):
                score += 13.0
            elif price >= lifeline:
                score += 8.0 if ll_slope > 0 else 4.0
            elif ll_sig == "BREAKDOWN":
                score -= 14.0
            else:
                score -= 6.0
        if tech.get("ribbonBreakout"):
            score += 7.0

        # 3. 主力口袋支點量比與 K 棒實體結構 (-8 ~ +12)
        pp_ratio = tech.get("pocketPivotRatio")
        close_pos = tech.get("closePos")
        if pp_ratio is not None and pct > 0:
            if pp_ratio >= 1.5:
                score += 8.0
            elif pp_ratio >= 1.15:
                score += 4.0
        if close_pos is not None:
            if close_pos >= 0.70 and pct > 0:
                score += 4.0
            elif close_pos <= 0.25 and pct < -1.5:
                score -= 5.0

        # 4. 盤口近端加權巨鯨買壓 (-6 ~ +8)
        w_bid = st.get("weightedBidRatio") or st.get("bidRatio")
        if w_bid is not None:
            score += max(-6.0, min(8.0, (w_bid - 50.0) * 0.28))

        return int(max(5, min(99, round(score))))

    def _evaluate_single_stock(self, st: Dict[str, Any], rules_by_id: Dict[str, Dict[str, Any]], custom_by_code: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        code = st["code"]
        price = st.get("price")
        if price is None or price <= 0:
            return []

        pct = st.get("changePct") or 0.0
        vol = st.get("volumeLots") or 0
        open_p = st.get("open")
        low_p = st.get("low")
        high_p = st.get("high")
        w_bid = st.get("weightedBidRatio") if st.get("weightedBidRatio") is not None else st.get("bidRatio")
        bid_lots = st.get("bidTotalLots") or 0
        tech = st.get("tech") or {}
        now_str = taipei_now().strftime("%H:%M:%S")
        taiex_pct = (self.indices.get("TAIEX") or {}).get("changePct") or 0.0
        rs_alpha = round(pct - taiex_pct, 2)

        # 即時 K 棒實體收盤位階與上下影線佔比（過濾隔日沖避雷針假突破與假跌破）
        rng_live = max(0.01, (high_p or price) - (low_p or price))
        close_pos_live = ((price - (low_p or price)) / rng_live) if (high_p and low_p and high_p > low_p) else (tech.get("closePos") if tech.get("closePos") is not None else 0.5)
        upper_wick_live = (((high_p or price) - max(open_p or price, price)) / rng_live * 100.0) if (high_p and low_p and high_p > low_p) else (tech.get("upperWickPct") or 0.0)
        wick_dn_live = (((min(open_p or price, price) - (low_p or price)) / rng_live) * 100.0) if (high_p and low_p and high_p > low_p) else (tech.get("lowerWickPct") or 0.0)

        triggered: List[Dict[str, Any]] = []

        # 自研戰法 1: 【α-生命線 2.0】實體躍線翻多＆縮量回踩守穩
        r_ll = rules_by_id.get("alpha_lifeline")
        lifeline = tech.get("lifeline")
        ll_slope = tech.get("lifelineSlope")
        vr20 = tech.get("volRatio20") or 1.0
        ll_bias_live = round(((price - lifeline) / lifeline) * 100.0, 2) if (lifeline and lifeline > 0) else None
        if r_ll and r_ll["enabled"] and lifeline and ll_bias_live is not None and vol >= 600:
            max_pb = float(r_ll["params"]["maxPullbackBias"]["value"])
            min_flip = float(r_ll["params"]["minFlipPct"]["value"])
            prev_c = st.get("prevClose") or price

            is_flip_bull = (
                prev_c <= lifeline * 1.002
                and price > lifeline * 1.004
                and pct >= min_flip
                and (open_p is None or price > open_p)
                and close_pos_live >= 0.58
                and upper_wick_live <= 36.0
                and vr20 >= 1.15
            )
            is_pullback_hold = (
                (ll_slope is not None and ll_slope >= 0.05)
                and (-0.3 <= ll_bias_live <= max_pb)
                and (low_p is not None and low_p <= lifeline * (1.0 + max_pb / 100.0))
                and (price >= lifeline)
                and close_pos_live >= 0.52
                and vr20 <= 1.35
                and ((open_p is not None and price >= open_p) or wick_dn_live >= 38.0)
            )
            if is_flip_bull or is_pullback_hold:
                mode_tag = "★買｜帶量躍線翻多" if is_flip_bull else "★買｜上揚縮量守線"
                sub_note = f"量比 {vr20:.1f}x 收盤飽滿" if is_flip_bull else f"量比 {vr20:.1f}x 洗盤守穩"
                triggered.append({
                    "ruleId": "alpha_lifeline",
                    "category": "LIFELINE",
                    "badge": r_ll["badge"],
                    "title": f"{mode_tag}（生命線 {lifeline} 元）",
                    "reason": (
                        f"{mode_tag}！現價 {price} 元站穩自研生命線 {lifeline} 元"
                        f"（乖離 {ll_bias_live:+.2f}% · {sub_note} · 漲幅 {pct:+.2f}%）"
                    ),
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 自研戰法 2: 【乾坤三線麻花】連3日波動壓縮旱地拔蔥
        r_sq = rules_by_id.get("ribbon_squeeze")
        if r_sq and r_sq["enabled"] and vol >= 600:
            max_sp = float(r_sq["params"]["maxSpreadPct"]["value"])
            min_bp = float(r_sq["params"]["minBreakPct"]["value"])
            sp_prev = tech.get("ribbonSpreadPrev")
            ema5_v = tech.get("ema5")
            ema10_v = tech.get("ema10")
            if (
                sp_prev is not None and sp_prev <= max_sp
                and lifeline and ema5_v and ema10_v
                and price > max(lifeline, ema5_v, ema10_v)
                and pct >= min_bp
                and close_pos_live >= 0.62
                and upper_wick_live <= 35.0
                and vr20 >= 1.25
                and (open_p is None or price > open_p)
            ):
                triggered.append({
                    "ruleId": "ribbon_squeeze",
                    "category": "BULL",
                    "badge": r_sq["badge"],
                    "title": r_sq["name"],
                    "reason": f"前3日三線麻花收斂僅 {sp_prev:.2f}%，今出量 {vr20:.1f}x 拉實體紅K {pct:+.2f}% 貫穿三線起漲",
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })
            elif not tech and pct >= max(6.0, min_bp) and vol >= 1800 and close_pos_live >= 0.70 and (open_p is not None and price > open_p):
                triggered.append({
                    "ruleId": "ribbon_squeeze",
                    "category": "BULL",
                    "badge": "🔥 旱地拔蔥",
                    "title": "全市場強勢實體長紅突破",
                    "reason": f"強攻 {pct:+.2f}% 收高檔飽滿長紅（開 {open_p} → 現 {price}），成交量放大至 {vol:,} 張",
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 自研戰法 3: 【主力口袋支點】勝10日最大黑K量＋箱頂創高戰法
        r_moat = rules_by_id.get("smart_moat_lock")
        if r_moat and r_moat["enabled"] and vol >= 800:
            p_params = r_moat.get("params") or {}
            min_pp = float((p_params.get("minPocketRatio") or p_params.get("minLockRate") or {"value": 1.3})["value"])
            min_alpha = float((p_params.get("minAlphaPct") or {"value": 2.0})["value"])
            pp_ratio = tech.get("pocketPivotRatio")
            brk_10d = tech.get("breakout10d")
            if (
                pp_ratio is not None
                and pp_ratio >= min_pp
                and brk_10d
                and lifeline is not None
                and price > lifeline
                and pct >= 2.0
                and close_pos_live >= 0.62
                and rs_alpha >= min_alpha
            ):
                triggered.append({
                    "ruleId": "smart_moat_lock",
                    "category": "CHIP",
                    "badge": r_moat["badge"],
                    "title": r_moat["name"],
                    "reason": (
                        f"主力口袋支點點火！創10日收盤新高且量勝最大黑K量 {pp_ratio:.2f} 倍，"
                        f"領先大盤 Alpha {rs_alpha:+.2f}%"
                    ),
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 自研戰法 4: 【九轉極值神針】恐慌衰竭＋下影承接抄底戰法
        r_nine = rules_by_id.get("nine_turn_bottom")
        if r_nine and r_nine["enabled"] and tech and vol >= 500:
            min_td = int(r_nine["params"]["minTdDown"]["value"])
            min_wick = float(r_nine["params"]["minWickPct"]["value"])
            td_c = tech.get("tdCount") or 0
            rsi_v = tech.get("rsi14")
            atr_p = tech.get("atrPct") or 2.5

            exhaustion_hit = (td_c <= -min_td) or (
                ll_bias_live is not None
                and ll_bias_live <= -max(7.2, atr_p * 2.4)
                and rsi_v is not None
                and rsi_v <= 31.0
            )
            reversal_candle = (wick_dn_live >= min_wick) or (open_p is not None and price > open_p and close_pos_live >= 0.62 and pct > 0.5)
            if exhaustion_hit and reversal_candle:
                td_txt = f"空方九轉第 {abs(td_c)} 轉" if td_c <= -min_td else f"生命線極限負乖離 {ll_bias_live:+.1f}%"
                wick_txt = f"神針下影線佔比 {wick_dn_live:.0f}%" if wick_dn_live >= min_wick else f"低檔紅K吞噬 {pct:+.2f}%"
                triggered.append({
                    "ruleId": "nine_turn_bottom",
                    "category": "TECH",
                    "badge": r_nine["badge"],
                    "title": r_nine["name"],
                    "reason": f"{td_txt} · {wick_txt}（RSI={rsi_v or '--'}），恐慌賣壓衰竭浮現低接買盤",
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 自研戰法 5: 【盤口巨鯨點火】近端加權五檔買壓＋紅K攻擊
        r_whale = rules_by_id.get("whale_orderbook")
        if r_whale and r_whale["enabled"] and w_bid is not None:
            min_wb = float(r_whale["params"]["minWeightedBid"]["value"])
            min_bl = float(r_whale["params"]["minBidLots"]["value"])
            bullish_intraday = (pct >= 0.0) or (open_p is not None and price >= open_p)
            if w_bid >= min_wb and bid_lots >= min_bl and bullish_intraday:
                triggered.append({
                    "ruleId": "whale_orderbook",
                    "category": "MICRO",
                    "badge": r_whale["badge"],
                    "title": r_whale["name"],
                    "reason": f"近端加權買壓高達 {w_bid:.1f}% 且股價紅盤強勢（五檔委買 {bid_lots:,} 張 vs 委賣 {st.get('askTotalLots', 0):,} 張）",
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 自研戰法 6: 【破線／高九警報】實質失守生命線＆高檔結帳風控
        r_brk = rules_by_id.get("lifeline_breakdown")
        if r_brk and r_brk["enabled"]:
            brk_bias = float(r_brk["params"]["breakBiasPct"]["value"])
            crash_pct = float(r_brk["params"]["crashPct"]["value"])
            prev_c = st.get("prevClose") or price
            td_c = tech.get("tdCount") or 0

            broke_lifeline = (
                lifeline is not None
                and ll_bias_live is not None
                and prev_c >= lifeline * 0.998
                and ll_bias_live <= brk_bias
                and pct <= -2.6
                and close_pos_live <= 0.42
                and vol >= 1000
            )
            top_exhaustion = (
                lifeline is not None
                and ll_bias_live is not None
                and td_c >= 8
                and ll_bias_live >= 7.0
                and upper_wick_live >= 42.0
                and vr20 >= 1.3
                and vol >= 1500
            )
            heavy_crash = (pct <= crash_pct and close_pos_live <= 0.35 and vol >= 3000)
            if broke_lifeline or top_exhaustion or heavy_crash:
                if broke_lifeline:
                    title_b = f"▼ 帶量長黑實質跌破生命線 ({lifeline} 元)"
                    reason_b = f"現價 {price} 元收低失守生命線 {lifeline} 元（負乖離 {ll_bias_live:+.2f}% · 跌幅 {pct:+.2f}%），嚴格執行破線停損"
                elif top_exhaustion:
                    title_b = f"▼ 高檔九轉第 +{td_c} 轉爆量長上影警訊"
                    reason_b = f"正乖離過熱達 {ll_bias_live:+.1f}% 且拉出 {upper_wick_live:.0f}% 長上影線（量比 {vr20:.1f}x），提防主力高檔結帳"
                else:
                    title_b = "❄️ 單日爆量長黑重挫風控警報"
                    reason_b = f"帶量 {vol:,} 張重挫 {pct:+.2f}% 收於當日低檔（現價 {price} 元），注意流動性與破位風險"
                triggered.append({
                    "ruleId": "lifeline_breakdown",
                    "category": "RISK",
                    "badge": r_brk["badge"],
                    "title": title_b,
                    "reason": reason_b,
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        # 策略 7: 使用者自訂目標價／生命線防守價警示
        for ca in custom_by_code.get(code, []):
            op = ca.get("operator", ">=")
            target = float(ca.get("targetPrice", 0))
            note = ca.get("note") or ("突破目標價" if op == ">=" else "跌破防守價")
            hit = (price >= target) if op == ">=" else (price <= target)
            if hit:
                triggered.append({
                    "ruleId": f"custom_{ca['id']}",
                    "category": "CUSTOM",
                    "badge": "🎯 自訂達標",
                    "title": f"自訂價位觸發：{note}",
                    "reason": f"現價 {price} 元 已達成設定條件（{op} {target} 元）",
                    "code": code,
                    "name": st["name"],
                    "marketLabel": st["marketLabel"],
                    "price": price,
                    "changePct": pct,
                    "quantScore": st["quantScore"],
                    "triggeredAt": now_str,
                })

        return triggered

    def evaluate_all_alerts(self, emit_sse: bool = False) -> List[Dict[str, Any]]:
        new_events: List[Dict[str, Any]] = []
        with self.lock:
            rules_by_id = {r["id"]: r for r in self.rules}
            custom_by_code: Dict[str, List[Dict[str, Any]]] = {}
            for ca in self.custom_price_alerts:
                custom_by_code.setdefault(ca["code"], []).append(ca)

            new_map: Dict[str, List[Dict[str, Any]]] = {}
            all_current: List[Dict[str, Any]] = []

            for code, st in self.stocks.items():
                res = self._evaluate_single_stock(st, rules_by_id, custom_by_code)
                if res:
                    new_map[code] = res
                    all_current.extend(res)
                    for ev in res:
                        dedup_key = f"{code}:{ev['ruleId']}"
                        if dedup_key not in self.seen_alert_keys:
                            self.seen_alert_keys.add(dedup_key)
                            new_events.append(ev)

            self.alerts_map = new_map
            # 排序：自訂達標 > 生命線戰法 > 三線拔蔥 > 主力鎖碼 > 九轉神針 > 巨鯨掃貨 > 破線警報
            cat_priority = {"CUSTOM": 0, "LIFELINE": 1, "BULL": 2, "CHIP": 3, "TECH": 4, "MICRO": 5, "RISK": 6}
            all_current.sort(
                key=lambda x: (
                    cat_priority.get(x["category"], 9),
                    -(x.get("quantScore") or 0),
                    -abs(x.get("changePct") or 0.0),
                )
            )
            self.recent_alert_events = all_current[:250]

        if emit_sse:
            with self.lock:
                rule_counts = {r["id"]: 0 for r in self.rules}
                stock_badges = {}
                for code_k, lst in self.alerts_map.items():
                    stock_badges[code_k] = [a["badge"] for a in lst[:3]]
                    for a in lst:
                        if a["ruleId"] in rule_counts:
                            rule_counts[a["ruleId"]] += 1
            self.broadcast(
                "alerts_sync",
                {
                    "alerts": self.recent_alert_events,
                    "totalAlerts": sum(len(v) for v in self.alerts_map.values()),
                    "ruleHitCounts": rule_counts,
                    "stockBadges": stock_badges,
                    "newAlerts": new_events[:8],
                },
            )
        return new_events

    def evaluate_codes_alerts(self, codes: List[str]) -> List[Dict[str, Any]]:
        if not codes:
            return []
        new_events: List[Dict[str, Any]] = []
        with self.lock:
            rules_by_id = {r["id"]: r for r in self.rules}
            custom_by_code: Dict[str, List[Dict[str, Any]]] = {}
            for ca in self.custom_price_alerts:
                custom_by_code.setdefault(ca["code"], []).append(ca)

            for code in codes:
                st = self.stocks.get(code)
                if not st:
                    continue
                res = self._evaluate_single_stock(st, rules_by_id, custom_by_code)
                if res:
                    self.alerts_map[code] = res
                    for ev in res:
                        dedup_key = f"{code}:{ev['ruleId']}"
                        if dedup_key not in self.seen_alert_keys:
                            self.seen_alert_keys.add(dedup_key)
                            new_events.append(ev)
                else:
                    self.alerts_map.pop(code, None)

            # 更新 recent_alert_events
            all_current: List[Dict[str, Any]] = []
            for lst in self.alerts_map.values():
                all_current.extend(lst)
            cat_priority = {"CUSTOM": 0, "LIFELINE": 1, "BULL": 2, "CHIP": 3, "TECH": 4, "MICRO": 5, "RISK": 6}
            all_current.sort(
                key=lambda x: (
                    cat_priority.get(x["category"], 9),
                    -(x.get("quantScore") or 0),
                    -abs(x.get("changePct") or 0.0),
                )
            )
            self.recent_alert_events = all_current[:250]

        return new_events

    # -------------------- Live Tick Stress-Test Simulator (盤後測試用) --------------------
    def step_simulation_ticks(self) -> None:
        """在非開盤時段若使用者開啟「模擬盤中跳動測試」，依台股真實升降單位模擬即時撮合與五檔變化"""
        if not self.sim_mode:
            return
        updated: List[Dict[str, Any]] = []
        now_str = taipei_now().strftime("%H:%M:%S")

        with self.lock:
            pool = list(self.focus_codes[:6])
            candidates = [c for c in self.hot_codes if c not in pool]
            if candidates:
                pool.extend(random.sample(candidates, min(16, len(candidates))))
            # 也納入自訂警示股讓使用者容易驗證觸發
            for ca in self.custom_price_alerts:
                if ca["code"] in self.stocks and ca["code"] not in pool:
                    pool.append(ca["code"])

            for code in pool:
                st = self.stocks.get(code)
                if not st or not st.get("price") or not st.get("prevClose"):
                    continue
                cur = float(st["price"])
                prev = float(st["prevClose"])
                tick = tw_tick_size(cur)

                # 隨機微幅跳動 -2 ~ +3 個 tick（略偏多方動能方便觀察各類突破與五檔失衡訊號）
                step = random.choice([-2, -1, -1, 0, 1, 1, 2, 3])
                nxt = round_tw_price(cur + step * tick)
                lu, ld = st.get("limitUp"), st.get("limitDown")
                if lu and nxt > lu:
                    nxt = lu
                if ld and nxt < ld:
                    nxt = ld

                tick_lots = random.randint(3, 180)
                st["price"] = nxt
                st["change"] = round(nxt - prev, 2)
                st["changePct"] = round((st["change"] / prev) * 100.0, 2)
                st["high"] = max(st.get("high") or nxt, nxt)
                st["low"] = min(st.get("low") or nxt, nxt)
                st["volumeLots"] = int((st.get("volumeLots") or 0) + tick_lots)
                st["lastTickLots"] = tick_lots
                st["updatedAt"] = now_str

                # 動態建立逼真五檔報價
                bids = []
                asks = []
                for lvl in range(1, 6):
                    bp = round_tw_price(max(ld or 0.01, nxt - (lvl - 1) * tick))
                    ap = round_tw_price(min(lu or (nxt * 2), nxt + lvl * tick))
                    bl = random.randint(40, 420) * (2 if step >= 2 else 1)
                    al = random.randint(40, 380) * (2 if step <= -2 else 1)
                    bids.append({"price": bp, "lots": bl})
                    asks.append({"price": ap, "lots": al})

                bid_sum = sum(x["lots"] for x in bids)
                ask_sum = sum(x["lots"] for x in asks)
                ob_weights = [1.6, 1.3, 1.0, 0.7, 0.5]
                w_bid = sum(bids[i]["lots"] * ob_weights[i] for i in range(len(bids)))
                w_ask = sum(asks[i]["lots"] * ob_weights[i] for i in range(len(asks)))
                st["orderbook"] = {"bids": bids, "asks": asks}
                st["bidPrice"] = bids[0]["price"]
                st["askPrice"] = asks[0]["price"]
                st["bidTotalLots"] = bid_sum
                st["askTotalLots"] = ask_sum
                st["bidRatio"] = round((bid_sum / max(1, bid_sum + ask_sum)) * 100.0, 1)
                st["weightedBidRatio"] = round((w_bid / max(1.0, w_bid + w_ask)) * 100.0, 1)
                st["quantScore"] = self._compute_quant_score(st)
                updated.append(st)

            # 微幅同步跳動加權指數
            taiex = self.indices.get("TAIEX")
            if taiex and taiex.get("price") and taiex.get("prevClose"):
                delta = round(random.uniform(-18.0, 22.0), 2)
                np_idx = round(taiex["price"] + delta, 2)
                taiex["price"] = np_idx
                taiex["change"] = round(np_idx - taiex["prevClose"], 2)
                taiex["changePct"] = round((taiex["change"] / taiex["prevClose"]) * 100.0, 2)
                taiex["updatedAt"] = now_str

        new_events = self.evaluate_codes_alerts([s["code"] for s in updated])
        self.broadcast(
            "tick",
            {
                "mode": "SIM",
                "marketOpen": is_tw_market_open(),
                "lastQuoteSync": now_str,
                "indices": self.indices,
                "stocks": [self._compact_stock(s) for s in updated],
                "newAlerts": new_events[:5],
            },
        )

    def set_sim_mode(self, enabled: bool) -> None:
        with self.lock:
            self.sim_mode = bool(enabled)
            if not self.sim_mode:
                # 還原真實收盤／即時報價快照
                self.stocks = json.loads(json.dumps(self.base_snapshot))
                self.seen_alert_keys.clear()
        if not enabled:
            self.refresh_indices_and_mis_batch(codes_override=self.focus_codes[:10])
        self.evaluate_all_alerts(emit_sse=True)

    # -------------------- Compact Serialization --------------------
    def _compact_stock(self, st: Dict[str, Any]) -> Dict[str, Any]:
        alerts = self.alerts_map.get(st["code"]) or []
        tech = st.get("tech") or {}
        return {
            "code": st["code"],
            "name": st["name"],
            "market": st["market"],
            "marketLabel": st["marketLabel"],
            "isEtf": st["isEtf"],
            "price": st["price"],
            "prevClose": st["prevClose"],
            "open": st["open"],
            "high": st["high"],
            "low": st["low"],
            "change": st["change"],
            "changePct": st["changePct"],
            "amplitude": st["amplitude"],
            "volumeLots": st["volumeLots"],
            "turnoverM": st["turnoverM"],
            "pe": st["pe"],
            "pb": st["pb"],
            "dividendYield": st["dividendYield"],
            "foreignNetLots": st["foreignNetLots"],
            "trustNetLots": st["trustNetLots"],
            "dealerNetLots": st["dealerNetLots"],
            "instTotalLots": st["instTotalLots"],
            "limitUp": st["limitUp"],
            "limitDown": st["limitDown"],
            "bidRatio": st["bidRatio"],
            "weightedBidRatio": st.get("weightedBidRatio"),
            "lifeline": tech.get("lifeline"),
            "lifelineBias": tech.get("lifelineBias"),
            "lifelineSignal": tech.get("lifelineSignal"),
            "pocketPivotRatio": tech.get("pocketPivotRatio"),
            "quantScore": st["quantScore"],
            "updatedAt": st["updatedAt"],
            "alertBadges": [a["badge"] for a in alerts[:3]],
            "alertCount": len(alerts),
        }

    def get_market_breadth(self) -> Dict[str, int]:
        up = down = flat = limit_up = limit_down = 0
        with self.lock:
            for st in self.stocks.values():
                p = st.get("price")
                chg = st.get("change") or 0.0
                pct = st.get("changePct") or 0.0
                if p is None:
                    continue
                if chg > 0:
                    up += 1
                    if (st.get("limitUp") and p >= st["limitUp"] - 1e-6) or pct >= 9.7:
                        limit_up += 1
                elif chg < 0:
                    down += 1
                    if (st.get("limitDown") and p <= st["limitDown"] + 1e-6) or pct <= -9.7:
                        limit_down += 1
                else:
                    flat += 1
        return {
            "total": len(self.stocks),
            "twseCount": sum(1 for s in self.stocks.values() if s["market"] == "TWSE"),
            "tpexCount": sum(1 for s in self.stocks.values() if s["market"] == "TPEX"),
            "esbCount": sum(1 for s in self.stocks.values() if s["market"] == "ESB"),
            "up": up,
            "down": down,
            "flat": flat,
            "limitUp": limit_up,
            "limitDown": limit_down,
        }

    def get_bootstrap_payload(self) -> Dict[str, Any]:
        with self.lock:
            all_list = [self._compact_stock(s) for s in self.stocks.values()]
            # 預設依成交額排序
            all_list.sort(key=lambda x: x.get("turnoverM") or 0.0, reverse=True)
            rule_counts: Dict[str, int] = {r["id"]: 0 for r in self.rules}
            for lst in self.alerts_map.values():
                for a in lst:
                    rid = a["ruleId"]
                    if rid in rule_counts:
                        rule_counts[rid] += 1

            return {
                "mode": "SIM" if self.sim_mode else "LIVE",
                "marketOpen": is_tw_market_open(),
                "dataDate": self.data_date,
                "lastFullSync": self.last_full_sync,
                "lastQuoteSync": self.last_quote_sync,
                "indices": self.indices,
                "breadth": self.get_market_breadth(),
                "rules": self.rules,
                "ruleHitCounts": rule_counts,
                "customPriceAlerts": self.custom_price_alerts,
                "alerts": self.recent_alert_events,
                "totalAlertCount": sum(len(v) for v in self.alerts_map.values()),
                "stocks": all_list,
            }

    def get_stock_detail(self, code: str, interval: str = "1d") -> Optional[Dict[str, Any]]:
        norm_q = normalize_search_text(code)
        with self.lock:
            resolved_code = norm_q if norm_q in self.stocks else None
            if not resolved_code and norm_q:
                # 支援直接傳入中文股名（例如手機直接輸入「台積電」或「臺積電」按搜尋）
                exact_name = [s["code"] for s in self.stocks.values() if normalize_search_text(s["name"]) == norm_q]
                if exact_name:
                    resolved_code = exact_name[0]
                else:
                    partial = [
                        s for s in self.stocks.values()
                        if norm_q in s["code"].upper() or norm_q in normalize_search_text(s["name"])
                    ]
                    if partial:
                        partial.sort(key=lambda x: -(x.get("turnoverM") or 0.0))
                        resolved_code = partial[0]["code"]
            if not resolved_code:
                return None
            code = resolved_code
            if code not in self.focus_codes:
                self.focus_codes.insert(0, code)
                self.focus_codes = self.focus_codes[:15]

        # 即時向證交所 MIS 拉取該個股最新撮合與五檔
        if not self.sim_mode:
            self.fetch_mis_quotes([code], include_indices=False)

        # 拉取 K 線與技術指標（若為 5m 也確保日K指標已計算）
        if interval == "5m":
            with self.lock:
                has_tech = bool(self.stocks[code].get("tech"))
            if not has_tech:
                self.fetch_stock_kline(code, "1d")
        kline = self.fetch_stock_kline(code, interval)
        self.evaluate_codes_alerts([code])

        with self.lock:
            st = json.loads(json.dumps(self.stocks[code]))
            st["alerts"] = self.alerts_map.get(code) or []
            st["kline"] = kline
        return st


ENGINE = MarketEngine()


def background_scheduler() -> None:
    """背景執行緒：每 5 秒同步 MIS 即時報價（或每 1.6 秒執行模擬盤中跳動），每 10 分鐘同步全市場收盤快照"""
    last_mis_ts = 0.0
    last_full_ts = time.time()
    while True:
        time.sleep(0.8)
        now = time.time()
        try:
            if ENGINE.sim_mode:
                if now - last_mis_ts >= 1.6:
                    last_mis_ts = now
                    ENGINE.step_simulation_ticks()
            else:
                # 開盤期間每 5 秒輪詢 MIS，盤後每 15 秒維持連線心跳與指數同步
                poll_sec = 5.0 if is_tw_market_open() else 15.0
                if now - last_mis_ts >= poll_sec:
                    last_mis_ts = now
                    ENGINE.refresh_indices_and_mis_batch()

            if is_tw_market_open() and (now - last_full_ts >= 600.0):
                last_full_ts = now
                ENGINE.load_all_taiwan_stocks()
        except Exception as exc:
            print(f"[WARN] Scheduler loop error: {exc}", file=sys.stderr)


class QuantHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 保持終端輸出簡潔
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        import gzip
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        accept_enc = self.headers.get("Accept-Encoding") or ""
        use_gzip = len(raw) > 1024 and ("gzip" in accept_enc.lower())
        if use_gzip:
            raw = gzip.compress(raw, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "", "/healthz", "/api/bootstrap"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        rel = path.lstrip("/")
        safe_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if safe_path.startswith(STATIC_DIR) and os.path.isfile(safe_path):
            self.send_response(200)
            self.send_header("Content-Length", str(os.path.getsize(safe_path)))
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            self._send_json({
                "ok": True,
                "ready": ENGINE.ready_event.is_set(),
                "stocks": len(ENGINE.stocks),
                "time": taipei_now().strftime("%H:%M:%S"),
            })
            return

        if path == "/api/bootstrap":
            if not ENGINE.ready_event.is_set():
                ENGINE.ready_event.wait(timeout=20.0)
            self._send_json(ENGINE.get_bootstrap_payload())
            return

        if path == "/api/mobile-info":
            import socket
            lan_ip = "127.0.0.1"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                lan_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass
            pub_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
            if not pub_url:
                fwd_proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
                host_hdr = (self.headers.get("Host") or "").strip()
                if fwd_proto == "https" and host_hdr and not host_hdr.startswith(("127.", "localhost", "192.168.", "172.")):
                    pub_url = f"https://{host_hdr}"
            if not pub_url:
                tunnel_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel_url.txt")
                if os.path.isfile(tunnel_file):
                    try:
                        with open(tunnel_file, "r", encoding="utf-8") as tf:
                            pub_url = tf.read().strip()
                    except Exception:
                        pass
            lan_url = pub_url if os.environ.get("RENDER") else f"http://{lan_ip}:{PORT}"
            self._send_json({
                "lanIp": lan_ip,
                "lanUrl": lan_url,
                "publicUrl": pub_url,
                "port": PORT,
            })
            return

        if path.startswith("/api/stock/"):
            if not ENGINE.ready_event.is_set():
                ENGINE.ready_event.wait(timeout=20.0)
            code = urllib.parse.unquote(path[len("/api/stock/") :]).strip()
            interval = (qs.get("interval") or ["1d"])[0]
            if interval not in ("1d", "5m"):
                interval = "1d"
            detail = ENGINE.get_stock_detail(code, interval=interval)
            if not detail:
                self._send_json({"error": f"找不到股票代號或名稱：{code}"}, status=404)
                return
            self._send_json(detail)
            return

        if path == "/api/search":
            if not ENGINE.ready_event.is_set():
                ENGINE.ready_event.wait(timeout=20.0)
            raw_q = (qs.get("q") or [""])[0]
            q = normalize_search_text(raw_q)
            with ENGINE.lock:
                matches = []
                for s in ENGINE.stocks.values():
                    norm_name = normalize_search_text(s["name"])
                    norm_code = s["code"].upper()
                    if not q or q in norm_code or q in norm_name:
                        matches.append(ENGINE._compact_stock(s))
                matches.sort(
                    key=lambda x: (
                        0 if (x["code"].upper() == q or normalize_search_text(x["name"]) == q)
                        else (1 if x["code"].upper().startswith(q) or normalize_search_text(x["name"]).startswith(q) else 2),
                        -(x.get("turnoverM") or 0.0),
                    )
                )
            self._send_json({"results": matches[:30]})
            return

        if path == "/api/stream":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = ENGINE.register_sse()
            try:
                init_msg = f"event: connected\ndata: {json.dumps({'time': taipei_now().strftime('%H:%M:%S')})}\n\n"
                self.wfile.write(init_msg.encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15.0)
                    except queue.Empty:
                        msg = f"event: heartbeat\ndata: {json.dumps({'time': taipei_now().strftime('%H:%M:%S')})}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ENGINE.unregister_sse(q)
            return

        # Static files
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        safe_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not safe_path.startswith(STATIC_DIR) or not os.path.isfile(safe_path):
            self.send_error(HTTPStatus.NOT_FOUND, "File Not Found")
            return

        ext = os.path.splitext(safe_path)[1].lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(ext, "application/octet-stream")

        with open(safe_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json_body()
        except Exception:
            self._send_json({"error": "Invalid JSON"}, status=400)
            return

        if path == "/api/rules":
            # 更新策略開關與門檻參數
            updates = body.get("rules")
            reset = body.get("reset", False)
            with ENGINE.lock:
                if reset:
                    ENGINE.rules = json.loads(json.dumps(DEFAULT_QUANT_RULES))
                elif isinstance(updates, list):
                    upd_map = {r["id"]: r for r in updates if isinstance(r, dict) and "id" in r}
                    for rule in ENGINE.rules:
                        u = upd_map.get(rule["id"])
                        if not u:
                            continue
                        if "enabled" in u:
                            rule["enabled"] = bool(u["enabled"])
                        if isinstance(u.get("params"), dict):
                            for pk, pv in u["params"].items():
                                if pk in rule["params"]:
                                    val = to_float(pv.get("value") if isinstance(pv, dict) else pv)
                                    if val is not None:
                                        rule["params"][pk]["value"] = val
                ENGINE.seen_alert_keys.clear()

            ENGINE.evaluate_all_alerts(emit_sse=True)
            with ENGINE.lock:
                rule_counts = {r["id"]: 0 for r in ENGINE.rules}
                for lst in ENGINE.alerts_map.values():
                    for a in lst:
                        if a["ruleId"] in rule_counts:
                            rule_counts[a["ruleId"]] += 1
            self._send_json({
                "ok": True,
                "rules": ENGINE.rules,
                "ruleHitCounts": rule_counts,
                "alerts": ENGINE.recent_alert_events,
                "totalAlertCount": sum(len(v) for v in ENGINE.alerts_map.values()),
            })
            return

        if path == "/api/price-alerts":
            action = body.get("action", "add")
            with ENGINE.lock:
                if action == "delete":
                    aid = str(body.get("id") or "")
                    ENGINE.custom_price_alerts = [x for x in ENGINE.custom_price_alerts if x["id"] != aid]
                else:
                    code = str(body.get("code") or "").strip().upper()
                    if code not in ENGINE.stocks:
                        self._send_json({"error": f"查無股票代號 {code}"}, status=404)
                        return
                    target = to_float(body.get("targetPrice"))
                    if target is None or target <= 0:
                        self._send_json({"error": "請輸入有效的目標價格"}, status=400)
                        return
                    op = body.get("operator") if body.get("operator") in (">=", "<=") else ">="
                    note = str(body.get("note") or "").strip()
                    st = ENGINE.stocks[code]
                    new_item = {
                        "id": f"{int(time.time() * 1000)}_{random.randint(100, 999)}",
                        "code": code,
                        "name": st["name"],
                        "operator": op,
                        "targetPrice": round(target, 2),
                        "note": note,
                        "createdAt": taipei_now().strftime("%H:%M:%S"),
                    }
                    ENGINE.custom_price_alerts.insert(0, new_item)
            new_ev = ENGINE.evaluate_all_alerts(emit_sse=True)
            self._send_json({
                "ok": True,
                "customPriceAlerts": ENGINE.custom_price_alerts,
                "alerts": ENGINE.recent_alert_events,
                "newAlerts": new_ev,
            })
            return

        if path == "/api/sim/toggle":
            enabled = bool(body.get("enabled", not ENGINE.sim_mode))
            ENGINE.set_sim_mode(enabled)
            self._send_json(ENGINE.get_bootstrap_payload())
            return

        if path == "/api/refresh":
            ENGINE.load_all_taiwan_stocks()
            self._send_json(ENGINE.get_bootstrap_payload())
            return

        self._send_json({"error": "Not found"}, status=404)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _startup_pipeline() -> None:
    try:
        ENGINE.load_all_taiwan_stocks()
    except Exception as exc:
        print(f"[ERROR] Initial market load failed: {exc}", file=sys.stderr)
    finally:
        ENGINE.ready_event.set()
    threading.Thread(target=ENGINE.preload_core_technicals, daemon=True).start()
    background_scheduler()


def main() -> None:
    print("[INIT] Bootstrapping TW-Quant Alpha Market Engine...")
    server = ReusableThreadingHTTPServer(("0.0.0.0", PORT), QuantHTTPRequestHandler)
    print(f"[READY] TW-Quant Alpha listening on 0.0.0.0:{PORT} (Local: http://127.0.0.1:{PORT})")
    threading.Thread(target=_startup_pipeline, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down server.")
        server.server_close()


if __name__ == "__main__":
    main()
