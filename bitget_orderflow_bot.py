#!/usr/bin/env python3
"""
Bitget USDT-M Orderflow Sinyal Botu
-----------------------------------
- Bitget public WebSocket: trade (gercek taker yonu) + books15 (emir defteri)
- Kendi mumlarini ve footprint'ini (fiyat seviyesi bazli alis/satis hacmi) uretir
- Volume Profile (POC/VAH/VAL/HVN/LVN), delta, CVD divergence, absorption,
  stacked imbalance, liquidity sweep + reclaim, DOM duvar/cekilme analizi
- Mum kapanisinda skorlar, esigi gecerse Telegram'a LONG/SHORT sinyali yollar

Kurulum:
    pip install aiohttp websockets
Calistirma:
    export TG_TOKEN="BotFather'dan aldigin token"
    export TG_CHAT="chat id"
    python bitget_orderflow_bot.py

GitHub Actions modu: RUN_MINUTES kadar calisir, state/ klasorune durumunu yazar,
workflow bir sonraki turu baslatir (bkz. bot.yml).

NOT: Yatirim tavsiyesi degildir. Once sadece mesaj modunda izle, sonra karar ver.
"""
import asyncio
import json
import math
import os
import pickle
import statistics
import time
from collections import defaultdict, deque

import aiohttp
import websockets

# ======================= AYARLAR =======================
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")
TOP_N = int(os.getenv("TOP_N", "30"))              # hacme gore ilk N coin
TF_MIN = int(os.getenv("TF_MIN", "5"))             # mum periyodu (dk)
WARMUP_BARS = int(os.getenv("WARMUP_BARS", "24"))  # sinyal icin gereken min. mum
PROFILE_BARS = int(os.getenv("PROFILE_BARS", "96"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))       # max 8
MIN_RR = float(os.getenv("MIN_RR", "1.5"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))
SYMS_PER_CONN = 20                                 # 20 coin x 2 kanal = 40 (<50)
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "0"))   # 0 = sinirsiz (yerel); GitHub: 340
STATE_FILE = os.getenv("STATE_FILE", "state/state.pkl")
STATE_MAX_AGE_MIN = int(os.getenv("STATE_MAX_AGE_MIN", "90"))

WS_URL = "wss://ws.bitget.com/v2/ws/public"
REST = "https://api.bitget.com"
TF_MS = TF_MIN * 60_000

SYMS = {}
HTTP = None
LAST_TRADE = time.time()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fmt(p):
    return f"{p:.6g}"


def nice_step(price):
    raw = price * 0.0004
    mag = 10 ** math.floor(math.log10(raw))
    n = raw / mag
    for m in (1, 2, 5, 10):
        if n <= m:
            return m * mag
    return 10 * mag


# ======================= VERI YAPILARI =======================
class Bar:
    __slots__ = ("t", "o", "h", "l", "c", "vol", "delta", "fp")

    def __init__(self, t, price):
        self.t = t
        self.o = self.h = self.l = self.c = price
        self.vol = 0.0
        self.delta = 0.0
        self.fp = {}  # seviye -> [satis, alis]

    def add(self, price, size, side, step):
        k = int(price // step)
        cell = self.fp.get(k)
        if cell is None:
            cell = self.fp[k] = [0.0, 0.0]
        if side == "buy":
            cell[1] += size
            self.delta += size
        else:
            cell[0] += size
            self.delta -= size
        self.vol += size
        if price > self.h:
            self.h = price
        if price < self.l:
            self.l = price
        self.c = price


class Sym:
    def __init__(self, name):
        self.name = name
        self.step = None
        self.bar = None
        self.hist = deque(maxlen=PROFILE_BARS)
        self.cvd_hist = deque(maxlen=PROFILE_BARS)
        self.cvd = 0.0
        self.book = None
        self.book_hist = deque(maxlen=60)
        self.last_book_store = 0.0
        self.last_signal = 0.0
        self.first = True  # yeniden baslatma sonrasi ilk (eksik) mumu atla


# ======================= ANALIZ =======================
def atr_of(bars, n=14):
    r = [b.h - b.l for b in list(bars)[-n:]]
    return sum(r) / len(r) if r else 0.0


def build_profile(bars, step):
    vols = defaultdict(float)
    for b in bars:
        for k, (s, bv) in b.fp.items():
            vols[k] += s + bv
    if not vols:
        return None
    kmin, kmax = min(vols), max(vols)
    total = sum(vols.values())
    poc = max(vols, key=vols.get)
    lo = hi = poc
    acc = vols[poc]
    while acc < 0.7 * total and (lo > kmin or hi < kmax):
        up = vols.get(hi + 1, 0.0) if hi < kmax else -1.0
        dn = vols.get(lo - 1, 0.0) if lo > kmin else -1.0
        if up >= dn:
            hi += 1
            acc += max(up, 0.0)
        else:
            lo -= 1
            acc += max(dn, 0.0)
    avg = total / (kmax - kmin + 1)
    hvn, lvn = [], []
    for k in range(kmin + 1, kmax):
        v = vols.get(k, 0.0)
        a = vols.get(k - 1, 0.0)
        c = vols.get(k + 1, 0.0)
        if v > a and v >= c and v > 1.5 * avg:
            hvn.append((k + 0.5) * step)
        if v < a and v <= c and v < 0.5 * avg:
            lvn.append((k + 0.5) * step)
    return {
        "poc": (poc + 0.5) * step,
        "vah": (hi + 1) * step,
        "val": lo * step,
        "hvn": hvn,
        "lvn": lvn,
    }


def stacked_imbalance(bar, ratio=3.0, need=3):
    """Diyagonal imbalance: alis[k] >= 3 x satis[k-1], satis[k] >= 3 x alis[k+1]."""
    if len(bar.fp) < need + 1:
        return 0, 0
    avg = bar.vol / len(bar.fp)
    best_b = best_s = run_b = run_s = 0
    prev_k = None
    for k in sorted(bar.fp):
        sell, buy = bar.fp[k]
        below = bar.fp.get(k - 1)
        above = bar.fp.get(k + 1)
        buy_imb = below is not None and buy >= 0.5 * avg and buy >= ratio * max(below[0], 1e-12)
        sell_imb = above is not None and sell >= 0.5 * avg and sell >= ratio * max(above[1], 1e-12)
        if prev_k is None or k != prev_k + 1:
            run_b = run_s = 0
        run_b = run_b + 1 if buy_imb else 0
        run_s = run_s + 1 if sell_imb else 0
        best_b = max(best_b, run_b)
        best_s = max(best_s, run_s)
        prev_k = k
    return best_b, best_s


def absorption(b, avg_abs_delta, atr):
    """+1: satis absorbe edildi (bullish), -1: alis absorbe edildi (bearish)."""
    rng = b.h - b.l
    if abs(b.delta) > 2 * avg_abs_delta and rng < 0.8 * atr:
        if b.delta < 0 and b.c > b.l + 0.5 * rng:
            return 1
        if b.delta > 0 and b.c < b.l + 0.5 * rng:
            return -1
    return 0


def walls(levels, mult=4.0):
    notional = [p * s for p, s in levels]
    if len(notional) < 5:
        return []
    med = statistics.median(notional)
    if med <= 0:
        return []
    return [(p, n) for (p, _), n in zip(levels, notional) if n >= mult * med]


def book_signals(sym):
    if not sym.book:
        return None
    bids, asks = sym.book
    bn = sum(p * s for p, s in bids[:10])
    an = sum(p * s for p, s in asks[:10])
    imb = (bn - an) / (bn + an) if bn + an > 0 else 0.0
    bw, aw = walls(bids), walls(asks)
    bid_wall = max(bw, key=lambda x: x[1]) if bw else None
    ask_wall = max(aw, key=lambda x: x[1]) if aw else None
    pulled_bid = pulled_ask = False
    old = None
    now = time.time()
    for ts, ob, oa in sym.book_hist:
        if now - ts >= 20:
            old = (ob, oa)
    if old:
        cur_b, cur_a = dict(bids), dict(asks)
        for p, n in walls(old[0]):
            if p * cur_b.get(p, 0.0) < 0.4 * n:
                pulled_bid = True
        for p, n in walls(old[1]):
            if p * cur_a.get(p, 0.0) < 0.4 * n:
                pulled_ask = True
    return {"imb": imb, "bid_wall": bid_wall, "ask_wall": ask_wall,
            "pulled_bid": pulled_bid, "pulled_ask": pulled_ask}


def get_targets(direction, entry, risk, prof):
    lv = [prof["poc"], prof["vah"], prof["val"]] + prof["hvn"] + prof["lvn"]
    sign = 1 if direction == "long" else -1
    if direction == "long":
        cand = sorted(x for x in lv if x > entry + MIN_RR * risk)
    else:
        cand = sorted((x for x in lv if x < entry - MIN_RR * risk), reverse=True)
    t1 = cand[0] if cand else entry + sign * MIN_RR * risk
    t2 = cand[1] if len(cand) > 1 else entry + sign * max(3 * risk, abs(t1 - entry) * 1.5)
    return t1, t2


def analyze(sym):
    hist = list(sym.hist)
    cvdh = list(sym.cvd_hist)
    bar = hist[-1]
    prev = hist[:-1]
    if len(prev) < 20 or bar.vol <= 0:
        return None
    atr = atr_of(prev)
    if atr <= 0:
        return None
    prof = build_profile(hist, sym.step)
    if not prof:
        return None

    dr = bar.delta / bar.vol
    avg_abs = sum(abs(b.delta) for b in prev[-20:]) / 20
    ab_now = absorption(bar, avg_abs, atr)
    ab_prev = absorption(prev[-1], avg_abs, atr)

    win = prev[-30:]
    rh = max(b.h for b in win)
    rl = min(b.l for b in win)
    sweep_hi = bar.h > rh and bar.c < rh
    sweep_lo = bar.l < rl and bar.c > rl

    p20 = prev[-20:]
    c20 = cvdh[:-1][-20:]
    bear_div = bar.h > max(b.h for b in p20) and cvdh[-1] < max(c20)
    bull_div = bar.l < min(b.l for b in p20) and cvdh[-1] > min(c20)

    sb, ss = stacked_imbalance(bar)
    bs = book_signals(sym)

    def near(p, levels):
        return any(abs(p - x) <= 0.5 * atr for x in levels)

    long_lv = [prof["val"], prof["poc"]] + prof["hvn"]
    short_lv = [prof["vah"], prof["poc"]] + prof["hvn"]

    # ---------- LONG ----------
    L = []
    if sweep_lo:
        L.append((2, f"Likidite süpürme + geri alma: {fmt(rl)} altına iğne, üstünde kapanış"))
    if dr > 0.10:
        L.append((1, f"Delta pozitif ({bar.delta:+.1f}, hacmin %{dr*100:.0f}'i)"))
    if ab_now == 1 or ab_prev == 1:
        L.append((1, "Satış absorpsiyonu: agresif satış fiyatı düşüremedi"))
    if bull_div:
        L.append((1, "Bullish CVD divergence: fiyat yeni dip, CVD yapmadı"))
    if sb >= 3:
        L.append((1, f"{sb} kademeli stacked buy imbalance"))
    if near(bar.l, long_lv):
        L.append((1, "Fiyat VAL / POC / HVN bölgesinde"))
    if bs and (bs["imb"] > 0.15 or bs["pulled_ask"] or
               (bs["bid_wall"] and 0 < bar.c - bs["bid_wall"][0] <= atr)):
        L.append((1, "Emir defteri alıcı lehine (bid ağırlığı / alt duvar / satıcı duvarı çekildi)"))
    sL = sum(p for p, _ in L)
    trig_long = sweep_lo or (ab_now == 1 and near(bar.l, long_lv))

    # ---------- SHORT ----------
    S = []
    if sweep_hi:
        S.append((2, f"Likidite süpürme + geri alma: {fmt(rh)} üstüne iğne, altında kapanış"))
    if dr < -0.10:
        S.append((1, f"Delta negatif ({bar.delta:+.1f}, hacmin %{abs(dr)*100:.0f}'i)"))
    if ab_now == -1 or ab_prev == -1:
        S.append((1, "Alış absorpsiyonu: agresif alış fiyatı yükseltemedi"))
    if bear_div:
        S.append((1, "Bearish CVD divergence: fiyat yeni zirve, CVD yapmadı"))
    if ss >= 3:
        S.append((1, f"{ss} kademeli stacked sell imbalance"))
    if near(bar.h, short_lv):
        S.append((1, "Fiyat VAH / POC / HVN bölgesinde"))
    if bs and (bs["imb"] < -0.15 or bs["pulled_bid"] or
               (bs["ask_wall"] and 0 < bs["ask_wall"][0] - bar.c <= atr)):
        S.append((1, "Emir defteri satıcı lehine (ask ağırlığı / üst duvar / alıcı duvarı çekildi)"))
    sS = sum(p for p, _ in S)
    trig_short = sweep_hi or (ab_now == -1 and near(bar.h, short_lv))

    direction = None
    if trig_long and sL >= MIN_SCORE and sL > sS:
        direction, score, reasons = "long", sL, L
    elif trig_short and sS >= MIN_SCORE and sS > sL:
        direction, score, reasons = "short", sS, S
    if not direction:
        return None

    entry = bar.c
    if direction == "long":
        stop = bar.l - 0.2 * atr
    else:
        stop = bar.h + 0.2 * atr
    risk = abs(entry - stop)
    if risk <= 0 or risk > 2.5 * atr:
        return None
    t1, t2 = get_targets(direction, entry, risk, prof)
    return {"dir": direction, "score": score, "entry": entry, "stop": stop,
            "t1": t1, "t2": t2, "rr1": abs(t1 - entry) / risk,
            "rr2": abs(t2 - entry) / risk,
            "reasons": [t for _, t in reasons], "prof": prof, "atr": atr}


def format_signal(sym, s):
    icon = "🟢 LONG" if s["dir"] == "long" else "🔴 SHORT"
    pr = s["prof"]
    lines = [
        f"{icon} | {sym.name} ({TF_MIN}dk)",
        f"Skor: {s['score']}/8",
        "",
        f"Giriş: {fmt(s['entry'])}",
        f"Stop: {fmt(s['stop'])}",
        f"Hedef 1: {fmt(s['t1'])} (R/R {s['rr1']:.1f})",
        f"Hedef 2: {fmt(s['t2'])} (R/R {s['rr2']:.1f})",
        "",
        "Gerekçe:",
    ]
    lines += [f"• {r}" for r in s["reasons"]]
    lines += [
        "",
        f"Profil: POC {fmt(pr['poc'])} | VAH {fmt(pr['vah'])} | VAL {fmt(pr['val'])}",
        "Geçersiz: stop seviyesinin ötesinde mum kapanışı.",
        "Yatırım tavsiyesi değildir.",
    ]
    return "\n".join(lines)


# ======================= OLAY ISLEME =======================
async def notify(text):
    log("SINYAL/MESAJ:\n" + text)
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        async with HTTP.post(url, json={"chat_id": TG_CHAT, "text": text}) as r:
            if r.status != 200:
                log("Telegram hata:", r.status, await r.text())
    except Exception as e:
        log("Telegram istisna:", e)


def close_bar(sym):
    b = sym.bar
    if sym.first:
        sym.first = False
        return
    sym.hist.append(b)
    sym.cvd_hist.append(sym.cvd)
    if len(sym.hist) < WARMUP_BARS:
        return
    if time.time() - sym.last_signal < COOLDOWN_MIN * 60:
        return
    try:
        sig = analyze(sym)
    except Exception as e:
        log("analiz hatasi", sym.name, repr(e))
        return
    if sig:
        sym.last_signal = time.time()
        asyncio.create_task(notify(format_signal(sym, sig)))


def on_trade(sym, price, size, side, ts):
    global LAST_TRADE
    LAST_TRADE = time.time()
    if sym.step is None:
        sym.step = nice_step(price)
    t = ts // TF_MS * TF_MS
    if sym.bar is not None and t > sym.bar.t:
        close_bar(sym)
        sym.bar = None
    if sym.bar is None:
        sym.bar = Bar(t, price)
    sym.bar.add(price, size, side, sym.step)
    sym.cvd += size if side == "buy" else -size


def on_book(sym, data):
    bids = [(float(p), float(s)) for p, s in data.get("bids", [])]
    asks = [(float(p), float(s)) for p, s in data.get("asks", [])]
    if not bids or not asks:
        return
    sym.book = (bids, asks)
    now = time.time()
    if now - sym.last_book_store >= 1.0:
        sym.book_hist.append((now, bids, asks))
        sym.last_book_store = now


def handle(raw):
    msg = json.loads(raw)
    arg = msg.get("arg")
    data = msg.get("data")
    if not arg or not data:
        return
    sym = SYMS.get(arg.get("instId"))
    if not sym:
        return
    ch = arg.get("channel")
    if ch == "trade":
        if msg.get("action") == "snapshot":
            return  # abonelikte gelen gecmis islemleri atla
        for t in data:
            on_trade(sym, float(t["price"]), float(t["size"]),
                     str(t["side"]).lower(), int(t["ts"]))
    elif ch == "books15":
        on_book(sym, data[0])


# ======================= BAGLANTI =======================
async def pinger(ws):
    while True:
        await asyncio.sleep(25)
        await ws.send("ping")


async def ws_worker(shard):
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2 ** 23) as ws:
                backoff = 1
                args = []
                for s in shard:
                    args.append({"instType": "USDT-FUTURES", "channel": "trade", "instId": s})
                    args.append({"instType": "USDT-FUTURES", "channel": "books15", "instId": s})
                for i in range(0, len(args), 10):
                    await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
                    await asyncio.sleep(0.3)
                task = asyncio.create_task(pinger(ws))
                log(f"WS bagli: {len(shard)} coin")
                try:
                    async for raw in ws:
                        if raw == "pong":
                            continue
                        try:
                            handle(raw)
                        except Exception as e:
                            log("mesaj hatasi", repr(e))
                finally:
                    task.cancel()
        except Exception as e:
            log("WS koptu:", repr(e), f"- {backoff}sn sonra tekrar")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def fetch_universe():
    async with HTTP.get(f"{REST}/api/v2/mix/market/tickers",
                        params={"productType": "USDT-FUTURES"}) as r:
        j = await r.json()
    rows = j.get("data", [])

    def vol(x):
        for k in ("usdtVolume", "quoteVolume"):
            try:
                return float(x.get(k) or 0)
            except (TypeError, ValueError):
                pass
        return 0.0

    rows.sort(key=vol, reverse=True)
    return [x["symbol"] for x in rows[:TOP_N]]


def save_state():
    if not SYMS:
        return
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        data = {"saved": time.time(), "syms": {
            n: {"step": s.step, "hist": list(s.hist), "cvd_hist": list(s.cvd_hist),
                "cvd": s.cvd, "last_signal": s.last_signal}
            for n, s in SYMS.items()}}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, STATE_FILE)
        log("state kaydedildi")
    except Exception as e:
        log("state kaydedilemedi:", repr(e))


def load_state():
    try:
        with open(STATE_FILE, "rb") as f:
            data = pickle.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:
        log("state okunamadi:", repr(e))
        return 0
    age = time.time() - data.get("saved", 0)
    if age > STATE_MAX_AGE_MIN * 60:
        log(f"state cok eski ({age/60:.0f} dk), kullanilmadi")
        return 0
    n = 0
    for name, d in data.get("syms", {}).items():
        s = SYMS.get(name)
        if not s:
            continue
        s.step = d["step"]
        s.hist.extend(d["hist"])
        s.cvd_hist.extend(d["cvd_hist"])
        s.cvd = d["cvd"]
        s.last_signal = d["last_signal"]
        n += 1
    log(f"state yuklendi: {n} coin, yas {age/60:.1f} dk")
    return n


async def heartbeat():
    while True:
        await asyncio.sleep(600)
        ready = sum(1 for s in SYMS.values() if len(s.hist) >= WARMUP_BARS)
        log(f"durum: {ready}/{len(SYMS)} coin hazir (warm-up {WARMUP_BARS} mum)")
        save_state()


async def watchdog():
    """5 dk islem verisi gelmezse (IP engeli, kopuk baglanti) uyar ve cik."""
    while True:
        await asyncio.sleep(60)
        if time.time() - LAST_TRADE > 300:
            await notify("⚠️ 5 dakikadir Bitget'ten islem verisi gelmiyor "
                         "(IP engeli veya baglanti sorunu olabilir). Bot duruyor.")
            raise RuntimeError("islem verisi yok")


async def main():
    global HTTP, LAST_TRADE
    HTTP = aiohttp.ClientSession()
    try:
        symbols = await fetch_universe()
        if not symbols:
            raise RuntimeError("Coin listesi alinamadi (Bitget REST erisimi?)")
        for s in symbols:
            SYMS[s] = Sym(s)
        loaded = load_state()
        shards = [symbols[i:i + SYMS_PER_CONN] for i in range(0, len(symbols), SYMS_PER_CONN)]
        if loaded:
            info = f"onceki veriler yuklendi ({loaded} coin), sinyaller acik."
        else:
            info = f"ilk sinyal icin ~{WARMUP_BARS * TF_MIN} dk veri toplanacak."
        await notify(f"🤖 Bot başladı: {len(symbols)} coin, {TF_MIN}dk, {info}")
        LAST_TRADE = time.time()
        tasks = asyncio.gather(*(ws_worker(sh) for sh in shards), heartbeat(), watchdog())
        timeout = RUN_MINUTES * 60 if RUN_MINUTES > 0 else None
        try:
            await asyncio.wait_for(tasks, timeout=timeout)
        except asyncio.TimeoutError:
            log("sure doldu, state kaydedilip cikiliyor")
    finally:
        save_state()
        aw
