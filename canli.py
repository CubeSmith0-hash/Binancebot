import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))
POLL_SEC = 1

# ================= ANINDA BAŞLANGIÇ SİNYALİ (CİDDİ PARA AKIŞI ODAKLI) =================
START_ENABLED = True
# (pencere_sn, min_yükseliş_%, min_hacim_katı, min_pencere_hacmi_USDT)
# Yalnızca devasa hacim ve para girişiyle eşleşen gerçek long pump'lar yakalanır.
START_TIERS = [
    (15, 1.5, 12.0, 25_000),    # çok hızlı ve sert: 15 sn'de %1.5 + 12x hacim + 25k USDT
    (30, 2.5, 9.0,  50_000),    # hızlı:     30 sn'de %2.5 + 9x hacim  + 50k USDT
    (60, 4.0, 6.0,  80_000),    # normal:    60 sn'de %4.0 + 6x hacim  + 80k USDT
]
START_MAX_PCT = 7.0        # Çok geç kalınmış (pencerede %7'den fazla gitmiş) coinler atlanır
START_PULLBACK_PCT = 0.3   # Tepe noktadan maksimum bu kadar geri çekilmiş olabilir (direnç kırma anı)
START_UP_TICKS = 3         # Son 3 saniyede fiyatın yönü yukarı veya yatay olmalı (düşmemeli)
START_BASE_MIN_SEC = 600   # Normal hacim hesabı için en az 10 dk geçmiş olmalı
START_BASE_GAP_SEC = 90    # Baz hesabında son 90 sn dışarıda tutulur (pump etkisini kirletmesin)
START_BASE_FLOOR = 0.4     # Baz hacim, 24s ortalamanın en az %40'ı sayılır
START_COOLDOWN_MIN = 20    # Aynı coin için tekrar sinyal üretme süresi (20 dk spam koruması)
START_LOG_FILE = "start_log.jsonl"

# Ciddi para girişi filtresi (OI = Açık Pozisyon)
START_OI_WINDOW_SEC = 45   # OI değişimine bakılan süre
START_OI_PCT = 2.5         # OI en az %2.5 artmalı (ciddi kaldıraçlı pozisyon girişi)
START_OI_MIN_USDT = 120_000# En az 120,000 USDT'lik yeni net borç/pozisyon açılmış olmalı
START_REQUIRE_OI = True    # OI şartı sağlanmazsa asla sinyal atılmaz

# ---- Genel ----
MIN_24H_VOL = 1_000_000    # Çöp coinleri elemek için günlük minimum 1M USDT hacim şartı

HISTORY_SEC = max([t[0] for t in START_TIERS] + [START_OI_WINDOW_SEC]) + 30
COARSE_STEP = 10           # Hacim tabanı örnekleme aralığı (sn)
SLOW_SEC = 1800            # Normal hacim tabanı için 30 dk geçmiş
TZ = timezone(timedelta(hours=3))
STATE_FILE = "canli_state.json"

HOST = "https://api.bitget.com"
TICKER_PATH = "/api/v2/mix/market/tickers"
TICKER_PARAMS = {"productType": "USDT-FUTURES"}

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
FIAT = {"BRL", "JPY", "CNH", "CNY", "EUR", "GBP", "TRY", "RUB", "INR",
        "KRW", "MXN", "CAD", "AUD", "CHF", "SGD", "IDR", "NGN", "ARS"}
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
    "DELL", "IBM", "NOW", "CRM", "IREN", "BX", "HPE", "CRWD",
}
NON_CRYPTO = {"PAXG", "XAUT"}

HISTORY = {}      # sembol -> deque[(zaman, fiyat, 24s_hacim, açık_pozisyon)]
SLOW = {}         # sembol -> deque[(zaman, 24s_hacim)]
LAST_ALERT = {}   # "S:sembol" -> son sinyal zamanı
WATCH = set()


def keep(sym):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in STABLES or base in NON_CRYPTO or base in STOCKS:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    if base.startswith("USD") and len(base) == 6 and base[3:] in FIAT:
        return False
    return True


def fmt(p):
    if p < 1:
        return f"{p:.8f}".rstrip("0").rstrip(".")
    return f"{p:.4f}".rstrip("0").rstrip(".")


def fmt_vol(v):
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def send(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
                timeout=15,
            )
            print("telegram:", r.status_code)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return
        except requests.RequestException:
            time.sleep(2)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            limit = time.time() - START_COOLDOWN_MIN * 60
            return {
                k: v for k, v in data.items()
                if isinstance(v, (int, float)) and v > limit
            }
        except (OSError, ValueError, AttributeError):
            pass
    return {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(LAST_ALERT, f)
    except OSError as e:
        print("state yazılamadı", e)


def to_float(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_snapshot():
    try:
        r = requests.get(HOST + TICKER_PATH, params=TICKER_PARAMS, timeout=15)
    except requests.RequestException as e:
        print("hata", e)
        return 0, None
    if r.status_code != 200:
        print("HTTP", r.status_code, TICKER_PATH)
        return r.status_code, None
    try:
        body = r.json()
    except ValueError:
        print("json okunamadı")
        return 0, None
    if body.get("code") != "00000":
        print("API hatası:", body.get("code"), body.get("msg"))
        return 0, None
    out = {}
    for t in body.get("data", []):
        try:
            vol = t.get("usdtVolume") or t.get("quoteVolume")
            out[t["symbol"]] = (
                float(t["lastPr"]),
                float(vol),
                to_float(t.get("fundingRate")),
                to_float(t.get("holdingAmount")),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return 200, out


def refresh_symbols(snap):
    WATCH.clear()
    for sym, d in snap.items():
        if keep(sym) and d[1] >= MIN_24H_VOL:
            WATCH.add(sym)
    print(f"{len(WATCH)} Bitget vadeli coin izleniyor (Filtre: min 1M 24s hacim)")


def window(hist, now, sec):
    out = []
    for p in reversed(hist):
        if p[0] < now - sec:
            break
        out.append(p)
    out.reverse()
    return out


def base_rate(slow, now, vol24):
    day_rate = vol24 / 86400.0
    floor = day_rate * START_BASE_FLOOR
    if len(slow) < 2 or now - slow[0][0] < START_BASE_MIN_SEC:
        return max(day_rate, 0.0)
    old = slow[0]
    recent = None
    for p in reversed(slow):
        if p[0] <= now - START_BASE_GAP_SEC:
            recent = p
            break
    if not recent or recent[0] <= old[0]:
        return max(day_rate, 0.0)
    rate = (recent[1] - old[1]) / (recent[0] - old[0])
    return max(rate, floor)


def oi_change(hist, now):
    pts = window(hist, now, START_OI_WINDOW_SEC)
    if len(pts) < 3 or now - pts[0][0] < START_OI_WINDOW_SEC * 0.7:
        return None
    o0, o1 = pts[0][3], pts[-1][3]
    if not o0 or not o1 or o0 <= 0:
        return None
    o_mid = pts[len(pts) // 2][3]
    if o_mid is None or not (o0 <= o_mid <= o1):
        return None
    pct = (o1 / o0 - 1) * 100
    usdt = (o1 - o0) * pts[-1][1]
    return pct, usdt


def start_eval(hist, slow, now, vol24):
    last_t, last_p, last_q = hist[-1][0], hist[-1][1], hist[-1][2]
    rate = base_rate(slow, now, vol24)
    if rate <= 0:
        return None

    recent = window(hist, now, START_UP_TICKS + 1)
    if len(recent) >= 2 and recent[-1][1] < recent[0][1]:
        return None

    best = None
    for sec, min_pct, min_mult, min_vol in START_TIERS:
        pts = window(hist, now, sec)
        if len(pts) < 3 or now - pts[0][0] < sec * 0.7:
            continue
        low = min(p[1] for p in pts)
        high = max(p[1] for p in pts)
        if low <= 0 or last_p <= low:
            continue
        pct = (last_p / low - 1) * 100
        if pct < min_pct or pct > START_MAX_PCT:
            continue
        if (high / last_p - 1) * 100 > START_PULLBACK_PCT:
            continue
        vol = last_q - pts[0][2]
        if vol < min_vol:
            continue
        span = max(now - pts[0][0], 1.0)
        mult = (vol / span) / rate
        if mult < min_mult:
            continue
        cand = (sec, last_p, pct, vol, mult)
        if best is None or mult > best[4]:
            best = cand
    if best is None:
        return None
    
    oi = oi_change(hist, now)
    if START_REQUIRE_OI and (
            oi is None or oi[0] < START_OI_PCT or oi[1] < START_OI_MIN_USDT):
        return None
    return best + (oi,)


def alert_start(sym, vol24, sec, price, pct, vol, mult, oi):
    LAST_ALERT["S:" + sym] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    oi_line = (f"💰 Ciddi Para Girişi ({START_OI_WINDOW_SEC} sn): "
               f"+{fmt_vol(max(oi[1], 0))} USDT yeni pozisyon "
               f"(OI {oi[0]:+.2f}%)")
    head = "🚀 SERT LONG PUMP"
    send(
        f"{head} {sym} (Bitget Vadeli)\n"
        f"Saat: {t}\n"
        f"Fiyat: {fmt(price)}\n"
        f"Son {sec} sn: +{pct:.2f}%\n"
        f"Hacim Patlaması: {fmt_vol(vol)} USDT ({mult:.1f}x normal)\n"
        f"{oi_line}\n"
        f"24s hacim: {fmt_vol(vol24)}"
    )
    try:
        with open(START_LOG_FILE, "a") as f:
            f.write(json.dumps({
                "t": int(time.time()), "sym": sym, "price": price,
                "sec": sec, "pct": round(pct, 3), "vol": round(vol),
                "mult": round(mult, 2),
                "oi_pct": None if oi is None else round(oi[0], 3),
                "oi_usdt": None if oi is None else round(oi[1]),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def process(snapshot, now):
    starts = []
    for sym in WATCH:
        d = snapshot.get(sym)
        if not d:
            continue
        h = HISTORY.setdefault(sym, deque())
        h.append((now, d[0], d[1], d[3]))
        while h and h[0][0] < now - HISTORY_SEC:
            h.popleft()

        s = SLOW.setdefault(sym, deque())
        if not s or now - s[-1][0] >= COARSE_STEP:
            s.append((now, d[1]))
            while s and s[0][0] < now - SLOW_SEC:
                s.popleft()

        if START_ENABLED:
            ls = LAST_ALERT.get("S:" + sym)
            if not (ls and now - ls < START_COOLDOWN_MIN * 60):
                res = start_eval(h, s, now, d[1])
                if res:
                    starts.append((sym, d[1]) + res)
    return starts


def main():
    LAST_ALERT.update(load_state())
    save_state()

    status, snap = fetch_snapshot()
    if not snap:
        print("Borsa verisi alınamadı")
        return
    refresh_symbols(snap)
    last_refresh = time.time()

    end = time.time() + LOOP_MINUTES * 60
    fails = 0
    print(f"Ciddi para akışı odaklı canlı tarama başladı: {POLL_SEC} sn aralık", flush=True)
    while time.time() < end:
        t0 = time.time()
        status, snap = fetch_snapshot()
        if snap is None:
            fails += 1
            wait = 60 if status in (418, 429) else min(5 * fails, 30)
            print(f"veri alınamadı ({status}), {wait} sn bekleniyor", flush=True)
            time.sleep(wait)
            continue
        fails = 0

        now = time.time()
        starts = process(snap, now)

        for sig in sorted(starts, key=lambda x: -x[6]):
            try:
                alert_start(*sig)
            except Exception as e:
                print("başlangıç sinyal hatası", e)

        if time.time() - last_refresh > 1800:
            refresh_symbols(snap)
            last_refresh = time.time()

        time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))
    print("süre doldu, çıkılıyor")


if __name__ == "__main__":
    main()
    
