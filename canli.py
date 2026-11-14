#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canli.py - Bitget USDT vadeli pump tarayici (SADECE MUM VERISI)

Mantik:
  Her taramada tum coinlerin son 30 mumu (varsayilan 1 dk) cekilir.
  SON mum, onceki 29 muma gore ani ve anormalse PUMP sinyali atilir:
    - son mum yesil ve yukselisi >= MIN_RISE_PCT
    - son mumun govdesi, onceki 29 mumun ortalama govdesinin >= BODY_MULT kati
    - (BEAT_MAX) son mumun govdesi, onceki 29 mumun en buyuk govdesinden de buyuk
Sinyalden sonra kagit ustu takip: TP/SL (marj %) + 15/30/60 dk sonuc mesaji + periyodik rapor.
Durum canli_state.json'a yazilir (workflow sonunda commit'lenir).
"""
import os
import json
import time
import math
import signal
from concurrent.futures import ThreadPoolExecutor

import requests


# ----------------------------------------------------------------- AYARLAR
def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


def _b(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "evet")


INTERVAL = os.getenv("INTERVAL", "1m")        # mum periyodu: 1m, 3m, 5m, 15m, 1H ...
LOOKBACK = int(_f("LOOKBACK", 30))            # izlenen mum sayisi (son mum dahil)
BODY_MULT = _f("BODY_MULT", 4.0)              # son mum govdesi >= onceki ortalama govde x bu
BEAT_MAX = _b("BEAT_MAX", True)               # son mum govdesi onceki en buyuk govdeden de buyuk olsun
MIN_RISE_PCT = _f("MIN_RISE_PCT", 1.0)        # taban: son mum en az %kac yukselsin (0 = kapali)
USE_FORMING = _b("USE_FORMING", True)         # True: olusan mumu da say (hizli); False: sadece kapanmis mum
MIN_24H_USDT_VOL = _f("MIN_24H_USDT_VOL", 50000)

LEVERAGE = _f("LEVERAGE", 20)
TP_MARGIN = _f("TP_MARGIN", 30)               # marj % kar
SL_MARGIN = _f("SL_MARGIN", 50)               # marj % zarar
TRACK_CHECKPOINTS = (15 * 60, 30 * 60, 60 * 60)

SCAN_SECONDS = _f("SCAN_SECONDS", 60)         # tarama periyodu
WORKERS = int(_f("WORKERS", 5))
REQ_SLEEP = _f("REQ_SLEEP", 0.2)
COOLDOWN_MIN = _f("COOLDOWN_MIN", 30)         # ayni coine tekrar sinyal bekleme
RUN_MINUTES = _f("RUN_MINUTES", 330)
REPORT_HOURS = _f("REPORT_HOURS", 3)
STATE_FILE = os.getenv("STATE_FILE", "canli_state.json")
MAX_CLOSED_KEPT = 2000

TG_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
            or os.getenv("BOT_TOKEN") or "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or ""

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
SESSION = requests.Session()
STOP = False

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
NON_CRYPTO = {"PAXG", "XAUT"}


def _on_signal(signum, frame):
    global STOP
    STOP = True
    print(f"[!] sinyal {signum} alindi, durum kaydedilip cikilacak", flush=True)


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def interval_ms(iv):
    n = int("".join(ch for ch in iv if ch.isdigit()) or 1)
    unit = iv[-1]
    if unit == "m":
        return n * 60_000
    if unit in ("H", "h"):
        return n * 3_600_000
    if unit in ("D", "d"):
        return n * 86_400_000
    return 60_000


IV_MS = interval_ms(INTERVAL)


# ----------------------------------------------------------------- YARDIMCI
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fmt_price(p):
    return f"{p:.8g}"


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log("[TG yok]", text.replace("\n", " | ")[:200])
        return
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log("[TG hata]", r.status_code, r.text[:200])
    except Exception as e:
        log("[TG hata]", e)


def api(path, params):
    for i in range(3):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=10)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "00000":
                    return j.get("data")
            elif r.status_code == 429:
                time.sleep(1 + i)
        except Exception as e:
            log("[api hata]", path, e)
        time.sleep(0.5)
    return None


# ----------------------------------------------------------------- DURUM
def load_state():
    st = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except FileNotFoundError:
        pass
    except Exception as e:
        log("[state okunamadi, sifirdan basliyor]", e)
        st = {}
    st.setdefault("v2_open", [])
    st.setdefault("v2_closed", [])
    st.setdefault("v2_cooldown", {})
    st.setdefault("v2_last_report", time.time())
    return st


def save_state(st):
    st["v2_closed"] = st["v2_closed"][-MAX_CLOSED_KEPT:]
    now = time.time()
    st["v2_cooldown"] = {k: v for k, v in st["v2_cooldown"].items()
                         if now - v < COOLDOWN_MIN * 60 * 2}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------- VERI
def keep_contract(c):
    sym = c.get("symbol", "")
    if not sym.endswith("USDT"):
        return False
    if c.get("symbolStatus", "normal") != "normal":
        return False
    if c.get("symbolType", "perpetual") != "perpetual":
        return False
    base = c.get("baseCoin") or sym[:-4]
    return base not in STABLES and base not in NON_CRYPTO


def fetch_tickers():
    data = api("/api/v2/mix/market/tickers", {"productType": PRODUCT})
    out = {}
    for d in data or []:
        try:
            sym = d["symbol"]
            price = float(d["lastPr"])
            vol24 = float(d.get("usdtVolume") or d.get("quoteVolume") or 0)
            if sym.endswith("USDT") and price > 0:
                out[sym] = (price, vol24)
        except Exception:
            continue
    return out


def fetch_candles(sym, limit):
    data = api("/api/v2/mix/market/candles",
               {"symbol": sym, "productType": PRODUCT, "granularity": INTERVAL, "limit": str(limit)})
    if not data:
        return None
    rows = []
    for r in data:
        try:
            ts, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            qv = float(r[6]) if len(r) > 6 else float(r[5]) * c
            rows.append((ts, o, h, l, c, qv))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0])
    return rows


# ----------------------------------------------------------------- ALGILAMA
def detect(rows):
    """rows: eskiden yeniye mumlar (ts, o, h, l, c, quoteVol). Son mum anormal yukselis mi?"""
    if not USE_FORMING:
        now_ms = int(time.time() * 1000)
        rows = [r for r in rows if r[0] + IV_MS <= now_ms]
    if len(rows) < LOOKBACK:
        return None
    win = rows[-LOOKBACK:]
    last, prev = win[-1], win[:-1]
    o, h, l, c = last[1], last[2], last[3], last[4]
    if o <= 0 or c <= o:
        return None
    rise = (c / o - 1.0) * 100.0
    if rise < MIN_RISE_PCT:
        return None
    body = c - o
    bodies = [abs(r[4] - r[1]) for r in prev]
    avg_body = max(mean(bodies), o * 1e-5)      # sifira bolmeyi onle
    ratio = body / avg_body
    if ratio < BODY_MULT:
        return None
    if BEAT_MAX and body <= max(bodies):
        return None
    base_v = mean(r[5] for r in prev)
    return {
        "ts": last[0], "open": o, "price": c, "rise": round(rise, 2),
        "ratio": round(ratio, 1),
        "vol": round(last[5] / base_v, 1) if base_v > 0 else 0.0,
        "wick": round((h - c) / (h - l), 2) if h > l else 0.0,
    }


def scan_symbol(sym):
    time.sleep(REQ_SLEEP)
    rows = fetch_candles(sym, LOOKBACK + 5)
    if not rows:
        return sym, None
    return sym, detect(rows)


# ----------------------------------------------------------------- TAKIP
def margin_pnl(entry, cur):
    return (cur / entry - 1.0) * 100.0 * LEVERAGE


def close_rec(st, rec, res, pnl):
    rec["res"] = res
    rec["pnl"] = round(pnl, 2)
    rec["t1"] = time.time()
    st["v2_open"].remove(rec)
    st["v2_closed"].append({k: rec.get(k) for k in ("sym", "t0", "entry", "m", "res", "pnl", "t1")})


def track(st, prices):
    now = time.time()
    for rec in list(st["v2_open"]):
        if rec["sym"] not in prices:
            continue
        cur = prices[rec["sym"]][0]
        pnl = margin_pnl(rec["entry"], cur)
        el = now - rec["t0"]
        notify = rec.get("notify", True)
        tp_price = rec["entry"] * (1 + TP_MARGIN / LEVERAGE / 100.0)
        sl_price = rec["entry"] * (1 - SL_MARGIN / LEVERAGE / 100.0)

        if pnl >= TP_MARGIN:
            if notify:
                send_tg(f"✅ TP vuruldu | {rec['sym']}\nGiriş {fmt_price(rec['entry'])} → {fmt_price(cur)}\n"
                        f"{LEVERAGE:.0f}x marj: +{TP_MARGIN:.0f}% ({el/60:.0f} dk)")
            close_rec(st, rec, "TP", TP_MARGIN)
            continue
        if pnl <= -SL_MARGIN:
            if notify:
                send_tg(f"❌ SL vuruldu | {rec['sym']}\nGiriş {fmt_price(rec['entry'])} → {fmt_price(cur)}\n"
                        f"{LEVERAGE:.0f}x marj: -{SL_MARGIN:.0f}% ({el/60:.0f} dk)")
            close_rec(st, rec, "SL", -SL_MARGIN)
            continue

        for cp in TRACK_CHECKPOINTS:
            key = f"cp{int(cp)}"
            if el >= cp and not rec.get(key):
                rec[key] = True
                if notify:
                    send_tg(f"⏱ {int(cp/60)} dk sonuç | {rec['sym']}\n"
                            f"Fiyat: {fmt_price(rec['entry'])} → {fmt_price(cur)} "
                            f"({(cur/rec['entry']-1)*100:+.2f}%)\n"
                            f"{LEVERAGE:.0f}x marj: {pnl:+.1f}%  "
                            f"(TP {fmt_price(tp_price)} / SL {fmt_price(sl_price)})")
        if el >= TRACK_CHECKPOINTS[-1]:
            close_rec(st, rec, "TIMEOUT", pnl)


# ----------------------------------------------------------------- RAPOR
def stats_line(rows):
    n = len(rows)
    if n == 0:
        return "n=0"
    tp = sum(1 for r in rows if r["res"] == "TP")
    sl = sum(1 for r in rows if r["res"] == "SL")
    avg = sum(r["pnl"] for r in rows) / n
    return f"n={n} | TP {tp} | SL {sl} | süre doldu {n - tp - sl} | ort. marj {avg:+.1f}%"


def make_report(st):
    closed = st["v2_closed"]
    if not closed:
        return None
    big = [r for r in closed if (r.get("m") or {}).get("ratio", 0) >= BODY_MULT * 2]
    lines = [
        "📊 RAPOR (sadece mum sinyali)",
        f"Tüm sinyaller: {stats_line(closed)}",
        f"Çok güçlü (gövde ≥{BODY_MULT * 2:.0f}x): {stats_line(big)}",
        f"\nAyar: {INTERVAL} mum, son {LOOKBACK} mum, gövde ≥{BODY_MULT}x"
        f"{', önceki en büyükten büyük' if BEAT_MAX else ''}, min yükseliş %{MIN_RISE_PCT}",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- ANA DONGU
def main():
    st = load_state()
    deadline = time.time() + RUN_MINUTES * 60
    last_save = time.time()
    log(f"Başladı. Açık takip: {len(st['v2_open'])}, kapanmış kayıt: {len(st['v2_closed'])}")
    send_tg(f"🟢 canli.py başladı | sadece mum | {INTERVAL} | son {LOOKBACK} mum | "
            f"gövde ≥{BODY_MULT}x{' + önceki en büyükten büyük' if BEAT_MAX else ''} | "
            f"min yükseliş %{MIN_RISE_PCT}")

    contracts = api("/api/v2/mix/market/contracts", {"productType": PRODUCT}) or []
    symbols_all = [c["symbol"] for c in contracts if keep_contract(c)]
    last_contract_refresh = time.time()
    log(f"{len(symbols_all)} vadeli coin bulundu")

    while not STOP and time.time() < deadline:
        t_cycle = time.time()

        if time.time() - last_contract_refresh > 3600:
            contracts = api("/api/v2/mix/market/contracts", {"productType": PRODUCT}) or []
            new = [c["symbol"] for c in contracts if keep_contract(c)]
            if new:
                symbols_all = new
            last_contract_refresh = time.time()

        prices = fetch_tickers()
        symbols = [s for s in symbols_all
                   if not prices or prices.get(s, (0, 0))[1] >= MIN_24H_USDT_VOL]
        open_syms = {r["sym"] for r in st["v2_open"]}
        now = time.time()
        symbols = [s for s in symbols
                   if s not in open_syms and now - st["v2_cooldown"].get(s, 0) >= COOLDOWN_MIN * 60]

        hits = []
        if symbols:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for sym, m in ex.map(scan_symbol, symbols):
                    if m:
                        hits.append((m["ratio"], sym, m))
        hits.sort(reverse=True)

        for _r, sym, m in hits:
            p = prices.get(sym, (m["price"], 0))[0] if prices else m["price"]
            st["v2_cooldown"][sym] = time.time()
            st["v2_open"].append({
                "sym": sym, "t0": time.time(), "entry": p, "notify": True, "m": m,
            })
            tp_price = p * (1 + TP_MARGIN / LEVERAGE / 100.0)
            sl_price = p * (1 - SL_MARGIN / LEVERAGE / 100.0)
            log(f"SİNYAL {sym} +{m['rise']}% gövde {m['ratio']}x hacim {m['vol']}x fitil {m['wick']}")
            send_tg(
                f"🚀 PUMP SİNYALİ | {sym}\n"
                f"Fiyat: {fmt_price(p)}\n"
                f"Son {INTERVAL} mum: +{m['rise']:.2f}% (ort. gövdenin {m['ratio']:.1f} katı)\n"
                f"Hacim: {m['vol']:.1f}x | Üst fitil: {m['wick']:.2f}\n"
                f"{LEVERAGE:.0f}x → TP {fmt_price(tp_price)} (+{TP_MARGIN:.0f}%) / "
                f"SL {fmt_price(sl_price)} (-{SL_MARGIN:.0f}%)"
            )

        if prices:
            track(st, prices)

        if time.time() - last_save > 60:
            save_state(st)
            last_save = time.time()
        if time.time() - st["v2_last_report"] > REPORT_HOURS * 3600:
            rep = make_report(st)
            if rep:
                send_tg(rep)
            st["v2_last_report"] = time.time()
            save_state(st)

        log(f"tarama bitti: {len(symbols)} coin, {len(hits)} sinyal, {time.time() - t_cycle:.0f} sn")
        end_sleep = t_cycle + SCAN_SECONDS
        while not STOP and time.time() < end_sleep:
            time.sleep(0.5)

    save_state(st)
    rep = make_report(st)
    if rep:
        send_tg(rep)
    log("Bitti, durum kaydedildi.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("[KRİTİK HATA]", repr(e))
        raise
                  
