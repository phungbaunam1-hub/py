# -*- coding: utf-8 -*-
"""
AI MT Trading  -  1 file duy nhất (Python + PyQt6)
=====================================================
Kết nối trực tiếp MT5 (gói MetaTrader5), phân tích 8 phương pháp kỹ thuật
(VSA, Price Action, ICT, Chart Pattern, Gann, Harmonic, Wyckoff, Elliott),
gửi ảnh chart + kết quả cho Gemini, gộp thành tín hiệu BUY/SELL/WAIT,
tuỳ chọn tự vào lệnh bằng chính MT5 API (không cần EA cầu nối).

Cài đặt (Windows, đã mở & đăng nhập MT5):
    pip install PyQt6 MetaTrader5 numpy requests
Chạy:
    python AI_MT_Trading.py
Đóng gói exe:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name AI_MT_Trading AI_MT_Trading.py

Nếu không có gói MetaTrader5 (Linux/Mac) -> tự chạy chế độ MÔ PHỎNG (dữ liệu giả).

Chart: lăn chuột = zoom, kéo = cuộn, kéo trục giá (bên phải) = co giãn giá, Shift+kéo = dịch giá,
       double-click = về tự động, phím ←/→ cuộn, +/- zoom, Home = về nến mới nhất.
Panel (dock bên phải, kéo ra ngoài được): lot thủ công/%/$, vào lệnh, đóng/trim/BE/Reverse, chỉnh SL/TP,
       tự động hoá (BE, cắt RR, trailing, auto SL, partial TP), quản lý vốn, điều khiển AI/Auto Trade.
Chat AI (dock bên phải): hỏi Gemini / ChatGPT / Claude / DeepSeek / dịch vụ OpenAI-compatible, tự đính kèm
       dữ liệu phân tích, có các prompt dựng sẵn và tuỳ chọn tự gửi sau mỗi lần phân tích.
Gộp MT5 (Windows, thử nghiệm): combo "Gộp" trên thanh công cụ.
Cấu hình: ai_mt_trading_config.json cạnh file chạy (hoặc đặt biến môi trường AIMT_CONFIG).
CẢNH BÁO: tín hiệu chỉ là kết quả thuật toán, không đảm bảo lợi nhuận.
Hãy chạy tài khoản DEMO / cent với lot nhỏ trước khi bật Auto Trade.
"""
import sys, os, re, json, time, math, base64, threading, traceback, webbrowser, copy, contextlib, html, multiprocessing, atexit
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field

import numpy as np
import requests

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except Exception:
    mt5 = None
    HAS_MT5 = False

from PyQt6 import QtCore, QtGui, QtWidgets
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    HAS_WEB = True
except Exception:
    HAS_WEB = False

APP_NAME = "AI MT Trading"
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("AIMT_CONFIG") or os.path.join(BASE_DIR, "ai_mt_trading_config.json")

# ============================================================ CẤU HÌNH
RISK = {
    "An toàn":  dict(risk=0.5, thr=0.40, sl=2.0, rr=1.5, max_pos=1),
    "Cân bằng": dict(risk=1.0, thr=0.28, sl=1.5, rr=2.0, max_pos=2),
    "Tấn công": dict(risk=2.0, thr=0.18, sl=1.2, rr=2.5, max_pos=3),
}
PRESETS = {
    "Tổng hợp":       dict(VSA=1.0, PA=1.2, ICT=1.3, CHART=0.9, GANN=0.5, HARM=0.9, WYCK=1.1, ELL=0.8),
    "Theo xu hướng":  dict(VSA=0.8, PA=1.5, ICT=1.2, CHART=0.7, GANN=0.4, HARM=0.4, WYCK=0.8, ELL=1.5),
    "Đảo chiều":      dict(VSA=1.4, PA=0.9, ICT=1.2, CHART=1.2, GANN=0.6, HARM=1.5, WYCK=1.4, ELL=0.6),
    "Price Action":   dict(VSA=1.0, PA=2.0, ICT=1.6, CHART=0.8, GANN=0.2, HARM=0.2, WYCK=0.8, ELL=0.4),
}
TF_MIN = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

PROVIDERS = {
    "gemini":    dict(label="Gemini (Google)", vision=True),
    "openai":    dict(label="ChatGPT (OpenAI)", vision=True, base="https://api.openai.com/v1", model="gpt-4o"),
    "anthropic": dict(label="Claude (Anthropic)", vision=True, base="https://api.anthropic.com", model="claude-sonnet-5-5"),
    "deepseek":  dict(label="DeepSeek", vision=False, base="https://api.deepseek.com", model="deepseek-chat"),
    "custom":    dict(label="Tuỳ chỉnh (OpenAI-compatible)", vision=False, base="", model=""),
}

DEFAULT_CFG = dict(
    symbol="XAUUSD", tf="M1", bars=300,
    gemini_key="", gemini_model="gemini-2.5-flash", gemini_fallbacks=["gemini-2.5-flash-lite"], use_ai=True, ai_weight=2.0,
    interval=60, risk_mode="Cân bằng", risk_pct=0.0,   # 0 = dùng % theo chế độ
    preset="Tổng hợp", magic=8800123, deviation=30,
    auto_trade=False, be_enabled=True, be_trigger=1.0,
    cooldown_min=5, max_spread=0, max_lot=0.0, hour_from=0, hour_to=24,
    mt5_path="", force_demo=False, exec_plan="BEST", pending_ttl_min=15,
    lot_mode="risk_pct", lot_fixed=0.01, risk_usd=10.0, panel_scope="AIMT", panel_magic=777,
    ai_levels="off", ai_adjust=False, chart_bars=1500, dock_mode=0, pa={},
    thr_override=0.0, side_mode="off", side_lookback=60, adx_thr=20.0, er_thr=0.30, drift_thr=0.40, side_votes=2, side_edge=0.25, side_rsi=True,
    copy_magic=8800124, theme="bloomberg", hotkeys=False, panel_target="VIEW",
    ai_provider="gemini", chat_provider="gemini", ai_min_interval=120, chat_auto=False,
    chat_ctx=True, chat_img=False,
    llm={k: dict(key="", model=v.get("model", ""), base=v.get("base", "")) for k, v in PROVIDERS.items() if k != "gemini"},
    trail_enabled=False, trail_atr=1.0, trim_enabled=False, trim_R=1.0, trim_pct=50,
    dca_enabled=False, dca_step_atr=1.5, dca_max=3, dca_mult=1.0, dca_total_lot=0.0,
    accounts=[dict(name="TK chính", path="", login=0, password="", server="", symbol="",
                   enabled=True, auto=True, max_lot=0.0)],
    news_every=300,
    news_feeds=["https://www.fxstreet.com/rss/news",
                "https://www.investing.com/rss/news_11.rss"],
)

def load_cfg():
    c = copy.deepcopy(DEFAULT_CFG)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    if not c.get("accounts"):
        c["accounts"] = copy.deepcopy(DEFAULT_CFG["accounts"])
    pa = copy.deepcopy(PA_DEFAULT)
    if isinstance(c.get("pa"), dict): pa.update(c["pa"])
    c["pa"] = pa
    llm = copy.deepcopy(DEFAULT_CFG["llm"])
    for k, v in (c.get("llm") or {}).items():
        if k in llm and isinstance(v, dict): llm[k].update(v)
    c["llm"] = llm
    return c

def save_cfg(c):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ============================================================ DỮ LIỆU & CHỈ BÁO
class Bars:
    def __init__(self, rates):
        self.t = np.asarray(rates["time"], dtype=np.int64)
        self.o = np.asarray(rates["open"], dtype=float)
        self.h = np.asarray(rates["high"], dtype=float)
        self.l = np.asarray(rates["low"], dtype=float)
        self.c = np.asarray(rates["close"], dtype=float)
        self.v = np.asarray(rates["tick_volume"], dtype=float)
        self.n = len(self.t)

def sma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.convolve(x, np.ones(n) / n, mode="valid")
    return out

def atr_arr(b, n=14):
    pc = np.roll(b.c, 1); pc[0] = b.c[0]
    tr = np.maximum(b.h - b.l, np.maximum(np.abs(b.h - pc), np.abs(b.l - pc)))
    return sma(tr, n)

def zigzag(b, left=3, right=3, min_move=0.0):
    raw = []
    for i in range(left, b.n - right):
        hh = b.h[i - left:i + right + 1]; ll = b.l[i - left:i + right + 1]
        isH = b.h[i] >= hh.max(); isL = b.l[i] <= ll.min()
        if isH and not isL: raw.append((i, float(b.h[i]), "H"))
        elif isL and not isH: raw.append((i, float(b.l[i]), "L"))
    pts = []
    for i, p, k in raw:
        if pts and pts[-1][2] == k:
            if (k == "H" and p > pts[-1][1]) or (k == "L" and p < pts[-1][1]):
                pts[-1] = (i, p, k)
        else:
            if pts and abs(p - pts[-1][1]) < min_move:
                continue
            pts.append((i, p, k))
    return pts

@dataclass
class Res:
    key: str
    name: str
    sig: int = 0
    conf: float = 0.0
    state: str = "Trung lập"
    notes: list = field(default_factory=list)
    zones: list = field(default_factory=list)    # (nhãn, giá thấp, giá cao, màu)
    levels: list = field(default_factory=list)   # (nhãn, giá, màu)
    extra: dict = field(default_factory=dict)

def fin(r, pts):
    tot = sum(p[0] for p in pts)
    r.notes = [p[1] for p in pts]
    if abs(tot) >= 0.8:
        r.sig = 1 if tot > 0 else -1
        r.conf = min(90.0, 35 + abs(tot) * 18)
        r.state = ("MUA " if r.sig > 0 else "BÁN ") + f"{r.conf:.0f}%"
    else:
        r.sig, r.conf, r.state = 0, 0.0, "Trung lập"
    return r

GREEN, RED, BLUE, GOLD = "#37d67a", "#ff6b7a", "#7aa2ff", "#f5b942"

# ---------------------------------------------------------------- VSA
def an_vsa(b, A):
    r = Res("VSA", "VSA")
    if b.n < 40:
        r.state = "Thiếu dữ liệu"; return r
    sp = b.h - b.l
    asp, av, m20 = sma(sp, 20), sma(b.v, 20), sma(b.c, 20)
    i = b.n - 2
    s, v, a_s, a_v = sp[i], b.v[i], asp[i], av[i]
    if not (a_s > 0 and a_v > 0):
        r.state = "Thiếu dữ liệu"; return r
    cp = (b.c[i] - b.l[i]) / s if s > 0 else 0.5
    tr = b.c[i] - m20[i]
    up = b.c[i] >= b.o[i]
    lowvol = v < b.v[i - 1] and v < b.v[i - 2]
    hi10, lo10 = b.h[i - 10:i].max(), b.l[i - 10:i].min()
    pts = []
    if tr < 0 and v > 1.6 * a_v and cp >= 0.6:
        pts.append((1.5, "Stopping volume / Selling climax (vol lớn, đóng cửa cao)"))
    if tr > 0 and v > 1.6 * a_v and cp <= 0.4:
        pts.append((-1.5, "Buying climax (vol lớn, đóng cửa thấp)"))
    if b.c[i] > b.c[i - 1] and tr > 0 and s < 0.8 * a_s and lowvol and cp <= 0.5:
        pts.append((-1.0, "No Demand (đóng cửa tăng, spread hẹp, vol thấp hơn 2 nến trước, đóng nửa dưới)"))
    if b.c[i] < b.c[i - 1] and tr < 0 and s < 0.8 * a_s and lowvol and cp >= 0.5:
        pts.append((1.0, "No Supply (đóng cửa giảm, spread hẹp, vol thấp hơn 2 nến trước, đóng nửa trên)"))
    if b.h[i] > hi10 and cp < 0.3 and v > 1.2 * a_v:
        pts.append((-1.5, "Upthrust (quét đỉnh, đóng cửa yếu)"))
    if b.l[i] < lo10 and cp > 0.6:
        pts.append((1.2, "Spring/Test (quét đáy, đóng cửa cao)"))
    if v > 2 * a_v and s < 0.6 * a_s:
        pts.append((-0.8 if tr > 0 else 0.8, "Effort không có Result (vol cao, biên độ nhỏ)"))
    if up and v > 1.3 * a_v and cp > 0.7:
        pts.append((0.6, "Nến tăng có vol xác nhận"))
    if (not up) and v > 1.3 * a_v and cp < 0.3:
        pts.append((-0.6, "Nến giảm có vol xác nhận"))
    r.notes = []
    return fin(r, pts)

# ---------------------------------------------------------------- PRICE ACTION
def an_pa(b, A):
    r = Res("PA", "PRICE ACTION")
    if b.n < 40:
        r.state = "Thiếu dữ liệu"; return r
    i = b.n - 2
    pts = []
    zz = zigzag(b, 3, 3, 0.8 * A)
    hs = [p for _, p, k in zz if k == "H"]
    ls = [p for _, p, k in zz if k == "L"]
    if len(hs) >= 2 and len(ls) >= 2:
        if hs[-1] > hs[-2] and ls[-1] > ls[-2]:
            pts.append((1.0, "Cấu trúc HH-HL (xu hướng tăng)"))
        elif hs[-1] < hs[-2] and ls[-1] < ls[-2]:
            pts.append((-1.0, "Cấu trúc LH-LL (xu hướng giảm)"))
    if hs: r.levels.append(("Kháng cự gần", hs[-1], RED))
    if ls: r.levels.append(("Hỗ trợ gần", ls[-1], GREEN))
    if hs and b.c[i] > hs[-1] and b.c[i - 1] <= hs[-1]:
        pts.append((0.9, "BOS: đóng cửa phá đỉnh swing"))
    if ls and b.c[i] < ls[-1] and b.c[i - 1] >= ls[-1]:
        pts.append((-0.9, "BOS: đóng cửa phá đáy swing"))
    body = abs(b.c[i] - b.o[i]); rng = max(b.h[i] - b.l[i], 1e-9)
    uw = b.h[i] - max(b.c[i], b.o[i]); lw = min(b.c[i], b.o[i]) - b.l[i]
    avgb = float(np.mean(np.abs(b.c - b.o)[i - 20:i]))
    nearS = bool(ls) and abs(b.l[i] - ls[-1]) < 0.5 * A
    nearR = bool(hs) and abs(b.h[i] - hs[-1]) < 0.5 * A
    if lw > 2 * body and lw > 0.6 * rng:
        pts.append((1.2 if nearS else 0.6, "Pin bar tăng (bóng dưới dài)"))
    if uw > 2 * body and uw > 0.6 * rng:
        pts.append((-1.2 if nearR else -0.6, "Pin bar giảm (bóng trên dài)"))
    if b.c[i] > b.o[i] and b.c[i - 1] < b.o[i - 1] and b.c[i] >= b.o[i - 1] \
            and b.o[i] <= b.c[i - 1] and body > avgb:
        pts.append((1.0, "Bullish engulfing"))
    if b.c[i] < b.o[i] and b.c[i - 1] > b.o[i - 1] and b.c[i] <= b.o[i - 1] \
            and b.o[i] >= b.c[i - 1] and body > avgb:
        pts.append((-1.0, "Bearish engulfing"))
    return fin(r, pts)

# ---------------------------------------------------------------- ICT POWER OF THREE (port từ ICT_POWER.mq5)
PO3_ATR, PO3_EXP, PO3_RR = 50, 0.5, 0.86

def rma_atr(b, n=PO3_ATR):
    pc = np.roll(b.c, 1)
    tr = np.maximum(b.h - b.l, np.maximum(np.abs(b.h - pc), np.abs(b.l - pc)))
    tr[0] = b.h[0] - b.l[0]
    out = np.full(b.n, np.nan)
    if b.n >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, b.n):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out

def po3_scan(b, algo="small", brk="wick", sl_mult=5.0):
    """Accumulation -> Manipulation -> Distribution. Chỉ dùng nến đã đóng (bỏ nến đang chạy)."""
    acc_len = 40 if algo == "small" else 11
    acc_mult = 2.0 if algo == "short" else 5.0
    man_mult = 0.6 if algo == "small" else 1.0
    atr = rma_atr(b)
    lst = []
    def new():
        return dict(state="WAIT_ACC", acc_top=None, acc_bot=None, acc_s=-1, acc_e=-1, end_top=None, end_bot=None,
                    man_dir=0, man_top=None, man_bot=None, man_s=-1, man_e=-1, side=0, entry=None, entry_i=-1,
                    entry_t=0, e_hi=None, e_lo=None, sl=None, tp=None, exit=None, exit_i=-1, res=None)
    for i in range(max(0, b.n - 4800), b.n - 1):
        if not lst or lst[-1]["exit"] is not None:
            lst.append(new())
        p = lst[-1]; a = atr[i]; atr_ok = a == a
        hl_ok = i >= acc_len - 1
        hh = ll = 0.0
        if hl_ok:
            hh = float(b.h[i - acc_len + 1:i + 1].max()); ll = float(b.l[i - acc_len + 1:i + 1].min())
        hi_b = float(b.h[i]) if brk == "wick" else float(b.c[i])
        lo_b = float(b.l[i]) if brk == "wick" else float(b.c[i])
        if p["state"] == "WAIT_ACC" and hl_ok and atr_ok and (hh - ll) <= a * acc_mult:
            p.update(state="WAIT_ACC_END", acc_s=i - acc_len, acc_e=i, acc_top=hh + a * PO3_EXP, acc_bot=ll - a * PO3_EXP)
        if p["state"] == "WAIT_ACC_END":
            if hi_b > p["acc_top"] or lo_b < p["acc_bot"]:
                p.update(state="WAIT_MANIP", end_top=hi_b, end_bot=lo_b)
            else:
                p["acc_e"] = i
        if p["state"] == "WAIT_MANIP" and i > p["acc_e"] and atr_ok:
            if hi_b > p["acc_top"] + a * man_mult:
                p.update(state="WAIT_DIST", man_dir=1, man_s=p["acc_e"], man_e=i, man_top=hi_b, man_bot=p["end_bot"])
            elif lo_b < p["acc_bot"] - a * man_mult:
                p.update(state="WAIT_DIST", man_dir=-1, man_s=p["acc_e"], man_e=i, man_top=p["end_top"], man_bot=lo_b)
        if p["state"] == "WAIT_DIST":
            e = float(b.c[i])
            p.update(state="ENTRY", entry_t=int(b.t[i]), entry_i=i, entry=e, e_hi=float(b.h[i]), e_lo=float(b.l[i]))
            if p["man_dir"] == -1:      # manipulation giảm -> LONG
                sl = e - a * sl_mult; p.update(side=1, sl=sl, tp=e + abs(e - sl) * PO3_RR)
            else:                       # manipulation tăng -> SHORT
                sl = e + a * sl_mult; p.update(side=-1, sl=sl, tp=e - abs(e - sl) * PO3_RR)
        if p["state"] == "ENTRY" and int(b.t[i]) > p["entry_t"]:
            hit_tp = (p["side"] == 1 and b.h[i] >= p["tp"]) or (p["side"] == -1 and b.l[i] <= p["tp"])
            hit_sl = (p["side"] == 1 and b.l[i] <= p["sl"]) or (p["side"] == -1 and b.h[i] >= p["sl"])
            if hit_sl:      # SL được xét sau TP nên thắng khi cả hai cùng chạm (đúng như bản gốc)
                p.update(state="SL", exit=p["sl"], exit_i=i, res="SL")
            elif hit_tp:
                p.update(state="TP", exit=p["tp"], exit_i=i, res="TP")
    return lst

# ---------------------------------------------------------------- ICT
def an_ict(b, A):
    r = Res("ICT", "ICT")
    if b.n < 60:
        r.state = "Thiếu dữ liệu"; return r
    P = b.c[-1]; n = b.n
    pts = []
    # Fair Value Gap gần nhất chưa lấp (mỗi chiều)
    fb = fs = False
    for j in range(n - 3, max(n - 70, 2), -1):
        if not fb and b.l[j] > b.h[j - 2] and (b.l[j] - b.h[j - 2]) > 0.15 * A \
                and abs(b.c[j - 1] - b.o[j - 1]) >= 0.5 * A and b.c[j - 1] > b.o[j - 1] \
                and b.l[j + 1:].min() > b.h[j - 2]:
            lo, hi = b.h[j - 2], b.l[j]; fb = True
            r.zones.append(("FVG+", lo, hi, GREEN))
            if lo - 0.3 * A <= P <= hi + 0.3 * A:
                pts.append((1.2, "Giá đang ở FVG tăng chưa lấp"))
        if not fs and b.h[j] < b.l[j - 2] and (b.l[j - 2] - b.h[j]) > 0.15 * A \
                and abs(b.c[j - 1] - b.o[j - 1]) >= 0.5 * A and b.c[j - 1] < b.o[j - 1] \
                and b.h[j + 1:].max() < b.l[j - 2]:
            lo, hi = b.h[j], b.l[j - 2]; fs = True
            r.zones.append(("FVG-", lo, hi, RED))
            if lo - 0.3 * A <= P <= hi + 0.3 * A:
                pts.append((-1.2, "Giá đang ở FVG giảm chưa lấp"))
        if fb and fs: break
    # Order Block (nến ngược chiều trước displacement)
    ob_b = ob_s = False
    for j in range(n - 5, max(n - 70, 2), -1):
        if not ob_b and b.c[j] < b.o[j] and b.c[j + 1:j + 4].max() - b.h[j] > 1.0 * A \
                and b.l[j + 1:].min() >= b.l[j]:
            ob_b = True
            r.zones.append(("OB+", b.l[j], b.h[j], GREEN))
            if b.l[j] - 0.2 * A <= P <= b.h[j] + 0.2 * A:
                pts.append((1.3, "Giá retest Order Block tăng"))
        if not ob_s and b.c[j] > b.o[j] and b.l[j] - b.c[j + 1:j + 4].min() > 1.0 * A \
                and b.h[j + 1:].max() <= b.h[j]:
            ob_s = True
            r.zones.append(("OB-", b.l[j], b.h[j], RED))
            if b.l[j] - 0.2 * A <= P <= b.h[j] + 0.2 * A:
                pts.append((-1.3, "Giá retest Order Block giảm"))
        if ob_b and ob_s: break
    # Liquidity sweep (3 nến đã đóng gần nhất)
    for k in range(n - 4, n - 1):
        ref_h, ref_l = b.h[k - 20:k].max(), b.l[k - 20:k].min()
        if b.h[k] > ref_h and b.c[k] < ref_h:
            pts.append((-1.4, "Quét thanh khoản đỉnh rồi đóng cửa dưới")); break
        if b.l[k] < ref_l and b.c[k] > ref_l:
            pts.append((1.4, "Quét thanh khoản đáy rồi đóng cửa trên")); break
    # Premium / Discount
    hi, lo = b.h[-60:].max(), b.l[-60:].min()
    eq = (hi + lo) / 2
    r.levels.append(("EQ 50%", eq, BLUE))
    if P < eq - 0.1 * (hi - lo):
        pts.append((0.5, "Vùng Discount (dưới 50% range 60 nến)"))
    elif P > eq + 0.1 * (hi - lo):
        pts.append((-0.5, "Vùng Premium (trên 50% range 60 nến)"))
    # ICT Power of Three (port từ ICT_POWER.mq5)
    stat = ""
    try:
        po = po3_scan(b)
        done = [x for x in po if x["res"]]
        w_ = sum(1 for x in done if x["res"] == "TP"); l_ = len(done) - w_
        stat = f"PO3 trên {b.n} nến đã tải: {w_} TP / {l_} SL" + (f" (winrate {100 * w_ / len(done):.0f}%)" if done else "")
        last = po[-1] if po else None
        if last:
            if last["state"] == "ENTRY" and last["exit"] is None and b.n - 1 - last["entry_i"] <= 12:
                pts.append((last["side"] * 1.6, f"PO3: setup {'BUY' if last['side'] > 0 else 'SELL'} đang hiệu lực "
                            f"(entry {last['entry']:.2f}, SL {last['sl']:.2f}, TP {last['tp']:.2f})"))
                r.extra["po3"] = last
                r.levels += [("PO3 SL", last["sl"], RED), ("PO3 TP", last["tp"], GREEN)]
            if last["state"] in ("WAIT_ACC_END", "WAIT_MANIP", "WAIT_DIST") and last["acc_top"] is not None:
                r.zones.append(("PO3 Accumulation", last["acc_bot"], last["acc_top"], GOLD))
    except Exception as ex:
        stat = f"PO3 lỗi: {ex}"
    res = fin(r, pts)
    if stat: res.notes.append(stat)
    return res

# ---------------------------------------------------------------- CHART PATTERN
def an_chart(b, A):
    r = Res("CHART", "CHART PATTERN")
    if b.n < 60:
        r.state = "Thiếu dữ liệu"; return r
    P = b.c[-2]; i = b.n - 2
    zz = zigzag(b, 4, 4, 1.5 * A)
    tol = max(1.0 * A, P * 0.0003)
    pts = []
    if len(zz) >= 3:
        a, m, c3 = zz[-3:]
        recent = b.n - 1 - c3[0] <= 15
        if a[2] == "H" and c3[2] == "H" and abs(a[1] - c3[1]) <= tol:
            if P < m[1]: pts.append((-1.6, "Double Top đã phá neckline"))
            elif recent and P < c3[1]: pts.append((-0.7, "Double Top đang hình thành"))
            r.levels.append(("Neckline DT", m[1], RED))
        if a[2] == "L" and c3[2] == "L" and abs(a[1] - c3[1]) <= tol:
            if P > m[1]: pts.append((1.6, "Double Bottom đã phá neckline"))
            elif recent and P > c3[1]: pts.append((0.7, "Double Bottom đang hình thành"))
            r.levels.append(("Neckline DB", m[1], GREEN))
    if len(zz) >= 5:
        t = zz[-5:]; kinds = "".join(x[2] for x in t); v = [x[1] for x in t]
        recent = b.n - 1 - t[-1][0] <= 15
        if kinds == "HLHLH" and v[2] > v[0] + 0.5 * A and v[2] > v[4] + 0.5 * A \
                and abs(v[0] - v[4]) <= 2 * tol:
            neck = (v[1] + v[3]) / 2
            r.levels.append(("Neckline H&S", neck, RED))
            if P < neck: pts.append((-1.8, "Head & Shoulders đã phá neckline"))
            elif recent: pts.append((-0.5, "Head & Shoulders đang hình thành"))
        if kinds == "LHLHL" and v[2] < v[0] - 0.5 * A and v[2] < v[4] - 0.5 * A \
                and abs(v[0] - v[4]) <= 2 * tol:
            neck = (v[1] + v[3]) / 2
            r.levels.append(("Neckline iH&S", neck, GREEN))
            if P > neck: pts.append((1.8, "Inverse H&S đã phá neckline"))
            elif recent: pts.append((0.5, "Inverse H&S đang hình thành"))
    hs = [(i_, p) for i_, p, k in zz if k == "H"][-3:]
    ls = [(i_, p) for i_, p, k in zz if k == "L"][-3:]
    if len(hs) >= 3 and len(ls) >= 3:
        sh = np.polyfit([x[0] for x in hs], [x[1] for x in hs], 1)
        sl_ = np.polyfit([x[0] for x in ls], [x[1] for x in ls], 1)
        up_line, lo_line = np.polyval(sh, i), np.polyval(sl_, i)
        fl = 0.03 * A
        if abs(sh[0]) <= fl and sl_[0] > fl:
            pts.append((1.4, "Tam giác tăng: phá cạnh trên") if P > up_line
                       else (0.4, "Tam giác tăng đang hình thành"))
        elif abs(sl_[0]) <= fl and sh[0] < -fl:
            pts.append((-1.4, "Tam giác giảm: phá cạnh dưới") if P < lo_line
                       else (-0.4, "Tam giác giảm đang hình thành"))
        elif sh[0] < -fl and sl_[0] > fl:
            if P > up_line: pts.append((1.2, "Tam giác cân: phá lên"))
            elif P < lo_line: pts.append((-1.2, "Tam giác cân: phá xuống"))
    return fin(r, pts)

# ---------------------------------------------------------------- GANN
def an_gann(b, A):
    r = Res("GANN", "GANN")
    if b.n < 60:
        r.state = "Thiếu dữ liệu"; return r
    P = b.c[-1]; i = b.n - 2
    zz0 = zigzag(b, 5, 5, 3 * A)
    anchor = zz0[-1][1] if zz0 else float(b.c[-60:].min())     # neo = swing lớn gần nhất
    root = math.sqrt(anchor)
    lv = sorted((root + k * 0.25) ** 2 for k in range(-24, 25))   # bước 45° quanh điểm neo
    below = [x for x in lv if x <= P]; above = [x for x in lv if x > P]
    pts = []
    if below and above:
        sup, res_ = below[-1], above[0]
        r.levels.append(("Gann S", sup, GREEN)); r.levels.append(("Gann R", res_, RED))
        prox = max(0.5 * A, 0.05 * (res_ - sup))
        if P - sup <= prox and b.c[i] > b.o[i]:
            pts.append((1.0, f"Giá bật từ mức Square of 9 {sup:.2f} (neo {anchor:.2f})"))
        if res_ - P <= prox and b.c[i] < b.o[i]:
            pts.append((-1.0, f"Giá bị chặn tại mức Square of 9 {res_:.2f} (neo {anchor:.2f})"))
    zz = zigzag(b, 5, 5, 3 * A)
    if zz:
        idx, p0, k = zz[-1]
        unit = 0.25 * A     # quy ước 1x1 = 0.25 ATR / nến (không phải chuẩn tuyệt đối)
        bars_since = i - idx
        if k == "L":
            line = p0 + unit * bars_since
            pts.append((0.5, "Giá trên đường Gann 1x1 tăng") if b.c[i] > line
                       else (-0.5, "Giá dưới đường Gann 1x1 tăng"))
        else:
            line = p0 - unit * bars_since
            pts.append((-0.5, "Giá dưới đường Gann 1x1 giảm") if b.c[i] < line
                       else (0.5, "Giá trên đường Gann 1x1 giảm"))
    return fin(r, pts)

# ---------------------------------------------------------------- HARMONIC
HP = {
    "Gartley":   ((0.618, 0.618), (0.382, 0.886), (0.786, 0.786)),
    "Bat":       ((0.382, 0.5),   (0.382, 0.886), (0.886, 0.886)),
    "Butterfly": ((0.786, 0.786), (0.382, 0.886), (1.272, 1.618)),
    "Crab":      ((0.382, 0.618), (0.382, 0.886), (1.618, 1.618)),
}

def harm_check(X, A_, B, C, D):
    xa, ab, bc = abs(A_ - X), abs(B - A_), abs(C - B)
    if xa <= 0 or ab <= 0:
        return None
    bull = X < A_
    if bull:
        if not (B < A_ and B > X and C > B and C < A_ and D < B): return None
    else:
        if not (B > A_ and B < X and C < B and C > A_ and D > B): return None
    rab, rbc, rad = ab / xa, bc / ab, abs(D - A_) / xa
    best, tol = None, 0.05
    for nm, (abr, bcr, adr) in HP.items():
        if abr[0] - tol <= rab <= abr[1] + tol and bcr[0] - tol <= rbc <= bcr[1] + tol \
                and adr[0] - tol <= rad <= adr[1] + tol:
            err = abs(rab - sum(abr) / 2) + abs(rad - sum(adr) / 2)
            if best is None or err < best[1]:
                best = (nm, err, rab, rad)
    if best:
        return best[0], (1 if bull else -1), best[1], best[2], best[3]
    return None

def an_harmonic(b, A):
    r = Res("HARM", "HARMONIC")
    if b.n < 80:
        r.state = "Thiếu dữ liệu"; return r
    zz = zigzag(b, 3, 3, 1.2 * A)
    P = b.c[-1]; pts = []
    found = []
    if len(zz) >= 5:
        res = harm_check(*[z[1] for z in zz[-5:]])
        if res and b.n - 1 - zz[-1][0] <= 12:
            found.append(("hoàn tất", res, zz[-1][1]))
    if len(zz) >= 4:
        res = harm_check(*([z[1] for z in zz[-4:]] + [P]))
        if res:
            found.append(("PRZ đang hình thành", res, P))
    for tag, (nm, sg, err, rab, rad), d in found:
        pts.append((sg * (1.8 - min(err, 0.3) * 2),
                    f"{nm} {'Bull' if sg > 0 else 'Bear'} {tag} (AB/XA={rab:.2f}, AD/XA={rad:.2f})"))
        r.zones.append((nm + " PRZ", d - 0.3 * A, d + 0.3 * A, GOLD))
    return fin(r, pts)

# ---------------------------------------------------------------- WYCKOFF
def an_wyckoff(b, A):
    r = Res("WYCK", "WYCKOFF")
    W = 60
    if b.n < W + 50:
        r.state = "Thiếu dữ liệu"; return r
    seg_h, seg_l = b.h[-W - 6:-6], b.l[-W - 6:-6]
    rh, rl = seg_h.max(), seg_l.min()
    ranging = (rh - rl) < 10 * A
    prior = b.c[-W - 6] - b.c[-W - 46]
    i = b.n - 2
    av = float(np.mean(b.v[-W - 6:-6]))
    pts = []
    r.levels.append(("TR high", rh, RED)); r.levels.append(("TR low", rl, GREEN))
    if ranging:
        ctx = "Tích lũy" if prior < 0 else "Phân phối"
        r.zones.append((f"TR ({ctx})", rl, rh, BLUE))
        for k in range(b.n - 6, b.n - 1):
            if b.l[k] < rl and b.c[k] > rl + 0.1 * A:
                pts.append((1.5 if prior < 0 else 0.9, "Spring: quét đáy TR rồi đóng cửa trong range")); break
        for k in range(b.n - 6, b.n - 1):
            if b.h[k] > rh and b.c[k] < rh - 0.1 * A:
                pts.append((-1.5 if prior > 0 else -0.9, "Upthrust (UTAD): quét đỉnh TR rồi đóng cửa trong range")); break
    if b.c[i] > rh and b.v[i] > 1.3 * av:
        pts.append((1.5, "SOS: phá đỉnh TR kèm volume"))
    if b.c[i] < rl and b.v[i] > 1.3 * av:
        pts.append((-1.5, "SOW: phá đáy TR kèm volume"))
    if not ranging and not pts:
        r.state = "Không có TR"
        return r
    return fin(r, pts)

# ---------------------------------------------------------------- ELLIOTT
def an_elliott(b, A):
    r = Res("ELL", "ELLIOTT")
    if b.n < 100:
        r.state = "Thiếu dữ liệu"; return r
    zz = zigzag(b, 4, 4, 2.5 * A)
    P = b.c[-1]; pts = []
    if len(zz) < 3:
        r.state = "Chưa rõ sóng"; return r

    def norm(seg):
        s = 1 if seg[0][2] == "L" else -1
        return s, [s * x[1] for x in seg]

    if len(zz) >= 5:                       # kết thúc sóng 4 -> kỳ vọng sóng 5
        s, q = norm(zz[-5:])
        w1, w3 = q[1] - q[0], q[3] - q[2]
        if q[2] > q[0] and q[3] > q[1] and q[4] > q[1] and w3 >= w1 * 0.99 and w3 > 0 \
                and 0.2 <= (q[3] - q[4]) / w3 <= 0.7:
            tgt = s * (q[4] + w1)
            pts.append((s * 1.6, f"Hoàn tất sóng 4 -> kỳ vọng sóng 5 (mục tiêu ~{tgt:.2f})"))
            r.levels.append(("Elliott W5", tgt, GOLD))
    if len(zz) >= 6:                       # đủ 5 sóng -> điều chỉnh
        s, q = norm(zz[-6:])
        w1, w3, w5 = q[1] - q[0], q[3] - q[2], q[5] - q[4]
        if q[2] > q[0] and q[3] > q[1] and q[4] > q[1] and q[5] > q[3] \
                and not (w3 < w1 and w3 < w5) and b.n - 1 - zz[-1][0] <= 12:
            pts.append((-s * 1.2, "Hoàn tất 5 sóng đẩy -> khả năng sóng điều chỉnh"))
    s, q = norm(zz[-3:])                   # kết thúc sóng 2 -> vào sóng 3
    w1 = q[1] - q[0]
    if w1 > 0 and q[2] > q[0] and 0.382 <= (q[1] - q[2]) / w1 <= 0.786 \
            and b.n - 1 - zz[-1][0] <= 15 and s * P > q[2]:
        pts.append((s * 1.3, "Kết thúc sóng 2 (retrace 38-79%) -> khả năng sóng 3"))
    return fin(r, pts)

ANALYZERS = [("VSA", "VSA", an_vsa), ("PA", "PRICE ACTION", an_pa), ("ICT", "ICT", an_ict),
             ("CHART", "CHART PATTERN", an_chart), ("GANN", "GANN", an_gann),
             ("HARM", "HARMONIC", an_harmonic), ("WYCK", "WYCKOFF", an_wyckoff),
             ("ELL", "ELLIOTT", an_elliott)]

def combine(results, weights, ai, ai_w):
    num = den = 0.0
    for r in results:
        w = weights.get(r.key, 1.0)
        num += w * r.sig * r.conf / 100.0
        den += w
    if ai is not None:
        s = {"BUY": 1, "SELL": -1}.get(ai["signal"], 0)
        num += ai_w * s * ai["confidence"] / 100.0
        den += ai_w
    return num / den if den else 0.0

# ============================================================ LOT / KẾ HOẠCH (hàm thuần)
def calc_lot(m, balance, sl_dist, risk_pct, log=None):
    if not balance or m["tick_size"] <= 0 or m["tick_value"] <= 0:
        return 0.0
    money = balance * risk_pct / 100.0
    loss_lot = sl_dist / m["tick_size"] * m["tick_value"]
    if loss_lot <= 0:
        return 0.0
    lot = math.floor(money / loss_lot / m["vstep"]) * m["vstep"]
    if lot < m["vmin"]:
        if m["vmin"] * loss_lot > money * 2:
            if log: log(f"Lot tối thiểu {m['vmin']} làm rủi ro vượt 2x mức cho phép -> bỏ lệnh.")
            return 0.0
        lot = m["vmin"]
    lot = min(lot, m["vmax"])
    return round(lot, 2 if m["vstep"] >= 0.01 else 3)

def norm_lot(m, lot):
    st = m["vstep"] or 0.01
    lot = math.floor(lot / st + 1e-9) * st
    lot = max(m["vmin"], min(m["vmax"], lot))
    return round(lot, 2 if st >= 0.01 else 3)

# ---------------------------------------------------------------- THỊ TRƯỜNG ĐI NGANG (sideway)
def adx_last(b, n=14):
    """ADX kiểu Wilder trên nến đã đóng (bỏ nến đang chạy)."""
    m = b.n - 1
    if m < 3 * n: return float("nan")
    h, l, c = b.h[:m], b.l[:m], b.c[:m]
    up, dn = h[1:] - h[:-1], l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = c[:-1]; tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    def wil(x):
        o = np.zeros(len(x)); o[n - 1] = x[:n].sum()
        for i in range(n, len(x)): o[i] = o[i - 1] - o[i - 1] / n + x[i]
        return o
    atr, p, q = wil(tr), wil(pdm), wil(mdm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, mdi = 100 * p / atr, 100 * q / atr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.nan_to_num(dx[n - 1:])
    if len(dx) < n: return float("nan")
    adx = float(dx[:n].mean())
    for v in dx[n:]: adx = (adx * (n - 1) + v) / n
    return adx

def rsi_last(b, n=14):
    c = b.c[:b.n - 1]
    if len(c) < n + 2: return float("nan")
    d = np.diff(c); up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au, ad = float(up[:n].mean()), float(dn[:n].mean())
    for u, v in zip(up[n:], dn[n:]):
        au = (au * (n - 1) + u) / n; ad = (ad * (n - 1) + v) / n
    return 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)

def detect_regime(b, A, cfg):
    """Đi ngang khi đủ số phiếu (mặc định 2/3): ER (Kaufman) thấp, độ trôi hồi quy nhỏ so với biên, ADX thấp.
    Đây là quy tắc heuristic của AIMT, chưa được backtest."""
    n = int(cfg["side_lookback"]); i = b.n - 2
    if b.n < max(80, n + 40): return dict(state="UNKNOWN")
    adx = adx_last(b)
    seg = b.c[i - n:i + 1]
    er = float(abs(seg[-1] - seg[0]) / max(float(np.abs(np.diff(seg)).sum()), 1e-9))
    hh, ll = float(b.h[i - n:i].max()), float(b.l[i - n:i].min())
    slope = float(np.polyfit(np.arange(len(seg)), seg, 1)[0])
    drift = abs(slope * n) / max(hh - ll, 1e-9)
    votes = int(er < cfg["er_thr"]) + int(drift < cfg["drift_thr"]) + int(adx == adx and adx < cfg["adx_thr"])
    return dict(state="SIDEWAYS" if votes >= int(cfg["side_votes"]) else "TREND", adx=adx, er=er, drift=drift,
                votes=votes, hh=hh, ll=ll, width=(hh - ll) / A if A > 0 else 0.0)

def range_signal(P, reg, cfg):
    """Đánh đảo chiều trong biên: BUY gần đáy biên, SELL gần đỉnh biên, cần nến xác nhận (+RSI nếu bật)."""
    b, A = P["bars"], P["atr"]; i = b.n - 2
    hh, ll = reg["hh"], reg["ll"]; rng = hh - ll
    if rng < 3 * A: return None, f"biên quá hẹp ({rng / A:.1f} ATR < 3)"
    c_, o_ = float(b.c[i]), float(b.o[i])
    if c_ > hh or c_ < ll: return None, "giá đã đóng cửa ngoài biên (nghi phá biên) nên không đánh đảo chiều"
    pos = (c_ - ll) / rng; edge = float(cfg["side_edge"])
    rg = max(float(b.h[i] - b.l[i]), 1e-9); lw = min(o_, c_) - float(b.l[i]); uw = float(b.h[i]) - max(o_, c_)
    rsi = rsi_last(b) if cfg.get("side_rsi") else None
    if pos <= edge and (c_ > o_ or lw >= 0.5 * rg) and (rsi is None or rsi != rsi or rsi <= 45):
        side = "BUY"
    elif pos >= 1 - edge and (c_ < o_ or uw >= 0.5 * rg) and (rsi is None or rsi != rsi or rsi >= 55):
        side = "SELL"
    else:
        return None, (f"giá ở {pos * 100:.0f}% biên (cần ≤{edge * 100:.0f}% để BUY hoặc ≥{(1 - edge) * 100:.0f}% để SELL, "
                      f"kèm nến xác nhận" + (f", RSI {rsi:.0f}" if rsi is not None and rsi == rsi else "") + ")")
    price = (P["ask"] if side == "BUY" else P["bid"]) or P["price"]
    sl = ll - 0.5 * A if side == "BUY" else hh + 0.5 * A
    tp = ll + 0.5 * rng if side == "BUY" else hh - 0.5 * rng
    sl_d, tp_d = abs(price - sl), abs(tp - price)
    if sl_d <= 0 or tp_d < sl_d: return None, f"RR {tp_d / max(sl_d, 1e-9):.2f} < 1 (TP về giữa biên không bù được SL)"
    strength = 1 - min(pos, 1 - pos) / max(edge, 1e-9)
    return dict(side=side, sl_d=sl_d, tp_d=tp_d, rr=tp_d / sl_d, pos=pos, hh=hh, ll=ll,
                conf10=int(max(1, min(10, round(4 + 6 * strength))))), ""

def build_range_plan(P, rg, reg):
    pl = dict(name="RANGE", side=rg["side"], kind="MARKET", off=0.0, sl_d=rg["sl_d"], tp_d=rg["tp_d"], rr=rg["rr"],
              conf10=rg["conf10"], zone=True,
              note=f"đảo chiều trong biên {rg['ll']:.2f}–{rg['hh']:.2f} (ER {reg['er']:.2f}, ADX {reg['adx']:.0f}); TP về giữa biên")
    return {"RANGE": pl, "BEST": dict(pl, name="BEST")}

def build_plans(P, side, score, mode):
    """3 kế hoạch: SCALP (vào ngay), SWING (lệnh chờ tại vùng/hồi), BEST (chọn theo quy tắc đơn giản)."""
    if side not in ("BUY", "SELL"):
        return {}
    A, buy = P["atr"], side == "BUY"
    ref = (P["ask"] if buy else P["bid"]) or P["price"]
    conf10 = int(max(1, min(10, round(min(1.0, abs(score) / 0.6) * 10))))   # thang 10 do app tự quy ước
    sl1 = 0.7 * mode["sl"] * A
    plans = {"SCALP": dict(name="SCALP", side=side, kind="MARKET", off=0.0, sl_d=sl1, tp_d=sl1 * 1.5,
                           rr=1.5, conf10=conf10, note="vào ngay theo giá thị trường", zone=False)}
    zs = []
    for r in P["results"]:
        for lab, lo, hi, _c in r.zones:
            if buy and lab in ("FVG+", "OB+") and hi < ref - 0.3 * A: zs.append((hi, lab))
            if (not buy) and lab in ("FVG-", "OB-") and lo > ref + 0.3 * A: zs.append((lo, lab))
    entry, note = None, ""
    if zs:
        e, lab = max(zs) if buy else min(zs)
        if abs(ref - e) <= 3 * A:
            entry, note = e, f"chờ retest {lab}"
    has_zone = entry is not None
    if entry is None:
        entry = ref - 0.5 * A if buy else ref + 0.5 * A
        note = "chờ hồi 0.5 ATR"
    sl2, rr2 = 1.5 * mode["sl"] * A, mode["rr"] + 0.5
    plans["SWING"] = dict(name="SWING", side=side, kind="LIMIT", off=entry - ref, sl_d=sl2, tp_d=sl2 * rr2,
                          rr=rr2, conf10=conf10 if has_zone else max(1, conf10 - 1), note=note, zone=has_zone)
    plans["BEST"] = dict(plans["SWING"] if has_zone else plans["SCALP"], name="BEST")
    ict = next((r for r in P["results"] if r.key == "ICT"), None)
    po = ict.extra.get("po3") if ict else None
    if po and po["side"] == (1 if buy else -1):
        sl3, tp3 = abs(po["entry"] - po["sl"]), abs(po["tp"] - po["entry"])
        if sl3 > 0:
            plans["PO3"] = dict(name="PO3", side=side, kind="MARKET", off=0.0, sl_d=sl3, tp_d=tp3, rr=tp3 / sl3,
                                conf10=conf10, note="setup ICT PO3 (SL = 5×ATR50, RR 0.86)", zone=True)
    return plans

def apply_ai_levels(plans, ai, P, cfg):
    """Dùng SL/TP do Gemini đề xuất nếu hợp lệ (đúng phía, khoảng cách hợp lý theo ATR); không hợp lệ -> giữ ATR."""
    if not ai or cfg.get("ai_levels", "off") != "auto" or not plans: return False
    try: asl, atp = float(ai.get("sl")), float(ai.get("tp"))
    except (TypeError, ValueError): return False
    A, applied = P["atr"], False
    for nm, pl in plans.items():
        if nm in ("PO3", "RANGE") or ai.get("signal") != pl["side"]: continue
        ref = (P["ask"] if pl["side"] == "BUY" else P["bid"]) or P["price"]
        e = ref + pl["off"]; buy = pl["side"] == "BUY"
        sl_d = e - asl if buy else asl - e
        tp_d = atp - e if buy else e - atp
        if not (0.5 * A <= sl_d <= 8 * A) or not (tp_d >= 0.5 * sl_d) or tp_d > 12 * A: continue
        pl.update(sl_d=sl_d, tp_d=tp_d, rr=tp_d / sl_d, src="AI"); applied = True
    return applied

def plan_prices(plan, ref, dg):
    entry = ref + plan["off"]
    buy = plan["side"] == "BUY"
    sl = entry - plan["sl_d"] if buy else entry + plan["sl_d"]
    tp = entry + plan["tp_d"] if buy else entry - plan["tp_d"]
    return round(entry, dg), round(sl, dg), round(tp, dg)

def plan_label(plan):
    return plan["side"] + (" LIMIT" if plan["kind"] == "LIMIT" else "")

# ============================================================ BROKER
class MT5Local:
    """Kết nối MT5 trực tiếp (chạy bên trong tiến trình worker của MT5Hub)."""
    demo = False
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.lock = threading.RLock()
        self.connected, self.cur, self.cur_path = False, None, None
        self.init_risk, self.trimmed = {}, set()

    # ---- kết nối / đổi tài khoản
    def connect(self, acc):
        path = (acc.get("path") or "").strip() or (self.cfg.get("mt5_path") or "").strip() or None
        try: login = int(acc.get("login") or 0)
        except Exception: login = 0
        pw, srv = acc.get("password") or "", acc.get("server") or ""
        key = (path, login)
        try:
            if self.connected and self.cur == key and mt5.terminal_info() is not None:
                ai = mt5.account_info()
                if ai is not None and (not login or ai.login == login):
                    return True
            if self.connected and self.cur_path == path and login and mt5.terminal_info() is not None:
                if mt5.login(login, password=pw, server=srv):
                    self.cur = key; return True
            if self.connected:
                mt5.shutdown(); self.connected = False
            kw = {}
            if path: kw["path"] = path
            if login: kw.update(login=login, password=pw, server=srv)
            if not mt5.initialize(**kw):
                self.log(f"[{acc.get('name')}] MT5 initialize lỗi: {mt5.last_error()} — mở MT5, đăng nhập rồi thử lại.")
                return False
            self.connected, self.cur, self.cur_path = True, key, path
            return True
        except Exception as ex:
            self.log(f"[{acc.get('name')}] lỗi kết nối: {ex}")
            self.connected = False
            return False

    def enter(self, acc):
        return self.connect(acc)

    @contextlib.contextmanager
    def session(self, acc):
        with self.lock:
            yield self.enter(acc)

    def shutdown(self):
        try: mt5.shutdown()
        except Exception: pass

    # ---- dữ liệu
    def resolve(self, name):
        if mt5.symbol_info(name) is None:
            cand = [s.name for s in (mt5.symbols_get() or []) if name.lower() in s.name.lower()]
            cand.sort(key=lambda x: (not x.lower().startswith(name.lower()), len(x)))
            if cand: name = cand[0]
        mt5.symbol_select(name, True)
        return name

    def symbols(self):
        try: return [s.name for s in (mt5.symbols_get() or []) if "XAU" in s.name.upper()][:40]
        except Exception: return []

    def rates(self, sym, tf, n):
        m = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
             "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
             "D1": mt5.TIMEFRAME_D1}
        return mt5.copy_rates_from_pos(sym, m[tf], 0, n)

    def tick(self, sym):
        t = mt5.symbol_info_tick(sym)
        return (t.bid, t.ask) if t else (0.0, 0.0)

    def meta(self, sym):
        i = mt5.symbol_info(sym)
        return dict(digits=i.digits, point=i.point, vmin=i.volume_min, vmax=i.volume_max,
                    vstep=i.volume_step, tick_size=i.trade_tick_size, tick_value=i.trade_tick_value,
                    stops_min=i.trade_stops_level * i.point, spread=i.spread)

    @staticmethod
    def _otype(t):
        return {2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP", 5: "SELL STOP"}.get(t, f"ORDER {t}")

    def snapshot(self, sym):
        a = mt5.account_info()
        acc = dict(login=a.login, server=a.server, balance=a.balance, equity=a.equity,
                   currency=a.currency) if a else {}
        allp = mt5.positions_get() or []
        magic = self.cfg["magic"]
        rows, mine = [], []
        for p in allp:
            d = dict(ticket=p.ticket, symbol=p.symbol, type="BUY" if p.type == 0 else "SELL",
                     volume=p.volume, open=p.price_open, sl=p.sl, tp=p.tp, profit=p.profit, magic=p.magic)
            rows.append(d)
            if p.magic == magic: mine.append(d)
        for o in (mt5.orders_get() or []):
            d = dict(ticket=o.ticket, symbol=o.symbol, type=self._otype(o.type), volume=o.volume_current,
                     open=o.price_open, sl=o.sl, tp=o.tp, profit=0.0, magic=o.magic)
            rows.append(d)
            if o.magic == magic: mine.append(d)
        pos_only = [r for r in rows if r["type"] in ("BUY", "SELL")]
        return dict(acc=acc, positions=rows, total=len(pos_only), mine=len(mine),
                    lots=sum(r["volume"] for r in pos_only), mine_list=mine)

    def lot_for_risk(self, sym, sl_dist, risk_pct):
        a = mt5.account_info()
        return calc_lot(self.meta(sym), a.balance if a else 0, sl_dist, risk_pct, self.log)

    # ---- đặt lệnh
    def _send(self, base, fills):
        last = None
        for fill in fills:
            res = mt5.order_send(dict(base, type_filling=fill))
            last = res
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                return True, res
            if res is not None and res.retcode != 10030:    # 10030 = filling mode không hỗ trợ
                break
        return False, last

    def order(self, sym, side, lot, sl, tp, comment="AIMT", magic=None):
        bid, ask = self.tick(sym)
        base = dict(action=mt5.TRADE_ACTION_DEAL, symbol=sym, volume=lot,
                    type=mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                    price=ask if side == "BUY" else bid, sl=sl, tp=tp, deviation=int(self.cfg["deviation"]),
                    magic=int(self.cfg["magic"] if magic is None else magic), comment=comment, type_time=mt5.ORDER_TIME_GTC)
        ok, res = self._send(base, (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN))
        if ok: return True, f"{side} {lot} {sym} @ {res.price} SL {sl} TP {tp} (ticket {res.order})"
        return False, f"order_send thất bại: {res.retcode if res else mt5.last_error()} {getattr(res, 'comment', '')}"

    def pending(self, sym, side, lot, price, sl, tp, comment="AIMT", kind="LIMIT"):
        tmap = {("BUY", "LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT, ("SELL", "LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
                ("BUY", "STOP"): mt5.ORDER_TYPE_BUY_STOP, ("SELL", "STOP"): mt5.ORDER_TYPE_SELL_STOP}
        base = dict(action=mt5.TRADE_ACTION_PENDING, symbol=sym, volume=lot, type=tmap[(side, kind)],
                    price=price, sl=sl, tp=tp, magic=int(self.cfg["magic"]), comment=comment,
                    type_time=mt5.ORDER_TIME_GTC)
        ok, res = self._send(base, (mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK))
        if ok: return True, f"{side} {kind} {lot} {sym} @ {price} SL {sl} TP {tp} (order {res.order})"
        return False, f"lệnh chờ thất bại: {res.retcode if res else mt5.last_error()} {getattr(res, 'comment', '')}"

    def modify(self, ticket, sym, sl, tp):
        res = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=sym, sl=sl, tp=tp))
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

    def close(self, p, vol=None):
        bid, ask = self.tick(p.symbol)
        buy = p.type == 0
        base = dict(action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=vol or p.volume, position=p.ticket,
                    type=mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                    price=bid if buy else ask, deviation=int(self.cfg["deviation"]),
                    magic=int(self.cfg["magic"]), comment="AIMT close")
        ok, _ = self._send(base, (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN))
        return ok

    # ---- quản lý lệnh: huỷ lệnh chờ quá hạn, BE, trailing, TRIM
    def manage(self, sym, A):
        out = dict(dca="TẮT", manage="NONE", trim="TẮT")
        c, magic = self.cfg, int(self.cfg["magic"])
        m = self.meta(sym); t = mt5.symbol_info_tick(sym)
        if t is None: return out
        bid, ask = t.bid, t.ask
        ttl = int(c.get("pending_ttl_min", 15)) * 60
        if ttl:
            for o in (mt5.orders_get(symbol=sym) or []):
                if o.magic == magic and t.time - o.time_setup > ttl:
                    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=o.ticket))
                    if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                        self.log(f"Huỷ lệnh chờ #{o.ticket} (quá {c['pending_ttl_min']} phút)")
        mine = [p for p in (mt5.positions_get(symbol=sym) or []) if p.magic == magic]
        live = {p.ticket for p in mine}
        self.init_risk = {k: v for k, v in self.init_risk.items() if k in live}
        self.trimmed &= live
        if c.get("dca_enabled"): out["dca"] = f"SẴN SÀNG {len(mine)}/{int(c['dca_max']) + 1}" if mine else "NONE"
        if c.get("trim_enabled"): out["trim"] = f"chờ {c['trim_R']}R"
        if not mine: return out
        acts = []
        for p in mine:
            buy = p.type == 0
            if p.ticket not in self.init_risk and p.sl:
                d = (p.price_open - p.sl) if buy else (p.sl - p.price_open)
                if d > 0: self.init_risk[p.ticket] = d
            risk = self.init_risk.get(p.ticket)
            if not risk: continue
            prof_d = (bid - p.price_open) if buy else (p.price_open - ask)
            buf = max(m["spread"] * m["point"], m["point"])
            sl = p.sl
            if c["be_enabled"] and prof_d >= c["be_trigger"] * risk:
                be = round(p.price_open + buf if buy else p.price_open - buf, m["digits"])
                if (buy and sl < be) or ((not buy) and sl > be):
                    if self.modify(p.ticket, sym, be, p.tp): sl = be; acts.append(f"BE #{p.ticket}")
            if c.get("trail_enabled") and prof_d >= c["be_trigger"] * risk:
                tr = round(bid - c["trail_atr"] * A if buy else ask + c["trail_atr"] * A, m["digits"])
                good = (buy and tr > sl and tr < bid - m["stops_min"]) or \
                       ((not buy) and tr < sl and tr > ask + m["stops_min"])
                if good and self.modify(p.ticket, sym, tr, p.tp): sl = tr; acts.append(f"TRAIL #{p.ticket}")
            if c.get("trim_enabled") and p.ticket not in self.trimmed and prof_d >= c["trim_R"] * risk:
                vol = round(math.floor(p.volume * c["trim_pct"] / 100 / m["vstep"]) * m["vstep"], 3)
                if vol >= m["vmin"] and p.volume - vol >= m["vmin"]:
                    if self.close(p, vol):
                        self.trimmed.add(p.ticket); acts.append(f"TRIM {vol}"); out["trim"] = f"đã chốt {vol}"
        if acts:
            out["manage"] = ", ".join(acts); self.log("MANAGE: " + out["manage"])
        else:
            out["manage"] = f"theo dõi {len(mine)} lệnh"
        return out

    # ---- API dạng dict (dùng cho Panel / PanelOps)
    def positions(self, sym=None):
        raw = mt5.positions_get(symbol=sym) if sym else mt5.positions_get()
        return [dict(ticket=p.ticket, ident=p.identifier, symbol=p.symbol, type="BUY" if p.type == 0 else "SELL",
                     volume=p.volume, open=p.price_open, sl=p.sl, tp=p.tp, profit=p.profit, swap=p.swap,
                     magic=p.magic, comment=p.comment) for p in (raw or [])]

    def orders(self, sym=None):
        raw = mt5.orders_get(symbol=sym) if sym else mt5.orders_get()
        return [dict(ticket=o.ticket, ident=o.ticket, symbol=o.symbol, type=self._otype(o.type), volume=o.volume_current,
                     open=o.price_open, sl=o.sl, tp=o.tp, profit=0.0, swap=0.0, magic=o.magic, comment=o.comment)
                for o in (raw or [])]

    def open_comm(self, ident):
        try:
            return float(sum(d.commission for d in (mt5.history_deals_get(position=ident) or [])))
        except Exception:
            return 0.0

    def account(self):
        a = mt5.account_info()
        return dict(login=a.login, server=a.server, balance=a.balance, equity=a.equity, currency=a.currency) if a else {}

    def modify_pos(self, ticket, sym, sl, tp):
        return self.modify(ticket, sym, sl, tp)

    def close_pos(self, d, vol=None):
        bid, ask = self.tick(d["symbol"]); buy = d["type"] == "BUY"
        base = dict(action=mt5.TRADE_ACTION_DEAL, symbol=d["symbol"], volume=vol or d["volume"], position=d["ticket"],
                    type=mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, price=bid if buy else ask,
                    deviation=int(self.cfg["deviation"]), magic=int(d.get("magic") or self.cfg["magic"]), comment="AIMT close")
        ok, _ = self._send(base, (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN))
        return ok

    def delete_order(self, ticket):
        r = mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=ticket))
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE

    def close_all_mine(self):
        n, magic = 0, int(self.cfg["magic"])
        for p in (mt5.positions_get() or []):
            if p.magic == magic: n += self.close(p)
        for o in (mt5.orders_get() or []):
            if o.magic == magic:
                r = mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=o.ticket))
                n += int(r is not None and r.retcode == mt5.TRADE_RETCODE_DONE)
        return n


# ============================================================ NHIỀU TERMINAL SONG SONG (mỗi terminal 1 tiến trình)
RPC_METHODS = {"resolve", "symbols", "rates", "tick", "meta", "snapshot", "lot_for_risk", "order", "pending", "modify",
               "close", "positions", "orders", "open_comm", "account", "modify_pos", "close_pos", "delete_order",
               "manage", "close_all_mine"}

def _mt5_worker(conn, cfg):
    """Chạy trong tiến trình riêng: giữ 1 kết nối MT5 và xử lý các lệnh gọi từ tiến trình chính."""
    logs = []
    br = MT5Local(cfg, logs.append)
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break
        if msg is None: break
        op, a, k = msg
        logs.clear()
        try:
            if op == "set_cfg":
                br.cfg.clear(); br.cfg.update(a[0]); res = True
            elif op == "enter":
                res = br.enter(a[0])
            else:
                res = getattr(br, op)(*a, **k)
            conn.send((True, res, list(logs)))
        except Exception as ex:
            try: conn.send((False, f"{type(ex).__name__}: {ex}", list(logs)))
            except Exception: break
    try: br.shutdown()
    except Exception: pass


class MT5Worker:
    def __init__(self, hub, key):
        self.hub, self.key = hub, key
        self.lock = threading.RLock(); self.proc = self.conn = None; self.cfg_sig = None

    def alive(self): return self.proc is not None and self.proc.is_alive()

    def start(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        self.proc = ctx.Process(target=_mt5_worker, args=(child, self.hub.cfg_for_worker()), daemon=True)
        self.proc.start(); child.close(); self.conn, self.cfg_sig = parent, None

    def kill(self):
        try:
            if self.proc is not None and self.proc.is_alive(): self.proc.terminate()
        except Exception: pass
        self.proc = self.conn = None

    def stop(self):
        try:
            if self.conn is not None: self.conn.send(None)
            if self.proc is not None: self.proc.join(2)
        except Exception: pass
        self.kill()

    def call(self, op, *a, timeout=60, **k):
        with self.lock:
            if not self.alive(): self.start()
            try:
                self.conn.send((op, a, k))
                if not self.conn.poll(timeout):
                    raise TimeoutError(f"terminal '{self.key}' không phản hồi ({op})")
                ok, res, logs = self.conn.recv()
            except (EOFError, OSError) as ex:
                self.kill(); raise RuntimeError(f"tiến trình terminal '{self.key}' đã dừng: {ex}")
            except TimeoutError:
                self.kill(); raise
            for l in logs: self.hub.log(l)
            if not ok: raise RuntimeError(res)
            return res


class MT5Hub:
    """Điểm vào duy nhất cho MT5 thật. Mỗi terminal (đường dẫn terminal64.exe) có 1 tiến trình riêng nên nhiều
    terminal chạy SONG SONG; các tài khoản dùng chung 1 terminal thì được xếp hàng."""
    demo = False
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.workers, self.wl, self.tl = {}, threading.Lock(), threading.local()
        self.tl.w = None

    def cfg_for_worker(self):
        return {k: v for k, v in self.cfg.items() if k not in ("llm", "gemini_key", "accounts")}

    def key_for(self, acc):
        p = ((acc.get("path") or "").strip() or (self.cfg.get("mt5_path") or "").strip())
        return os.path.normcase(p) if p else "default"

    def worker(self, acc):
        key = self.key_for(acc)
        with self.wl:
            w = self.workers.get(key)
            if w is None: w = self.workers[key] = MT5Worker(self, key)
            return w

    def _enter(self, w, acc):
        sig = json.dumps(self.cfg_for_worker(), sort_keys=True, default=str)
        if sig != w.cfg_sig:
            w.call("set_cfg", self.cfg_for_worker()); w.cfg_sig = sig
        return bool(w.call("enter", acc, timeout=90))

    def acquire(self, acc, timeout=0.3):
        w = self.worker(acc)
        if not w.lock.acquire(timeout=timeout): return False
        try:
            if not self._enter(w, acc):
                w.lock.release(); return False
        except Exception as ex:
            w.lock.release(); self.log(f"[{acc.get('name')}] lỗi kết nối terminal: {ex}"); return False
        self.tl.w = w
        return True

    def release(self, acc):
        self.tl.w = None
        try: self.worker(acc).lock.release()
        except RuntimeError: pass

    @contextlib.contextmanager
    def session(self, acc):
        w = self.worker(acc)
        with w.lock:
            ok = False
            try: ok = self._enter(w, acc)
            except Exception as ex: self.log(f"[{acc.get('name')}] lỗi kết nối terminal: {ex}")
            self.tl.w = w
            try: yield ok
            finally: self.tl.w = None

    def shutdown(self):
        for w in list(self.workers.values()): w.stop()

    def __getattr__(self, name):
        if name in RPC_METHODS:
            return lambda *a, **k: self.tl.w.call(name, *a, **k)
        raise AttributeError(name)


class DemoBroker:
    """Dữ liệu giả để chạy thử giao diện khi không có MT5. Mỗi tài khoản có số dư/lệnh riêng."""
    demo = True
    def __init__(self, cfg, log):
        self.cfg, self.log, self.connected = cfg, log, True
        self.lock = threading.RLock()
        rng = np.random.default_rng(7)
        vol = np.abs(rng.normal(0, 1, 30000)) * 0.25 + 0.12
        reg = np.repeat(rng.normal(0, 0.03, 300), 100)[:30000]
        self.m1 = 4100 + np.cumsum(rng.normal(reg, vol))
        self.m1v = 100 + np.abs(np.diff(self.m1, prepend=self.m1[0])) * 250 + rng.random(30000) * 60
        self.acc, self.st = {}, None

    def enter(self, acc):
        n = acc.get("name", "x")
        self.st = self.acc.setdefault(n, dict(bal=1000.0, pos=[], pend=[], tk=1000 + 100 * len(self.acc)))
        return True

    def acquire(self, acc, timeout=0.3):
        if not self.lock.acquire(timeout=timeout): return False
        self.enter(acc); return True

    def release(self, acc):
        try: self.lock.release()
        except RuntimeError: pass

    @contextlib.contextmanager
    def session(self, acc):
        with self.lock:
            yield self.enter(acc)

    def shutdown(self): pass
    def resolve(self, name): return name
    def symbols(self): return ["XAUUSD"]

    def rates(self, sym, tf, n):
        k = TF_MIN[tf]; n = max(30, min(n, (30000 - 40) // k)); total = n * k
        off = int(time.time() // 60) % (30000 - total - 10) + total + 5
        c1 = self.m1[off - total:off]; v1 = self.m1v[off - total:off]
        c = c1.reshape(n, k); vv = v1.reshape(n, k)
        o = np.concatenate([[c1[0]], c[:-1, -1]]) if n > 1 else c[:, 0]
        o = np.where(np.arange(n) == 0, c[:, 0], o)
        spread = np.abs(c1 - np.roll(c1, 1)).reshape(n, k).max(axis=1) * 0.3
        h = np.maximum(c.max(axis=1), o) + spread
        l = np.minimum(c.min(axis=1), o) - spread
        t = (int(time.time() // (60 * k)) * 60 * k) - (n - 1 - np.arange(n)) * 60 * k
        dtp = np.dtype([("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
                        ("close", "f8"), ("tick_volume", "i8")])
        out = np.zeros(n, dtype=dtp)
        out["time"], out["open"], out["high"], out["low"] = t, o, h, l
        out["close"], out["tick_volume"] = c[:, -1], vv.sum(axis=1).astype(np.int64)
        return out

    def tick(self, sym):
        p = float(self.m1[int(time.time() // 60) % (30000 - 20) + 10]) + 0.25 * math.sin(time.time() * 0.9)
        return round(p - 0.15, 2), round(p + 0.15, 2)

    def meta(self, sym):
        return dict(digits=2, point=0.01, vmin=0.01, vmax=100.0, vstep=0.01, tick_size=0.01,
                    tick_value=1.0, stops_min=0.5, spread=30)

    def _fill(self):
        bid, ask = self.tick("")
        for o in list(self.st["pend"]):
            k = o.get("kind", "LIMIT")
            hit = (o["side"] == "BUY" and ((k == "LIMIT" and ask <= o["price"]) or (k == "STOP" and ask >= o["price"]))) or \
                  (o["side"] == "SELL" and ((k == "LIMIT" and bid >= o["price"]) or (k == "STOP" and bid <= o["price"])))
            if hit:
                self.st["pend"].remove(o)
                self.st["pos"].append(dict(ticket=o["ticket"], ident=o["ticket"], symbol=o["symbol"], type=o["side"],
                                           volume=o["volume"], open=o["price"], sl=o["sl"], tp=o["tp"], swap=0.0,
                                           magic=o.get("magic", self.cfg["magic"])))

    def snapshot(self, sym):
        self._fill()
        bid, ask = self.tick(sym); st = self.st
        pos = []
        for p in st["pos"]:
            cur = bid if p["type"] == "BUY" else ask
            d = (cur - p["open"]) if p["type"] == "BUY" else (p["open"] - cur)
            pos.append(dict(p, profit=round(d * 100 * p["volume"], 2)))
        pend = [dict(ticket=o["ticket"], symbol=o["symbol"], type=o["side"] + " " + o.get("kind", "LIMIT"), volume=o["volume"],
                     open=o["price"], sl=o["sl"], tp=o["tp"], profit=0.0, magic=self.cfg["magic"]) for o in st["pend"]]
        eq = st["bal"] + sum(p["profit"] for p in pos)
        return dict(acc=dict(login=0, server="MÔ PHỎNG", balance=st["bal"], equity=eq, currency="USD"),
                    positions=pos + pend, total=len(pos), mine=len(pos) + len(pend),
                    lots=sum(p["volume"] for p in pos), mine_list=pos + pend)

    def lot_for_risk(self, sym, sl_dist, risk_pct):
        return calc_lot(self.meta(sym), self.st["bal"], sl_dist, risk_pct, self.log)

    def order(self, sym, side, lot, sl, tp, comment="AIMT", magic=None):
        bid, ask = self.tick(sym); self.st["tk"] += 1
        self.st["pos"].append(dict(ticket=self.st["tk"], ident=self.st["tk"], symbol=sym, type=side, volume=lot, swap=0.0,
                                   open=ask if side == "BUY" else bid, sl=sl, tp=tp,
                                   magic=self.cfg["magic"] if magic is None else magic, comment=comment))
        return True, f"[MÔ PHỎNG] {side} {lot} {sym} SL {sl} TP {tp}"

    def pending(self, sym, side, lot, price, sl, tp, comment="AIMT", kind="LIMIT"):
        self.st["tk"] += 1
        self.st["pend"].append(dict(ticket=self.st["tk"], symbol=sym, side=side, volume=lot, price=price, sl=sl, tp=tp,
                                    kind=kind, magic=self.cfg["magic"]))
        return True, f"[MÔ PHỎNG] {side} {kind} {lot} {sym} @ {price} SL {sl} TP {tp}"

    # ---- API dạng dict
    def _live_pos(self, sym=None):
        self._fill(); bid, ask = self.tick(sym or "")
        out = []
        for p in self.st["pos"]:
            if sym and p["symbol"] != sym: continue
            cur = bid if p["type"] == "BUY" else ask
            d = (cur - p["open"]) if p["type"] == "BUY" else (p["open"] - cur)
            out.append(dict(p, profit=round(d * 100 * p["volume"], 2)))
        return out

    def positions(self, sym=None): return self._live_pos(sym)

    def orders(self, sym=None):
        return [dict(ticket=o["ticket"], ident=o["ticket"], symbol=o["symbol"], type=o["side"] + " " + o.get("kind", "LIMIT"),
                     volume=o["volume"], open=o["price"], sl=o["sl"], tp=o["tp"], profit=0.0, swap=0.0,
                     magic=o.get("magic", self.cfg["magic"]), comment="") for o in self.st["pend"] if not sym or o["symbol"] == sym]

    def open_comm(self, ident): return 0.0

    def account(self):
        eq = self.st["bal"] + sum(p["profit"] for p in self._live_pos())
        return dict(login=0, server="MÔ PHỎNG", balance=self.st["bal"], equity=eq, currency="USD")

    def modify_pos(self, ticket, sym, sl, tp):
        for p in self.st["pos"]:
            if p["ticket"] == ticket: p["sl"], p["tp"] = sl, tp; return True
        return False

    def close_pos(self, d, vol=None):
        bid, ask = self.tick(d["symbol"])
        for p in list(self.st["pos"]):
            if p["ticket"] != d["ticket"]: continue
            v = min(vol or p["volume"], p["volume"])
            cur = bid if p["type"] == "BUY" else ask
            diff = (cur - p["open"]) if p["type"] == "BUY" else (p["open"] - cur)
            self.st["bal"] += diff * 100 * v
            if v >= p["volume"] - 1e-9: self.st["pos"].remove(p)
            else: p["volume"] = round(p["volume"] - v, 3)
            return True
        return False

    def delete_order(self, ticket):
        n = len(self.st["pend"]); self.st["pend"] = [o for o in self.st["pend"] if o["ticket"] != ticket]
        return len(self.st["pend"]) < n

    def manage(self, sym, A):
        c = self.cfg
        return dict(dca=("SẴN SÀNG" if c.get("dca_enabled") else "TẮT"), manage="(mô phỏng)",
                    trim=("chờ " + str(c["trim_R"]) + "R" if c.get("trim_enabled") else "TẮT"))

    def close_all_mine(self):
        n = len(self.st["pos"]) + len(self.st["pend"]); self.st["pos"], self.st["pend"] = [], []
        return n


# ============================================================ PANEL OPS (port từ TradePanelPro.mq5)
PA_DEFAULT = dict(
    sl_pts=1000, tp_pts=3000, gap_pts=300, step_pts=100, use_ai_levels=False,
    trim_matched=True, trim_pct=30.0, trim_net=10.0, trim_exact=True, trim_close_comm=0.0,
    max_orders=0, max_lots=0.0,
    be_on=False, be_usd=50.0, be_plus=30,
    rr_on=False, rr_val=1.0, rr_pct=50.0, rr_be=True,
    trail_on=False, trail_start=500, trail_dist=300, trail_step=50,
    sl_on=False, sl_auto_pts=1000,
    pt_on=False, pt1=[True, 30.0, 1000], pt2=[True, 30.0, 2000], pt3=[True, 40.0, 3000],
    mm_on=False, cap_profit=500.0, cap_loss=0.0, cap_profit_pct=0.0, cap_loss_pct=0.0, cap_dd=0.0,
)

class PanelOps:
    """Các thao tác quản lý lệnh. Gọi bên trong br.session(...) hoặc khi đã giữ br.lock + br.enter(acc)."""
    def __init__(self, br, cfg, log):
        self.br, self.cfg, self.log = br, cfg, log
        self.maxeq, self.pt, self.pt_lv, self.rr_done = 0.0, {}, None, set()

    def pa(self): return self.cfg["pa"]

    def _in_scope(self, p):
        sc = self.cfg.get("panel_scope", "AIMT")
        if sc == "AIMT": return p["magic"] in (int(self.cfg["magic"]), int(self.cfg.get("copy_magic", 8800124)))
        if sc == "MAGIC": return p["magic"] == int(self.cfg.get("panel_magic", 777))
        return True

    def pos(self, sym): return [p for p in self.br.positions(sym) if self._in_scope(p)]
    def ords(self, sym): return [o for o in self.br.orders(sym) if self._in_scope(o)]

    @staticmethod
    def _side_ok(p, side):          # side: 1 = buy, -1 = sell, 0 = tất cả
        return side == 0 or (side == 1 and p["type"] == "BUY") or (side == -1 and p["type"] == "SELL")

    # ---- đóng
    def close_all(self, sym):
        return f"Đã đóng {sum(bool(self.br.close_pos(p)) for p in self.pos(sym))} lệnh"

    def close_side(self, sym, side):
        return f"Đã đóng {sum(bool(self.br.close_pos(p)) for p in self.pos(sym) if self._side_ok(p, side))} lệnh"

    def close_profit(self, sym, profitable):
        n = 0
        for p in self.pos(sym):
            v = p["profit"] + p.get("swap", 0.0)
            if (profitable and v > 0) or ((not profitable) and v < 0): n += bool(self.br.close_pos(p))
        return f"Đã đóng {n} lệnh {'lãi' if profitable else 'lỗ'}"

    def close_half(self, sym, side):
        m = self.br.meta(sym); n = 0
        for p in self.pos(sym):
            if not self._side_ok(p, side): continue
            v = norm_lot(m, p["volume"] * 0.5)
            if v >= m["vmin"] and v < p["volume"]: n += bool(self.br.close_pos(p, v))
        return f"Đã chốt 1/2 {n} lệnh"

    def trim_percent(self, sym, side, pct):
        if pct <= 0 or pct > 100: return "Trim %: giá trị không hợp lệ"
        m = self.br.meta(sym); n = 0
        for p in self.pos(sym):
            if not self._side_ok(p, side): continue
            v = norm_lot(m, p["volume"] * pct / 100.0)
            if v < m["vmin"]: continue
            n += bool(self.br.close_pos(p) if v >= p["volume"] else self.br.close_pos(p, v))
        return f"Trim {pct}%: xử lý {n} lệnh"

    def net_now(self, p, close_comm):
        return p["profit"] + p.get("swap", 0.0) + self.br.open_comm(p["ident"]) - abs(close_comm) * p["volume"]

    def trim_matched(self, sym, side, target, exact, close_comm):
        """Chọn tập lệnh lỗ + lãi sao cho kết quả ròng >= target (thuật toán tham lam như TradePanelPro)."""
        m = self.br.meta(sym); win, los = [], []
        for p in self.pos(sym):
            if not self._side_ok(p, side): continue
            n = self.net_now(p, close_comm); d = dict(p, net=n)
            if n > 0: win.append(d)
            elif n < 0: los.append(d)
        if not los: return "Trim $: không có lệnh lỗ để xoá"
        if not win: return "Trim $: không có lệnh lãi để bù"
        los.sort(key=lambda x: x["net"]); win.sort(key=lambda x: -x["net"])
        wi = li = 0; s = 0.0
        for L in range(len(los)):
            trial = s + los[L]["net"]; wt = wi
            while trial < target and wt < len(win):
                trial += win[wt]["net"]; wt += 1
            if trial < target: break
            s, wi, li = trial, wt, L + 1
        if li == 0: return f"Trim $: lệnh lãi không đủ bù lỗ (cần >= {target:.2f})"
        last, excess, part, do_part = wi - 1, s - target, 0.0, False
        if exact and last >= 0 and excess > 0:
            W = win[last]["net"]
            if W > 0:
                part = norm_lot(m, win[last]["volume"] * (W - excess) / W)
                do_part = part >= m["vmin"] and part < win[last]["volume"]
        for i in range(li): self.br.close_pos(los[i])
        for i in range(wi): self.br.close_pos(win[i], part if (do_part and i == last) else None)
        return f"Trim $: đóng {li} lệnh LỖ + {wi} lệnh LÃI | net dự kiến {target if do_part else s:.2f}"

    def reverse_all(self, sym):
        n = 0
        for p in self.pos(sym):
            if not self.br.close_pos(p): continue
            ns = "SELL" if p["type"] == "BUY" else "BUY"
            ok, _ = self.br.order(sym, ns, p["volume"], p["tp"], p["sl"], "AIMT rev"); n += bool(ok)
        return f"Đảo chiều {n} lệnh"

    def clear_sltp(self, sym):
        return f"Xoá SL/TP {sum(bool(self.br.modify_pos(p['ticket'], sym, 0.0, 0.0)) for p in self.pos(sym))} lệnh"

    def delete_pendings(self, sym):
        return f"Đã huỷ {sum(bool(self.br.delete_order(o['ticket'])) for o in self.ords(sym))} lệnh chờ"

    # ---- BE / chỉnh SL TP
    def move_be(self, sym, p, plus_pts):
        m = self.br.meta(sym); bid, ask = self.br.tick(sym); pt = m["point"]
        if p["type"] == "BUY":
            be = round(p["open"] + plus_pts * pt, m["digits"])
            if bid > be + m["stops_min"] and (p["sl"] == 0 or p["sl"] < be):
                return bool(self.br.modify_pos(p["ticket"], sym, be, p["tp"]))
        else:
            be = round(p["open"] - plus_pts * pt, m["digits"])
            if ask < be - m["stops_min"] and (p["sl"] == 0 or p["sl"] > be):
                return bool(self.br.modify_pos(p["ticket"], sym, be, p["tp"]))
        return False

    def move_all_be(self, sym):
        pl = self.pa()["be_plus"]
        return f"BE {sum(self.move_be(sym, p, pl) for p in self.pos(sym))} lệnh"

    def adjust(self, sym, is_sl, points):
        m = self.br.meta(sym); n = 0
        for p in self.pos(sym):
            d = 1.0 if p["type"] == "BUY" else -1.0
            sl, tp = p["sl"], p["tp"]
            if is_sl:
                if sl == 0: continue
                sl = round(sl + d * points * m["point"], m["digits"])
            else:
                if tp == 0: continue
                tp = round(tp + d * points * m["point"], m["digits"])
            n += bool(self.br.modify_pos(p["ticket"], sym, sl, tp))
        return f"Chỉnh {'SL' if is_sl else 'TP'} {points:+.0f} point: {n} lệnh"

    def move_price(self, sym, is_sl, price):
        if price <= 0: return "Giá không hợp lệ"
        m = self.br.meta(sym); n = 0
        for p in self.pos(sym):
            sl, tp = (round(price, m["digits"]), p["tp"]) if is_sl else (p["sl"], round(price, m["digits"]))
            n += bool(self.br.modify_pos(p["ticket"], sym, sl, tp))
        return f"Đặt {'SL' if is_sl else 'TP'} = {price}: {n} lệnh"

    # ---- vào lệnh từ panel
    def _limits_ok(self, sym, lot):
        pa = self.pa()
        if pa["max_orders"] <= 0 and pa["max_lots"] <= 0: return True
        allp = self.br.positions(sym)
        if pa["max_orders"] > 0 and len(allp) + 1 > pa["max_orders"]: self.log("Panel: vượt số lệnh tối đa"); return False
        if pa["max_lots"] > 0 and sum(p["volume"] for p in allp) + lot > pa["max_lots"]: self.log("Panel: vượt tổng lot tối đa"); return False
        return True

    def market(self, sym, side, lot, sl_pts=0, tp_pts=0, sl_abs=None, tp_abs=None):
        m = self.br.meta(sym); bid, ask = self.br.tick(sym); pt = m["point"]
        if not self._limits_ok(sym, lot): return "Bị chặn bởi giới hạn lệnh/lot"
        price = ask if side == "BUY" else bid; sgn = 1 if side == "BUY" else -1
        sl = sl_abs if sl_abs else (price - sgn * sl_pts * pt if sl_pts > 0 else 0.0)
        tp = tp_abs if tp_abs else (price + sgn * tp_pts * pt if tp_pts > 0 else 0.0)
        ok, msg = self.br.order(sym, side, lot, round(sl, m["digits"]) if sl else 0.0, round(tp, m["digits"]) if tp else 0.0, "AIMT panel")
        return ("✔ " if ok else "✘ ") + msg

    def pending(self, sym, kind, side, lot, gap_pts, sl_pts=0, tp_pts=0):
        m = self.br.meta(sym); bid, ask = self.br.tick(sym); pt = m["point"]
        if not self._limits_ok(sym, lot): return "Bị chặn bởi giới hạn lệnh/lot"
        gap = max(gap_pts * pt, m["stops_min"] * 1.1)
        if side == "BUY": price = ask - gap if kind == "LIMIT" else ask + gap
        else: price = bid + gap if kind == "LIMIT" else bid - gap
        sgn = 1 if side == "BUY" else -1
        sl = price - sgn * sl_pts * pt if sl_pts > 0 else 0.0
        tp = price + sgn * tp_pts * pt if tp_pts > 0 else 0.0
        ok, msg = self.br.pending(sym, side, lot, round(price, m["digits"]), round(sl, m["digits"]) if sl else 0.0,
                                  round(tp, m["digits"]) if tp else 0.0, "AIMT panel", kind)
        return ("✔ " if ok else "✘ ") + msg

    # ---- Partial TP
    def toggle_partial(self, sym, side):
        if self.pt_lv:
            self.pt_lv = None; return "Đã huỷ Partial TP"
        pa = self.pa(); m = self.br.meta(sym); bid, ask = self.br.tick(sym)
        base, d = (ask, 1.0) if side == "BUY" else (bid, -1.0)
        lv = dict(side=side)
        for k in ("pt1", "pt2", "pt3"):
            if pa[k][0]: lv[k] = round(base + d * pa[k][2] * m["point"], m["digits"])
        self.pt_lv, self.pt = lv, {}
        return "Đã bật Partial TP: " + ", ".join(f"{k}={v}" for k, v in lv.items() if k != "side")

    def _check_partial(self, sym, ps):
        lv, pa = self.pt_lv, self.pa()
        if not lv: return
        m = self.br.meta(sym); bid, ask = self.br.tick(sym)
        for p in ps:
            if p["type"] != lv["side"]: continue
            buy = p["type"] == "BUY"; cur = bid if buy else ask
            f = self.pt.setdefault(p["ticket"], [False, False, False])
            for i, k in enumerate(("pt1", "pt2", "pt3")):
                if k in lv and pa[k][0] and not f[i] and ((buy and cur >= lv[k]) or ((not buy) and cur <= lv[k])):
                    v = norm_lot(m, p["volume"] * pa[k][1] / 100.0)
                    if v >= m["vmin"] and v < p["volume"] and self.br.close_pos(p, v): f[i] = True
                    break           # như bản gốc: mỗi lượt chỉ xử lý 1 mức

    # ---- vòng tự động (gọi ~2 lần/giây từ LiveFeed)
    def any_auto(self):
        pa = self.pa()
        return any((pa["be_on"], pa["rr_on"], pa["trail_on"], pa["sl_on"], pa["pt_on"] and self.pt_lv, pa["mm_on"]))

    def auto_tick(self, sym):
        pa = self.pa(); acc = self.br.account()
        if not acc: return
        bal, eq = acc["balance"], acc["equity"]; self.maxeq = max(self.maxeq, eq)
        if pa["mm_on"] and bal > 0:
            pl = eq - bal
            hit = (pa["cap_profit"] > 0 and pl >= pa["cap_profit"]) or (pa["cap_loss"] > 0 and pl <= -pa["cap_loss"]) or \
                  (pa["cap_profit_pct"] > 0 and pl / bal * 100 >= pa["cap_profit_pct"]) or \
                  (pa["cap_loss_pct"] > 0 and pl / bal * 100 <= -pa["cap_loss_pct"]) or \
                  (pa["cap_dd"] > 0 and self.maxeq > 0 and (self.maxeq - eq) / self.maxeq * 100 >= pa["cap_dd"])
            if hit and (self.pos(sym) or self.ords(sym)):
                self.close_all(sym); self.delete_pendings(sym); self.maxeq = eq
                self.log("Quản lý vốn: đã đóng tất cả lệnh theo điều kiện đặt trước"); return
        ps = self.pos(sym)
        if not ps: return
        m = self.br.meta(sym); bid, ask = self.br.tick(sym); pt = m["point"]
        if pa["be_on"]:
            for p in ps:
                if p["profit"] >= pa["be_usd"]: self.move_be(sym, p, pa["be_plus"])
        if pa["rr_on"]:
            for p in ps:
                if p["sl"] <= 0 or p["ticket"] in self.rr_done: continue
                buy = p["type"] == "BUY"
                if (buy and p["sl"] >= p["open"]) or ((not buy) and p["sl"] <= p["open"]): continue
                risk = abs(p["open"] - p["sl"]); gain = (bid - p["open"]) if buy else (p["open"] - ask)
                if risk <= 0 or gain / risk < pa["rr_val"]: continue
                v = norm_lot(m, p["volume"] * pa["rr_pct"] / 100.0)
                if v >= m["vmin"] and v < p["volume"]: self.br.close_pos(p, v)
                self.rr_done.add(p["ticket"])
                if pa["rr_be"]: self.move_be(sym, p, pa["be_plus"])
        if pa["trail_on"]:
            for p in ps:
                if p["type"] == "BUY":
                    if (bid - p["open"]) / pt < pa["trail_start"]: continue
                    n = round(bid - pa["trail_dist"] * pt, m["digits"])
                    if (p["sl"] == 0 or n >= p["sl"] + pa["trail_step"] * pt) and n < bid - m["stops_min"]:
                        self.br.modify_pos(p["ticket"], sym, n, p["tp"])
                else:
                    if (p["open"] - ask) / pt < pa["trail_start"]: continue
                    n = round(ask + pa["trail_dist"] * pt, m["digits"])
                    if (p["sl"] == 0 or n <= p["sl"] - pa["trail_step"] * pt) and n > ask + m["stops_min"]:
                        self.br.modify_pos(p["ticket"], sym, n, p["tp"])
        if pa["sl_on"]:
            for p in ps:
                if p["sl"] != 0: continue
                sl = p["open"] - pa["sl_auto_pts"] * pt if p["type"] == "BUY" else p["open"] + pa["sl_auto_pts"] * pt
                self.br.modify_pos(p["ticket"], sym, round(sl, m["digits"]), p["tp"])
        if pa["pt_on"]: self._check_partial(sym, ps)


# ============================================================ COPY GIỮA CÁC TERMINAL
def parse_map(text):
    m = {}
    for part in (text or "").split(","):
        if "=" in part:
            a, b = part.split("=", 1)
            if a.strip() and b.strip(): m[a.strip().lower()] = b.strip()
    return m

class CopyEngine(threading.Thread):
    """Mỗi ~0.7 giây: đọc lệnh của tài khoản nguồn và đồng bộ sang các tài khoản nhận (mở / sửa SL-TP / chốt bớt / đóng)."""
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app, self.alive = app, True
        self.ignored, self.recent, self.last_err, self.warned = {}, {}, 0.0, set()

    def run(self):
        while self.alive:
            t0 = time.time()
            try:
                self.step()
            except RuntimeError as ex:
                if "deleted" in str(ex): return
                self.err(ex)
            except Exception as ex:
                self.err(ex)
            time.sleep(max(0.05, 0.7 - (time.time() - t0)))

    def err(self, ex):
        if time.time() - self.last_err > 30:
            self.last_err = time.time(); self.app.log(f"Copy lỗi: {ex}")

    def step(self):
        app, br = self.app, self.app.broker
        accs = app.cfg["accounts"]
        fol = [a for a in accs if a.get("enabled") and a.get("copy_on") and a.get("copy_from")]
        if not fol: return
        src_data = {}
        for sname in {a["copy_from"] for a in fol}:
            sacc = next((x for x in accs if x["name"] == sname), None)
            if not sacc or not sacc.get("enabled"): continue
            if not br.acquire(sacc, 0.6): continue
            try:
                src_data[sname] = (br.positions(), br.account())
            except Exception as ex:
                self.err(ex)
            finally:
                br.release(sacc)
        with ThreadPoolExecutor(max_workers=len(fol)) as pool:
            list(pool.map(lambda f: self.sync(f, src_data.get(f["copy_from"])), fol))

    def sync(self, f, sd):
        app, br = self.app, self.app.broker
        if not sd: return
        spos, sacc = sd
        cmag = int(app.cfg["copy_magic"]); name = f["name"]
        smag = int(f.get("copy_src_magic", -1)); smap = parse_map(f.get("copy_map", ""))
        src = [p for p in spos if not str(p.get("comment", "")).startswith("CP") and (smag < 0 or p["magic"] == smag)]
        src_all = {p["ticket"] for p in spos}
        if name not in self.ignored:                      # lần đầu: bỏ qua lệnh đang có (trừ khi chọn copy cả lệnh đang mở)
            self.ignored[name] = set() if f.get("copy_existing") else {p["ticket"] for p in src}
        with br.session(f) as ok:
            if not ok: return
            mine = {}
            for q in br.positions():
                c = str(q.get("comment", ""))
                if q["magic"] == cmag and c.startswith("CP") and c[2:].isdigit(): mine[int(c[2:])] = q
            halted = app.eval_halt(f, br.account())
            for p in src:
                t = p["ticket"]
                if t in mine or t in self.ignored[name]: continue
                if halted:
                    if (name, "h") not in self.warned: self.warned.add((name, "h")); app.log(f"[{name}] đang NGƯNG (lỗ vượt giới hạn) -> không copy lệnh mới.")
                    continue
                if time.time() - self.recent.get((name, t), 0) < 8: continue
                fsym = smap.get(p["symbol"].lower()) or br.resolve(p["symbol"])
                meta = br.meta(fsym)
                if meta is None: continue
                mode = f.get("copy_mode", "mult")
                if mode == "fixed": lot = float(f.get("copy_fixed", 0.01))
                elif mode == "equity":
                    ra = (br.account().get("equity", 0) / sacc["equity"]) if sacc.get("equity") else 1.0
                    lot = p["volume"] * ra * float(f.get("copy_mult", 1.0))
                else: lot = p["volume"] * float(f.get("copy_mult", 1.0))
                lot = norm_lot(meta, lot)
                side = p["type"]
                if f.get("copy_reverse"): side = "SELL" if side == "BUY" else "BUY"
                sl = tp = 0.0
                if f.get("copy_sltp"):
                    sl, tp = (p["sl"], p["tp"]) if not f.get("copy_reverse") else (p["tp"], p["sl"])
                self.recent[(name, t)] = time.time()
                okk, msg = br.order(fsym, side, lot, sl, tp, f"CP{t}", cmag)
                app.log(f"[{name}] COPY #{t} ← {f['copy_from']}: " + ("✔ " if okk else "✘ ") + msg)
            for t, q in list(mine.items()):
                sp = next((x for x in src if x["ticket"] == t), None)
                if t not in src_all:                       # nguồn đã đóng -> đóng bản copy
                    okk = br.close_pos(q); app.log(f"[{name}] COPY đóng #{t} (nguồn đã đóng): {'✔' if okk else '✘'}")
                    continue
                if sp is None: continue
                if f.get("copy_sltp"):
                    sl, tp = (sp["sl"], sp["tp"]) if not f.get("copy_reverse") else (sp["tp"], sp["sl"])
                    meta = br.meta(q["symbol"]); tol = max(meta["point"], 1e-9)
                    if abs(sl - q["sl"]) > tol or abs(tp - q["tp"]) > tol:
                        br.modify_pos(q["ticket"], q["symbol"], sl, tp)
                if f.get("copy_mode", "mult") in ("mult", "equity"):   # nguồn chốt bớt -> chốt bớt theo tỉ lệ
                    meta = br.meta(q["symbol"])
                    if f.get("copy_mode") == "mult": target = norm_lot(meta, sp["volume"] * float(f.get("copy_mult", 1.0)))
                    else: target = None
                    if target is not None and q["volume"] - target >= meta["vmin"] - 1e-9 and q["volume"] > target:
                        cut = round(q["volume"] - target, 3)
                        if cut >= meta["vmin"]: br.close_pos(q, cut)


# ============================================================ LIVE FEED (chart realtime + panel tự động)
class LiveFeed(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app, self.alive = app, True
        self.rates, self.key, self.t_full, self.t_pos = None, None, 0.0, 0.0
        self.syms, self.last_err = {}, 0.0
        self.pos, self.ords, self.acc, self.meta = [], [], {}, None

    def merge(self, new):
        if new is None or len(new) == 0 or self.rates is None: return
        a = self.rates
        if not a.flags.writeable: a = a.copy()
        for r in new:
            t = int(r["time"])
            if t == int(a[-1]["time"]): a[-1] = r
            elif t > int(a[-1]["time"]): a = np.concatenate([a, np.array([r], dtype=a.dtype)])
        self.rates = a[-4000:]

    def run(self):
        while self.alive:
            t0 = time.time()
            try:
                self.step()
            except RuntimeError as ex:
                if "deleted" in str(ex): return            # cửa sổ đã đóng
                if time.time() - self.last_err > 30:
                    self.last_err = time.time(); self.app.log(f"LiveFeed lỗi: {ex}")
            except Exception as ex:
                if time.time() - self.last_err > 30:
                    self.last_err = time.time(); self.app.log(f"LiveFeed lỗi: {ex}")
            time.sleep(max(0.05, 0.5 - (time.time() - t0)))

    def step(self):
        app = self.app; br = app.broker; acc = app.primary()
        if acc is None or not app.feed_on: return
        if not br.acquire(acc, 0.3): return                  # đang bận phân tích/giao dịch -> bỏ lượt
        try:
            name, want = acc["name"], app.acc_symbol(acc)
            sym = self.syms.get((name, want)) or self.syms.setdefault((name, want), br.resolve(want))
            tf = app.cfg["tf"]; key = (name, sym, tf)
            n_hist = int(app.cfg.get("chart_bars", 1500))
            if self.rates is None or key != self.key or br.demo or time.time() - self.t_full > 60:
                r = br.rates(sym, tf, n_hist)
                if r is None or len(r) < 5: return
                self.rates, self.key, self.t_full = np.array(r), key, time.time()   # copy: mảng MT5 trả về là chỉ-đọc
            else:
                self.merge(br.rates(sym, tf, 3))
            bid, ask = br.tick(sym)
            if time.time() - self.t_pos > 0.9 or self.meta is None:
                self.pos, self.ords, self.acc = br.positions(sym), br.orders(sym), br.account()
                self.meta, self.t_pos = br.meta(sym), time.time()
            if app.ops.any_auto():
                try: app.ops.auto_tick(sym)
                except Exception as ex: app.log(f"Panel auto lỗi: {ex}")
            snap = dict(ok=True, sym=sym, tf=tf, rates=self.rates.copy(), bid=bid, ask=ask, pos=list(self.pos),
                        orders=list(self.ords), acc=dict(self.acc), meta=self.meta, name=name,
                        demo=br.demo, pt=dict(app.ops.pt_lv) if app.ops.pt_lv else None)
        finally:
            br.release(acc)
        app.bus.live.emit(snap)


# ============================================================ GỘP VỚI TERMINAL MT5 (Windows)
class WinDock:
    """Tìm cửa sổ terminal MT5 (terminal64.exe) và gắn cạnh / nhúng. Chỉ chạy trên Windows."""
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    STRIP = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000   # CAPTION|THICKFRAME|SYSMENU|MIN|MAX

    def __init__(self):
        self.ok = sys.platform == "win32"
        self.saved = {}
        if self.ok:
            import ctypes
            from ctypes import wintypes
            self.ct, self.wt = ctypes, wintypes
            self.u, self.k = ctypes.windll.user32, ctypes.windll.kernel32
            self.getl = getattr(self.u, "GetWindowLongPtrW", None) or self.u.GetWindowLongW
            self.setl = getattr(self.u, "SetWindowLongPtrW", None) or self.u.SetWindowLongW
            self.getl.restype = ctypes.c_ssize_t; self.getl.argtypes = [wintypes.HWND, ctypes.c_int]
            self.setl.restype = ctypes.c_ssize_t; self.setl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            w_ = wintypes; ct_ = ctypes
            self.u.SetParent.argtypes = [w_.HWND, w_.HWND]; self.u.SetParent.restype = w_.HWND
            self.u.GetParent.argtypes = [w_.HWND]; self.u.GetParent.restype = w_.HWND
            for fn_ in ("IsWindow", "IsIconic", "IsZoomed", "IsWindowVisible"):
                getattr(self.u, fn_).argtypes = [w_.HWND]; getattr(self.u, fn_).restype = w_.BOOL
            self.u.MoveWindow.argtypes = [w_.HWND, ct_.c_int, ct_.c_int, ct_.c_int, ct_.c_int, w_.BOOL]
            self.u.ShowWindow.argtypes = [w_.HWND, ct_.c_int]
            self.u.SetWindowPos.argtypes = [w_.HWND, w_.HWND, ct_.c_int, ct_.c_int, ct_.c_int, ct_.c_int, w_.UINT]
            self.u.GetWindowRect.argtypes = [w_.HWND, ct_.POINTER(w_.RECT)]
            self.u.GetWindowTextW.argtypes = [w_.HWND, w_.LPWSTR, ct_.c_int]
            self.u.GetWindowThreadProcessId.argtypes = [w_.HWND, ct_.POINTER(w_.DWORD)]
            self.k.OpenProcess.restype = wintypes.HANDLE
            self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    def _exe(self, pid):
        h = self.k.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return ""
        try:
            buf = self.ct.create_unicode_buffer(1024); n = self.wt.DWORD(1024)
            if self.k.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(n)): return buf.value
            return ""
        finally:
            self.k.CloseHandle(h)

    def find(self):
        """Trả về list cửa sổ chính của các terminal MT5 đang chạy."""
        if not self.ok: return []
        found = {}
        Enum = self.ct.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)
        def cb(h, _l):
            if not self.u.IsWindowVisible(h): return True
            title = self.ct.create_unicode_buffer(512); self.u.GetWindowTextW(h, title, 512)
            if not title.value: return True
            pid = self.wt.DWORD(); self.u.GetWindowThreadProcessId(h, self.ct.byref(pid))
            exe = self._exe(pid.value)
            if os.path.basename(exe).lower() not in ("terminal64.exe", "terminal.exe"): return True
            r = self.wt.RECT(); self.u.GetWindowRect(h, self.ct.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if pid.value not in found or area > found[pid.value]["area"]:
                found[pid.value] = dict(hwnd=int(h), title=title.value, pid=pid.value, exe=exe, area=area)
            return True
        self.u.EnumWindows(Enum(cb), 0)
        return list(found.values())

    def pick(self, acc):
        lst = self.find()
        want = os.path.normcase((acc or {}).get("path") or "") if acc else ""
        for t in lst:
            if want and os.path.normcase(t["exe"]) == want: return t
        return lst[0] if lst else None

    def alive(self, hwnd): return bool(self.ok and self.u.IsWindow(hwnd))
    def iconic(self, hwnd): return bool(self.u.IsIconic(hwnd))

    def rect(self, hwnd):
        r = self.wt.RECT(); self.u.GetWindowRect(hwnd, self.ct.byref(r))
        return r.left, r.top, r.right, r.bottom

    def move(self, hwnd, x, y, w, h):
        self.u.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)

    WS_CHILD, WS_POPUP, WS_VISIBLE, WS_EX_APPWINDOW = 0x40000000, 0x80000000, 0x10000000, 0x00040000

    def embed(self, hwnd, parent):
        """Nhúng cửa sổ MT5 làm con của 'parent' bằng thuần Win32 (không qua Qt) và LƯU trạng thái gốc để khôi phục."""
        u = self.u
        maxed = bool(u.IsZoomed(hwnd))
        if maxed or u.IsIconic(hwnd): u.ShowWindow(hwnd, 9)                 # SW_RESTORE
        sv = dict(style=self.getl(hwnd, self.GWL_STYLE), ex=self.getl(hwnd, self.GWL_EXSTYLE),
                  rect=self.rect(hwnd), parent=int(u.GetParent(hwnd) or 0), maxed=maxed)
        self.saved[hwnd] = sv
        u.SetParent(hwnd, parent)
        self.setl(hwnd, self.GWL_STYLE, (sv["style"] & ~self.STRIP & ~self.WS_POPUP) | self.WS_CHILD | self.WS_VISIBLE)
        self.setl(hwnd, self.GWL_EXSTYLE, sv["ex"] & ~self.WS_EX_APPWINDOW)
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0040)   # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER|SHOWWINDOW

    def fit(self, hwnd, w, h):
        self.u.MoveWindow(hwnd, 0, 0, int(w), int(h), True)

    def restore(self, hwnd):
        """Tách MT5 khỏi AIMT: trả về cửa sổ độc lập, đúng style/vị trí/kích thước/trạng thái phóng to như trước khi gộp."""
        sv = self.saved.pop(hwnd, None)
        if sv is None or not self.alive(hwnd): return False
        u = self.u
        self.setl(hwnd, self.GWL_STYLE, sv["style"] & ~self.WS_CHILD)       # bỏ WS_CHILD trước khi tách cha
        u.SetParent(hwnd, sv["parent"])
        self.setl(hwnd, self.GWL_STYLE, sv["style"]); self.setl(hwnd, self.GWL_EXSTYLE, sv["ex"])
        l, t, r, b = sv["rect"]
        u.SetWindowPos(hwnd, 0, l, t, r - l, b - t, 0x0020 | 0x0004 | 0x0040)
        u.ShowWindow(hwnd, 3 if sv["maxed"] else 5)                          # SW_MAXIMIZE / SW_SHOW
        return True

    def restore_all(self):
        for h in list(self.saved):
            try: self.restore(h)
            except Exception: pass


# ============================================================ TIN TỨC
BULL_KW = ["safe-haven", "safe haven", "rate cut", "dovish", "weaker dollar", "dollar falls", "dollar slips",
           "war", "tension", "geopolit", "escalat", "attack", "conflict", "missile", "drone", "sanction"]
BEAR_KW = ["rate hike", "hawkish", "stronger dollar", "dollar rises", "dollar jumps", "ceasefire",
           "peace", "yields rise", "yields jump", "risk-on"]

def fetch_news(feeds):
    items = []
    for u in feeds:
        try:
            r = requests.get(u, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if not title: continue
                try:
                    d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone()
                except Exception:
                    d = dt.datetime.now().astimezone()
                low = title.lower()
                sc = sum(k in low for k in BULL_KW) - sum(k in low for k in BEAR_KW)
                items.append((d, title, 1 if sc > 0 else (-1 if sc < 0 else 0)))
        except Exception:
            continue
    seen, out = set(), []
    for d, t, s in sorted(items, key=lambda x: x[0], reverse=True):
        if t not in seen:
            seen.add(t); out.append((d, t, s))
    return out[:40]

# ============================================================ GEMINI
def build_prompt(sym, tf, P, news):
    b, dg = P["bars"], P["digits"]
    n = min(40, b.n)
    rows = [f"{dt.datetime.fromtimestamp(int(b.t[k]), dt.timezone.utc):%m-%d %H:%M},"
            f"{b.o[k]:.{dg}f},{b.h[k]:.{dg}f},{b.l[k]:.{dg}f},{b.c[k]:.{dg}f},{int(b.v[k])}"
            for k in range(b.n - n, b.n)]
    loc = [{"phuong_phap": r.name, "tin_hieu": {1: "BUY", -1: "SELL", 0: "NEUTRAL"}[r.sig],
            "do_tin_cay": round(r.conf), "ghi_chu": r.notes[:4]} for r in P["results"]]
    nw = [t for _, t, _ in news[:8]]
    return (
        "Bạn là chuyên gia phân tích kỹ thuật. Dựa DUY NHẤT trên ảnh chart và dữ liệu bên dưới, "
        "hãy đưa ra nhận định. Không bịa số liệu; nếu không đủ cơ sở hãy trả WAIT. "
        "Trả lời bằng tiếng Việt, ĐÚNG định dạng JSON (không markdown) với các khoá:\n"
        '{"signal":"BUY|SELL|WAIT","confidence":0-100,"entry":số,"sl":số,"tp":số,'
        '"chart":"mô tả chart/thang giá","indicator":"nhận xét volume/chỉ báo",'
        '"price_action":"cấu trúc, S/R, mẫu nến","summary":"kết luận ngắn","risks":"rủi ro chính",'
        '"position_advice":{"new_sl":số hoặc null,"new_tp":số hoặc null,"note":"chỉ khi đang có lệnh mở"}}\n'
        "sl/tp phải ở đúng phía so với entry (BUY: sl<entry<tp; SELL: sl>entry>tp), dựa trên cấu trúc giá/ATR.\n\n"
        f"Symbol: {sym}  Khung: {tf}  Giá hiện tại: {P['price']:.{dg}f}  Spread: {P['spread']} point  "
        f"ATR14: {P['atr']:.{dg}f}\n"
        f"40 nến gần nhất (time,O,H,L,C,tickvol; nến cuối đang chạy):\n" + "\n".join(rows) + "\n\n"
        "Kết quả thuật toán cục bộ:\n" + json.dumps(loc, ensure_ascii=False) + "\n\n"
        "Lệnh đang mở của app (nếu có):\n" + (P.get("pos_text") or "(không có)") + "\n\n"
        "Tin gần đây:\n- " + "\n- ".join(nw if nw else ["(không có)"])
    )

def parse_ai(txt):
    t = txt.strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m: t = m.group(0)
    d = json.loads(t)
    sig = str(d.get("signal", "WAIT")).upper()
    if sig not in ("BUY", "SELL", "WAIT"): sig = "WAIT"
    try: conf = max(0.0, min(100.0, float(d.get("confidence", 0))))
    except Exception: conf = 0.0
    d["signal"], d["confidence"] = sig, conf
    return d

# ============================================================ NHÀ CUNG CẤP AI (Gemini / ChatGPT / Claude / DeepSeek / tuỳ chỉnh)
class LLMError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg); self.status = status

def provider_label(name):
    return PROVIDERS.get(name, {}).get("label", name)

def prov_conf(cfg, name):
    """Trả về dict(key, model, base) của nhà cung cấp."""
    if name == "gemini":
        return dict(key=cfg.get("gemini_key", ""), model=cfg.get("gemini_model", ""), base="")
    d = dict(PROVIDERS[name]); d.update((cfg.get("llm") or {}).get(name, {}))
    return dict(key=d.get("key", ""), model=d.get("model", ""), base=d.get("base", ""))

def _gemini_post(cfg, body, log=None):
    """Gọi Gemini: thử lại khi 5xx, chuyển model dự phòng khi 429/404; lỗi khác (key sai...) báo ngay."""
    models = [cfg["gemini_model"]] + [m for m in cfg.get("gemini_fallbacks", []) if m and m != cfg["gemini_model"]]
    last_status, last = None, "không có model"
    for mi, model in enumerate(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt, wait in enumerate((0, 3, 8)):
            if wait: time.sleep(wait)
            try:
                r = requests.post(url, json=body, timeout=90,
                                  headers={"x-goog-api-key": cfg["gemini_key"], "Content-Type": "application/json"})
            except requests.RequestException as ex:
                last_status, last = None, f"{model}: {ex}"
                if log: log(f"Gemini {model} lỗi mạng (lần {attempt + 1}): {ex}")
                continue
            if r.status_code == 200:
                j = r.json()
                try:
                    txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
                except (KeyError, IndexError):
                    raise LLMError(f"Gemini trả về rỗng/bị chặn: {str(j)[:200]}")
                if mi > 0 and log: log(f"Gemini: đã dùng model dự phòng {model}")
                return txt, model
            last_status, last = r.status_code, f"{model} HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (500, 502, 503, 504):
                if log: log(f"Gemini {model} HTTP {r.status_code} (lần {attempt + 1}/3) -> thử lại")
                continue
            if r.status_code in (429, 404):
                if log: log(f"Gemini {model} HTTP {r.status_code} ({'hết quota/quá tần suất' if r.status_code == 429 else 'không có model'}) -> thử model dự phòng")
                break
            raise LLMError(last, r.status_code)
    raise LLMError(last, last_status)

def llm_call(cfg, provider, system, messages, png=None, json_mode=False, log=None):
    """Gọi 1 nhà cung cấp. messages: [{"role": "user"|"assistant", "content": str}]; png đính vào tin nhắn user cuối."""
    pc = prov_conf(cfg, provider)
    if not pc["key"]:
        raise LLMError(f"Chưa nhập API key cho {provider_label(provider)} (Cài đặt → AI).")
    vision = PROVIDERS[provider]["vision"]
    img = base64.b64encode(png).decode() if (png and vision) else None
    if provider == "gemini":
        contents = []
        for i, m in enumerate(messages):
            parts = [{"text": m["content"]}]
            if img and i == len(messages) - 1 and m["role"] == "user":
                parts.append({"inline_data": {"mime_type": "image/png", "data": img}})
            contents.append({"role": "user" if m["role"] == "user" else "model", "parts": parts})
        body = {"contents": contents, "generationConfig": {"temperature": 0.3}}
        if system: body["system_instruction"] = {"parts": [{"text": system}]}
        if json_mode: body["generationConfig"]["responseMimeType"] = "application/json"
        return _gemini_post(cfg, body, log)[0]
    if provider == "anthropic":
        msgs = []
        for i, m in enumerate(messages):
            if img and i == len(messages) - 1 and m["role"] == "user":
                c = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                     {"type": "text", "text": m["content"]}]
            else:
                c = m["content"]
            msgs.append({"role": m["role"], "content": c})
        body = {"model": pc["model"], "max_tokens": 3000, "messages": msgs}
        if system: body["system"] = system
        url = (pc["base"] or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        r = requests.post(url, json=body, timeout=120, headers={"x-api-key": pc["key"],
                          "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if r.status_code != 200: raise LLMError(f"Claude HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    # OpenAI / DeepSeek / tuỳ chỉnh (giao thức OpenAI chat/completions)
    if not pc["base"]:
        raise LLMError(f"Chưa nhập Base URL cho {provider_label(provider)} (Cài đặt → AI).")
    msgs = [{"role": "system", "content": system}] if system else []
    for i, m in enumerate(messages):
        if img and i == len(messages) - 1 and m["role"] == "user":
            c = [{"type": "text", "text": m["content"]}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]
        else:
            c = m["content"]
        msgs.append({"role": m["role"], "content": c})
    body = {"model": pc["model"], "messages": msgs}
    if json_mode and provider in ("openai", "deepseek"): body["response_format"] = {"type": "json_object"}
    r = requests.post(pc["base"].rstrip("/") + "/chat/completions", json=body, timeout=120,
                      headers={"Authorization": "Bearer " + pc["key"], "Content-Type": "application/json"})
    if r.status_code != 200: raise LLMError(f"{provider_label(provider)} HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise LLMError(f"{provider_label(provider)} trả về rỗng: {r.text[:200]}")

SIGNAL_SYS = ("Bạn là chuyên gia phân tích kỹ thuật. Chỉ dựa trên dữ liệu được cung cấp, không bịa số liệu. "
              "Luôn trả lời bằng đúng 1 đối tượng JSON theo định dạng yêu cầu, không kèm markdown.")

def ask_gemini(cfg, prompt, png, log=None):
    """Phân tích tín hiệu bằng Gemini (giữ tên cũ cho tương thích)."""
    d = parse_ai(llm_call(cfg, "gemini", SIGNAL_SYS, [dict(role="user", content=prompt)], png, True, log))
    d["_model"] = cfg["gemini_model"]; return d

def ask_signal(cfg, prompt, png, log=None):
    """Phân tích tín hiệu bằng nhà cung cấp đang chọn (cfg['ai_provider'])."""
    prov = cfg.get("ai_provider", "gemini")
    d = parse_ai(llm_call(cfg, prov, SIGNAL_SYS, [dict(role="user", content=prompt)], png, True, log))
    d["_model"] = f"{provider_label(prov)}:{prov_conf(cfg, prov)['model']}"; return d

# ---- prompt cho khung Chat AI
CHAT_SYS = (
    "Bạn là trợ lý giao dịch vàng/forex trong phần mềm AIMT, trả lời bằng tiếng Việt, rõ ràng, có cấu trúc. "
    "Nguyên tắc: (1) chỉ dựa vào dữ liệu bối cảnh được cung cấp và kiến thức chung, không bịa số liệu; "
    "(2) nếu thiếu dữ liệu hãy nói rõ thay vì đoán; (3) phân biệt rõ SỰ THẬT từ dữ liệu và SUY LUẬN của bạn; "
    "(4) không đảm bảo lợi nhuận, luôn nêu điều kiện vô hiệu hoá kịch bản và rủi ro; "
    "(5) khi đề xuất lệnh phải nêu: hướng, vùng vào, SL, TP, tỉ lệ RR và lý do; "
    "(6) tôn trọng khẩu vị rủi ro người dùng đã chọn trong bối cảnh; (7) trả lời gọn, ưu tiên gạch đầu dòng, tránh lan man.")

PROMPT_FULL = (
    "Hãy phân tích ĐẦY ĐỦ thị trường dựa trên toàn bộ dữ liệu bối cảnh bên dưới (giá, nến gần nhất, kết quả 8 phương pháp, "
    "kế hoạch lệnh của app, lệnh đang mở, tin tức). Trình bày theo đúng thứ tự:\n"
    "1) Bối cảnh & xu hướng hiện tại (cấu trúc HH/HL hay LH/LL, vị trí giá so với vùng premium/discount).\n"
    "2) Đọc 8 phương pháp: chỗ ĐỒNG THUẬN, chỗ MÂU THUẪN, phương pháp nào đáng tin hơn trong bối cảnh này và vì sao.\n"
    "3) Vùng quan trọng: hỗ trợ/kháng cự, FVG/Order Block, vùng PO3 (nếu có), mức thanh khoản có thể bị quét.\n"
    "4) Kịch bản CHÍNH và kịch bản PHỤ: điều kiện kích hoạt, mục tiêu, điều kiện vô hiệu hoá.\n"
    "5) Kế hoạch lệnh đề xuất: hướng, loại lệnh (thị trường/chờ), entry, SL, TP, RR, lot gợi ý theo khẩu vị rủi ro đang chọn. "
    "So sánh với kế hoạch SCALP/SWING/PO3 của app và nói rõ bạn đồng ý/không đồng ý chỗ nào.\n"
    "6) Quản lý lệnh: khi nào dời BE, chốt một phần (trim), trailing, khi nào KHÔNG nên nhồi (DCA).\n"
    "7) Rủi ro & tin tức cần lưu ý (spread, giờ thanh khoản thấp, tin có thể làm giá giật).\n"
    "8) KẾT LUẬN 1 dòng: BUY / SELL / WAIT, độ tin cậy 0-100%, và 3 điều kiện sẽ khiến bạn đổi ý.\n"
    "Đánh dấu rõ phần nào là suy luận. Không đưa số liệu không có trong dữ liệu.")
PROMPT_POS = (
    "Hãy ĐÁNH GIÁ các lệnh đang mở trong bối cảnh bên dưới: với từng lệnh nêu (a) còn phù hợp với cấu trúc giá hiện tại không, "
    "(b) SL/TP hiện tại có hợp lý theo ATR và cấu trúc không, (c) nên giữ / dời SL (chỉ siết, không nới) / chốt một phần / đóng, "
    "kèm mức giá cụ thể và lý do; (d) rủi ro tổng của rổ lệnh (tổng lot, kịch bản xấu nhất). Nếu không có lệnh nào, nói rõ.")
PROMPT_LEVELS = (
    "Hãy đề xuất ENTRY / SL / TP cụ thể cho kịch bản có xác suất tốt nhất hiện tại. Yêu cầu: SL đặt ngoài cấu trúc/vùng thanh khoản "
    "hợp lý, khoảng cách tính theo ATR; TP theo vùng cản kế tiếp; nêu RR; nếu chưa có điểm vào tốt hãy trả lời WAIT và "
    "nêu mức giá/điều kiện cần chờ. So sánh với SL/TP mà app đang đề xuất.")
PROMPT_RISK = (
    "Hãy tư vấn QUẢN TRỊ RỦI RO cho phiên hiện tại: lot hợp lý theo số dư và khẩu vị đang chọn, mức rủi ro tối đa mỗi lệnh/mỗi ngày, "
    "số lệnh tối đa, khi nào nên dừng giao dịch, và các cảnh báo (spread, tin, thanh khoản) từ dữ liệu bối cảnh.")
PROMPT_EXPLAIN = (
    "Hãy GIẢI THÍCH vì sao app đưa ra tín hiệu hiện tại (BUY/SELL/WAIT): phương pháp nào đóng góp nhiều nhất, điểm hội tụ so với "
    "ngưỡng của khẩu vị, và những điểm yếu/độ tin cậy thấp của tín hiệu này.")

# ============================================================ VẼ CHART / NODE
def chart_geom(rect):
    return rect.left() + 6, rect.right() - 72, rect.top() + 22, rect.bottom() - 20      # L, R, T, B

def nice_step(rng, n=6):
    raw = max(rng / n, 1e-9); mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw: return m * mag
    return 10 * mag

def fmt_time(t):
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%d/%m %H:%M")

def draw_chart(p, rect, bars, ov, view=None):
    """Vẽ chart. view: n_vis, shift, yman, cross, live, trades, title. Trả về dict hệ toạ độ (cho chuột)."""
    v = view or {}
    n_vis = max(10, float(v.get("n_vis", 120))); shift = float(v.get("shift", 0.0))
    yman, cross, live, tr = v.get("yman"), v.get("cross"), v.get("live"), v.get("trades") or {}
    p.fillRect(rect, QtGui.QColor(v.get("bg", "#05070d")))
    if bars is None or bars.n < 5:
        p.setPen(QtGui.QColor("#8892a6"))
        p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, "Đang tải dữ liệu chart...")
        return None
    L, R, T, B = chart_geom(rect); n = bars.n
    right_idx = n - 1 - shift; left_idx = right_idx - n_vis + 1
    i0 = max(0, int(math.ceil(left_idx))); i1 = min(n - 1, int(math.floor(right_idx)))
    if i1 < i0: i0 = i1 = n - 1
    if yman: lo, hi = yman
    else:
        hi, lo = float(bars.h[i0:i1 + 1].max()), float(bars.l[i0:i1 + 1].min())
        pad = (hi - lo) * 0.07 or 1.0; hi += pad; lo -= pad
    w = (R - L) / n_vis
    xi = lambda i: L + (i - left_idx + 0.5) * w
    yv = lambda val: T + (hi - val) / (hi - lo) * (B - T)
    f = p.font(); f.setPointSize(8); p.setFont(f)
    # lưới + nhãn giá
    step = nice_step(hi - lo); val = math.ceil(lo / step) * step
    while val <= hi:
        y = yv(val)
        p.setPen(QtGui.QPen(QtGui.QColor("#121826"), 1)); p.drawLine(L, int(y), R, int(y))
        p.setPen(QtGui.QColor("#6f7a90")); p.drawText(R + 4, int(y) + 4, f"{val:.2f}")
        val += step
    p.setClipRect(QtCore.QRect(L, T, R - L, B - T))
    for lab, a, bb, col in ov.get("zones", []):
        if bb < lo or a > hi: continue
        y1, y2 = yv(min(bb, hi)), yv(max(a, lo))
        q = QtGui.QColor(col); q.setAlpha(34)
        p.fillRect(QtCore.QRectF(L, y1, R - L, max(2.0, y2 - y1)), q)
        q.setAlpha(150); p.setPen(q); p.drawText(L + 4, int(y1) + 11, lab)
    for i in range(i0, i1 + 1):
        x = xi(i); up = bars.c[i] >= bars.o[i]
        col = QtGui.QColor("#2ecc71" if up else "#ff4d5e")
        p.setPen(QtGui.QPen(col, 1)); p.drawLine(QtCore.QPointF(x, yv(bars.h[i])), QtCore.QPointF(x, yv(bars.l[i])))
        y1, y2 = yv(max(bars.o[i], bars.c[i])), yv(min(bars.o[i], bars.c[i]))
        p.fillRect(QtCore.QRectF(x - w * 0.36, y1, max(1.0, w * 0.72), max(1.0, y2 - y1)), col)
    def hline(price, col, label, style=QtCore.Qt.PenStyle.DashLine, wd=1):
        if price < lo or price > hi: return
        y = int(yv(price)); p.setPen(QtGui.QPen(QtGui.QColor(col), wd, style)); p.drawLine(L, y, R, y)
        p.drawText(R - 230, y - 3, f"{label} {price:.2f}")
    for lab, val_, col in ov.get("levels", []): hline(val_, col, lab)
    for q_ in tr.get("pos", []):                     # lệnh đang mở (như MT5)
        buy = q_["type"] == "BUY"; c_ = "#4da3ff" if buy else "#ff9f43"
        hline(q_["open"], c_, f"{q_['type']} {q_['volume']:.2f} ({q_['profit']:+.2f})", QtCore.Qt.PenStyle.SolidLine, 2)
        if q_["sl"]: hline(q_["sl"], "#ff4d5e", "SL")
        if q_["tp"]: hline(q_["tp"], "#2ecc71", "TP")
    for o_ in tr.get("orders", []):
        hline(o_["open"], "#c792ea", f"{o_['type']} {o_['volume']:.2f}", QtCore.Qt.PenStyle.DotLine)
        if o_["sl"]: hline(o_["sl"], "#ff4d5e", "SL?")
        if o_["tp"]: hline(o_["tp"], "#2ecc71", "TP?")
    ptl = tr.get("pt")
    if ptl:
        for k, c_ in (("pt1", "#7CFC00"), ("pt2", "#00BFFF"), ("pt3", "#FF00FF")):
            if k in ptl: hline(ptl[k], c_, k.upper().replace("PT", "TP"))
    p.setClipping(False)
    # giá hiện tại (bid)
    last_px = float(live[0]) if live and live[0] else float(bars.c[-1])
    if lo <= last_px <= hi:
        y = int(yv(last_px)); p.setPen(QtGui.QPen(QtGui.QColor("#f5b942"), 1, QtCore.Qt.PenStyle.DotLine)); p.drawLine(L, y, R, y)
        p.fillRect(QtCore.QRect(R + 1, y - 8, 70, 16), QtGui.QColor("#f5b942"))
        p.setPen(QtGui.QColor("#000")); p.drawText(R + 4, y + 4, f"{last_px:.2f}")
    # nhãn thời gian
    p.setPen(QtGui.QColor("#6f7a90"))
    for k in range(5):
        idx = int(left_idx + (right_idx - left_idx) * k / 4 + 0.5)
        if 0 <= idx < n: p.drawText(int(xi(idx)) - 30, rect.bottom() - 4, fmt_time(bars.t[idx]))
    # header OHLC
    ci = n - 1; mouse_idx = None
    if cross and L <= cross[0] <= R and T <= cross[1] <= B:
        mouse_idx = int(round(left_idx + (cross[0] - L) / w - 0.5)); ci = min(max(mouse_idx, 0), n - 1)
    p.setPen(QtGui.QColor("#cfd6e6"))
    p.drawText(L + 4, rect.top() + 14, f"{v.get('title', '')}   O {bars.o[ci]:.2f}  H {bars.h[ci]:.2f}  "
               f"L {bars.l[ci]:.2f}  C {bars.c[ci]:.2f}  V {int(bars.v[ci])}   "
               f"[{'THỦ CÔNG' if yman else 'AUTO'}]  ({fmt_time(bars.t[ci])} giờ server)")
    if cross and L <= cross[0] <= R and T <= cross[1] <= B:
        p.setPen(QtGui.QPen(QtGui.QColor("#5b6478"), 1, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(int(cross[0]), T, int(cross[0]), B); p.drawLine(L, int(cross[1]), R, int(cross[1]))
        price = hi - (cross[1] - T) / (B - T) * (hi - lo)
        p.fillRect(QtCore.QRect(R + 1, int(cross[1]) - 8, 70, 16), QtGui.QColor("#2a3556"))
        p.setPen(QtGui.QColor("#fff")); p.drawText(R + 4, int(cross[1]) + 4, f"{price:.2f}")
    return dict(L=L, R=R, T=T, B=B, hi=hi, lo=lo, w=w, left=left_idx, n=n)

class ChartWidget(QtWidgets.QWidget):
    """Chart realtime: lăn chuột = zoom, kéo = cuộn, kéo trục giá = co giãn giá, Shift+kéo = dịch giá,
    double-click = về tự động, phím ←/→ cuộn, +/- zoom, Home = về nến mới nhất."""
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(250); self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.bars, self.ov, self.trades, self.live, self.title = None, {}, {}, None, ""
        self.n_vis, self.shift, self.yman, self.cross = 120.0, 0.0, None, None
        self._geo, self._drag = None, None

    # --- dữ liệu
    def set_overlay(self, ov): self.ov = ov; self.update()
    def set_data(self, bars, ov): self.bars, self.ov = bars, ov; self.update()
    def set_bars(self, bars):
        if self.bars is not None and self.shift > 0 and bars.n > self.bars.n:
            self.shift += bars.n - self.bars.n          # giữ nguyên vùng đang xem khi có nến mới
        self.bars = bars; self.update()
    def set_live(self, bid, ask): self.live = (bid, ask); self.update()
    def set_trades(self, pos, orders, pt): self.trades = dict(pos=pos, orders=orders, pt=pt); self.update()

    def view(self):
        return dict(n_vis=self.n_vis, shift=self.shift, yman=self.yman, cross=self.cross, live=self.live,
                    trades=self.trades, title=self.title, bg=getattr(self, "bg", "#05070d"))

    def paintEvent(self, e):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self._geo = draw_chart(p, self.rect(), self.bars, self.ov, self.view())

    def clamp(self):
        if self.bars is None: return
        self.n_vis = max(15.0, min(self.n_vis, float(min(self.bars.n, 1500))))
        self.shift = max(-self.n_vis * 0.4, min(self.shift, self.bars.n - 6.0))

    def wheelEvent(self, e):
        g = self._geo
        if not g or self.bars is None: return
        x = e.position().x(); f = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
        anchor = g["left"] + (x - g["L"]) / g["w"] - 0.5           # chỉ số nến dưới con trỏ
        self.n_vis = max(15.0, min(self.n_vis * f, float(min(self.bars.n, 1500))))
        w2 = (g["R"] - g["L"]) / self.n_vis
        left2 = anchor - (x - g["L"]) / w2 + 0.5
        self.shift = self.bars.n - 1 - (left2 + self.n_vis - 1)
        self.clamp(); self.update()

    def mousePressEvent(self, e):
        g = self._geo
        if not g or e.button() != QtCore.Qt.MouseButton.LeftButton: return
        pos = e.position(); self.setFocus()
        mode = "axis" if pos.x() > g["R"] else ("vshift" if e.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier else "pan")
        self._drag = dict(mode=mode, x=pos.x(), y=pos.y(), shift=self.shift, hi=g["hi"], lo=g["lo"])

    def mouseMoveEvent(self, e):
        g = self._geo; pos = e.position()
        self.cross = (pos.x(), pos.y())
        if self._drag and g:
            d = self._drag
            if d["mode"] == "pan":
                self.shift = d["shift"] + (pos.x() - d["x"]) / g["w"]; self.clamp()
            elif d["mode"] == "axis":
                c = (d["hi"] + d["lo"]) / 2; half = (d["hi"] - d["lo"]) / 2 * math.exp((pos.y() - d["y"]) * 0.006)
                self.yman = (c - half, c + half)
            else:
                dp = (pos.y() - d["y"]) / (g["B"] - g["T"]) * (d["hi"] - d["lo"])
                self.yman = (d["lo"] + dp, d["hi"] + dp)
        if g and pos.x() > g["R"]: self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
        else: self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, e): self._drag = None
    def leaveEvent(self, e): self.cross = None; self.update()
    def mouseDoubleClickEvent(self, e): self.yman = None; self.shift = 0.0; self.n_vis = 120.0; self.update()

    def keyPressEvent(self, e):
        k = e.key(); K = QtCore.Qt.Key
        if k == K.Key_Left: self.shift += 5
        elif k == K.Key_Right: self.shift -= 5
        elif k in (K.Key_Plus, K.Key_Equal): self.n_vis *= 0.85
        elif k == K.Key_Minus: self.n_vis /= 0.85
        elif k == K.Key_Home: self.shift = 0.0
        elif k == K.Key_A: self.yman = None
        else: return super().keyPressEvent(e)
        self.clamp(); self.update()

def render_png(bars, ov, w=1000, h=560):
    img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_RGB32)
    p = QtGui.QPainter(img); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    draw_chart(p, QtCore.QRect(0, 0, w, h), bars, ov, dict(n_vis=100, shift=0.0)); p.end()
    ba = QtCore.QByteArray(); buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly); img.save(buf, "PNG")
    return bytes(ba.data())

class NodeGraph(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(); self.setMinimumHeight(125)
        self.items = [(k, nm) for k, nm, _ in ANALYZERS]
        self.res, self.analyzing = {}, False
        self.title, self.sub = "KẾT NỐI MT5", "chưa kết nối"
    def set_state(self, res=None, analyzing=False, sub=None):
        if res is not None: self.res = {r.key: r for r in res}
        self.analyzing = analyzing
        if sub is not None: self.sub = sub
        self.update()
    def paintEvent(self, e):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        g = QtGui.QLinearGradient(0, 0, 0, H)
        c0, c1 = getattr(self, "bg", ("#0b1020", "#070a14"))
        g.setColorAt(0, QtGui.QColor(c0)); g.setColorAt(1, QtGui.QColor(c1))
        p.fillRect(self.rect(), g)
        cx, cy = W / 2, 30
        n = len(self.items)
        f = p.font(); f.setPointSize(8); p.setFont(f)
        for i, (k, nm) in enumerate(self.items):
            x = W * (i + 0.5) / n; y = H - 52
            r = self.res.get(k)
            if self.analyzing: col, st = "#f5b942", "Đang phân tích"
            elif r is None: col, st = "#5b6478", "Chờ tới lượt"
            elif r.state in ("Lỗi", "Thiếu dữ liệu"): col, st = "#555b6a", r.state
            elif r.sig > 0: col, st = GREEN, r.state
            elif r.sig < 0: col, st = RED, r.state
            else: col, st = BLUE, r.state
            path = QtGui.QPainterPath(); path.moveTo(cx, cy + 18)
            path.cubicTo(cx, cy + 70, x, y - 80, x, y - 16)
            q = QtGui.QColor(col); q.setAlpha(150)
            p.setPen(QtGui.QPen(q, 1.6)); p.setBrush(QtCore.Qt.BrushStyle.NoBrush); p.drawPath(path)
            p.setBrush(QtGui.QColor("#0e1426")); p.setPen(QtGui.QPen(QtGui.QColor(col), 2))
            p.drawEllipse(QtCore.QPointF(x, y), 14, 14)
            p.setPen(QtGui.QColor(col)); p.drawText(QtCore.QRectF(x - 14, y - 14, 28, 28),
                QtCore.Qt.AlignmentFlag.AlignCenter, nm[0])
            p.setPen(QtGui.QColor("#e9d9a6")); p.drawText(QtCore.QRectF(x - W / n / 2, y + 16, W / n, 14),
                QtCore.Qt.AlignmentFlag.AlignCenter, nm)
            p.setPen(QtGui.QColor(col)); p.drawText(QtCore.QRectF(x - W / n / 2, y + 30, W / n, 14),
                QtCore.Qt.AlignmentFlag.AlignCenter, st)
        p.setBrush(QtGui.QColor("#1a1530")); p.setPen(QtGui.QPen(QtGui.QColor(GOLD), 2))
        p.drawEllipse(QtCore.QPointF(cx, cy), 18, 18)
        p.setPen(QtGui.QColor(GOLD)); ff = p.font(); ff.setPointSize(14); p.setFont(ff)
        p.drawText(QtCore.QRectF(cx - 18, cy - 18, 36, 36), QtCore.Qt.AlignmentFlag.AlignCenter, "★")
        ff.setPointSize(8); ff.setBold(True); p.setFont(ff); p.setPen(QtGui.QColor("#ffd980"))
        p.drawText(QtCore.QRectF(cx + 24, cy - 14, 260, 14), QtCore.Qt.AlignmentFlag.AlignLeft, self.title)
        ff.setBold(False); p.setFont(ff); p.setPen(QtGui.QColor("#9aa4bb"))
        p.drawText(QtCore.QRectF(cx + 24, cy + 1, 320, 14), QtCore.Qt.AlignmentFlag.AlignLeft, self.sub)

# ============================================================ GIAO DIỆN
QSS = """
QWidget{background:#0a0e1a;color:#dfe6f5;font-size:11px}
QPushButton{background:#161d33;border:1px solid #2a3556;border-radius:5px;padding:4px 8px}
QPushButton:hover{background:#1e2848}
QPushButton:checked{background:#1f3b6e;border-color:#4d7dd8}
QPushButton#buy{background:#12452b;border-color:#2ecc71}
QPushButton#sell{background:#4a1820;border-color:#ff4d5e}
QComboBox,QLineEdit,QSpinBox,QDoubleSpinBox,QPlainTextEdit,QTextEdit,QListWidget,QTableWidget{
 background:#0d1324;border:1px solid #232d4b;border-radius:4px}
QTabBar::tab{background:#111830;padding:6px 14px;border:1px solid #232d4b}
QTabBar::tab:selected{background:#1b2650}
QHeaderView::section{background:#111830;border:0;padding:4px}
QSplitter::handle{background:#161d33}
QCheckBox::indicator{width:13px;height:13px;border:1px solid #4a5a8a;border-radius:3px;background:#0d1324}
QCheckBox::indicator:checked{background:#2e6bd8;border-color:#7aa2ff}
"""

QSS_BBG = """
QWidget{background:#000000;color:#ffb000;font-size:11px;font-family:Consolas,'Courier New',monospace}
QPushButton{background:#1a1200;border:1px solid #5a4000;border-radius:2px;padding:4px 8px;color:#ffb000}
QPushButton:hover{background:#2e2000}
QPushButton:checked{background:#5a4000;color:#ffffff;border-color:#ffb000}
QPushButton#buy{background:#04210d;border-color:#00c853;color:#7dffa6}
QPushButton#sell{background:#2a0606;border-color:#ff3b30;color:#ff8a80}
QComboBox,QLineEdit,QSpinBox,QDoubleSpinBox,QPlainTextEdit,QTextEdit,QListWidget,QTableWidget,QTextBrowser{
 background:#0a0700;border:1px solid #3a2a00;border-radius:2px;color:#ffd27a;selection-background-color:#5a4000}
QComboBox QAbstractItemView{background:#0a0700;color:#ffd27a;selection-background-color:#5a4000}
QTabBar::tab{background:#120d00;padding:5px 12px;border:1px solid #3a2a00;color:#c89200}
QTabBar::tab:selected{background:#2e2000;color:#ffb000}
QHeaderView::section{background:#1a1200;color:#ffb000;border:0;border-right:1px solid #3a2a00;padding:4px}
QTableWidget{gridline-color:#2a1d00}
QSplitter::handle{background:#2a1d00}
QCheckBox::indicator{width:13px;height:13px;border:1px solid #ffb000;border-radius:2px;background:#000}
QCheckBox::indicator:checked{background:#ffb000}
QScrollBar:vertical{background:#0a0700;width:10px} QScrollBar::handle:vertical{background:#5a4000;min-height:20px}
QDockWidget{color:#ffb000} QDockWidget::title{background:#1a1200;padding:4px}
"""

def _norm(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").upper().replace("Đ", "D")

HELP_KEYS = [
    ("F1", "Trợ giúp (bảng này)"), ("F2", "Nhảy vào ô lệnh (command bar)"), ("F3", "Bật/tắt Auto Trade"),
    ("F4", "Phân tích ngay"), ("F5", "Bật/tắt vòng 'Tự động' phân tích"), ("F6", "Chuyển sang tài khoản kế tiếp (xem)"),
    ("F7", "Áp dụng: TK đang xem ⇄ TẤT CẢ tài khoản"), ("F8", "Đổi chế độ đi ngang: Tắt → Lọc → Đảo chiều"),
    ("F9", "Chuyển Panel ⇄ Chat"), ("F10", "Bật/tắt phím tắt chữ (như TradePanelPro)"), ("Esc", "Huỷ lệnh đang chờ xác nhận"),
]
HELP_HK = [
    ("B / S", "Chuẩn bị BUY / SELL (rồi bấm Q xác nhận trong 4 giây, X huỷ)"), ("C / V / Z", "Đóng tất cả / chỉ Buy / chỉ Sell"),
    ("1 / 2 / 3", "Đóng 1/2: Buy / Sell / tất cả"), ("T", "Trim tất cả (theo chế độ $ hoặc % của panel)"),
    ("E", "Dời BE"), ("R", "Reverse (đảo chiều)"), ("P / L", "Đóng lệnh lãi / lệnh lỗ"),
    ("U / J", "SL + / − một bước"), ("I / K", "TP + / − một bước"), ("D", "Huỷ lệnh chờ"), ("G", "Xoá SL/TP"),
]
HELP_CMDS = [
    ("BUY [lot] / SELL [lot]", "Vào lệnh thị trường (lot tuỳ chọn)"), ("CLOSE [ALL|BUY|SELL|LOSS|PROFIT]", "Đóng lệnh"),
    ("HALF [ALL|BUY|SELL]", "Đóng 1/2"), ("TRIM [ALL|BUY|SELL] [giá trị]", "Trim; có giá trị thì đặt Net$ (chế độ $) hoặc % (chế độ %)"),
    ("BE · REV · CLR · DEL", "Dời BE · đảo chiều · xoá SL/TP · huỷ lệnh chờ"), ("SL <pts> · TP <pts>", "Chỉnh SL/TP theo points, ví dụ SL 100 hoặc TP -50"),
    ("ACC <tên>|ALL", "Chọn tài khoản đang xem / áp dụng cho tất cả"), ("AUTO [ON|OFF]", "Auto Trade"), ("LOT <số>", "Đặt lot cố định"),
    ("RISK <an toan|can bang|tan cong>", "Khẩu vị"), ("THR <số>", "Ngưỡng điểm (0 = theo khẩu vị)"), ("SIDE <OFF|FILTER|REV>", "Chế độ đi ngang"),
    ("PLAN <BEST|SCALP|SWING|PO3>", "Kế hoạch thực thi"), ("ANALYZE", "Phân tích ngay"), ("RESUME [tên]", "Mở lại tài khoản bị NGƯNG vì lỗ"),
    ("COPY <nhận> <nguồn>|OFF", "Đặt/tắt copy: tài khoản <nhận> copy từ <nguồn>"), ("HELP", "Trợ giúp"),
]

class Bus(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    local_done = QtCore.pyqtSignal(object)
    ai_done = QtCore.pyqtSignal(object)
    news_done = QtCore.pyqtSignal(object)
    snap = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()
    live = QtCore.pyqtSignal(object)
    chat_done = QtCore.pyqtSignal(object)


class AccountDialog(QtWidgets.QDialog):
    def __init__(self, acc=None, parent=None):
        super().__init__(parent); self.setWindowTitle("Tài khoản MT5"); self.resize(560, 520)
        a = acc or {}; self.new = acc is None
        names = [x["name"] for x in (parent.cfg["accounts"] if parent else []) if x["name"] != a.get("name")]
        root = QtWidgets.QVBoxLayout(self); tabs = QtWidgets.QTabWidget(); root.addWidget(tabs, 1)
        # ---- tab kết nối
        t1 = QtWidgets.QWidget(); f = QtWidgets.QFormLayout(t1)
        self.name = QtWidgets.QLineEdit(a.get("name", ""))
        self.path = QtWidgets.QLineEdit(a.get("path", ""))
        bp = QtWidgets.QPushButton("Chọn..."); bp.clicked.connect(self.browse)
        row = QtWidgets.QHBoxLayout(); row.addWidget(self.path); row.addWidget(bp)
        self.login = QtWidgets.QLineEdit(str(a.get("login") or ""))
        self.pw = QtWidgets.QLineEdit(a.get("password", "")); self.pw.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.server = QtWidgets.QLineEdit(a.get("server", "")); self.sym = QtWidgets.QLineEdit(a.get("symbol", ""))
        self.mlot = QtWidgets.QDoubleSpinBox(); self.mlot.setRange(0, 1000); self.mlot.setValue(float(a.get("max_lot") or 0))
        note = QtWidgets.QLabel("Mỗi terminal cần 1 đường dẫn terminal64.exe RIÊNG (bản portable) thì các terminal mới chạy SONG SONG. "
                                "Các tài khoản dùng chung 1 terminal sẽ được xếp hàng. Để trống login/mật khẩu nếu terminal đã tự đăng nhập; "
                                "mật khẩu (nếu nhập) lưu chữ thường trong file cấu hình.")
        note.setWordWrap(True); note.setStyleSheet("color:#9aa4bb")
        f.addRow("Tên hiển thị", self.name); f.addRow("terminal64.exe", row); f.addRow("Login", self.login)
        f.addRow("Mật khẩu", self.pw); f.addRow("Server", self.server)
        f.addRow("Symbol riêng (trống = dùng chung)", self.sym); f.addRow("Lot tối đa riêng (0 = chung)", self.mlot); f.addRow(note)
        tabs.addTab(t1, "Kết nối")
        # ---- tab rủi ro
        t2 = QtWidgets.QWidget(); f2 = QtWidgets.QFormLayout(t2)
        self.loss = QtWidgets.QDoubleSpinBox(); self.loss.setRange(0, 100); self.loss.setDecimals(1)
        self.loss.setValue(float(a.get("max_loss_pct", 20.0 if self.new else 0.0)))
        self.lmode = QtWidgets.QComboBox(); self.lmode.addItems(["Lỗ nổi: (Balance − Equity) / Balance", "Sụt từ đỉnh Equity: (Đỉnh − Equity) / Đỉnh"])
        self.lmode.setCurrentIndex(1 if a.get("loss_mode") == "peak" else 0)
        n2 = QtWidgets.QLabel("Khi mức lỗ đạt ngưỡng, tài khoản này bị NGƯNG vào lệnh mới tự động (AIMT auto, DCA, copy). "
                              "Lệnh đang mở không bị đóng. Bấm 'Mở lại' ở tab Tài khoản để cho chạy tiếp. 0 = tắt. "
                              "Lệnh bấm tay vẫn được phép nhưng app sẽ cảnh báo.")
        n2.setWordWrap(True); n2.setStyleSheet("color:#9aa4bb")
        f2.addRow("Ngừng vào lệnh khi lỗ ≥ (%)", self.loss); f2.addRow("Cách tính lỗ", self.lmode); f2.addRow(n2)
        tabs.addTab(t2, "Giới hạn lỗ")
        # ---- tab copy
        t3 = QtWidgets.QWidget(); f3 = QtWidgets.QFormLayout(t3)
        self.cfrom = QtWidgets.QComboBox(); self.cfrom.addItem("(không copy)", ""); [self.cfrom.addItem(n, n) for n in names]
        i = self.cfrom.findData(a.get("copy_from", "")); self.cfrom.setCurrentIndex(max(i, 0))
        self.cmode = QtWidgets.QComboBox(); self.cmode.addItems(["Nhân hệ số (lot nguồn × hệ số)", "Lot cố định", "Theo tỉ lệ Equity (× hệ số)"])
        self.cmode.setCurrentIndex(["mult", "fixed", "equity"].index(a.get("copy_mode", "mult")))
        self.cmult = QtWidgets.QDoubleSpinBox(); self.cmult.setRange(0.001, 1000); self.cmult.setDecimals(3); self.cmult.setValue(float(a.get("copy_mult", 1.0)))
        self.cfix = QtWidgets.QDoubleSpinBox(); self.cfix.setRange(0.001, 1000); self.cfix.setDecimals(3); self.cfix.setValue(float(a.get("copy_fixed", 0.01)))
        self.crev = QtWidgets.QCheckBox("Đảo chiều (nguồn BUY → copy SELL)"); self.crev.setChecked(bool(a.get("copy_reverse")))
        self.csl = QtWidgets.QCheckBox("Copy SL/TP và đồng bộ khi nguồn sửa"); self.csl.setChecked(bool(a.get("copy_sltp", True)))
        self.cex = QtWidgets.QCheckBox("Copy cả lệnh ĐANG MỞ lúc bật (mặc định: chỉ lệnh mới)"); self.cex.setChecked(bool(a.get("copy_existing")))
        self.cmg = QtWidgets.QSpinBox(); self.cmg.setRange(-1, 2147483647); self.cmg.setValue(int(a.get("copy_src_magic", -1)))
        self.cmap = QtWidgets.QLineEdit(a.get("copy_map", "")); self.cmap.setPlaceholderText("XAUUSDm=XAUUSD,EURUSDm=EURUSD")
        n3 = QtWidgets.QLabel("Tài khoản NÀY là bên nhận (follower): nó copy lệnh của tài khoản nguồn chọn ở trên. "
                              "Bật/tắt copy bằng ô 'Copy' ở tab Tài khoản. Lệnh copy mang magic riêng và comment CP<ticket nguồn>. "
                              "Nguồn có thể là terminal đang chạy EA (đặt Magic nguồn để chỉ copy lệnh của EA đó; -1 = copy mọi lệnh).")
        n3.setWordWrap(True); n3.setStyleSheet("color:#9aa4bb")
        f3.addRow("Copy từ tài khoản", self.cfrom); f3.addRow("Cách tính lot", self.cmode); f3.addRow("Hệ số", self.cmult)
        f3.addRow("Lot cố định", self.cfix); f3.addRow("Magic nguồn (-1 = tất cả)", self.cmg)
        f3.addRow("", self.crev); f3.addRow("", self.csl); f3.addRow("", self.cex); f3.addRow("Ánh xạ symbol (nguồn=đích)", self.cmap); f3.addRow(n3)
        tabs.addTab(t3, "Copy từ terminal khác")
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject); root.addWidget(bb)

    def browse(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Chọn terminal64.exe", "", "terminal64.exe (*.exe)")
        if p: self.path.setText(p)

    def data(self, old=None):
        try: lg = int(self.login.text().strip() or 0)
        except ValueError: lg = 0
        d = dict(old or {})
        d.update(name=self.name.text().strip() or "TK", path=self.path.text().strip(), login=lg,
                 password=self.pw.text(), server=self.server.text().strip(), symbol=self.sym.text().strip(),
                 max_lot=self.mlot.value(), max_loss_pct=self.loss.value(), loss_mode="peak" if self.lmode.currentIndex() else "float",
                 copy_from=self.cfrom.currentData() or "", copy_mode=["mult", "fixed", "equity"][self.cmode.currentIndex()],
                 copy_mult=self.cmult.value(), copy_fixed=self.cfix.value(), copy_reverse=self.crev.isChecked(),
                 copy_sltp=self.csl.isChecked(), copy_existing=self.cex.isChecked(), copy_src_magic=self.cmg.value(),
                 copy_map=self.cmap.text().strip())
        d.setdefault("enabled", True); d.setdefault("auto", False); d.setdefault("copy_on", False)
        if not d["copy_from"]: d["copy_on"] = False
        return d


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent); self.cfg = cfg; self.setWindowTitle("Cài đặt"); self.resize(620, 700)
        root = QtWidgets.QVBoxLayout(self)
        sc = QtWidgets.QScrollArea(); sc.setWidgetResizable(True); root.addWidget(sc, 1)
        inner = QtWidgets.QWidget(); f = QtWidgets.QFormLayout(inner); sc.setWidget(inner)
        def spin(lo, hi, val, dec=0):
            w = QtWidgets.QDoubleSpinBox() if dec else QtWidgets.QSpinBox()
            if dec: w.setRange(lo, hi); w.setDecimals(dec); w.setValue(val)
            else: w.setRange(int(lo), int(min(hi, 2147483647))); w.setValue(int(val))
            return w
        def pw(text):
            e = QtWidgets.QLineEdit(text); e.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password); return e
        def head(t):
            l = QtWidgets.QLabel(f"<b style='color:{GOLD}'>{t}</b>"); f.addRow(l)
        llm = cfg["llm"]
        head("AI — nhà cung cấp & khoá API (lưu dạng chữ thường trong file cấu hình)")
        self.prov = QtWidgets.QComboBox()
        for k, d in PROVIDERS.items(): self.prov.addItem(d["label"], k)
        self.prov.setCurrentIndex(list(PROVIDERS).index(cfg.get("ai_provider", "gemini")))
        self.use_ai = QtWidgets.QCheckBox("Dùng AI để phân tích tín hiệu"); self.use_ai.setChecked(cfg["use_ai"])
        self.aiw = spin(0, 10, cfg["ai_weight"], 2); self.ai_int = spin(10, 3600, cfg["ai_min_interval"])
        self.key = pw(cfg["gemini_key"]); self.model = QtWidgets.QLineEdit(cfg["gemini_model"])
        self.fb = QtWidgets.QLineEdit(",".join(cfg.get("gemini_fallbacks", [])))
        self.o_key = pw(llm["openai"]["key"]); self.o_model = QtWidgets.QLineEdit(llm["openai"]["model"])
        self.a_key = pw(llm["anthropic"]["key"]); self.a_model = QtWidgets.QLineEdit(llm["anthropic"]["model"])
        self.d_key = pw(llm["deepseek"]["key"]); self.d_model = QtWidgets.QLineEdit(llm["deepseek"]["model"])
        self.c_base = QtWidgets.QLineEdit(llm["custom"]["base"]); self.c_key = pw(llm["custom"]["key"]); self.c_model = QtWidgets.QLineEdit(llm["custom"]["model"])
        for lab, w in (("AI phân tích tín hiệu", self.prov), ("", self.use_ai), ("Trọng số AI trong điểm hội tụ", self.aiw),
                       ("Gọi AI tối thiểu mỗi (giây)", self.ai_int),
                       ("Gemini — API key", self.key), ("Gemini — model", self.model), ("Gemini — model dự phòng (dấu phẩy)", self.fb),
                       ("ChatGPT — API key", self.o_key), ("ChatGPT — model", self.o_model),
                       ("Claude — API key", self.a_key), ("Claude — model", self.a_model),
                       ("DeepSeek — API key", self.d_key), ("DeepSeek — model", self.d_model),
                       ("Tuỳ chỉnh — Base URL (…/v1)", self.c_base), ("Tuỳ chỉnh — API key", self.c_key), ("Tuỳ chỉnh — model", self.c_model)):
            f.addRow(lab, w)
        bt = QtWidgets.QPushButton("Kiểm tra AI đang chọn"); bt.clicked.connect(self.test_ai); f.addRow(bt)

        head("Giao dịch & rủi ro")
        self.sym = QtWidgets.QLineEdit(cfg["symbol"])
        self.bars = spin(150, 5000, cfg["bars"]); self.interval = spin(10, 3600, cfg["interval"])
        self.rpct = spin(0, 20, cfg["risk_pct"], 2); self.magic = spin(1, 2147483647, cfg["magic"]); self.dev = spin(0, 1000, cfg["deviation"])
        self.cool = spin(0, 1440, cfg["cooldown_min"]); self.mlot = spin(0, 1000, cfg["max_lot"], 2)
        self.msp = spin(0, 10000, cfg["max_spread"]); self.hf = spin(0, 24, cfg["hour_from"]); self.ht = spin(0, 24, cfg["hour_to"])
        self.ttl = spin(0, 1440, cfg["pending_ttl_min"]); self.cbars = spin(200, 5000, cfg["chart_bars"])
        self.thr = spin(0, 1, cfg["thr_override"], 2)
        self.theme = QtWidgets.QComboBox(); self.theme.addItems(["Bloomberg (đen – cam)", "Xanh đậm (cũ)"])
        self.theme.setCurrentIndex(0 if cfg.get("theme", "bloomberg") == "bloomberg" else 1)
        self.side_mode = QtWidgets.QComboBox(); self.side_mode.addItems(["Tắt", "Lọc: không vào lệnh khi đi ngang", "Đánh đảo chiều trong biên"])
        self.side_mode.setCurrentIndex(["off", "filter", "reversion"].index(cfg["side_mode"]))
        self.side_n = spin(30, 300, cfg["side_lookback"]); self.adx_thr = spin(5, 50, cfg["adx_thr"], 1)
        self.drift_thr = spin(0.05, 2, cfg["drift_thr"], 2); self.side_votes = spin(1, 3, cfg["side_votes"])
        self.er_thr = spin(0.05, 1, cfg["er_thr"], 2); self.side_edge = spin(0.1, 0.45, cfg["side_edge"], 2)
        self.side_rsi = QtWidgets.QCheckBox("Đảo chiều: yêu cầu RSI xác nhận (BUY ≤ 45, SELL ≥ 55)"); self.side_rsi.setChecked(cfg["side_rsi"])
        for lab, w in (("Symbol", self.sym), ("Số nến phân tích", self.bars), ("Chu kỳ tự phân tích (giây)", self.interval),
                       ("Risk % ghi đè (0 = theo khẩu vị)", self.rpct), ("Magic number", self.magic), ("Deviation (point)", self.dev),
                       ("Cooldown giữa 2 lệnh (phút)", self.cool), ("Lot tối đa mỗi lệnh (0 = tắt)", self.mlot),
                       ("Spread tối đa (point, 0 = tắt)", self.msp), ("Giờ cho phép vào lệnh (từ)", self.hf),
                       ("Giờ cho phép vào lệnh (đến)", self.ht), ("Tự huỷ lệnh chờ sau (phút, 0 = tắt)", self.ttl),
                       ("Số nến lịch sử của chart", self.cbars),
                       ("Giao diện", self.theme), ("Ngưỡng điểm (0 = theo khẩu vị)", self.thr), ("Thị trường đi ngang", self.side_mode),
                       ("Đi ngang: số nến nhìn lại", self.side_n), ("Đi ngang: ADX nhỏ hơn", self.adx_thr),
                       ("Đi ngang: ER (Kaufman) nhỏ hơn", self.er_thr), ("Đi ngang: độ trôi nhỏ hơn (× biên)", self.drift_thr),
                       ("Đi ngang: số phiếu cần đạt (1–3)", self.side_votes), ("Đảo chiều: vùng biên (0,1–0,45)", self.side_edge),
                       ("", self.side_rsi)):
            f.addRow(lab, w)

        head("Quản lý lệnh của AIMT (chạy mỗi chu kỳ phân tích)")
        self.be = QtWidgets.QCheckBox("Dời SL về hoà vốn (BE)"); self.be.setChecked(cfg["be_enabled"])
        self.bet = spin(0.2, 5, cfg["be_trigger"], 2)
        self.trail = QtWidgets.QCheckBox("Trailing stop theo ATR (sau khi đạt mức BE)"); self.trail.setChecked(cfg["trail_enabled"])
        self.trail_atr = spin(0.2, 10, cfg["trail_atr"], 2)
        self.trim = QtWidgets.QCheckBox("TRIM: chốt một phần khi đạt x R"); self.trim.setChecked(cfg["trim_enabled"])
        self.trim_r = spin(0.2, 10, cfg["trim_R"], 2); self.trim_p = spin(5, 90, cfg["trim_pct"])
        self.dca = QtWidgets.QCheckBox("DCA (nhồi khi giá đi ngược và tín hiệu vẫn cùng chiều)"); self.dca.setChecked(cfg["dca_enabled"])
        self.dca_step = spin(0.3, 20, cfg["dca_step_atr"], 2); self.dca_max = spin(1, 10, cfg["dca_max"])
        self.dca_mult = spin(1.0, 3.0, cfg["dca_mult"], 2); self.dca_tot = spin(0, 100000, cfg["dca_total_lot"], 2)
        for lab, w in (("", self.be), ("Kích hoạt BE/Trailing/TRIM khi lãi (x R)", self.bet), ("", self.trail),
                       ("Trailing: khoảng cách (x ATR)", self.trail_atr), ("", self.trim), ("TRIM: đạt (x R)", self.trim_r),
                       ("TRIM: chốt (% volume)", self.trim_p), ("", self.dca), ("DCA: bước (x ATR)", self.dca_step),
                       ("DCA: số lần tối đa", self.dca_max), ("DCA: hệ số nhân lot (1.0 = không nhân)", self.dca_mult),
                       ("DCA: TỔNG lot tối đa (bắt buộc > 0)", self.dca_tot)):
            f.addRow(lab, w)

        head("Khác")
        self.path = QtWidgets.QLineEdit(cfg["mt5_path"])
        self.demo = QtWidgets.QCheckBox("Ép chạy chế độ mô phỏng"); self.demo.setChecked(cfg["force_demo"])
        self.feeds = QtWidgets.QPlainTextEdit("\n".join(cfg["news_feeds"])); self.feeds.setFixedHeight(60)
        for lab, w in (("Đường dẫn terminal64.exe mặc định", self.path), ("", self.demo), ("RSS tin tức (mỗi dòng 1 link)", self.feeds)):
            f.addRow(lab, w)
        ok = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok.accepted.connect(self.accept); ok.rejected.connect(self.reject); root.addWidget(ok)

    def _llm(self):
        return {"openai": dict(key=self.o_key.text().strip(), model=self.o_model.text().strip(), base=PROVIDERS["openai"]["base"]),
                "anthropic": dict(key=self.a_key.text().strip(), model=self.a_model.text().strip(), base=PROVIDERS["anthropic"]["base"]),
                "deepseek": dict(key=self.d_key.text().strip(), model=self.d_model.text().strip(), base=PROVIDERS["deepseek"]["base"]),
                "custom": dict(key=self.c_key.text().strip(), model=self.c_model.text().strip(), base=self.c_base.text().strip())}

    def test_ai(self):
        try:
            c = dict(self.cfg, ai_provider=self.prov.currentData(), gemini_key=self.key.text().strip(),
                     gemini_model=self.model.text().strip(), llm=self._llm(),
                     gemini_fallbacks=[x.strip() for x in self.fb.text().split(",") if x.strip()])
            d = ask_signal(c, 'Trả JSON {"signal":"WAIT","confidence":0,"summary":"ok"}', None)
            QtWidgets.QMessageBox.information(self, "AI", f"Kết nối OK ({d.get('_model')}): " + str(d.get("summary", "")))
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "AI", str(ex))

    def apply(self):
        self.cfg.update(
            ai_provider=self.prov.currentData(), ai_min_interval=self.ai_int.value(), llm=self._llm(),
            gemini_key=self.key.text().strip(), gemini_model=self.model.text().strip(),
            gemini_fallbacks=[x.strip() for x in self.fb.text().split(",") if x.strip()],
            use_ai=self.use_ai.isChecked(), ai_weight=self.aiw.value(), symbol=self.sym.text().strip(),
            bars=self.bars.value(), interval=self.interval.value(), risk_pct=self.rpct.value(),
            magic=self.magic.value(), deviation=self.dev.value(), be_enabled=self.be.isChecked(),
            be_trigger=self.bet.value(), trail_enabled=self.trail.isChecked(), trail_atr=self.trail_atr.value(),
            trim_enabled=self.trim.isChecked(), trim_R=self.trim_r.value(), trim_pct=self.trim_p.value(),
            dca_enabled=self.dca.isChecked(), dca_step_atr=self.dca_step.value(), dca_max=self.dca_max.value(),
            dca_mult=self.dca_mult.value(), dca_total_lot=self.dca_tot.value(), pending_ttl_min=self.ttl.value(),
            cooldown_min=self.cool.value(), max_lot=self.mlot.value(), max_spread=self.msp.value(),
            hour_from=self.hf.value(), hour_to=self.ht.value(), mt5_path=self.path.text().strip(),
            force_demo=self.demo.isChecked(), chart_bars=self.cbars.value(), thr_override=self.thr.value(), theme="bloomberg" if self.theme.currentIndex() == 0 else "dark",
            side_mode=["off", "filter", "reversion"][self.side_mode.currentIndex()], side_lookback=self.side_n.value(),
            adx_thr=self.adx_thr.value(), er_thr=self.er_thr.value(), drift_thr=self.drift_thr.value(), side_votes=self.side_votes.value(), side_edge=self.side_edge.value(), side_rsi=self.side_rsi.isChecked(),
            news_feeds=[x.strip() for x in self.feeds.toPlainText().splitlines() if x.strip()])


class PanelDock(QtWidgets.QDockWidget):
    """Panel quản lý lệnh dạng gọn (4 tab) — bố cục theo TradePanelPro + điều khiển AI/auto trade."""
    QSS = """
    QGroupBox{font-size:11px;font-weight:bold;color:#e9d9a6;border:1px solid #232d4b;border-radius:5px;margin-top:9px;padding-top:6px}
    QGroupBox::title{subcontrol-origin:margin;left:6px;padding:0 3px}
    QPushButton{padding:3px 4px;font-size:11px;min-height:18px}
    QSpinBox,QDoubleSpinBox,QComboBox,QLineEdit{font-size:11px;min-height:18px;padding:0 2px}
    QSpinBox,QDoubleSpinBox{max-width:84px}
    QCheckBox{font-size:11px;spacing:4px} QLabel{font-size:11px}
    """
    def __init__(self, app):
        super().__init__("Panel", app)
        self.app, self.cfg, self.pa = app, app.cfg, app.cfg["pa"]
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setMinimumWidth(320); self.setMaximumWidth(460)
        root = QtWidgets.QWidget(); root.setStyleSheet(self.QSS)
        rv = QtWidgets.QVBoxLayout(root); rv.setContentsMargins(4, 4, 4, 4); rv.setSpacing(4)
        self.l1 = QtWidgets.QLabel("Bal: - | Eq: - | P&L: -"); self.l2 = QtWidgets.QLabel("Buy: 0 | 0.00   Sell: 0 | 0.00   Spread: -")
        self.l1.setStyleSheet("font-weight:bold;color:#dfe6f5"); self.l1.setWordWrap(True); self.l2.setWordWrap(True)
        rv.addWidget(self.l1); rv.addWidget(self.l2)
        self.cb_tgt = QtWidgets.QComboBox(); self.cb_tgt.addItems(["Áp dụng: tài khoản ĐANG XEM", "Áp dụng: TẤT CẢ tài khoản đang bật (song song)"])
        self.cb_tgt.setCurrentIndex(1 if app.cfg.get("panel_target") == "ALL" else 0)
        self.cb_tgt.currentIndexChanged.connect(lambda i: (app.cfg.update(panel_target="ALL" if i else "VIEW"), save_cfg(app.cfg), app.update_strip()))
        rv.addWidget(self.cb_tgt)
        self.tabs = QtWidgets.QTabWidget(); rv.addWidget(self.tabs, 1)
        self.setWidget(root)
        self.build()

    # ---- helpers
    def page(self, title):
        sc = QtWidgets.QScrollArea(); sc.setWidgetResizable(True); sc.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QtWidgets.QWidget(); self.cur = QtWidgets.QVBoxLayout(host)
        self.cur.setContentsMargins(2, 2, 2, 2); self.cur.setSpacing(4)
        sc.setWidget(host); self.tabs.addTab(sc, title)

    def end_page(self): self.cur.addStretch(1)

    def group(self, title):
        g = QtWidgets.QGroupBox(title); gl = QtWidgets.QGridLayout(g)
        gl.setContentsMargins(5, 8, 5, 5); gl.setHorizontalSpacing(4); gl.setVerticalSpacing(3)
        for c in range(4): gl.setColumnStretch(c, 1)
        self.cur.addWidget(g); return gl

    def btn(self, text, fn, obj=None, tip=""):
        b = QtWidgets.QPushButton(text); b.clicked.connect(lambda _=False: fn())
        if obj: b.setObjectName(obj)
        if tip: b.setToolTip(tip)
        return b

    def spin(self, store, key, lo, hi, dec=0, idx=None):
        w = QtWidgets.QDoubleSpinBox() if dec else QtWidgets.QSpinBox()
        if dec: w.setRange(lo, hi); w.setDecimals(dec)
        else: w.setRange(int(lo), int(min(hi, 2147483647)))
        cur = store[key] if idx is None else store[key][idx]
        w.setValue(cur if dec else int(cur)); w.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        def put(val):
            if idx is None: store[key] = val
            else: store[key][idx] = val
            save_cfg(self.cfg)
        w.valueChanged.connect(put)
        return w

    def chk(self, store, key, text, idx=None, tip=""):
        w = QtWidgets.QCheckBox(text)
        w.setChecked(bool(store[key] if idx is None else store[key][idx]))
        def put(on):
            if idx is None: store[key] = bool(on)
            else: store[key][idx] = bool(on)
            save_cfg(self.cfg)
        w.toggled.connect(put)
        if tip: w.setToolTip(tip)
        return w

    def lab(self, t):
        l = QtWidgets.QLabel(t); l.setStyleSheet("color:#8892a6"); l.setWordWrap(True); return l

    def pair(self, g, r, c, text, w):          # nhãn + ô nhập chiếm 2 cột lưới
        g.addWidget(self.lab(text), r, c); g.addWidget(w, r, c + 1)

    def run(self, fn): self.app.panel_run(fn)

    # ---- dựng giao diện
    def build(self):
        app, pa, cfg, B = self.app, self.pa, self.cfg, self.btn
        # ===== Tab 1: GIAO DỊCH
        self.page("Giao dịch & Quản lý")
        g = self.group("Lot / khẩu vị rủi ro")
        self.cb_lot = QtWidgets.QComboBox(); self.cb_lot.addItems(["Theo % rủi ro", "Lot cố định", "Theo $ rủi ro"])
        self.cb_lot.setCurrentIndex(["risk_pct", "fixed", "risk_usd"].index(cfg["lot_mode"]))
        self.cb_lot.currentIndexChanged.connect(lambda i: (cfg.update(lot_mode=["risk_pct", "fixed", "risk_usd"][i]), save_cfg(cfg)))
        self.cb_kv = QtWidgets.QComboBox(); self.cb_kv.addItems(list(RISK)); self.cb_kv.setCurrentText(cfg["risk_mode"])
        self.cb_kv.currentTextChanged.connect(lambda t: app.cb_risk.setCurrentText(t))
        g.addWidget(self.cb_lot, 0, 0, 1, 2); g.addWidget(self.cb_kv, 0, 2, 1, 2)
        self.sp_lot = self.spin(cfg, "lot_fixed", 0.001, 1000, 3)
        self.pair(g, 1, 0, "Lot", self.sp_lot); self.pair(g, 1, 2, "Risk %", self.spin(cfg, "risk_pct", 0, 20, 2))
        self.pair(g, 2, 0, "Risk $", self.spin(cfg, "risk_usd", 0, 1000000, 2)); self.pair(g, 2, 2, "SL pts", self.spin(pa, "sl_pts", 0, 100000))
        self.pair(g, 3, 0, "TP pts", self.spin(pa, "tp_pts", 0, 100000)); g.addWidget(B("Tính lot", app.panel_calc_lot), 3, 2, 1, 2)
        sp_thr = self.spin(cfg, "thr_override", 0, 1, 2); sp_thr.valueChanged.connect(lambda _v: app.recompute_plan())
        self.pair(g, 4, 0, "Ngưỡng điểm", sp_thr); g.addWidget(self.lab("0 = theo khẩu vị"), 4, 2, 1, 2)
        self.cb_side = QtWidgets.QComboBox(); self.cb_side.addItems(["Đi ngang: Tắt", "Đi ngang: Lọc (không vào lệnh)", "Đi ngang: Đánh đảo chiều trong biên"])
        self.cb_side.setCurrentIndex(["off", "filter", "reversion"].index(cfg["side_mode"]))
        self.cb_side.currentIndexChanged.connect(lambda i: (cfg.update(side_mode=["off", "filter", "reversion"][i]), save_cfg(cfg), app.recompute_plan()))
        g.addWidget(self.cb_side, 5, 0, 1, 4)
        g.addWidget(self.lab("Risk % = 0 → dùng % của khẩu vị đang chọn"), 6, 0, 1, 4)

        g = self.group("Vào lệnh")
        g.addWidget(B("SELL", lambda: app.panel_order("SELL"), "sell"), 0, 0, 1, 2)
        g.addWidget(B("BUY", lambda: app.panel_order("BUY"), "buy"), 0, 2, 1, 2)
        g.addWidget(self.chk(pa, "use_ai_levels", "SL/TP theo kế hoạch AI (thay SL/TP pts)"), 1, 0, 1, 4)
        g.addWidget(B("Buy Stop", lambda: app.panel_pending("STOP", "BUY"), "buy"), 2, 0, 1, 2)
        g.addWidget(B("Sell Stop", lambda: app.panel_pending("STOP", "SELL"), "sell"), 2, 2, 1, 2)
        g.addWidget(B("Buy Limit", lambda: app.panel_pending("LIMIT", "BUY"), "buy"), 3, 0, 1, 2)
        g.addWidget(B("Sell Limit", lambda: app.panel_pending("LIMIT", "SELL"), "sell"), 3, 2, 1, 2)
        self.pair(g, 4, 0, "Khoảng cách (pts)", self.spin(pa, "gap_pts", 0, 100000)); g.addWidget(B("Huỷ lệnh chờ", lambda: self.run(lambda o, s: o.delete_pendings(s))), 4, 2, 1, 2)

        g = self.group("Đóng / Trim")
        g.addWidget(B("Close Sell", lambda: self.run(lambda o, s: o.close_side(s, -1)), "sell"), 0, 0, 1, 1)
        g.addWidget(B("Close ALL", lambda: self.run(lambda o, s: o.close_all(s))), 0, 1, 1, 2)
        g.addWidget(B("Close Buy", lambda: self.run(lambda o, s: o.close_side(s, 1)), "buy"), 0, 3, 1, 1)
        g.addWidget(B("1/2 Sell", lambda: self.run(lambda o, s: o.close_half(s, -1)), "sell"), 1, 0, 1, 1)
        g.addWidget(B("1/2 ALL", lambda: self.run(lambda o, s: o.close_half(s, 0))), 1, 1, 1, 2)
        g.addWidget(B("1/2 Buy", lambda: self.run(lambda o, s: o.close_half(s, 1)), "buy"), 1, 3, 1, 1)
        g.addWidget(B("Trim Sell", lambda: self.trim(-1), "sell"), 2, 0, 1, 1)
        g.addWidget(B("Trim ALL", lambda: self.trim(0)), 2, 1, 1, 2)
        g.addWidget(B("Trim Buy", lambda: self.trim(1), "buy"), 2, 3, 1, 1)
        self.b_mode = QtWidgets.QPushButton("$"); self.b_mode.setCheckable(True); self.b_mode.setChecked(pa["trim_matched"])
        self.b_mode.setToolTip("$ = Profit-Matched (xoá lệnh lỗ bằng lệnh lãi, đạt Net$)   % = cắt thẳng % mỗi lệnh")
        self.tv = QtWidgets.QDoubleSpinBox(); self.tv.setRange(0, 1000000); self.tv.setDecimals(2)
        self.tv.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.b_mode.toggled.connect(self.on_mode); self.tv.valueChanged.connect(self.on_tv); self.on_mode(pa["trim_matched"], first=True)
        g.addWidget(self.lab("Chế độ trim / giá trị"), 3, 0, 1, 2); g.addWidget(self.b_mode, 3, 2); g.addWidget(self.tv, 3, 3)
        g.addWidget(self.chk(pa, "trim_exact", "Cắt lẻ lệnh lãi cuối cho đúng Net$"), 4, 0, 1, 4)
        g.addWidget(B("Close Loss", lambda: self.run(lambda o, s: o.close_profit(s, False)), "sell"), 5, 0, 1, 1)
        g.addWidget(B("Clr SL/TP", lambda: self.run(lambda o, s: o.clear_sltp(s))), 5, 1, 1, 2)
        g.addWidget(B("Close Profit", lambda: self.run(lambda o, s: o.close_profit(s, True)), "buy"), 5, 3, 1, 1)
        g.addWidget(B("BE", lambda: self.run(lambda o, s: o.move_all_be(s))), 6, 0, 1, 2)
        g.addWidget(B("Reverse", lambda: self.run(lambda o, s: o.reverse_all(s))), 6, 2, 1, 2)

        g = self.group("Chỉnh SL / TP (points)")
        g.addWidget(B("SL -", lambda: self.adj(True, -1), "sell"), 0, 0); g.addWidget(B("SL +", lambda: self.adj(True, 1), "sell"), 0, 1)
        g.addWidget(B("TP -", lambda: self.adj(False, -1), "buy"), 0, 2); g.addWidget(B("TP +", lambda: self.adj(False, 1), "buy"), 0, 3)
        self.pair(g, 1, 0, "Bước (pts)", self.spin(pa, "step_pts", 1, 100000))
        self.e_sl = QtWidgets.QLineEdit("0.0"); self.e_tp = QtWidgets.QLineEdit("0.0")
        g.addWidget(self.e_sl, 2, 0, 1, 2); g.addWidget(B("Move SL", lambda: self.run(lambda o, s: o.move_price(s, True, self.fl(self.e_sl))), "sell"), 2, 2, 1, 2)
        g.addWidget(self.e_tp, 3, 0, 1, 2); g.addWidget(B("Move TP", lambda: self.run(lambda o, s: o.move_price(s, False, self.fl(self.e_tp))), "buy"), 3, 2, 1, 2)

        g = self.group("Phạm vi tác động của panel")
        self.cb_scope = QtWidgets.QComboBox(); self.cb_scope.addItems(["Chỉ lệnh AIMT (magic của app)", "Tất cả lệnh của symbol", "Magic tuỳ chọn (vd EA khác)"])
        self.cb_scope.setCurrentIndex(["AIMT", "ALL", "MAGIC"].index(cfg["panel_scope"]))
        self.cb_scope.currentIndexChanged.connect(lambda i: (cfg.update(panel_scope=["AIMT", "ALL", "MAGIC"][i]), save_cfg(cfg)))
        g.addWidget(self.cb_scope, 0, 0, 1, 4); self.pair(g, 1, 0, "Magic tuỳ chọn", self.spin(cfg, "panel_magic", 0, 2147483647))
        self.end_page()

        # ===== Tab 3: TỰ ĐỘNG
        self.page("Tự động")
        g = self.group("BE / Cắt RR")
        g.addWidget(self.chk(pa, "be_on", "Auto BE khi lãi ≥ $"), 0, 0, 1, 2); g.addWidget(self.spin(pa, "be_usd", 0, 1e6, 2), 0, 2); g.addWidget(self.spin(pa, "be_plus", 0, 10000), 0, 3)
        g.addWidget(self.lab("(cột phải: BE + pts)"), 1, 0, 1, 4)
        g.addWidget(self.chk(pa, "rr_on", "Cắt khi đạt RR ≥"), 2, 0, 1, 2); g.addWidget(self.spin(pa, "rr_val", 0.1, 20, 2), 2, 2); g.addWidget(self.spin(pa, "rr_pct", 1, 99, 1), 2, 3)
        g.addWidget(self.lab("(cột phải: cắt % volume)"), 3, 0, 1, 3); g.addWidget(self.chk(pa, "rr_be", "BE sau cắt"), 3, 3)
        g = self.group("Trailing / Auto SL")
        g.addWidget(self.chk(pa, "trail_on", "Trailing stop"), 0, 0, 1, 4)
        self.pair(g, 1, 0, "Bắt đầu (pts)", self.spin(pa, "trail_start", 0, 100000)); self.pair(g, 1, 2, "Khoảng (pts)", self.spin(pa, "trail_dist", 0, 100000))
        self.pair(g, 2, 0, "Bước (pts)", self.spin(pa, "trail_step", 0, 100000))
        g.addWidget(self.chk(pa, "sl_on", "Tự đặt SL cho lệnh chưa có SL (pts)"), 3, 0, 1, 3); g.addWidget(self.spin(pa, "sl_auto_pts", 1, 100000), 3, 3)
        g = self.group("Partial TP")
        g.addWidget(self.chk(pa, "pt_on", "Bật Partial TP"), 0, 0, 1, 2)
        self.cb_pt = QtWidgets.QComboBox(); self.cb_pt.addItems(["BUY", "SELL"]); g.addWidget(self.cb_pt, 0, 2)
        g.addWidget(self.btn("Áp dụng/Huỷ", lambda: self.run(lambda o, s: o.toggle_partial(s, self.cb_pt.currentText()))), 0, 3)
        g.addWidget(self.lab("Mức"), 1, 0); g.addWidget(self.lab("Chốt %"), 1, 1); g.addWidget(self.lab("Cách giá (pts)"), 1, 2, 1, 2)
        for i, k in enumerate(("pt1", "pt2", "pt3")):
            g.addWidget(self.chk(pa, k, f"TP{i + 1}", idx=0), 2 + i, 0); g.addWidget(self.spin(pa, k, 1, 100, 1, idx=1), 2 + i, 1)
            g.addWidget(self.spin(pa, k, 1, 100000, 0, idx=2), 2 + i, 2, 1, 2)
        g = self.group("Quản lý vốn (đóng tất cả khi chạm ngưỡng)")
        g.addWidget(self.chk(pa, "mm_on", "Bật quản lý vốn"), 0, 0, 1, 4)
        for i, (k, t, dec) in enumerate((("cap_profit", "Lãi $", 2), ("cap_loss", "Lỗ $", 2), ("cap_profit_pct", "Lãi %", 2),
                                         ("cap_loss_pct", "Lỗ %", 2), ("cap_dd", "DD% đỉnh", 2),
                                         ("max_orders", "Max lệnh", 0), ("max_lots", "Max lot", 2))):
            self.pair(g, 1 + i // 2, (i % 2) * 2, t, self.spin(pa, k, 0, 1e9, dec))
        g.addWidget(self.lab("Giá trị 0 = tắt điều kiện đó"), 5, 0, 1, 4)
        self.end_page()

        # ===== Tab 4: AI
        self.page("AI")
        g = self.group("Tự động vào lệnh theo tín hiệu AI")
        self.c_auto = QtWidgets.QCheckBox("Auto Trade (TK đánh dấu Auto)")
        self.c_auto.setChecked(cfg["auto_trade"]); self.c_auto.clicked.connect(lambda on: app.set_auto_trade(on))
        g.addWidget(self.c_auto, 0, 0, 1, 4)
        self.cb_ai = QtWidgets.QComboBox(); self.cb_ai.addItems(["SL/TP theo ATR", "SL/TP tự động theo AI"])
        self.cb_ai.setCurrentIndex(1 if cfg["ai_levels"] == "auto" else 0)
        self.cb_ai.currentIndexChanged.connect(lambda i: (cfg.update(ai_levels="auto" if i else "off"), save_cfg(cfg), app.refresh_plan()))
        g.addWidget(self.lab("SL/TP kế hoạch"), 1, 0, 1, 1); g.addWidget(self.cb_ai, 1, 1, 1, 3)
        g.addWidget(self.chk(cfg, "ai_adjust", "AI chỉnh SL/TP lệnh mở (chỉ siết SL)"), 2, 0, 1, 4)
        g.addWidget(self.chk(cfg, "dca_enabled", "DCA khi giá đi ngược"), 3, 0, 1, 4)
        g.addWidget(self.lab("DCA cần đặt TỔNG lot tối đa ở Cài đặt. SL/TP của AI chỉ được dùng khi hợp lệ (đúng phía, khoảng cách theo ATR), nếu không sẽ quay về ATR."), 4, 0, 1, 4)
        self.end_page()
        self.tabs.setCurrentIndex(0)

    def fl(self, e):
        try: return float(e.text().replace(",", "."))
        except ValueError: return 0.0

    def on_mode(self, matched, first=False):
        self.pa["trim_matched"] = bool(matched); self.b_mode.setText("$" if matched else "%")
        self.tv.blockSignals(True); self.tv.setValue(self.pa["trim_net" if matched else "trim_pct"]); self.tv.blockSignals(False)
        if not first: save_cfg(self.cfg)

    def on_tv(self, v):
        self.pa["trim_net" if self.pa["trim_matched"] else "trim_pct"] = v; save_cfg(self.cfg)

    def trim(self, side):
        pa = self.pa
        if pa["trim_matched"]:
            self.run(lambda o, s: o.trim_matched(s, side, pa["trim_net"], pa["trim_exact"], pa["trim_close_comm"]))
        else:
            self.run(lambda o, s: o.trim_percent(s, side, pa["trim_pct"]))

    def adj(self, is_sl, sign):
        pts = sign * self.pa["step_pts"]
        self.run(lambda o, s: o.adjust(s, is_sl, pts))

    def update_stats(self, d):
        a = d.get("acc") or {}
        if a:
            self.l1.setText(f"Bal {a['balance']:.2f} | Eq {a['equity']:.2f} | P&L {a['equity'] - a['balance']:+.2f} {a.get('currency', '')}")
        pos = d.get("pos", [])
        b = [p for p in pos if p["type"] == "BUY"]; s = [p for p in pos if p["type"] == "SELL"]
        sp = (d.get("meta") or {}).get("spread", "-")
        self.l2.setText(f"Buy {len(b)}|{sum(p['volume'] for p in b):.2f}  Sell {len(s)}|{sum(p['volume'] for p in s):.2f}  Spread {sp}")


def md_lite(t):
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"^#{1,4}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    return t.replace("\n", "<br>")


class ChatDock(QtWidgets.QDockWidget):
    """Chat với AI (Gemini / ChatGPT / Claude / DeepSeek / tuỳ chỉnh) có thể tự đính kèm dữ liệu phân tích."""
    def __init__(self, app):
        super().__init__("Chat AI", app)
        self.app, self.cfg, self.hist, self.busy = app, app.cfg, [], False
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setMinimumWidth(320); self.setMaximumWidth(520)
        root = QtWidgets.QWidget(); root.setStyleSheet(PanelDock.QSS)
        v = QtWidgets.QVBoxLayout(root); v.setContentsMargins(4, 4, 4, 4); v.setSpacing(4)
        r1 = QtWidgets.QHBoxLayout()
        self.cb = QtWidgets.QComboBox()
        for k, d in PROVIDERS.items(): self.cb.addItem(d["label"], k)
        self.cb.setCurrentIndex(list(PROVIDERS).index(self.cfg.get("chat_provider", "gemini")))
        self.e_model = QtWidgets.QLineEdit(); self.e_model.setPlaceholderText("model")
        r1.addWidget(self.cb, 3); r1.addWidget(self.e_model, 2); v.addLayout(r1)
        self.cb.currentIndexChanged.connect(self.on_prov); self.e_model.editingFinished.connect(self.on_model)
        self.c_ctx = QtWidgets.QCheckBox("Kèm dữ liệu phân tích hiện tại (giá, nến, 8 phương pháp, lệnh, tin)")
        self.c_img = QtWidgets.QCheckBox("Kèm ảnh chart (chỉ AI có vision)")
        self.c_auto = QtWidgets.QCheckBox("Tự gửi prompt phân tích đầy đủ sau mỗi lần phân tích")
        for w_, k in ((self.c_ctx, "chat_ctx"), (self.c_img, "chat_img"), (self.c_auto, "chat_auto")):
            w_.setChecked(bool(self.cfg.get(k)))
            w_.toggled.connect(lambda on, k=k: (self.cfg.update({k: bool(on)}), save_cfg(self.cfg)))
            v.addWidget(w_)
        g = QtWidgets.QGridLayout(); g.setSpacing(3)
        for i, (t, p_) in enumerate((("Phân tích đầy đủ", PROMPT_FULL), ("Đánh giá lệnh mở", PROMPT_POS),
                                     ("Đề xuất SL/TP", PROMPT_LEVELS), ("Quản trị rủi ro", PROMPT_RISK),
                                     ("Giải thích tín hiệu", PROMPT_EXPLAIN))):
            b = QtWidgets.QPushButton(t); b.clicked.connect(lambda _=False, p_=p_, t=t: self.send(p_, shown=t, force_ctx=True))
            g.addWidget(b, i // 2, i % 2)
        v.addLayout(g)
        self.view = QtWidgets.QTextBrowser(); self.view.setOpenLinks(False); v.addWidget(self.view, 1)
        self.inp = QtWidgets.QPlainTextEdit(); self.inp.setFixedHeight(66)
        self.inp.setPlaceholderText("Hỏi AI về trading... (Ctrl+Enter để gửi)"); v.addWidget(self.inp)
        r2 = QtWidgets.QHBoxLayout()
        self.b_send = QtWidgets.QPushButton("Gửi"); self.b_send.clicked.connect(self.on_send)
        b_clr = QtWidgets.QPushButton("Xoá hội thoại"); b_clr.clicked.connect(self.clear)
        r2.addWidget(self.b_send, 2); r2.addWidget(b_clr, 1); v.addLayout(r2)
        self.lbl = QtWidgets.QLabel(""); self.lbl.setStyleSheet("color:#8892a6"); self.lbl.setWordWrap(True); v.addWidget(self.lbl)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            QtGui.QShortcut(QtGui.QKeySequence(key), self.inp, activated=self.on_send)
        self.setWidget(root); self.on_prov(self.cb.currentIndex())
        app.bus.chat_done.connect(self.on_done)

    def prov(self): return self.cb.currentData()

    def on_prov(self, _i):
        p = self.prov(); self.cfg["chat_provider"] = p; save_cfg(self.cfg)
        self.e_model.setText(prov_conf(self.cfg, p)["model"])
        self.c_img.setEnabled(PROVIDERS[p]["vision"])
        self.lbl.setText("" if prov_conf(self.cfg, p)["key"] else f"Chưa có API key cho {provider_label(p)} — nhập ở Cài đặt → AI.")

    def on_model(self):
        p, m = self.prov(), self.e_model.text().strip()
        if p == "gemini": self.cfg["gemini_model"] = m
        else: self.cfg["llm"][p]["model"] = m
        save_cfg(self.cfg)

    def clear(self): self.hist = []; self.view.clear()

    def bubble(self, who, text, color):
        self.view.append(f"<div style='margin-top:6px'><b style='color:{color}'>{html.escape(who)}</b><br>{md_lite(text)}</div>")
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def on_send(self):
        t = self.inp.toPlainText().strip()
        if t and self.send(t): self.inp.clear()

    def auto_send(self):
        now = time.time()
        if self.busy or now - getattr(self, "last_auto", 0.0) < self.cfg["ai_min_interval"]: return
        if prov_conf(self.cfg, self.prov())["key"]:
            self.last_auto = now
            self.send(PROMPT_FULL, shown="Phân tích đầy đủ (tự động)", force_ctx=True)

    def send(self, text, shown=None, force_ctx=False):
        if self.busy:
            self.lbl.setText("Đang chờ phản hồi trước đó..."); return False
        prov = self.prov()
        if not prov_conf(self.cfg, prov)["key"]:
            self.lbl.setText(f"Chưa có API key cho {provider_label(prov)} — nhập ở Cài đặt → AI."); return False
        use_ctx = self.c_ctx.isChecked() or force_ctx
        png = self.app.chat_png() if (self.c_img.isChecked() and PROVIDERS[prov]["vision"]) else None
        ctx = self.app.chat_context() if use_ctx else ""
        content = (ctx + "\n\n=====\nYÊU CẦU CỦA TÔI:\n" + text) if ctx else text
        msgs = self.hist[-12:] + [dict(role="user", content=content)]
        while msgs and msgs[0]["role"] != "user": msgs.pop(0)
        self.bubble("Bạn", shown or text, "#7aa2ff")
        self.busy = True; self.b_send.setEnabled(False); self.lbl.setText(f"Đang hỏi {provider_label(prov)}...")
        cfg, log, bus = self.cfg, self.app.log, self.app.bus
        def job():
            try:
                out = llm_call(cfg, prov, CHAT_SYS, msgs, png, False, log)
                bus.chat_done.emit(dict(ok=True, text=out, prov=prov, user=text))
            except Exception as ex:
                bus.chat_done.emit(dict(ok=False, err=str(ex), prov=prov, user=text, status=getattr(ex, "status", None)))
        threading.Thread(target=job, daemon=True).start()
        return True

    def on_done(self, r):
        self.busy = False; self.b_send.setEnabled(True); self.lbl.setText("")
        if r["ok"]:
            self.hist += [dict(role="user", content=r["user"]), dict(role="assistant", content=r["text"])]
            self.bubble(provider_label(r["prov"]), r["text"] or "(rỗng)", GOLD)
        else:
            tip = " (hết quota hoặc gọi quá dày — chờ một lúc hoặc đổi nhà cung cấp)" if r.get("status") == 429 else ""
            self.bubble("Lỗi", r["err"] + tip, RED)


class WebAIDock(QtWidgets.QDockWidget):
    """Dùng tài khoản AI trên web (không cần API): chép prompt/ảnh vào clipboard, dán vào chat web, rồi nhận lại phản hồi."""
    SITES = [("Gemini", "https://gemini.google.com/app"), ("ChatGPT", "https://chatgpt.com/"),
             ("Claude", "https://claude.ai/new"), ("DeepSeek", "https://chat.deepseek.com/")]
    def __init__(self, app):
        super().__init__("AI Web", app); self.app, self.cfg, self.view = app, app.cfg, None
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setMinimumWidth(340)
        root = QtWidgets.QWidget(); root.setStyleSheet(PanelDock.QSS)
        self.v = QtWidgets.QVBoxLayout(root); self.v.setContentsMargins(4, 4, 4, 4); self.v.setSpacing(4)
        info = QtWidgets.QLabel("Dùng tài khoản AI trên web (không cần API key, không dính quota API). Quy trình bán tự động, bạn giữ quyền gửi:\n"
                                "② Chép ẢNH → dán vào ô chat web.  ① Chép PROMPT → dán và gửi.  Khi AI trả lời: bôi đen toàn bộ câu trả lời, Ctrl+C, "
                                "rồi bấm ③ — AIMT tự đọc JSON trong câu trả lời và dùng làm tín hiệu AI.")
        info.setWordWrap(True); info.setStyleSheet("color:#9aa4bb"); self.v.addWidget(info)
        r = QtWidgets.QHBoxLayout()
        self.cb = QtWidgets.QComboBox(); [self.cb.addItem(n, u) for n, u in self.SITES]
        b_ext = QtWidgets.QPushButton("Mở ngoài trình duyệt"); b_ext.clicked.connect(lambda: webbrowser.open(self.cb.currentData()))
        r.addWidget(self.cb, 1); r.addWidget(b_ext)
        if HAS_WEB:
            b_in = QtWidgets.QPushButton("Mở trong khung"); b_in.clicked.connect(self.open_inline); r.addWidget(b_in)
        self.v.addLayout(r)
        if not HAS_WEB:
            l = QtWidgets.QLabel("Chưa cài PyQt6-WebEngine nên không nhúng được trình duyệt. Dùng 'Mở ngoài trình duyệt' "
                                 "hoặc chạy: pip install PyQt6-WebEngine"); l.setWordWrap(True); l.setStyleSheet("color:#f5b942"); self.v.addWidget(l)
        self.holder = QtWidgets.QVBoxLayout(); self.v.addLayout(self.holder, 1)
        g = QtWidgets.QGridLayout(); g.setSpacing(3)
        def mk(t, fn):
            b = QtWidgets.QPushButton(t); b.clicked.connect(lambda _=False, f=fn: f()); return b
        g.addWidget(mk("② Chép ẢNH chart", self.copy_image), 0, 0); g.addWidget(mk("① Chép PROMPT", self.copy_prompt), 0, 1)
        g.addWidget(mk("③ Nhận phản hồi từ clipboard → tín hiệu AI", self.read_reply), 1, 0, 1, 2)
        g.addWidget(mk("④ Hiện phản hồi vào Chat", self.to_chat), 2, 0, 1, 2)
        self.v.addLayout(g)
        self.lbl = QtWidgets.QLabel(""); self.lbl.setWordWrap(True); self.lbl.setStyleSheet(f"color:{GOLD}"); self.v.addWidget(self.lbl)
        self.setWidget(root)

    def msg(self, t): self.lbl.setText(t); self.app.log("[AI Web] " + t)

    def open_inline(self):
        if self.view is None:
            prof = QWebEngineProfile("aimt_web", self)
            d = os.path.join(BASE_DIR, "webprofile"); prof.setPersistentStoragePath(d); prof.setCachePath(os.path.join(d, "cache"))
            self.view = QWebEngineView(); self.view.setPage(QWebEnginePage(prof, self.view)); self.holder.addWidget(self.view)
        self.view.setUrl(QtCore.QUrl(self.cb.currentData()))
        self.msg("Đã mở trong khung. Nếu trang đăng nhập Google từ chối trình duyệt nhúng, hãy dùng 'Mở ngoài trình duyệt' và làm theo các bước chép/dán.")

    def copy_image(self):
        png = self.app.chat_png()
        if not png: self.msg("Chưa có dữ liệu — bấm Phân tích trước."); return
        QtWidgets.QApplication.clipboard().setImage(QtGui.QImage.fromData(png))
        self.msg("Đã chép ẢNH chart vào clipboard — dán (Ctrl+V) vào ô chat web.")

    def copy_prompt(self):
        P = self.app.payload
        if not P: self.msg("Chưa có dữ liệu — bấm Phân tích trước."); return
        txt = "(Tôi dán kèm ảnh chart ở tin nhắn này hoặc ngay trước đó.)\n\n" + build_prompt(P["sym"], self.cfg["tf"], P, self.app.news)
        QtWidgets.QApplication.clipboard().setText(txt)
        self.msg(f"Đã chép PROMPT ({len(txt)} ký tự) — dán vào ô chat web rồi gửi.")

    def read_reply(self):
        txt = QtWidgets.QApplication.clipboard().text()
        if not txt.strip(): self.msg("Clipboard trống — hãy bôi đen câu trả lời của AI và Ctrl+C trước."); return
        try:
            d = parse_ai(txt)
        except Exception:
            self.msg("Không tìm thấy JSON hợp lệ trong clipboard. Hãy chép TOÀN BỘ câu trả lời (có khối {...}); hoặc dùng ④ để xem ở Chat."); return
        if self.app.apply_external_ai(d):
            self.msg(f"Đã nhận: {d['signal']} {d['confidence']:.0f}% — đã dùng làm tín hiệu AI.")
        else:
            self.msg("Chưa có dữ liệu phân tích — bấm Phân tích trước rồi nhận lại.")

    def to_chat(self):
        txt = QtWidgets.QApplication.clipboard().text()
        if not txt.strip(): self.msg("Clipboard trống."); return
        self.app.chat.bubble("AI Web", txt, GOLD); self.app.show_dock(self.app.chat)


class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg(); self.bus = Bus()
        self.broker = DemoBroker(self.cfg, self.log) if (not HAS_MT5 or self.cfg["force_demo"]) \
            else MT5Hub(self.cfg, self.log)
        self.busy = False; self.payload = None; self.ai = None; self.plan = None; self.news = []
        self.snaps, self.infos, self.last_bar, self.last_ts = {}, {}, {}, {}
        self._refreshing = False; self.cur_sym = None; self.last_live = None; self.feed_on = True
        self.ops = PanelOps(self.broker, self.cfg, self.log)
        self.windock = WinDock(); self.dock_hwnd = None; self.embedded = None
        self.last_ai, self.last_ai_ts, self.last_ai_try, self.ai_block_until = None, 0.0, 0.0, 0.0
        self.auto_msg = ""; self.halt = {}; self._last_save = 0.0
        self.remain = self.cfg["interval"]; self.news_remain = 1
        self.setWindowTitle(APP_NAME); self.armed = None; self.apply_theme()
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.resize(min(1280, int(scr.width() * 0.96)), min(860, int(scr.height() * 0.92)))
        self.move(scr.left() + 4, scr.top() + 4)
        self.build_ui()
        self.panel = PanelDock(self); self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.resizeDocks([self.panel], [350], QtCore.Qt.Orientation.Horizontal)
        self.chat = ChatDock(self); self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.chat)
        self.setTabPosition(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, QtWidgets.QTabWidget.TabPosition.North)
        self.web = WebAIDock(self); self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.web)
        self.tabifyDockWidget(self.panel, self.chat); self.tabifyDockWidget(self.chat, self.web); self.panel.show(); self.panel.raise_()
        self.b_panel.clicked.connect(lambda: self.show_dock(self.panel))
        self.bus.live.connect(self.on_live)
        self.bus.log.connect(self.append_log); self.bus.local_done.connect(self.on_local)
        self.bus.ai_done.connect(self.on_ai); self.bus.news_done.connect(self.on_news)
        self.bus.snap.connect(self.on_snap); self.bus.finished.connect(self.on_finished)
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self.on_tick); self.timer.start(1000)
        self.refresh_accounts(); self.apply_theme(); self.fill_summary(); self.update_strip()
        self.dock_timer = QtCore.QTimer(self); self.dock_timer.timeout.connect(self.dock_tick); self.dock_timer.start(300)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.embed_timer = QtCore.QTimer(self); self.embed_timer.timeout.connect(self.fit_embed)
        atexit.register(self.windock.restore_all)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.release_embed)
        _old_hook = sys.excepthook
        def _hook(et, ev, tb):                       # lỗi không bắt được: trả MT5 về cửa sổ riêng rồi mới báo lỗi
            try: self.windock.restore_all()
            except Exception: pass
            _old_hook(et, ev, tb)
        sys.excepthook = _hook
        self.feed = LiveFeed(self); self.feed.start()
        self.copy = CopyEngine(self); self.copy.start()
        if self.broker.demo:
            self.log("Chạy chế độ MÔ PHỎNG (không có MT5/gói MetaTrader5 hoặc bị ép bật). Dữ liệu là giả.")

    def log(self, s):
        self.bus.log.emit(s)

    # ------------------------------------------------ tài khoản
    def enabled_accounts(self):
        return [a for a in self.cfg["accounts"] if a.get("enabled")]

    def primary(self):
        ea = self.enabled_accounts()
        name = self.cb_acc.currentText() if hasattr(self, "cb_acc") else ""
        for a in ea:
            if a["name"] == name: return a
        return ea[0] if ea else (self.cfg["accounts"][0] if self.cfg["accounts"] else None)

    def acc_symbol(self, acc):
        return acc.get("symbol") or self.cfg["symbol"]

    # ------------------------------------------------ UI
    def build_ui(self):
        root = QtWidgets.QWidget(); self.setCentralWidget(root)
        v = QtWidgets.QVBoxLayout(root); v.setContentsMargins(6, 6, 6, 6)
        bar = QtWidgets.QHBoxLayout(); v.addLayout(bar)
        bar.addWidget(QtWidgets.QLabel(f"<b style='color:{GOLD};font-size:14px'>◆ AIMT</b>"))
        self.cb_sym = QtWidgets.QComboBox(); self.cb_sym.setEditable(True); self.cb_sym.setMinimumWidth(110)
        self.cb_sym.addItem(self.cfg["symbol"]); self.cb_sym.setCurrentText(self.cfg["symbol"])
        self.cb_tf = QtWidgets.QComboBox(); self.cb_tf.addItems(list(TF_MIN)); self.cb_tf.setCurrentText(self.cfg["tf"])
        self.cb_pre = QtWidgets.QComboBox(); self.cb_pre.addItems(list(PRESETS)); self.cb_pre.setCurrentText(self.cfg["preset"])
        self.cb_risk = QtWidgets.QComboBox(); self.cb_risk.addItems(list(RISK)); self.cb_risk.setCurrentText(self.cfg["risk_mode"])
        self.cb_plan = QtWidgets.QComboBox(); self.cb_plan.addItems(["BEST", "SCALP", "SWING", "PO3"]); self.cb_plan.setCurrentText(self.cfg["exec_plan"])
        self.cb_acc = QtWidgets.QComboBox(); self.cb_acc.setToolTip("Tài khoản đang xem (chart, panel, tự động hoá)")
        self.cb_dock = QtWidgets.QComboBox(); self.cb_dock.setToolTip("Gộp app với terminal MT5 (chỉ Windows)")
        self.cb_dock.addItems(["Gộp: Tắt", "Gộp: Gắn phải MT5", "Gộp: Gắn dưới MT5", "Gộp: Nhúng MT5"])
        self.cb_dock.setCurrentIndex(0)
        self.b_panel = QtWidgets.QPushButton("Panel")
        self.setMinimumSize(900, 560)
        for w_, n_ in ((self.cb_acc, 7), (self.cb_sym, 8), (self.cb_tf, 3), (self.cb_pre, 8), (self.cb_risk, 7), (self.cb_plan, 5), (self.cb_dock, 9)):
            w_.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            w_.setMinimumContentsLength(n_); bar.addWidget(w_)
        bar.addWidget(self.b_panel)
        b_chat = QtWidgets.QPushButton("Chat"); b_chat.clicked.connect(lambda: self.show_dock(self.chat))
        b_web = QtWidgets.QPushButton("AI Web"); b_web.clicked.connect(lambda: self.show_dock(self.web)); bar.addWidget(b_web)
        bar.addWidget(b_chat)
        self.cb_dock.currentIndexChanged.connect(self.set_dock_mode)
        self.cb_sym.currentTextChanged.connect(lambda t: self.cfg.update(symbol=t.strip()))
        self.cb_tf.currentTextChanged.connect(lambda t: self.cfg.update(tf=t))
        self.cb_pre.currentTextChanged.connect(lambda t: self.cfg.update(preset=t))
        self.cb_risk.currentTextChanged.connect(lambda t: self.cfg.update(risk_mode=t))
        self.cb_plan.currentTextChanged.connect(self.on_plan_change)
        b_an = QtWidgets.QPushButton("Phân tích"); b_an.clicked.connect(lambda: self.start_analysis(True))
        self.b_auto = QtWidgets.QPushButton("Tự động"); self.b_auto.setCheckable(True)
        self.b_trade = QtWidgets.QPushButton("Auto"); self.b_trade.setToolTip("Auto Trade: tự vào lệnh theo tín hiệu"); self.b_trade.setCheckable(True)
        self.b_trade.clicked.connect(self.toggle_trade)
        b_tv = QtWidgets.QPushButton("TV"); b_tv.clicked.connect(self.open_tv)
        self.b_news = QtWidgets.QPushButton("Tin"); self.b_news.setCheckable(True); self.b_news.setChecked(True)
        b_set = QtWidgets.QPushButton("Cài đặt"); b_set.clicked.connect(self.open_settings)
        for w_ in (b_an, self.b_auto, self.b_trade, b_tv, self.b_news, b_set): bar.addWidget(w_)
        bar.addStretch(1)
        self.lbl_cd = QtWidgets.QLabel(""); bar.addWidget(self.lbl_cd)
        self.lbl_acc = QtWidgets.QLabel("MT5: chưa kết nối"); v.addWidget(self.lbl_acc)
        cr = QtWidgets.QHBoxLayout(); cr.addWidget(QtWidgets.QLabel("▶"))
        self.cmd = QtWidgets.QLineEdit(); self.cmd.returnPressed.connect(self.run_cmd)
        self.cmd.setPlaceholderText("Lệnh nhanh: BUY 0.1 · SELL · CLOSE ALL · HALF · TRIM · BE · REV · ACC <tên>|ALL · AUTO · COPY <nhận> <nguồn> …  (F1 = trợ giúp, F2 = vào ô này)")
        cr.addWidget(self.cmd, 1); self.lbl_cmd = QtWidgets.QLabel(""); self.lbl_cmd.setStyleSheet(f"color:{GOLD}"); cr.addWidget(self.lbl_cmd)
        self.b_hk = QtWidgets.QPushButton("HK"); self.b_hk.setCheckable(True); self.b_hk.setChecked(bool(self.cfg.get("hotkeys")))
        self.b_hk.setToolTip("Phím tắt chữ kiểu TradePanelPro (B S C V Z 1 2 3 T E R P L U J I K D G) — F10"); self.b_hk.clicked.connect(lambda on: self.toggle_hotkeys(on))
        b_h = QtWidgets.QPushButton("F1"); b_h.clicked.connect(self.show_help); cr.addWidget(self.b_hk); cr.addWidget(b_h)
        v.addLayout(cr)

        hs = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal); v.addWidget(hs, 1)
        left = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.chart = ChartWidget(); self.nodes = NodeGraph()
        left.addWidget(self.chart); left.addWidget(self.nodes); left.setSizes([400, 170])
        self.holder = QtWidgets.QWidget(); self.holder_l = QtWidgets.QVBoxLayout(self.holder); self.holder_l.setContentsMargins(0, 0, 0, 0)
        self.holder.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True); self.holder.hide(); self.hs = hs
        hs.addWidget(self.holder); hs.addWidget(left)
        self.right = QtWidgets.QWidget(); rv = QtWidgets.QVBoxLayout(self.right); rv.setContentsMargins(0, 0, 0, 0)
        self.right.setMinimumWidth(230)
        self.card = QtWidgets.QLabel(); self.card.setWordWrap(True); self.card.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.card.setMinimumHeight(170); self.card.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.card.setStyleSheet("background:#0d1324;border:1px solid #232d4b;border-radius:6px;padding:8px")
        rv.addWidget(self.card)
        hb = QtWidgets.QHBoxLayout()
        self.b_buy = QtWidgets.QPushButton("BUY"); self.b_buy.setObjectName("buy")
        self.b_sell = QtWidgets.QPushButton("SELL"); self.b_sell.setObjectName("sell")
        self.b_buy.clicked.connect(lambda: self.manual("BUY")); self.b_sell.clicked.connect(lambda: self.manual("SELL"))
        hb.addWidget(self.b_buy); hb.addWidget(self.b_sell); rv.addLayout(hb)
        rv.addWidget(QtWidgets.QLabel("<b>Tin mới</b> <span style='color:#6f7a90'>(▲/▼ = từ khoá heuristic, không phải dự báo)</span>"))
        self.lst = QtWidgets.QListWidget(); self.lst.setWordWrap(True); rv.addWidget(self.lst, 1)
        hs.addWidget(self.right); hs.setSizes([0, 760, 300]); hs.setStretchFactor(1, 3); hs.setStretchFactor(2, 1)
        self.b_news.toggled.connect(self.right.setVisible)

        self.tabs = QtWidgets.QTabWidget(); self.tabs.setMaximumHeight(190); v.addWidget(self.tabs)
        self.txt_ai = QtWidgets.QTextEdit(); self.txt_ai.setReadOnly(True)
        # tab tài khoản
        aw = QtWidgets.QWidget(); al = QtWidgets.QVBoxLayout(aw); al.setContentsMargins(0, 0, 0, 0)
        self.tbl_acc = QtWidgets.QTableWidget(0, 10)
        self.tbl_acc.setHorizontalHeaderLabels(["Tài khoản", "Server", "Equity", "Lỗ / Giới hạn", "Lệnh", "EA", "Auto", "Copy", "Trạng thái", "DCA · MANAGE · TRIM"])
        self.tbl_acc.horizontalHeader().setStretchLastSection(True)
        self.tbl_acc.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_acc.itemChanged.connect(self.on_acc_item); self.tbl_acc.itemDoubleClicked.connect(self.on_acc_dbl)
        al.addWidget(self.tbl_acc)
        ab = QtWidgets.QHBoxLayout()
        for txt, fn in (("Bật hết", lambda: self.set_all(True)), ("Dừng hết", lambda: self.set_all(False)),
                        ("Thêm", self.add_acc), ("Sửa", self.edit_acc), ("Xoá", self.del_acc),
                        ("Mở lại", self.resume_acc), ("Chi tiết lệnh", self.show_detail)):
            b = QtWidgets.QPushButton(txt); b.clicked.connect(fn); ab.addWidget(b)
        al.addLayout(ab)
        # tab lệnh
        tw = QtWidgets.QWidget(); tl = QtWidgets.QVBoxLayout(tw); tl.setContentsMargins(0, 0, 0, 0)
        self.tbl = QtWidgets.QTableWidget(0, 12)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.itemDoubleClicked.connect(lambda it: self.show_detail_row(it.row()))
        tl.addWidget(self.tbl)
        b_close = QtWidgets.QPushButton("Đóng tất cả lệnh + huỷ lệnh chờ của EA (magic) — mọi tài khoản đang bật")
        b_close.clicked.connect(self.close_all); tl.addWidget(b_close)
        self.txt_log = QtWidgets.QPlainTextEdit(); self.txt_log.setReadOnly(True)
        self.tabs.addTab(self.txt_ai, "Phân tích"); self.tabs.addTab(aw, "Tài khoản")
        self.tabs.addTab(tw, "Lệnh"); self.tabs.addTab(self.txt_log, "Nhật ký")
        self.render_card()

    def show_dock(self, dock):
        """Hiện dock và đưa lên trên cùng (khi Panel và Chat xếp chồng thành tab)."""
        dock.show(); dock.raise_(); dock.activateWindow()

    def append_log(self, s):
        self.txt_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {s}")

    def open_tv(self):
        s = self.cfg["symbol"].upper()
        webbrowser.open("https://www.tradingview.com/chart/?symbol=" + ("OANDA:XAUUSD" if "XAU" in s else s))

    def open_settings(self):
        d = SettingsDialog(self.cfg, self)
        if d.exec():
            d.apply(); save_cfg(self.cfg); self.remain = self.cfg["interval"]; self.apply_theme()
            self.cb_sym.setCurrentText(self.cfg["symbol"])

    def on_plan_change(self, t):
        self.cfg["exec_plan"] = t; save_cfg(self.cfg)
        if self.payload and self.plan:
            self.plan = self.pack_plan(self.payload, self.plan["side"], self.plan["score"], self.ai, self.plan.get("rng"), self.plan.get("note", ""))
            self.chart.set_data(self.payload["bars"], self.overlay(self.payload, self.plan)); self.render_card()

    def toggle_trade(self, on):
        self.set_auto_trade(on)

    # ------------------------------------------------ bảng tài khoản
    # ------------------------------------------------ giới hạn lỗ theo tài khoản
    def loss_pct(self, acc, ai):
        bal, eq = float(ai.get("balance", 0) or 0), float(ai.get("equity", 0) or 0)
        if acc.get("loss_mode") == "peak":
            peak = max(float(acc.get("peak", 0) or 0), eq)
            return (peak - eq) / peak * 100 if peak > 0 else 0.0
        return (bal - eq) / bal * 100 if bal > 0 else 0.0

    def eval_halt(self, acc, ai):
        """True nếu tài khoản đang bị NGƯNG vào lệnh mới do lỗ vượt giới hạn (chốt chặn cho tới khi bấm 'Mở lại')."""
        name = acc["name"]; pct = float(acc.get("max_loss_pct") or 0)
        if not ai or not ai.get("equity"): return name in self.halt
        eq = float(ai["equity"])
        if eq > float(acc.get("peak", 0) or 0):
            acc["peak"] = eq
            if time.time() - self._last_save > 60: self._last_save = time.time(); save_cfg(self.cfg)
        if pct <= 0:
            self.halt.pop(name, None); return False
        loss = self.loss_pct(acc, ai)
        if loss >= pct:
            if name not in self.halt:
                self.log(f"[{name}] LỖ {loss:.1f}% ≥ {pct:g}% -> NGƯNG vào lệnh mới (tab Tài khoản → 'Mở lại' để chạy tiếp).")
            self.halt[name] = dict(loss=loss, t=time.time())
            return True
        return name in self.halt

    def resume_acc(self):
        a = self.selected_acc()
        if not a: return
        self.halt.pop(a["name"], None)
        ac = (self.snaps.get(a["name"]) or {}).get("acc") or {}
        if ac.get("equity"): a["peak"] = float(ac["equity"])
        save_cfg(self.cfg); self.log(f"[{a['name']}] đã MỞ LẠI vào lệnh (đỉnh Equity đặt lại = {a.get('peak', 0):.2f})."); self.refresh_accounts()

    def selected_acc(self):
        r = self.tbl_acc.currentRow()
        return self.cfg["accounts"][r] if 0 <= r < len(self.cfg["accounts"]) else None

    def on_acc_dbl(self, it):
        a = self.cfg["accounts"][it.row()]
        if a.get("enabled") and a["name"] in [self.cb_acc.itemText(i) for i in range(self.cb_acc.count())]:
            self.cb_acc.setCurrentText(a["name"]); self.log(f"Đang xem tài khoản: {a['name']}"); self.show_dock(self.panel)

    def show_detail_row(self, row):
        names = [x for x in self.cfg["accounts"] if x.get("enabled")]
        if 1 <= row <= len(names): self.show_detail(names[row - 1])       # dòng 0 là TỔNG

    def show_detail(self, acc=None):
        a = acc if isinstance(acc, dict) else (self.selected_acc() or self.primary())
        s = self.snaps.get(a["name"]) if a else None
        d = QtWidgets.QDialog(self); d.setWindowTitle(f"Chi tiết lệnh — {a['name'] if a else ''}"); d.resize(760, 420)
        v = QtWidgets.QVBoxLayout(d); t = QtWidgets.QTableWidget(0, 8)
        t.setHorizontalHeaderLabels(["Ticket", "Symbol", "Loại", "Lot", "Giá vào", "SL", "TP", "Lãi/Lỗ"]); v.addWidget(t)
        rows = (s or {}).get("positions", []); t.setRowCount(len(rows))
        for r, p_ in enumerate(rows):
            for c, val in enumerate([p_["ticket"], p_["symbol"], p_["type"], f"{p_['volume']:.2f}", p_["open"], p_["sl"], p_["tp"], f"{p_['profit']:.2f}"]):
                it = QtWidgets.QTableWidgetItem(str(val))
                if c == 7: it.setForeground(QtGui.QColor(GREEN if p_["profit"] >= 0 else RED))
                t.setItem(r, c, it)
        t.horizontalHeader().setStretchLastSection(True); d.exec()

    # ------------------------------------------------ bảng tài khoản
    def refresh_accounts(self):
        self._refreshing = True
        names = [a["name"] for a in self.enabled_accounts()]
        if [self.cb_acc.itemText(i) for i in range(self.cb_acc.count())] != names:
            cur = self.cb_acc.currentText(); self.cb_acc.blockSignals(True); self.cb_acc.clear(); self.cb_acc.addItems(names)
            if cur in names: self.cb_acc.setCurrentText(cur)
            self.cb_acc.blockSignals(False)
        accs = self.cfg["accounts"]
        self.tbl_acc.setRowCount(len(accs))
        for r, a in enumerate(accs):
            s = self.snaps.get(a["name"]); ac = (s or {}).get("acc", {})
            info = self.infos.get(a["name"], {}); h = self.halt.get(a["name"])
            lim = float(a.get("max_loss_pct") or 0)
            lp = self.loss_pct(a, ac) if ac else None
            loss_txt = "—" if lp is None else (f"{lp:.1f}% / {lim:g}%" if lim else f"{lp:.1f}%")
            status = f"NGƯNG (lỗ {h['loss']:.1f}%)" if h else ("HOẠT ĐỘNG" if s else ("OFFLINE" if a.get("enabled") else "TẮT"))
            vals = [a["name"], f"{ac.get('login', a.get('login') or '')}@{ac.get('server', a.get('server') or '')}",
                    f"{ac['equity']:.2f}" if ac else "—", loss_txt, str(s["total"]) if s else "—", "", "",
                    f"← {a['copy_from']}" if a.get("copy_from") else "", status,
                    f"DCA {info.get('dca', '—')} · {info.get('manage', '—')} · TRIM {info.get('trim', '—')}"]
            for c, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(val)
                if c in (5, 6) or (c == 7 and a.get("copy_from")):
                    it.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsEnabled)
                    key = {5: "enabled", 6: "auto", 7: "copy_on"}[c]
                    it.setCheckState(QtCore.Qt.CheckState.Checked if a.get(key) else QtCore.Qt.CheckState.Unchecked)
                else:
                    it.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if c == 8: it.setForeground(QtGui.QColor(RED if h else (GREEN if s else "#8892a6")))
                self.tbl_acc.setItem(r, c, it)
        self._refreshing = False

    def on_acc_item(self, it):
        if self._refreshing or it.column() not in (5, 6, 7): return
        a = self.cfg["accounts"][it.row()]
        key = {5: "enabled", 6: "auto", 7: "copy_on"}[it.column()]
        if key == "copy_on" and not a.get("copy_from"): return
        a[key] = it.checkState() == QtCore.Qt.CheckState.Checked
        save_cfg(self.cfg)
        if key == "copy_on" and a[key]: self.copy.ignored.pop(a["name"], None)
        if key == "copy_on" and a[key]: self.log(f"[{a['name']}] BẬT copy từ {a.get('copy_from')} (chỉ lệnh mới, trừ khi đã chọn copy lệnh đang mở).")
        if key == "enabled": self.refresh_accounts()

    def fill_summary(self):
        heads = ["Tài khoản", "Balance", "Equity", "Lệnh B/S", "Lot B", "Lot S", "Lot tổng", "P/L Buy", "P/L Sell", "P/L tổng", "% Bal", "Trạng thái"]
        rows, T = [], dict(nb=0, ns=0, lb=0.0, ls=0.0, pb=0.0, ps=0.0, bal=0.0, eq=0.0); cur = set()
        for a in self.cfg["accounts"]:
            if not a.get("enabled"): continue
            s = self.snaps.get(a["name"])
            if not s or not s.get("acc"):
                rows.append([a["name"], "—", "—", "—", "—", "—", "—", "—", "—", "—", "—", "OFFLINE"]); continue
            pos = [p for p in s["positions"] if p["type"] in ("BUY", "SELL")]
            b = [p for p in pos if p["type"] == "BUY"]; sl = [p for p in pos if p["type"] == "SELL"]
            pb, ps = sum(p["profit"] for p in b), sum(p["profit"] for p in sl)
            lb, ls = sum(p["volume"] for p in b), sum(p["volume"] for p in sl)
            ac = s["acc"]; cur.add(ac.get("currency", ""))
            T["nb"] += len(b); T["ns"] += len(sl); T["lb"] += lb; T["ls"] += ls; T["pb"] += pb; T["ps"] += ps
            T["bal"] += ac["balance"]; T["eq"] += ac["equity"]
            h = self.halt.get(a["name"])
            rows.append([a["name"], f"{ac['balance']:.2f}", f"{ac['equity']:.2f}", f"{len(b)}/{len(sl)}", f"{lb:.2f}", f"{ls:.2f}",
                         f"{lb + ls:.2f}", f"{pb:+.2f}", f"{ps:+.2f}", f"{pb + ps:+.2f}",
                         f"{(pb + ps) / ac['balance'] * 100:+.1f}%" if ac["balance"] else "—", f"NGƯNG {h['loss']:.0f}%" if h else "OK"])
        note = "" if len(cur) <= 1 else "*"
        rows.insert(0, ["TỔNG" + note, f"{T['bal']:.2f}", f"{T['eq']:.2f}", f"{T['nb']}/{T['ns']}", f"{T['lb']:.2f}", f"{T['ls']:.2f}",
                     f"{T['lb'] + T['ls']:.2f}", f"{T['pb']:+.2f}", f"{T['ps']:+.2f}", f"{T['pb'] + T['ps']:+.2f}",
                     f"{(T['pb'] + T['ps']) / T['bal'] * 100:+.1f}%" if T["bal"] else "—", ""])
        self.tbl.setColumnCount(len(heads)); self.tbl.setHorizontalHeaderLabels(heads); self.tbl.setRowCount(len(rows))
        for r, vals in enumerate(rows):
            last = r == 0
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(str(v))
                if c in (7, 8, 9, 10) and v not in ("—", ""):
                    it.setForeground(QtGui.QColor(GREEN if str(v).startswith("+") or str(v) in ("+0.00",) else RED))
                if last:
                    f_ = it.font(); f_.setBold(True); it.setFont(f_)
                    it.setBackground(QtGui.QColor("#2a1d00" if self.cfg.get("theme") == "bloomberg" else "#1b2650"))
                if c == 11 and str(v).startswith("NGƯNG"): it.setForeground(QtGui.QColor(RED))
                self.tbl.setItem(r, c, it)
        if note: self.tbl.horizontalHeaderItem(0).setToolTip("* Các tài khoản có đơn vị tiền khác nhau: dòng TỔNG cộng thô, không quy đổi.")

    def update_strip(self):
        d = self.last_live; parts = []
        if d:
            parts.append(f"{d['sym']}  {d['bid']:.{d['meta']['digits']}f}/{d['ask']:.{d['meta']['digits']}f}  SPR {d['meta']['spread']}")
        tot = 0.0
        for a in self.enabled_accounts():
            s = self.snaps.get(a["name"])
            if s:
                pl = sum(p["profit"] for p in s["positions"] if p["type"] in ("BUY", "SELL")); tot += pl
                parts.append(f"{a['name']} {pl:+.2f}" + (" ⛔" if a["name"] in self.halt else ""))
        n_on = len([a for a in self.enabled_accounts() if self.snaps.get(a["name"])])
        p = self.primary()
        parts.append(f"ΣP/L {tot:+.2f}")
        parts.append(f"XEM: {p['name'] if p else '-'}  |  ÁP DỤNG: {'TẤT CẢ' if self.cfg.get('panel_target') == 'ALL' else 'TK ĐANG XEM'}  |  {n_on} TK online")
        self.lbl_acc.setText(("[MÔ PHỎNG]  " if self.broker.demo else "") + "   │   ".join(parts))

    def set_all(self, on):
        for a in self.cfg["accounts"]: a["enabled"] = on; a["auto"] = on
        save_cfg(self.cfg); self.refresh_accounts()

    def add_acc(self):
        d = AccountDialog(None, self)
        if d.exec():
            a = d.data(); a["name"] = self.unique_name(a["name"])
            self.cfg["accounts"].append(a); save_cfg(self.cfg); self.refresh_accounts()

    def edit_acc(self):
        r = self.tbl_acc.currentRow()
        if r < 0: return
        old = self.cfg["accounts"][r]; d = AccountDialog(old, self)
        if d.exec():
            new = d.data(old)
            if new["name"] != old["name"]: new["name"] = self.unique_name(new["name"])
            self.cfg["accounts"][r] = new; save_cfg(self.cfg); self.refresh_accounts()

    def del_acc(self):
        r = self.tbl_acc.currentRow()
        if r < 0 or len(self.cfg["accounts"]) <= 1: return
        del self.cfg["accounts"][r]; save_cfg(self.cfg); self.refresh_accounts()

    def unique_name(self, n):
        names = {a["name"] for a in self.cfg["accounts"]}; k, base = n, n; i = 2
        while k in names: k = f"{base} ({i})"; i += 1
        return k

    # ------------------------------------------------ chu kỳ
    def on_tick(self):
        if self.b_auto.isChecked():
            self.remain -= 1
            self.lbl_cd.setText(f"Tín hiệu kế tiếp: {max(self.remain, 0)}s")
            if self.remain <= 0:
                self.remain = self.cfg["interval"]
                if not self.busy: self.start_analysis(False)
        else:
            self.lbl_cd.setText("")
        self.news_remain -= 1
        if self.news_remain <= 0:
            self.news_remain = self.cfg["news_every"]
            threading.Thread(target=lambda: self.bus.news_done.emit(fetch_news(self.cfg["news_feeds"])),
                             daemon=True).start()

    def on_news(self, items):
        if not items: return
        self.news = items; self.lst.clear()
        for d, t, s in items:
            it = QtWidgets.QListWidgetItem(f"{d:%H:%M} {'▲' if s > 0 else ('▼' if s < 0 else '•')} {t}")
            it.setForeground(QtGui.QColor(GREEN if s > 0 else (RED if s < 0 else "#cfd6e6")))
            self.lst.addItem(it)

    # ------------------------------------------------ pipeline
    def start_analysis(self, manual):
        if self.busy: return
        if not self.enabled_accounts():
            self.log("Chưa có tài khoản nào được bật (tab Tài khoản)."); return
        self.busy = True; self.ai = None
        self.nodes.set_state(analyzing=True)
        self.txt_ai.setPlainText("Đang phân tích...")
        threading.Thread(target=self.job_local, args=(manual,), daemon=True).start()

    def on_finished(self):
        self.busy = False

    def job_local(self, manual):
        try:
            br, acc = self.broker, self.primary()
            with br.session(acc) as ok:
                if not ok:
                    self.bus.finished.emit(); return
                sym = br.resolve(self.acc_symbol(acc))
                rates = br.rates(sym, self.cfg["tf"], int(self.cfg["bars"]))
                meta = br.meta(sym); bid, ask = br.tick(sym)
            if rates is None or len(rates) < 120:
                self.log("Không lấy đủ dữ liệu nến (cần >= 120). Kiểm tra symbol/khung thời gian.")
                self.bus.finished.emit(); return
            b = Bars(rates)
            a = atr_arr(b, 14)[-2]
            A = float(a) if a == a and a > 0 else float((b.h - b.l).mean())
            results = []
            for key, name, fn in ANALYZERS:
                try:
                    r = fn(b, A); r.key, r.name = key, name
                except Exception as ex:
                    r = Res(key, name, state="Lỗi"); r.notes = [repr(ex)]
                    self.log(f"{name} lỗi: {ex}")
                results.append(r)
            reg = detect_regime(b, A, self.cfg)
            snaps, infos = {}, {}
            def cycle(a_):                               # quản lý lệnh + chụp trạng thái 1 tài khoản
                try:
                    with br.session(a_) as ok2:
                        if not ok2: snaps[a_["name"]] = None; return
                        s2 = br.resolve(self.acc_symbol(a_))
                        infos[a_["name"]] = br.manage(s2, A)
                        snaps[a_["name"]] = br.snapshot(s2)
                except Exception as ex:
                    self.log(f"[{a_['name']}] lỗi chu kỳ: {ex}")
            accs_ = self.enabled_accounts()
            with ThreadPoolExecutor(max_workers=max(1, len(accs_))) as pool:    # các terminal khác nhau chạy song song
                list(pool.map(cycle, accs_))
            self.infos.update(infos); self.bus.snap.emit(snaps)
            ps = snaps.get(acc["name"]); bal = ps["acc"]["balance"] if ps and ps.get("acc") else 0.0
            pos_text = "\n".join(f"- {q['type']} {q['volume']} @ {q['open']} SL {q['sl']} TP {q['tp']} P/L {q['profit']:.2f}"
                                 for q in (ps["mine_list"] if ps else []) if q["symbol"] == sym) or ""
            self.bus.local_done.emit(dict(sym=sym, bars=b, atr=A, results=results, bid=bid, ask=ask,
                                          price=(bid + ask) / 2 if bid else float(b.c[-1]),
                                          digits=meta["digits"], spread=meta["spread"], meta=meta, bal=bal, pos_text=pos_text, regime=reg,
                                          bar_id=int(b.t[-1]), manual=manual))
        except Exception:
            self.log("Lỗi phân tích: " + traceback.format_exc()); self.bus.finished.emit()

    def overlay(self, P, plan=None):
        ov = dict(zones=[], levels=[])
        for r in P["results"]:
            ov["zones"] += r.zones; ov["levels"] += r.levels
        reg = P.get("regime")
        if reg and reg.get("state") == "SIDEWAYS":
            ov["zones"].append(("Biên đi ngang", reg["ll"], reg["hh"], BLUE))
            ov["levels"].append(("Giữa biên", (reg["hh"] + reg["ll"]) / 2, BLUE))
        if plan and plan["side"] != "WAIT" and plan.get("entry"):
            ov["levels"] += [("Entry", plan["entry"], "#ffffff"), ("SL", plan["sl"], RED), ("TP", plan["tp"], GREEN)]
        return ov

    def threshold(self):
        c = self.cfg
        return float(c["thr_override"]) if c["thr_override"] > 0 else RISK[c["risk_mode"]]["thr"]

    def make_plan(self, P, ai):
        c = self.cfg; w = PRESETS[c["preset"]]; thr = self.threshold()
        score = combine(P["results"], w, ai, c["ai_weight"])
        side = "BUY" if score >= thr else ("SELL" if score <= -thr else "WAIT")
        if side != "WAIT" and ai is not None and c["risk_mode"] == "An toàn" \
                and ai["signal"] in ("BUY", "SELL") and ai["signal"] != side:
            side = "WAIT"
        rng, note = None, ""
        reg, sm = P.get("regime"), c.get("side_mode", "off")
        if sm != "off" and reg and reg.get("state") == "SIDEWAYS":
            head = (f"Thị trường ĐI NGANG (đạt {reg['votes']}/3 phiếu: ER {reg['er']:.2f}, trôi {reg['drift']:.2f}×biên, ADX {reg['adx']:.0f})")
            if sm == "filter":
                side, note = "WAIT", head + ": bộ lọc 'tránh trend' chặn lệnh theo xu hướng."
            else:
                rng, why = range_signal(P, reg, c)
                if rng is None:
                    side, note = "WAIT", head + ": chưa có điểm đánh đảo chiều — " + why
                elif c["risk_mode"] == "An toàn" and ai is not None and ai["signal"] in ("BUY", "SELL") and ai["signal"] != rng["side"]:
                    side, rng, note = "WAIT", None, head + ": AI ngược hướng đảo chiều (khẩu vị An toàn) nên không vào."
                else:
                    side, note = rng["side"], head + f": đánh đảo chiều {rng['side']} trong biên."
        return self.pack_plan(P, side, score, ai, rng, note)

    def pack_plan(self, P, side, score, ai=None, rng=None, note=""):
        mode = RISK[self.cfg["risk_mode"]]
        if rng:
            plans = build_range_plan(P, rng, P["regime"]); used_ai = False
        else:
            plans = build_plans(P, side, score, mode)
            used_ai = apply_ai_levels(plans, ai, P, self.cfg)
        d = dict(side=side, score=score, plans=plans, entry=0, sl=0, tp=0, sl_d=0, ai_levels=used_ai,
                 rng=rng, note=note, mode="RANGE" if rng else "TREND")
        sel = plans.get(self.cfg["exec_plan"]) or plans.get("BEST")
        if sel:
            ref = (P["ask"] if side == "BUY" else P["bid"]) or P["price"]
            e, s_, t_ = plan_prices(sel, ref, P["digits"])
            d.update(entry=e, sl=s_, tp=t_, sl_d=sel["sl_d"])
        return d

    def recompute_plan(self):
        if self.payload:
            self.plan = self.make_plan(self.payload, self.ai)
            self.chart.set_overlay(self.overlay(self.payload, self.plan)); self.render_card()

    def ai_ready(self):
        return bool(self.cfg["use_ai"] and prov_conf(self.cfg, self.cfg.get("ai_provider", "gemini"))["key"])

    def on_local(self, P):
        self.payload = P
        self.plan = self.make_plan(P, None)
        if self.chart.bars is None: self.chart.set_bars(P["bars"])
        self.chart.set_overlay(self.overlay(P, self.plan))
        self.nodes.set_state(P["results"], False)
        self.render_card()
        pv = provider_label(self.cfg.get("ai_provider", "gemini"))
        if not self.ai_ready():
            self.txt_ai.setPlainText(f"Phân tích cục bộ xong. Chưa bật/không có API key {pv} -> chỉ dùng 8 phương pháp cục bộ.")
            self.finalize(None, False); return
        self.txt_ai.setPlainText(f"Phân tích cục bộ xong. Đang chờ {pv}...")
        now = time.time()
        wait = max(self.ai_block_until - now, self.last_ai_try + self.cfg["ai_min_interval"] - now)
        if wait > 0:                                      # giới hạn tần suất / đang nghỉ do 429
            if self.last_ai and now - self.last_ai_ts <= 3 * self.cfg["ai_min_interval"] + 60:
                self.log(f"AI: dùng lại kết quả cách đây {int(now - self.last_ai_ts)}s (còn {int(wait)}s mới gọi lại)")
                self.finalize(self.last_ai, False)
            else:
                self.log(f"AI đang nghỉ {int(wait)}s (giới hạn tần suất/quota) và chưa có kết quả gần đây -> không auto trade chu kỳ này.")
                self.finalize(None, True)
            return
        self.last_ai_try = now
        png = render_png(P["bars"], self.overlay(P), 800, 450)
        prompt = build_prompt(P["sym"], self.cfg["tf"], P, self.news)
        threading.Thread(target=self.job_ai, args=(prompt, png), daemon=True).start()

    def job_ai(self, prompt, png):
        try:
            self.bus.ai_done.emit(dict(ok=True, data=ask_signal(self.cfg, prompt, png, self.log)))
        except Exception as ex:
            if getattr(ex, "status", None) == 429: self.ai_block_until = time.time() + 180
            self.bus.ai_done.emit(dict(ok=False, err=str(ex), status=getattr(ex, "status", None)))

    def on_ai(self, r):
        if r["ok"]:
            self.last_ai, self.last_ai_ts = r["data"], time.time()
            self.finalize(r["data"], False); return
        self.log("AI lỗi: " + r["err"])
        if r.get("status") == 429:
            self.log("AI: hết quota/quá tần suất (429) -> tạm nghỉ gọi AI 180s. Có thể tăng 'Gọi AI tối thiểu mỗi (giây)' hoặc đổi nhà cung cấp trong Cài đặt.")
        if self.last_ai and time.time() - self.last_ai_ts <= 3 * self.cfg["ai_min_interval"] + 60:
            self.log(f"AI: dùng lại kết quả cách đây {int(time.time() - self.last_ai_ts)}s")
            self.finalize(self.last_ai, False)
        else:
            self.finalize(None, True)

    def finalize(self, ai, ai_failed):
        P = self.payload; self.ai = ai
        self.plan = self.make_plan(P, ai)
        self.chart.set_overlay(self.overlay(P, self.plan))
        self.auto_msg = self.auto_reason(ai_failed)
        self.log("[Auto] " + self.auto_msg)
        self.render_card(ai_failed)
        if ai and self.cfg["ai_adjust"]:
            threading.Thread(target=self.job_ai_adjust, args=(P, ai), daemon=True).start()
        if ai:
            age = int(time.time() - self.last_ai_ts)
            tag = f" — kết quả cách đây {age}s" if age > 5 else ""
            lines = [f"<b>Phân tích</b> — {ai.get('_model', '')}: <b>{ai['signal']}</b> ({ai['confidence']:.0f}%){tag}<br>"]
            for k, lab in (("chart", "Chart"), ("indicator", "Indicator"), ("price_action", "Price action"),
                           ("summary", "Kết luận"), ("risks", "Rủi ro")):
                if ai.get(k): lines.append(f"<b>[{lab}]</b> {ai[k]}<br>")
            if ai.get("entry") or ai.get("sl") or ai.get("tp"):
                lines.append(f"<i>Mức AI đề xuất (chỉ được dùng khi bật 'SL/TP tự động theo AI'): "
                             f"Entry {ai.get('entry')} / SL {ai.get('sl')} / TP {ai.get('tp')}</i>")
            self.txt_ai.setHtml("".join(lines))
        else:
            notes = [f"<b>[{r.name}]</b> " + "; ".join(r.notes) for r in P["results"] if r.notes]
            self.txt_ai.setHtml("<b>Phân tích cục bộ</b><br>" + "<br>".join(notes or ["Không có tín hiệu nổi bật."]))
        if self.cfg["chat_auto"]:
            self.chat.auto_send()
        if self.cfg["auto_trade"] and self.plan["side"] != "WAIT":
            if self.ai_ready() and ai_failed:
                self.log("AI không khả dụng -> bỏ qua auto trade chu kỳ này.")
            else:
                threading.Thread(target=self.job_trade, args=(self.plan["side"], P, False), daemon=True).start()
                return
        self.bus.finished.emit()

    def auto_reason(self, ai_failed):
        """Giải thích ngắn gọn vì sao chu kỳ này có / không đi tới bước vào lệnh."""
        c, p = self.cfg, self.plan
        thr = self.threshold()
        pre = "" if self.b_auto.isChecked() else "Nút 'Tự động' đang TẮT (chỉ phân tích khi bạn bấm 'Phân tích'). "
        if not c["auto_trade"]:
            return pre + "Auto Trade đang TẮT (bật nút 'Auto') nên app chỉ phân tích, không vào lệnh."
        if not [a for a in self.enabled_accounts() if a.get("auto")]:
            return pre + "Chưa có tài khoản nào tick cả 'EA bật' và 'Auto' (tab Tài khoản)."
        if p["side"] == "WAIT" and p.get("note"):
            return pre + p["note"]
        if p["side"] == "WAIT":
            extra = " (khẩu vị An toàn: AI ngược hướng cũng làm thành WAIT)" if c["risk_mode"] == "An toàn" else ""
            return (pre + f"Tín hiệu WAIT: điểm {p['score']:+.2f}, cần ≥ +{thr:.2f} (BUY) hoặc ≤ −{thr:.2f} (SELL) "
                    f"với khẩu vị {c['risk_mode']}{extra}. Chưa đủ mạnh nên không vào lệnh.")
        if self.ai_ready() and ai_failed:
            return pre + "AI không khả dụng và chưa có kết quả gần đây -> bỏ qua chu kỳ này."
        return pre + f"Tín hiệu {p['side']} đạt ngưỡng -> đang kiểm tra từng tài khoản (xem Nhật ký: các dòng [tên tài khoản])."

    def render_card(self, ai_failed=False):
        if not self.plan:
            self.card.setText("<span style='color:#8892a6'>Chưa có tín hiệu. Bấm <b>Phân tích ngay</b>.</span>"); return
        p, P = self.plan, self.payload
        col = {"BUY": GREEN, "SELL": RED, "WAIT": "#9aa4bb"}[p["side"]]
        h = [f"<div style='font-size:26px;font-weight:bold;color:{col}'>{p['side']}</div>",
             f"Điểm hội tụ: <b>{p['score']:+.2f}</b> (ngưỡng ±{self.threshold():.2f}{' tự đặt' if self.cfg['thr_override'] > 0 else ''}, "
             f"{self.cfg['risk_mode']}, {self.cfg['preset']})<br>"]
        buy = sum(1 for r in P["results"] if r.sig > 0); sell = sum(1 for r in P["results"] if r.sig < 0)
        h.append(f"8 phương pháp: <span style='color:{GREEN}'>{buy} mua</span> / "
                 f"<span style='color:{RED}'>{sell} bán</span> / {8 - buy - sell} trung lập<br>")
        reg = P.get("regime")
        if reg and reg.get("state") in ("SIDEWAYS", "TREND"):
            if reg["state"] == "SIDEWAYS":
                h.append(f"<span style='color:{BLUE}'>Thị trường: <b>ĐI NGANG</b> ({reg['votes']}/3 phiếu: ER {reg['er']:.2f}, trôi {reg['drift']:.2f}, ADX {reg['adx']:.0f}; chế độ: "
                         f"{ {'off': 'tắt', 'filter': 'lọc', 'reversion': 'đảo chiều'}[self.cfg['side_mode']] })</span><br>")
            else:
                h.append(f"<span style='color:#8892a6'>Thị trường: XU HƯỚNG ({reg['votes']}/3 phiếu đi ngang: ER {reg['er']:.2f}, trôi {reg['drift']:.2f}, ADX {reg['adx']:.0f})</span><br>")
        if self.ai: h.append(f"AI: <b>{self.ai['signal']}</b> {self.ai['confidence']:.0f}%<br>")
        if self.auto_msg:
            h.append(f"<div style='margin:3px 0;color:#f5b942'><b>Auto:</b> {self.auto_msg}</div>")
        if ai_failed: h.append("<span style='color:#f5b942'>AI lỗi/không khả dụng — xem tab Nhật ký.</span><br>")
        mode = RISK[self.cfg["risk_mode"]]; risk = self.cfg["risk_pct"] or mode["risk"]
        ref = (P["ask"] if p["side"] == "BUY" else P["bid"]) or P["price"]
        for nm in ("SCALP", "SWING", "PO3", "RANGE", "BEST"):
            pl = p["plans"].get(nm)
            if not pl: continue
            e, s_, t_ = plan_prices(pl, ref, P["digits"])
            lot = self.lot_est(P, pl["sl_d"], risk)
            star = "★ " if nm == self.cfg["exec_plan"] else ""
            ai_tag = " · <b style='color:#f5b942'>SL/TP: AI</b>" if pl.get("src") == "AI" else ""
            note = pl["note"]; conf = pl["conf10"]
            h.append(f"<div style='margin-top:5px'><b style='color:{col}'>{plan_label(pl)}</b> {star}<b>{nm}</b> "
                     f"Entry {e} · SL {s_} · TP {t_} · Lot ~{lot} · Conf {conf}/10{ai_tag}<br>"
                     f"<span style='color:#8892a6'>{note}</span></div>")
        for a in self.enabled_accounts()[:4]:
            i = self.infos.get(a["name"])
            if i: h.append(f"<div style='color:#9aa4bb;margin-top:3px'>{a['name']}: DCA {i['dca']} · "
                           f"MANAGE {i['manage']} · TRIM {i['trim']}</div>")
        self.card.setText("".join(h))

    def on_snap(self, snaps):
        for k, v in snaps.items():
            self.snaps[k] = v
            a = next((x for x in self.cfg["accounts"] if x["name"] == k), None)
            if a and v and v.get("acc"): self.eval_halt(a, v["acc"])
        live = [s for s in self.snaps.values() if s]
        tot = sum(s["total"] for s in live); lots = sum(s["lots"] for s in live)
        n_on = len([a for a in self.enabled_accounts() if self.snaps.get(a["name"])])
        self.nodes.title = "TRẠI MT5"
        self.nodes.set_state(sub=f"ĐANG HÚT {n_on} MT5 · {tot} lệnh · {lots:.2f} lot")
        self.refresh_accounts(); self.fill_summary(); self.update_strip()

    # ------------------------------------------------ giao dịch
    def manual(self, side):
        if not self.payload:
            QtWidgets.QMessageBox.information(self, APP_NAME, "Hãy bấm 'Phân tích ngay' trước."); return
        pl = self.pack_plan(self.payload, side, 0.0)["plans"]["SCALP"]
        ref = (self.payload["ask"] if side == "BUY" else self.payload["bid"]) or self.payload["price"]
        e, s_, t_ = plan_prices(pl, ref, self.payload["digits"])
        m = QtWidgets.QMessageBox.question(self, "Xác nhận lệnh",
            f"{side} {self.payload['sym']} (chỉ tài khoản đầu tiên đang bật: {self.primary()['name']})\n"
            f"Entry ~{e}  SL {s_}  TP {t_}\nLot tính theo rủi ro chế độ '{self.cfg['risk_mode']}'.")
        if m == QtWidgets.QMessageBox.StandardButton.Yes:
            threading.Thread(target=self.job_trade, args=(side, self.payload, True), daemon=True).start()

    def job_trade(self, side, P, manual):
        try:
            if manual:
                targets = [self.primary()]
            else:
                targets = [a for a in self.enabled_accounts() if a.get("auto")]
                if not targets: self.log("Không có tài khoản nào bật Auto.")
            rng = self.plan.get("rng") if (self.plan and not manual and self.plan["side"] == side) else None
            plans = self.pack_plan(P, side, self.plan["score"] if self.plan else 0.0, self.ai, rng)["plans"]
            plan = (plans.get("SCALP") or plans["BEST"]) if manual else (plans.get(self.cfg["exec_plan"]) or plans["BEST"])
            snaps = {}
            def one(a):
                try:
                    s_ = self.trade_account(a, side, P, plan, manual)
                    if s_: snaps[a["name"]] = s_
                except Exception:
                    self.log(f"[{a['name']}] lỗi giao dịch: " + traceback.format_exc())
            with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:   # gửi lệnh đồng thời nhiều terminal
                list(pool.map(one, targets))
            if snaps: self.bus.snap.emit(snaps)
        except Exception:
            self.log("Lỗi giao dịch: " + traceback.format_exc())
        finally:
            self.bus.finished.emit()

    def trade_account(self, acc, side, P, plan, manual):
        br, mode, name, c = self.broker, RISK[self.cfg["risk_mode"]], acc["name"], self.cfg
        with br.session(acc) as ok:
            if not ok:
                self.log(f"[{name}] không kết nối -> bỏ qua."); return None
            sym = br.resolve(self.acc_symbol(acc)); meta = br.meta(sym); bid, ask = br.tick(sym)
            snap = br.snapshot(sym)
            if self.eval_halt(acc, snap.get("acc")):
                if manual: self.log(f"[{name}] CẢNH BÁO: tài khoản đang NGƯNG vì lỗ (lệnh tay vẫn được gửi).")
                else:
                    self.log(f"[{name}] đang NGƯNG vào lệnh mới do lỗ ≥ {acc.get('max_loss_pct')}% (bấm 'Mở lại' ở tab Tài khoản)."); return snap
            own = [p for p in snap["mine_list"] if p["symbol"] == sym]
            pos = [p for p in own if p["type"] in ("BUY", "SELL")]
            pend = [p for p in own if p not in pos]
            if not manual:
                h = dt.datetime.now().hour
                if not (c["hour_from"] <= h < c["hour_to"]):
                    self.log(f"[{name}] ngoài khung giờ cho phép."); return snap
                if time.time() - self.last_ts.get(name, 0) < c["cooldown_min"] * 60:
                    self.log(f"[{name}] đang cooldown."); return snap
                if any(p["type"] != side for p in pos):
                    self.log(f"[{name}] đang có lệnh ngược chiều của EA -> bỏ qua."); return snap
                if any(p["type"].startswith(side) for p in pend):
                    self.log(f"[{name}] đã có lệnh chờ cùng chiều -> bỏ qua."); return snap
                if self.last_bar.get(name) == P["bar_id"]:
                    self.log(f"[{name}] đã vào lệnh ở nến này, chờ nến mới."); return snap
            if c["max_spread"] and meta["spread"] > c["max_spread"]:
                self.log(f"[{name}] spread {meta['spread']} > {c['max_spread']} -> bỏ qua."); return snap
            risk = c["risk_pct"] or mode["risk"]
            cap = acc.get("max_lot") or c["max_lot"]
            dg, price = meta["digits"], (ask if side == "BUY" else bid)
            # ---- DCA (chỉ khi đã có lệnh cùng chiều)
            if pos and not manual:
                if not c["dca_enabled"]:
                    self.log(f"[{name}] đang có lệnh cùng chiều và DCA tắt -> không vào thêm."); return snap
                if len(pos) >= int(c["dca_max"]) + 1:
                    self.log(f"[{name}] đã đủ {int(c['dca_max'])} lần DCA."); return snap
                if c["dca_total_lot"] <= 0:
                    self.log(f"[{name}] DCA bật nhưng chưa đặt TỔNG lot tối đa (Cài đặt) -> bỏ qua."); return snap
                opens = [p["open"] for p in pos]
                worst = min(opens) if side == "BUY" else max(opens)
                step = c["dca_step_atr"] * P["atr"]
                if not ((side == "BUY" and ask <= worst - step) or (side == "SELL" and bid >= worst + step)):
                    self.log(f"[{name}] DCA: giá chưa đi ngược đủ {c['dca_step_atr']} ATR so với lệnh xấu nhất."); return snap
                base = sorted(pos, key=lambda p: p["ticket"])[0]["volume"]
                lot = round(math.floor(base * c["dca_mult"] ** len(pos) / meta["vstep"]) * meta["vstep"], 3)
                if cap: lot = min(lot, cap)
                lot = max(meta["vmin"], min(lot, meta["vmax"]))
                if sum(p["volume"] for p in pos) + lot > c["dca_total_lot"]:
                    self.log(f"[{name}] DCA vượt TỔNG lot tối đa {c['dca_total_lot']} -> bỏ qua."); return snap
                sl_d = max(mode["sl"] * P["atr"], meta["stops_min"] * 1.1)
                sl = price - sl_d if side == "BUY" else price + sl_d
                tps = [p["tp"] for p in pos if p["tp"]]
                tp = tps[0] if tps else (price + sl_d * mode["rr"] if side == "BUY" else price - sl_d * mode["rr"])
                ok2, msg = br.order(sym, side, lot, round(sl, dg), round(tp, dg), "AIMT DCA")
                self.log(f"[{name}] DCA #{len(pos)}: " + ("✔ " if ok2 else "✘ ") + msg)
                if ok2: self.last_ts[name] = time.time()
                return br.snapshot(sym)
            if len(own) >= mode["max_pos"] and not manual:
                self.log(f"[{name}] đã đạt tối đa {mode['max_pos']} lệnh của EA."); return snap
            # ---- lệnh mới theo kế hoạch
            e, s_, t_ = plan_prices(plan, price, dg)
            sl_d = max(plan["sl_d"], meta["stops_min"] * 1.1); tp_d = max(plan["tp_d"], meta["stops_min"] * 1.1)
            sl = e - sl_d if side == "BUY" else e + sl_d
            tp = e + tp_d if side == "BUY" else e - tp_d
            lot = self.lot_for(br, sym, meta, sl_d, risk)
            if cap and lot > cap:
                self.log(f"[{name}] lot {lot} > giới hạn {cap} -> hạ xuống giới hạn."); lot = float(cap)
            if lot <= 0:
                self.log(f"[{name}] không tính được lot hợp lệ -> bỏ lệnh."); return snap
            if plan["kind"] == "LIMIT" and not manual:
                gap = max(meta["stops_min"], meta["spread"] * meta["point"]) * 1.1
                lim = ask - gap if side == "BUY" else bid + gap       # giá chờ phải cách thị trường tối thiểu
                if (side == "BUY" and e > lim) or (side == "SELL" and e < lim):
                    self.log(f"[{name}] giá chờ {e} quá gần thị trường -> dời về {round(lim, dg)}.")
                    e = round(lim, dg)
                    sl = e - sl_d if side == "BUY" else e + sl_d
                    tp = e + tp_d if side == "BUY" else e - tp_d
                ok2, msg = br.pending(sym, side, lot, e, round(sl, dg), round(tp, dg))
            else:
                ok2, msg = br.order(sym, side, lot, round(sl, dg), round(tp, dg))
            self.log(f"[{name}] " + ("✔ " if ok2 else "✘ ") + msg)
            if ok2:
                self.last_bar[name] = P["bar_id"]; self.last_ts[name] = time.time()
            return br.snapshot(sym)

    # ------------------------------------------------ theme / lệnh / phím tắt (kiểu Bloomberg)
    def apply_theme(self):
        bbg = self.cfg.get("theme") == "bloomberg"
        self.setStyleSheet(QSS_BBG if bbg else QSS)
        if hasattr(self, "chart"):
            self.chart.bg = "#000000" if bbg else "#05070d"; self.chart.update()
            self.nodes.bg = ("#0a0700", "#000000") if bbg else ("#0b1020", "#070a14"); self.nodes.update()
            self.card.setStyleSheet("background:#0a0700;border:1px solid #3a2a00;border-radius:3px;padding:8px" if bbg else
                                    "background:#0d1324;border:1px solid #232d4b;border-radius:6px;padding:8px")
        if hasattr(self, "tbl"): self.fill_summary()

    def find_acc(self, text):
        t = _norm(text.strip())
        for a in self.cfg["accounts"]:
            if _norm(a["name"]) == t: return a
        hits = [a for a in self.cfg["accounts"] if t and t in _norm(a["name"])]
        return hits[0] if len(hits) == 1 else None

    def say(self, msg):
        self.lbl_cmd.setText(msg); self.log("» " + msg)

    def run_cmd(self):
        t = self.cmd.text().strip(); self.cmd.clear()
        if t: self.exec_command(t)

    def exec_command(self, text):
        raw = text.strip(); parts = raw.split()
        if not parts: return
        cmd = _norm(parts[0]); args = [_norm(x) for x in parts[1:]]; rest = raw.split(None, 1)[1] if len(parts) > 1 else ""
        pa = self.cfg["pa"]; P_ = self.panel
        def num(x):
            try: return float(x.replace(",", "."))
            except ValueError: return None
        sides = {"ALL": 0, "BUY": 1, "SELL": -1}
        try:
            if cmd in ("BUY", "SELL"):
                lot = num(args[0]) if args else None
                self.panel_order(cmd, lot); self.say(f"{cmd} {lot or '(lot theo panel)'}")
            elif cmd == "CLOSE":
                w = args[0] if args else "ALL"
                f = {"ALL": lambda o, s: o.close_all(s), "BUY": lambda o, s: o.close_side(s, 1), "SELL": lambda o, s: o.close_side(s, -1),
                     "LOSS": lambda o, s: o.close_profit(s, False), "PROFIT": lambda o, s: o.close_profit(s, True)}.get(w)
                if not f: return self.say("CLOSE [ALL|BUY|SELL|LOSS|PROFIT]")
                self.panel_run(f); self.say(f"CLOSE {w}")
            elif cmd == "HALF":
                sd = sides.get(args[0] if args else "ALL", 0); self.panel_run(lambda o, s: o.close_half(s, sd)); self.say("HALF")
            elif cmd == "TRIM":
                sd = sides.get(args[0], 0) if args and args[0] in sides else 0
                v = num(args[-1]) if args and num(args[-1]) is not None else None
                if v is not None: pa["trim_net" if pa["trim_matched"] else "trim_pct"] = v; P_.on_mode(pa["trim_matched"])
                P_.trim(sd); self.say(f"TRIM {list(sides)[[0, 1, -1].index(sd)]} {v if v is not None else ''}")
            elif cmd == "BE": self.panel_run(lambda o, s: o.move_all_be(s)); self.say("BE")
            elif cmd in ("REV", "REVERSE"): self.panel_run(lambda o, s: o.reverse_all(s)); self.say("REVERSE")
            elif cmd in ("CLR", "CLEAR"): self.panel_run(lambda o, s: o.clear_sltp(s)); self.say("Xoá SL/TP")
            elif cmd in ("DEL", "DELETE"): self.panel_run(lambda o, s: o.delete_pendings(s)); self.say("Huỷ lệnh chờ")
            elif cmd in ("SL", "TP"):
                v = num(args[0]) if args else None
                if v is None: return self.say(f"{cmd} <points>, ví dụ {cmd} 100 hoặc {cmd} -50")
                self.panel_run(lambda o, s: o.adjust(s, cmd == "SL", v)); self.say(f"{cmd} {v:+.0f} pts")
            elif cmd == "ACC":
                if args and args[0] == "ALL": self.cfg["panel_target"] = "ALL"; P_.cb_tgt.setCurrentIndex(1)
                else:
                    a = self.find_acc(rest)
                    if not a or not a.get("enabled"): return self.say(f"Không thấy tài khoản đang bật: '{rest}'")
                    self.cb_acc.setCurrentText(a["name"]); self.cfg["panel_target"] = "VIEW"; P_.cb_tgt.setCurrentIndex(0)
                save_cfg(self.cfg); self.update_strip(); self.say(f"Đang điều khiển: {'TẤT CẢ' if self.cfg['panel_target'] == 'ALL' else self.primary()['name']}")
            elif cmd == "AUTO":
                on = (args[0] == "ON") if args else not self.cfg["auto_trade"]
                self.set_auto_trade(on); self.say(f"Auto Trade {'BẬT' if self.cfg['auto_trade'] else 'TẮT'}")
            elif cmd == "LOT":
                v = num(args[0]) if args else None
                if v is None or v <= 0: return self.say("LOT <số>")
                self.cfg["lot_fixed"] = v; self.cfg["lot_mode"] = "fixed"; P_.sp_lot.setValue(v); P_.cb_lot.setCurrentIndex(1); save_cfg(self.cfg); self.say(f"Lot cố định = {v}")
            elif cmd == "RISK":
                m = {"AN TOAN": "An toàn", "CAN BANG": "Cân bằng", "TAN CONG": "Tấn công"}.get(" ".join(args))
                if not m: return self.say("RISK <an toan|can bang|tan cong>")
                self.cb_risk.setCurrentText(m); P_.cb_kv.setCurrentText(m); self.recompute_plan(); self.say(f"Khẩu vị: {m}")
            elif cmd == "THR":
                v = num(args[0]) if args else None
                if v is None: return self.say("THR <số> (0 = theo khẩu vị)")
                self.cfg["thr_override"] = max(0.0, v); save_cfg(self.cfg); self.recompute_plan(); self.say(f"Ngưỡng điểm = {self.threshold():.2f}")
            elif cmd == "SIDE":
                m = {"OFF": "off", "FILTER": "filter", "REV": "reversion", "REVERSION": "reversion"}.get(args[0] if args else "")
                if not m: return self.say("SIDE <OFF|FILTER|REV>")
                self.cfg["side_mode"] = m; P_.cb_side.setCurrentIndex(["off", "filter", "reversion"].index(m)); save_cfg(self.cfg); self.recompute_plan(); self.say(f"Chế độ đi ngang: {m}")
            elif cmd == "PLAN":
                if not args or args[0] not in ("BEST", "SCALP", "SWING", "PO3"): return self.say("PLAN <BEST|SCALP|SWING|PO3>")
                self.cb_plan.setCurrentText(args[0]); self.say(f"Kế hoạch thực thi: {args[0]}")
            elif cmd in ("ANALYZE", "A"): self.start_analysis(True); self.say("Phân tích...")
            elif cmd == "RESUME":
                a = self.find_acc(rest) if rest else self.primary()
                if not a: return self.say("RESUME <tên tài khoản>")
                self.halt.pop(a["name"], None); ac = (self.snaps.get(a["name"]) or {}).get("acc") or {}
                if ac.get("equity"): a["peak"] = float(ac["equity"])
                save_cfg(self.cfg); self.refresh_accounts(); self.say(f"[{a['name']}] đã mở lại")
            elif cmd == "COPY":
                if len(parts) < 2: return self.say("COPY <nhận> <nguồn> | COPY <nhận> OFF")
                toks = rest.rsplit(" ", 1)
                if _norm(toks[-1]) == "OFF" and len(toks) == 2:
                    a = self.find_acc(toks[0])
                    if not a: return self.say(f"Không thấy tài khoản '{toks[0]}'")
                    a["copy_on"] = False; save_cfg(self.cfg); self.refresh_accounts(); return self.say(f"[{a['name']}] TẮT copy")
                done = False
                for k in range(1, len(parts) - 1):
                    dst, src = self.find_acc(" ".join(parts[1:1 + k])), self.find_acc(" ".join(parts[1 + k:]))
                    if dst and src and dst is not src:
                        dst["copy_from"] = src["name"]; dst["copy_on"] = True; self.copy.ignored.pop(dst["name"], None)
                        save_cfg(self.cfg); self.refresh_accounts(); self.say(f"[{dst['name']}] BẬT copy từ [{src['name']}] (chỉ lệnh mới)"); done = True; break
                if not done: self.say("COPY <tên nhận> <tên nguồn>")
            elif cmd in ("HELP", "?"): self.show_help()
            else: self.say(f"Lệnh không hợp lệ: {raw}  (gõ HELP hoặc F1)")
        except Exception as ex:
            self.say(f"Lỗi lệnh '{raw}': {ex}")

    def show_help(self):
        d = QtWidgets.QDialog(self); d.setWindowTitle("Trợ giúp — phím tắt & lệnh"); d.resize(760, 640)
        v = QtWidgets.QVBoxLayout(d); tb = QtWidgets.QTextBrowser(); v.addWidget(tb)
        h = ["<h3>Phím chức năng (luôn hoạt động)</h3><table cellpadding=3>"]
        h += [f"<tr><td><b>{k}</b></td><td>{t}</td></tr>" for k, t in HELP_KEYS]
        h.append("</table><h3>Phím chữ (bật bằng nút HK hoặc F10; theo TradePanelPro)</h3><table cellpadding=3>")
        h += [f"<tr><td><b>{k}</b></td><td>{t}</td></tr>" for k, t in HELP_HK]
        h.append("</table><p style='color:#999'>Phím chữ không hoạt động khi bạn đang gõ trong một ô nhập.</p><h3>Lệnh gõ ở ô ▶ (Enter)</h3><table cellpadding=3>")
        h += [f"<tr><td><b>{k}</b></td><td>{t}</td></tr>" for k, t in HELP_CMDS]
        h.append("</table><p>Panel áp dụng cho <b>tài khoản đang xem</b> hoặc <b>TẤT CẢ tài khoản</b> (F7 / lệnh <b>ACC ALL</b>).</p>")
        tb.setHtml("".join(h)); d.exec()

    def toggle_hotkeys(self, on=None):
        on = (not self.cfg.get("hotkeys")) if on is None else bool(on)
        self.cfg["hotkeys"] = on; save_cfg(self.cfg); self.b_hk.setChecked(on)
        self.say(f"Phím tắt chữ: {'BẬT' if on else 'TẮT'}")

    def toggle_target(self):
        to_all = self.cfg.get("panel_target") != "ALL"
        self.cfg["panel_target"] = "ALL" if to_all else "VIEW"; self.panel.cb_tgt.setCurrentIndex(1 if to_all else 0)
        save_cfg(self.cfg); self.update_strip(); self.say("Áp dụng cho: " + ("TẤT CẢ tài khoản" if to_all else "tài khoản đang xem"))

    def toggle_dock(self):
        self._top = "chat" if getattr(self, "_top", "panel") == "panel" else "panel"
        self.show_dock(self.chat if self._top == "chat" else self.panel)

    def cycle_account(self):
        names = [self.cb_acc.itemText(i) for i in range(self.cb_acc.count())]
        if len(names) > 1:
            i = (names.index(self.cb_acc.currentText()) + 1) % len(names); self.cb_acc.setCurrentIndex(i)
            self.cfg["panel_target"] = "VIEW"; self.panel.cb_tgt.setCurrentIndex(0); self.update_strip(); self.say(f"Đang xem: {names[i]}")

    def arm(self, side):
        self.armed = (side, time.time()); self.say(f"{side} đã sẵn sàng — bấm Q để xác nhận (4 giây), X để huỷ")

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.Type.KeyPress: return False
        win = QtWidgets.QApplication.activeWindow()
        if win is not self and not (win is not None and self.isAncestorOf(win)) and not isinstance(win, QtWidgets.QDockWidget): return False
        k, K = ev.key(), QtCore.Qt.Key
        fw = QtWidgets.QApplication.focusWidget()
        typing = isinstance(fw, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox)) or \
                 (isinstance(fw, QtWidgets.QComboBox) and fw.isEditable())
        if K.Key_F1 <= k <= K.Key_F10:
            {K.Key_F1: self.show_help, K.Key_F2: lambda: self.cmd.setFocus(), K.Key_F3: lambda: self.exec_command("AUTO"),
             K.Key_F4: lambda: self.start_analysis(True), K.Key_F5: lambda: self.b_auto.click(), K.Key_F6: self.cycle_account,
             K.Key_F7: self.toggle_target,
             K.Key_F8: lambda: self.exec_command("SIDE " + {"off": "FILTER", "filter": "REV", "reversion": "OFF"}[self.cfg["side_mode"]]),
             K.Key_F9: self.toggle_dock, K.Key_F10: self.toggle_hotkeys}[k]()
            return True
        if k == K.Key_Escape and self.armed: self.armed = None; self.say("Đã huỷ lệnh chờ xác nhận"); return True
        if not self.cfg.get("hotkeys") or typing or ev.modifiers() & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier): return False
        ch = (ev.text() or "").upper()
        if not ch: return False
        if ch == "Q" and self.armed:
            side, t = self.armed; self.armed = None
            if time.time() - t <= 4: self.exec_command(side)
            else: self.say("Hết thời gian xác nhận")
            return True
        if ch == "X" and self.armed: self.armed = None; self.say("Đã huỷ"); return True
        m = {"B": lambda: self.arm("BUY"), "S": lambda: self.arm("SELL"), "C": lambda: self.exec_command("CLOSE ALL"),
             "V": lambda: self.exec_command("CLOSE BUY"), "Z": lambda: self.exec_command("CLOSE SELL"),
             "1": lambda: self.exec_command("HALF BUY"), "2": lambda: self.exec_command("HALF SELL"), "3": lambda: self.exec_command("HALF ALL"),
             "T": lambda: self.exec_command("TRIM ALL"), "E": lambda: self.exec_command("BE"), "R": lambda: self.exec_command("REV"),
             "P": lambda: self.exec_command("CLOSE PROFIT"), "L": lambda: self.exec_command("CLOSE LOSS"),
             "U": lambda: self.panel.adj(True, 1), "J": lambda: self.panel.adj(True, -1),
             "I": lambda: self.panel.adj(False, 1), "K": lambda: self.panel.adj(False, -1),
             "D": lambda: self.exec_command("DEL"), "G": lambda: self.exec_command("CLR")}.get(ch)
        if m: m(); return True
        return False

    # ------------------------------------------------ Chat AI: bối cảnh + ảnh
    def apply_external_ai(self, d):
        """Nhận kết quả AI dán từ web (clipboard) và dùng như kết quả AI của chu kỳ hiện tại."""
        if not self.payload: return False
        d["_model"] = "Web (dán tay)"
        self.last_ai, self.last_ai_ts = d, time.time()
        self.finalize(d, False); return True

    def chat_png(self):
        P = self.payload
        return render_png(P["bars"], self.overlay(P, self.plan)) if P else None

    def chat_context(self):
        P, c = self.payload, self.cfg
        if not P:
            return "[BỐI CẢNH] Chưa có dữ liệu phân tích (người dùng chưa bấm 'Phân tích')."
        b, dg = P["bars"], P["digits"]; n = min(40, b.n); mode = RISK[c["risk_mode"]]
        L = [f"[BỐI CẢNH THỊ TRƯỜNG — snapshot lúc {time.strftime('%H:%M:%S')} giờ máy]",
             f"Symbol {P['sym']} | Khung {c['tf']} | Bid {P['bid']} Ask {P['ask']} | Spread {P['spread']} point | ATR14 {P['atr']:.{dg}f}",
             "40 nến gần nhất (giờ server UTC, O, H, L, C, tickvol; nến cuối đang chạy):"]
        for k in range(b.n - n, b.n):
            L.append(f"{dt.datetime.fromtimestamp(int(b.t[k]), dt.timezone.utc):%m-%d %H:%M},{b.o[k]:.{dg}f},{b.h[k]:.{dg}f},"
                     f"{b.l[k]:.{dg}f},{b.c[k]:.{dg}f},{int(b.v[k])}")
        L.append("\n[8 PHƯƠNG PHÁP CỤC BỘ] (mua/bán/trung lập, độ tin cậy do app tự quy ước)")
        for r in P["results"]:
            sg = {1: "MUA", -1: "BÁN", 0: "TRUNG LẬP"}[r.sig]
            L.append(f"- {r.name}: {sg} {r.conf:.0f}% | {r.state} | " + "; ".join(r.notes[:5]))
        if self.plan:
            p = self.plan
            L.append(f"\n[TÍN HIỆU TỔNG HỢP CỦA APP] {p['side']} | điểm {p['score']:+.2f} (ngưỡng ±{self.threshold():.2f}) | "
                     f"khẩu vị {c['risk_mode']} (risk {c['risk_pct'] or mode['risk']}%/lệnh, SL×{mode['sl']} ATR, RR {mode['rr']}, tối đa {mode['max_pos']} lệnh) | "
                     f"bộ trọng số {c['preset']} | kế hoạch thực thi: {c['exec_plan']} | Auto Trade: {'BẬT' if c['auto_trade'] else 'TẮT'}")
            ref = (P["ask"] if p["side"] == "BUY" else P["bid"]) or P["price"]
            for nm, pl in p["plans"].items():
                e, s_, t_ = plan_prices(pl, ref, dg)
                L.append(f"- Kế hoạch {nm}: {plan_label(pl)} entry {e} SL {s_} TP {t_} RR {pl['rr']:.2f} conf {pl['conf10']}/10 ({pl['note']})")
        rg_ = P.get("regime")
        if rg_ and rg_.get("state") in ("SIDEWAYS", "TREND"):
            L.append(f"[CHẾ ĐỘ THỊ TRƯỜNG] {rg_['state']} | ADX {rg_['adx']:.1f} | ER {rg_['er']:.2f} | biên {rg_['ll']:.{dg}f}–{rg_['hh']:.{dg}f} "
                     f"| chế độ sideway của app: {c.get('side_mode')}")
        if self.ai:
            L.append(f"[AI TÍN HIỆU GẦN NHẤT] {self.ai.get('signal')} {self.ai.get('confidence', 0):.0f}% | {self.ai.get('summary', '')}")
        L.append("\n[TÀI KHOẢN & LỆNH]")
        for a in self.enabled_accounts():
            s = self.snaps.get(a["name"]) or {}
            ac = s.get("acc") or {}
            L.append(f"- {a['name']}: balance {ac.get('balance', '?')} equity {ac.get('equity', '?')} {ac.get('currency', '')}")
        lv = self.last_live
        poss = lv["pos"] if lv else []
        if poss:
            for q in poss:
                L.append(f"  • {q['type']} {q['volume']} @ {q['open']} SL {q['sl']} TP {q['tp']} lãi/lỗ {q['profit']:+.2f}")
        else:
            L.append("  (không có lệnh đang mở trên symbol này)")
        L.append("\n[TIN GẦN ĐÂY] (▲/▼ chỉ là gắn nhãn từ khoá, không phải dự báo)")
        for d_, t_, s_ in self.news[:8]:
            L.append(f"- {d_:%H:%M} {'▲' if s_ > 0 else ('▼' if s_ < 0 else '•')} {t_}")
        if not self.news: L.append("(chưa có)")
        return "\n".join(L)

    # ------------------------------------------------ realtime / panel / gộp MT5
    def on_live(self, d):
        self.last_live = d; self.cur_sym = d["sym"]
        self.chart.title = f"{d['sym']},{d['tf']}" + ("  [MÔ PHỎNG]" if d.get("demo") else "")
        self.chart.set_bars(Bars(d["rates"])); self.chart.set_live(d["bid"], d["ask"])
        self.chart.set_trades(d["pos"], d["orders"], d.get("pt"))
        self.panel.update_stats(d)
        a = self.primary()
        if a and d.get("acc"): self.eval_halt(a, d["acc"])
        self.update_strip()

    def set_auto_trade(self, on):
        if on:
            n = len([a for a in self.enabled_accounts() if a.get("auto")])
            m = QtWidgets.QMessageBox.question(self, "Auto Trade",
                f"Bật tự động vào lệnh theo tín hiệu trên {n} tài khoản đang đánh dấu Auto?\n"
                "Khuyến nghị thử trên DEMO/lot nhỏ trước. Tín hiệu không đảm bảo lợi nhuận.")
            if m != QtWidgets.QMessageBox.StandardButton.Yes: on = False
        self.cfg["auto_trade"] = on; save_cfg(self.cfg)
        self.b_trade.setChecked(on)
        if hasattr(self, "panel"): self.panel.c_auto.setChecked(on)

    def refresh_plan(self):
        if self.payload and self.plan:
            self.plan = self.pack_plan(self.payload, self.plan["side"], self.plan["score"], self.ai, self.plan.get("rng"), self.plan.get("note", ""))
            self.chart.set_overlay(self.overlay(self.payload, self.plan)); self.render_card()

    def lot_for(self, br, sym, meta, sl_d, risk):
        c, m = self.cfg, self.cfg.get("lot_mode", "risk_pct")
        if m == "fixed": return norm_lot(meta, float(c["lot_fixed"]))
        if m == "risk_usd": return calc_lot(meta, float(c["risk_usd"]), sl_d, 100.0, self.log)
        return br.lot_for_risk(sym, sl_d, risk)

    def lot_est(self, P, sl_d, risk):
        c, m = self.cfg, self.cfg.get("lot_mode", "risk_pct")
        if m == "fixed": lot = norm_lot(P["meta"], float(c["lot_fixed"]))
        elif m == "risk_usd": lot = calc_lot(P["meta"], float(c["risk_usd"]), sl_d, 100.0)
        else: lot = calc_lot(P["meta"], P["bal"], sl_d, risk)
        return min(lot, c["max_lot"]) if c["max_lot"] and lot > c["max_lot"] else lot

    def panel_lot(self, meta, bal, sl_pts, force_risk=False):
        c = self.cfg; mode = c["lot_mode"]
        if (mode == "fixed" and not force_risk) or sl_pts <= 0:
            return norm_lot(meta, float(c["lot_fixed"]))
        sl_d = sl_pts * meta["point"]
        if mode == "risk_usd": return calc_lot(meta, float(c["risk_usd"]), sl_d, 100.0, self.log)
        return calc_lot(meta, bal, sl_d, c["risk_pct"] or RISK[c["risk_mode"]]["risk"], self.log)

    def panel_calc_lot(self):
        d = self.last_live
        if not d: self.log("Panel: chưa có dữ liệu."); return
        lot = self.panel_lot(d["meta"], (d.get("acc") or {}).get("balance", 0), self.cfg["pa"]["sl_pts"], True)
        if lot > 0: self.panel.sp_lot.setValue(lot); self.log(f"Panel: lot tính được = {lot}")

    def panel_run(self, fn, order=False):
        """Chạy 1 thao tác panel trên tài khoản đang xem, hoặc song song trên TẤT CẢ tài khoản đang bật."""
        accs = self.enabled_accounts() if self.cfg.get("panel_target") == "ALL" else [self.primary()]
        accs = [a for a in accs if a]
        if not accs: self.log("Panel: chưa có tài khoản đang bật."); return
        def one(acc):
            try:
                if order and acc["name"] in self.halt:
                    self.log(f"[{acc['name']}] CẢNH BÁO: tài khoản đang NGƯNG vì lỗ {self.halt[acc['name']]['loss']:.1f}% (lệnh tay vẫn được gửi).")
                with self.broker.session(acc) as ok:
                    if not ok: self.log(f"[{acc['name']}] Panel: không kết nối được."); return
                    sym = self.broker.resolve(self.acc_symbol(acc))
                    msg = fn(self.ops, sym)
                    if msg: self.log(f"[Panel/{acc['name']}] " + str(msg))
            except Exception:
                self.log(f"[{acc['name']}] Panel lỗi: " + traceback.format_exc())
        def job():
            with ThreadPoolExecutor(max_workers=len(accs)) as pool: list(pool.map(one, accs))
        threading.Thread(target=job, daemon=True).start()

    def panel_order(self, side, lot_override=None):
        pa, plan, lv = self.cfg["pa"], self.plan, self.last_live
        def fn(o, sym):
            br = o.br; meta = br.meta(sym); bal = (br.account() or {}).get("balance", 0)
            lot = float(lot_override) if lot_override else self.panel_lot(meta, bal, pa["sl_pts"])
            lot = norm_lot(meta, lot) if lot > 0 else 0.0
            cap = self.cfg["max_lot"]
            if cap and lot > cap: lot = float(cap)
            if lot <= 0: return "không tính được lot hợp lệ"
            sl_abs = tp_abs = None
            if pa["use_ai_levels"] and plan and plan["side"] == side and "SCALP" in plan["plans"] and lv:
                ref = lv["ask"] if side == "BUY" else lv["bid"]
                _e, sl_abs, tp_abs = plan_prices(plan["plans"]["SCALP"], ref, meta["digits"])
            return o.market(sym, side, lot, pa["sl_pts"], pa["tp_pts"], sl_abs, tp_abs)
        self.panel_run(fn, order=True)

    def panel_pending(self, kind, side):
        pa = self.cfg["pa"]
        def fn(o, sym):
            br = o.br; meta = br.meta(sym); bal = (br.account() or {}).get("balance", 0)
            lot = norm_lot(meta, self.panel_lot(meta, bal, pa["sl_pts"]))
            cap = self.cfg["max_lot"]
            if cap and lot > cap: lot = float(cap)
            return o.pending(sym, kind, side, lot, pa["gap_pts"], pa["sl_pts"], pa["tp_pts"])
        self.panel_run(fn, order=True)

    # ---- AI tự chỉnh SL/TP lệnh đang mở (chỉ siết SL, không nới)
    def job_ai_adjust(self, P, ai):
        try:
            sig = ai.get("signal")
            if sig not in ("BUY", "SELL"): return
            def num(x):
                try: return float(x)
                except (TypeError, ValueError): return None
            adv = ai.get("position_advice") or {}
            nsl = num(adv.get("new_sl")) or num(ai.get("sl")); ntp = num(adv.get("new_tp")) or num(ai.get("tp"))
            A, br = P["atr"], self.broker
            for acc in [a for a in self.enabled_accounts() if a.get("auto")]:
                with br.session(acc) as ok:
                    if not ok: continue
                    sym = br.resolve(self.acc_symbol(acc)); m = br.meta(sym); bid, ask = br.tick(sym); mn = m["stops_min"]
                    for p in br.positions(sym):
                        if p["magic"] != int(self.cfg["magic"]) or p["type"] != sig: continue
                        buy = sig == "BUY"; sl, tp, why = p["sl"], p["tp"], []
                        if nsl:
                            tighter = (buy and nsl < bid - mn and (sl == 0 or nsl > sl + 0.1 * A)) or \
                                      ((not buy) and nsl > ask + mn and (sl == 0 or nsl < sl - 0.1 * A))
                            if tighter: sl = round(nsl, m["digits"]); why.append(f"SL {p['sl']} -> {sl}")
                        if ntp:
                            ok_tp = (buy and ntp > bid + max(mn, 0.5 * A)) or ((not buy) and ntp < ask - max(mn, 0.5 * A))
                            if ok_tp and abs(ntp - tp) >= 0.1 * A: tp = round(ntp, m["digits"]); why.append(f"TP {p['tp']} -> {tp}")
                        if why and br.modify_pos(p["ticket"], sym, sl, tp):
                            self.log(f"[{acc['name']}] AI chỉnh #{p['ticket']}: " + "; ".join(why))
        except Exception:
            self.log("AI chỉnh SL/TP lỗi: " + traceback.format_exc())

    # ---- gộp với terminal MT5
    def set_dock_mode(self, idx):
        self.cfg["dock_mode"] = idx; self.release_embed(); self.dock_hwnd = None; self._last_dock = None
        if idx == 0: return
        def reset(msg):
            self.log(msg); self.cb_dock.blockSignals(True); self.cb_dock.setCurrentIndex(0); self.cb_dock.blockSignals(False)
            self.cfg["dock_mode"] = 0
        if not self.windock.ok: reset("Chế độ gộp MT5 chỉ chạy trên Windows."); return
        t = self.windock.pick(self.primary())
        if not t: reset("Không thấy cửa sổ terminal64.exe đang chạy. Hãy mở MT5 trước."); return
        self.dock_hwnd = t["hwnd"]; self.log(f"Gộp MT5: {t['title']} ({t['exe']})")
        if idx == 3: self.embed_mt5(t)

    def embed_mt5(self, t):
        m = QtWidgets.QMessageBox.question(self, "Nhúng MT5",
            "Nhúng cửa sổ MT5 vào app?\n- MT5 sẽ nằm trong khung bên trái của AIMT (mất thanh tiêu đề).\n"
            "- Khi chọn 'Gộp: Tắt' hoặc đóng AIMT bình thường, MT5 được TÁCH RA và khôi phục đúng như trước khi gộp.\n"
            "- Nếu AIMT bị tắt đột ngột (kill/crash), Windows có thể đóng luôn cửa sổ MT5 vì nó đang là cửa sổ con. "
            "Hãy lưu công việc trong MT5 trước khi dùng chế độ này.")
        if m != QtWidgets.QMessageBox.StandardButton.Yes:
            self.cb_dock.setCurrentIndex(0); return
        try:
            hwnd = t["hwnd"]
            self.holder.show(); self.hs.setSizes([620, 500, 260]); QtWidgets.QApplication.processEvents()
            self.windock.embed(hwnd, int(self.holder.winId()))
            self.embedded = dict(hwnd=hwnd); self.fit_embed(); self.embed_timer.start(250)
            self.log("Đã nhúng MT5 vào AIMT.")
        except Exception:
            self.log("Nhúng MT5 lỗi: " + traceback.format_exc())
            try: self.windock.restore(t["hwnd"])
            except Exception: pass
            self.embedded = None; self.holder.hide(); self.cb_dock.setCurrentIndex(0)

    def fit_embed(self):
        e = self.embedded
        if not e: return
        if not self.windock.alive(e["hwnd"]):
            self.embedded = None; self.embed_timer.stop(); self.holder.hide(); self.hs.setSizes([0, 760, 300])
            self.log("Cửa sổ MT5 đã đóng."); return
        dpr = self.holder.devicePixelRatioF()
        self.windock.fit(e["hwnd"], self.holder.width() * dpr, self.holder.height() * dpr)

    def release_embed(self):
        e = self.embedded
        if not e: return
        self.embedded = None; self.embed_timer.stop(); ok = False
        try: ok = self.windock.restore(e["hwnd"])
        except Exception as ex: self.log(f"Khôi phục MT5 lỗi: {ex}")
        self.holder.hide(); self.hs.setSizes([0, 760, 300])
        self.log("Đã tách MT5 và khôi phục như trước khi gộp." if ok else "Không khôi phục được MT5 (cửa sổ đã đóng?).")

    def dock_tick(self):
        m = self.cfg.get("dock_mode", 0)
        if m not in (1, 2) or not self.windock.ok: return
        h = self.dock_hwnd
        if not h or not self.windock.alive(h):
            t = self.windock.pick(self.primary()); self.dock_hwnd = h = t["hwnd"] if t else None
            if not h: return
        if self.windock.iconic(h):
            if self.isVisible(): self.hide()
            return
        if not self.isVisible(): self.show()
        l, t_, r, b = self.windock.rect(h)
        key = (m, l, t_, r, b)
        if key == getattr(self, "_last_dock", None): return
        self._last_dock = key; own = int(self.winId())
        if m == 1: self.windock.move(own, r, t_, 520, b - t_)
        else: self.windock.move(own, l, b, r - l, 420)

    def close_all(self):
        if QtWidgets.QMessageBox.question(self, "Đóng lệnh",
                "Đóng tất cả lệnh và huỷ lệnh chờ có magic của EA trên mọi tài khoản đang bật?") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        def job():
            snaps = {}
            def one(a):
                try:
                    with self.broker.session(a) as ok:
                        if not ok: return
                        n = self.broker.close_all_mine(); self.log(f"[{a['name']}] đã đóng/huỷ {n} lệnh.")
                        snaps[a["name"]] = self.broker.snapshot(self.acc_symbol(a))
                except Exception:
                    self.log(f"[{a['name']}] lỗi đóng lệnh: " + traceback.format_exc())
            accs_ = self.enabled_accounts()
            with ThreadPoolExecutor(max_workers=max(1, len(accs_))) as pool:
                list(pool.map(one, accs_))
            if snaps: self.bus.snap.emit(snaps)
        threading.Thread(target=job, daemon=True).start()

    def closeEvent(self, e):
        self.feed.alive = False; self.copy.alive = False
        try: self.set_dock_mode(0)
        except Exception: pass
        save_cfg(self.cfg); self.broker.shutdown(); e.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = Main(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
