#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canli.py - Bitget USDT vadeli pump tarayici

Akis:
 1) Tum vadeli coinleri TEK istekle (tickers) her SCAN_INTERVAL sn'de okur, kayan pencerede
    (WINDOW_SEC) ani yukselisi yakalar.
 2) Aday cikinca 1 dk mumlarla kontrol eder:
      - Hacim:   son pencere hacmi / onceki 60 mum ortalamasi   >= VOL_MULT
      - ATR:     hareket buyuklugu / ATR(30)                    >= ATR_MULT
      - Fitil:   ust fitil orani (tepeden satis)                <= MAX_WICK_RATIO
      - Sikisma: yukselisten onceki 15 mum volatilitesi / onceki 60 mum volatilitesi <= SQUEEZE_RATIO
 3) Tum filtreleri gecen aday -> Telegram sinyali.
    Hacmi gecip filtreye takilanlar da SESSIZCE kagit ustunde takip edilir (eski/yeni kural kiyasi).
 4) Kagit ustu takip: TP/SL (marj %) + 15/30/60 dk sonuc mesaji. Periyodik arastirma raporu.
 5) Durum canli_state.json'a yazilir (workflow sonunda commit'lenir).
"""
import os
import sys
import json
import time
import math
import signal
from collections import deque

import requests

# ----------------------------------------------------------------- AYARLAR
def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


def _b(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "evet")


WINDOW_SEC = _f("WINDOW_SEC", 120)        # yukselis penceresi (sn)
MIN_PCT = _f("MIN_PCT", 5.0)              # pencere icinde min yukselis %
MAX_PCT = _f("MAX_PCT", 0)                # 0 = ust sinir yok
VOL_MULT = _f("VOL_MULT", 10)             # hacim carpani (alt sinir)
MIN_24H_USDT_VOL = _f("MIN_24H_USDT_VOL", 0)  # cok kucuk coinleri elemek icin (0 = kapali)

USE_ATR = _b("USE_ATR", True)
ATR_MULT = _f("ATR_MULT", 3.0)            # hareket >= ATR x ATR_MULT
USE_WICK = _b("USE_WICK", True)
MAX_WICK_RATIO = _f("MAX_WICK_RATIO", 0.35)   # ust fitil / pencere araligi
USE_SQUEEZE = _b("USE_SQUEEZE", True)
SQUEEZE_RATIO = _f("SQUEEZE_RATIO", 0.9)  # kisa vol / uzun vol <= bu

LEVERAGE = _f("LEVERAGE", 20)
TP_MARGIN = _f("TP_MARGIN", 30)           # marj % kar
SL_MARGIN = _f("SL_MARGIN", 50)           # marj % zarar
TRACK_CHECKPOINTS = (15 * 60, 30 * 60, 60 * 60)

SCAN_INTERVAL = _f("SCAN_INTERVAL", 15)   # sn
COOLDOWN_MIN = _f("COOLDOWN_MIN", 60)     # ayni coine tekrar sinyal bekleme
RECHECK_SEC = _f("RECHECK_SEC", 60)       # filtreye/hacme takilan coini tekrar kontrol bekleme
MAX_CANDIDATES_PER_CYCLE = int(_f("MAX_CANDIDATES_PER_CYCLE", 8))
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


def _on_signal(signum, frame):
    global STOP
    STOP = True
    print(f"[!] sinyal {signum} alindi, durum kaydedilip cikilacak", flush=True)


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


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
def fetch_tickers():
    data = api("/api/v2/mix/market/tickers", {"productType": PRODUCT})
    if not data:
        return {}
    out = {}
    for d in data:
        try:
            sym = d["symbol"]
            if not sym.endswith("USDT"):
                continue
            price = float(d["lastPr"])
            vol24 = float(d.get("usdtVolume") or d.get("quoteVolume") or 0)
            if price > 0:
                out[sym] = (price, vol24)
        except Exception:
            continue
    return out


def fetch_candles(sym, limit=120):
    data = api("/api/v2/mix/market/candles",
               {"symbol": sym, "productType": PRODUCT, "granularity": "1m", "limit": str(limit)})
    if not data:
        return None
    rows = []
    for r in data:
        try:
            ts, o, h, l, c = float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            qv = float(r[6]) if len(r) > 6 else float(r[5]) * c
            rows.append((ts, o, h, l, c, qv))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0])
    return rows


def analyze(sym, cur, low_w, candles=None):
    """Aday icin hacim / ATR / fitil / sikisma metriklerini hesaplar."""
    c = candles if candles is not None else fetch_candles(sym)
    if not c or len(c) < 60:
        return None
    k = max(1, math.ceil(WINDOW_SEC / 60.0))
    recent, prior = c[-k:], c[:-k]
    if len(prior) < 40:
        return None

    # Hacim: pencere ortalama mum hacmi / onceki 60 mum ortalamasi
    base_v = mean(r[5] for r in prior[-60:])
    vol_ratio = (sum(r[5] for r in recent) / k) / base_v if base_v > 0 else 0.0

    # ATR(30) - onceki mumlardan
    seg = prior[-31:]
    trs = []
    for i in range(1, len(seg)):
        h, l, pc = seg[i][2], seg[i][3], seg[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = mean(trs)
    move = cur - low_w
    atr_ratio = move / atr if atr > 0 else 0.0

    # Ust fitil orani: pencere tepesinden ne kadar geri verdi
    hi = max(max(r[2] for r in recent), cur)
    lo = min(low_w, min(r[3] for r in recent))
    wick_ratio = (hi - cur) / (hi - lo) if hi > lo else 0.0

    # Sikisma: yukselisten onceki 15 mum volatilitesi / onceki 60 mum volatilitesi
    def rng_pct(rows):
        return mean((r[2] - r[3]) / r[4] for r in rows if r[4] > 0)

    short_v = rng_pct(prior[-15:])
    long_v = rng_pct(prior[-75:-15])
    squeeze_ratio = short_v / long_v if long_v > 0 else 1.0

    return {
        "vol": vol_ratio, "atr": atr_ratio, "wick": wick_ratio, "squeeze": squeeze_ratio,
    }


def check_filters(m):
    f = {
        "atr": (not USE_ATR) or m["atr"] >= ATR_MULT,
        "wick": (not USE_WICK) or m["wick"] <= MAX_WICK_RATIO,
        "squeeze": (not USE_SQUEEZE) or m["squeeze"] <= SQUEEZE_RATIO,
    }
    return f, all(f.values())


# ----------------------------------------------------------------- TAKIP
def margin_pnl(entry, cur):
    return (cur / entry - 1.0) * 100.0 * LEVERAGE


def close_rec(st, rec, res, pnl):
    rec["res"] = res
    rec["pnl"] = round(pnl, 2)
    rec["t1"] = time.time()
    st["v2_open"].remove(rec)
    st["v2_closed"].append({k: rec[k] for k in ("sym", "t0", "entry", "passed", "f", "m", "res", "pnl", "t1")})


def track(st, prices):
    now = time.time()
    for rec in list(st["v2_open"]):
        if rec["sym"] not in prices:
            continue
        cur = prices[rec["sym"]][0]
        pnl = margin_pnl(rec["entry"], cur)
        el = now - rec["t0"]
        tp_price = rec["entry"] * (1 + TP_MARGIN / LEVERAGE / 100.0)
        sl_price = rec["entry"] * (1 - SL_MARGIN / LEVERAGE / 100.0)

        if pnl >= TP_MARGIN:
            if rec["notify"]:
                send_tg(f"✅ TP vuruldu | {rec['sym']}\nGiriş {fmt_price(rec['entry'])} → {fmt_price(cur)}\n"
                        f"{LEVERAGE:.0f}x marj: +{TP_MARGIN:.0f}% ({el/60:.0f} dk)")
            close_rec(st, rec, "TP", TP_MARGIN)
            continue
        if pnl <= -SL_MARGIN:
            if rec["notify"]:
                send_tg(f"❌ SL vuruldu | {rec['sym']}\nGiriş {fmt_price(rec['entry'])} → {fmt_price(cur)}\n"
                        f"{LEVERAGE:.0f}x marj: -{SL_MARGIN:.0f}% ({el/60:.0f} dk)")
            close_rec(st, rec, "SL", -SL_MARGIN)
            continue

        for cp in TRACK_CHECKPOINTS:
            key = f"cp{int(cp)}"
            if el >= cp and not rec.get(key):
                rec[key] = True
                if rec["notify"]:
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
    passed = [r for r in closed if r["passed"]]
    rej = [r for r in closed if not r["passed"]]
    lines = [
        "📊 ARAŞTIRMA RAPORU",
        f"Eski kural (yükseliş+hacim): {stats_line(closed)}",
        f"Yeni kural (tüm filtreler): {stats_line(passed)}",
        f"Filtreye takılan: {stats_line(rej)}",
        "",
        "Tek tek filtre etkisi (eski kural adayları içinde):",
    ]
    for key, label in (("atr", "ATR"), ("wick", "Fitil"), ("squeeze", "Sıkışma")):
        ok = [r for r in closed if r["f"].get(key)]
        no = [r for r in closed if not r["f"].get(key)]
        lines.append(f"{label} geçen: {stats_line(ok)}")
        lines.append(f"{label} takılan: {stats_line(no)}")
    lines.append(f"\nAyar: pencere {WINDOW_SEC:.0f}sn, ≥%{MIN_PCT}, hacim ≥{VOL_MULT}x, "
                 f"ATR ≥{ATR_MULT}x, fitil ≤{MAX_WICK_RATIO}, sıkışma ≤{SQUEEZE_RATIO}")
    return "\n".join(lines)


# ----------------------------------------------------------------- ANA DONGU
def main():
    st = load_state()
    hist = {}                # sym -> deque[(t, price)]
    next_check = {}          # sym -> bu zamandan once tekrar kontrol etme
    deadline = time.time() + RUN_MINUTES * 60
    last_save = time.time()
    log(f"Başladı. Açık takip: {len(st['v2_open'])}, kapanmış kayıt: {len(st['v2_closed'])}")
    send_tg(f"🟢 canli.py başladı | pencere {WINDOW_SEC:.0f}sn ≥%{MIN_PCT} | hacim ≥{VOL_MULT}x | "
            f"ATR {'≥%.1fx' % ATR_MULT if USE_ATR else 'kapalı'} | "
            f"fitil {'≤%.2f' % MAX_WICK_RATIO if USE_WICK else 'kapalı'} | "
            f"sıkışma {'≤%.2f' % SQUEEZE_RATIO if USE_SQUEEZE else 'kapalı'}")

    while not STOP and time.time() < deadline:
        t_cycle = time.time()
        prices = fetch_tickers()
        if prices:
            now = time.time()
            open_syms = {r["sym"] for r in st["v2_open"]}
            cands = []
            for sym, (p, v24) in prices.items():
                h = hist.setdefault(sym, deque())
                h.append((now, p))
                while h and now - h[0][0] > WINDOW_SEC + SCAN_INTERVAL * 2:
                    h.popleft()
                if v24 < MIN_24H_USDT_VOL or sym in open_syms:
                    continue
                if now < next_check.get(sym, 0):
                    continue
                if now - st["v2_cooldown"].get(sym, 0) < COOLDOWN_MIN * 60:
                    continue
                win = [x for x in h if x[0] >= now - WINDOW_SEC]
                if len(win) < 2:
                    continue
                low_w = min(x[1] for x in win)
                rise = (p / low_w - 1.0) * 100.0
                if rise < MIN_PCT or (MAX_PCT > 0 and rise > MAX_PCT):
                    continue
                cands.append((rise, sym, p, low_w))

            cands.sort(reverse=True)
            for rise, sym, p, low_w in cands[:MAX_CANDIDATES_PER_CYCLE]:
                m = analyze(sym, p, low_w)
                next_check[sym] = time.time() + RECHECK_SEC
                if m is None:
                    continue
                m["rise"] = round(rise, 2)
                if m["vol"] < VOL_MULT:
                    log(f"{sym} +{rise:.1f}% ama hacim {m['vol']:.1f}x < {VOL_MULT}x, elendi")
                    continue
                f, passed = check_filters(m)
                st["v2_cooldown"][sym] = time.time()
                rec = {
                    "sym": sym, "t0": time.time(), "entry": p, "passed": passed, "notify": passed,
                    "f": f, "m": {k: round(v, 3) for k, v in m.items()},
                }
                st["v2_open"].append(rec)
                log(f"ADAY {sym} +{rise:.1f}% vol {m['vol']:.1f}x atr {m['atr']:.1f}x "
                    f"fitil {m['wick']:.2f} sıkışma {m['squeeze']:.2f} -> {'SİNYAL' if passed else 'takip (sessiz)'}")
                if passed:
                    tp_price = p * (1 + TP_MARGIN / LEVERAGE / 100.0)
                    sl_price = p * (1 - SL_MARGIN / LEVERAGE / 100.0)
                    send_tg(
                        f"⚡ ANLIK PUMP | {sym}\n"
                        f"Fiyat: {fmt_price(p)}\n"
                        f"{WINDOW_SEC:.0f} sn yükseliş: +{rise:.2f}%\n"
                        f"Hacim: {m['vol']:.1f}x | ATR: {m['atr']:.1f}x | "
                        f"Üst fitil: {m['wick']:.2f} | Sıkışma: {m['squeeze']:.2f}\n"
                        f"{LEVERAGE:.0f}x → TP {fmt_price(tp_price)} (+{TP_MARGIN:.0f}%) / "
                        f"SL {fmt_price(sl_price)} (-{SL_MARGIN:.0f}%)"
                    )

            track(st, prices)

        # periyodik kayit + rapor
        if time.time() - last_save > 60:
            save_state(st)
            last_save = time.time()
        if time.time() - st["v2_last_report"] > REPORT_HOURS * 3600:
            rep = make_report(st)
            if rep:
                send_tg(rep)
            st["v2_last_report"] = time.time()
            save_state(st)

        sleep_for = SCAN_INTERVAL - (time.time() - t_cycle)
        end_sleep = time.time() + max(0.0, sleep_for)
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
            
