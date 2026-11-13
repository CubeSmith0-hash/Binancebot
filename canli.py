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

# ================= ANINDA BAŞLANGIÇ SİNYALİ (YENİ) =================
# Pump daha başlar başlamaz, kısa pencerede hacim patlaması + fiyat ivmesi.
START_ENABLED = True
# (pencere_sn, min_yükseliş_%, min_hacim_katı, min_pencere_hacmi_USDT)
START_TIERS = [
    (15, 1.2, 8.0, 10_000),    # çok hızlı: 15 sn'de %1.2 + 8x hacim
    (30, 2.0, 6.0, 20_000),    # hızlı:     30 sn'de %2   + 6x hacim
    (60, 3.0, 4.0, 30_000),    # normal:    60 sn'de %3   + 4x hacim
]
START_MAX_PCT = 6.0        # pencerede bundan fazla yükselmişse geç kalınmış, atla
START_PULLBACK_PCT = 0.4   # fiyat pencere tepesinden en fazla % bu kadar düşmüş olabilir
START_UP_TICKS = 2         # son N saniyede fiyat düşmemiş olmalı (ivme devam ediyor)
START_BASE_MIN_SEC = 600   # normal hacim hesabı için en az bu kadar geçmiş (10 dk)
START_BASE_GAP_SEC = 90    # baz hesabında son 90 sn dışarıda tutulur
START_BASE_FLOOR = 0.3     # baz hacim, 24s ortalamanın en az %30'u sayılır
START_COOLDOWN_MIN = 10    # aynı coin için tekrar başlangıç sinyali (dk)
START_LOG_FILE = "start_log.jsonl"
# açık pozisyon (OI) = gerçek para girişi göstergesi
START_OI_WINDOW_SEC = 45   # OI değişimine bakılan süre
START_OI_PCT = 0.5         # bu kadar % artmışsa "güçlü" sinyal (💰)
START_REQUIRE_OI = True    # fiyat + hacim + OI aynı anda artmalı (False: OI'siz de sinyal gelir)

# ---- Genel ----
MIN_24H_VOL = 500_000      # 24s hacmi bunun altındaki coinler taranmaz

HISTORY_SEC = max([t[0] for t in START_TIERS] + [START_OI_WINDOW_SEC]) + 30
COARSE_STEP = 10           # hacim tabanı örnekleme aralığı (sn)
SLOW_SEC = 1800            # normal hacim tabanı için 30 dk geçmiş
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
SLOW = {}         # sembol -> deque[(zaman, 24s_hacim)] (10 sn aralıklı, 30 dk)
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
        return False   # USDBRL, USDJPY gibi döviz pariteleri
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
        if r.status_code in (403, 451):
            print("bu sunucunun IP'si Bitget tarafından engelli")
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
    print(f"{len(WATCH)} Bitget vadeli coin izleniyor")


def window(hist, now, sec):
    """Son 'sec' saniyedeki noktalar (eskiden yeniye)."""
    out = []
    for p in reversed(hist):
        if p[0] < now - sec:
            break
        out.append(p)
    out.reverse()
    return out


# ------------------------------------------------- ANINDA BAŞLANGIÇ (YENİ)

def base_rate(slow, now, vol24):
    """Normal hacim akışı (USDT/sn): son 30 dk, son 90 sn hariç."""
    day_rate = vol24 / 86400.0
    floor = day_rate * START_BASE_FLOOR
    if len(slow) < 2 or now - slow[0][0] < START_BASE_MIN_SEC:
        return max(day_rate, 0.0)   # yeterli geçmiş yok, 24s ortalaması
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
    """Son START_OI_WINDOW_SEC içinde açık pozisyon % değişimi (yoksa None)."""
    pts = window(hist, now, START_OI_WINDOW_SEC)
    if len(pts) < 3 or now - pts[0][0] < START_OI_WINDOW_SEC * 0.7:
        return None
    o0, o1 = pts[0][3], pts[-1][3]
    if not o0 or not o1 or o0 <= 0:
        return None
    return (o1 / o0 - 1) * 100


def start_eval(hist, slow, now, vol24):
    last_t, last_p, last_q = hist[-1][0], hist[-1][1], hist[-1][2]
    rate = base_rate(slow, now, vol24)
    if rate <= 0:
        return None

    # ivme: son START_UP_TICKS saniyede fiyat düşmemiş olmalı
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
            continue   # tepeden dönmüş
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
    if START_REQUIRE_OI and (oi is None or oi < START_OI_PCT):
        return None
    return best + (oi,)


def alert_start(sym, vol24, sec, price, pct, vol, mult, oi):
    LAST_ALERT["S:" + sym] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    if oi is None:
        oi_line = "Açık pozisyon: veri yok"
        head = "⚡ ANLIK PUMP"
    elif oi >= START_OI_PCT:
        oi_line = f"💰 Açık pozisyon {START_OI_WINDOW_SEC} sn: {oi:+.2f}% (para girişi VAR)"
        head = "⚡ ANLIK PUMP"
    else:
        oi_line = f"Açık pozisyon {START_OI_WINDOW_SEC} sn: {oi:+.2f}% (para girişi yok, zayıf)"
        head = "⚡ ANLIK PUMP"
    send(
        f"{head} {sym} (Bitget Vadeli)\n"
        f"Saat: {t}\n"
        f"Fiyat: {fmt(price)}\n"
        f"Son {sec} sn: +{pct:.2f}%\n"
        f"Hacim: {fmt_vol(vol)} USDT ({mult:.1f}x normal)\n"
        f"{oi_line}\n"
        f"24s hacim: {fmt_vol(vol24)}"
    )
    try:
        with open(START_LOG_FILE, "a") as f:
            f.write(json.dumps({
                "t": int(time.time()), "sym": sym, "price": price,
                "sec": sec, "pct": round(pct, 3), "vol": round(vol),
                "mult": round(mult, 2),
                "oi": None if oi is None else round(oi, 3),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def process(snapshot, now):
    """Geçmişi günceller, şartı sağlayan coinleri döner."""
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
    print(f"canlı tarama başladı: {POLL_SEC} sn aralık", flush=True)
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
    
