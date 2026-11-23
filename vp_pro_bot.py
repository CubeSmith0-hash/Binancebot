import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
GRAN = "15m"

VP_LOOKBACK = 96          # ana profil: 96 x 15dk = 24 saat
HTF_LOOKBACK = 672        # üst profil: 672 x 15dk = 7 gün (onay için)
BINS = 48
VALUE_AREA = 0.70         # VAH/VAL için hacim oranı

VOL_MULT = 1.3            # kırılım mumlarının hacmi ortalamanın en az kaç katı
MIN_VA_PCT = 0.4          # değer alanı genişliği fiyatın en az %kaçı (çok dar profili ele)
MIN_RR = 1.0              # TP1 için minimum risk/ödül
MIN_QUOTE_VOL = 500_000   # profil dönemi toplam USDT hacmi alt sınırı
COOLDOWN_SEC = 7200       # aynı coin+sinyal tipi için bekleme
REQUIRE_HTF = False       # True: sadece 7 günlük profil onaylı sinyalleri gönder
WORKERS = 8

sent = {}


def get_symbols():
    r = requests.get(f"{BASE}/api/v2/mix/market/contracts",
                     params={"productType": PRODUCT}, timeout=15)
    r.raise_for_status()
    return [c["symbol"] for c in r.json()["data"]
            if c.get("symbolStatus", "normal") == "normal"]


def candles(symbol, limit):
    r = requests.get(f"{BASE}/api/v2/mix/market/candles",
                     params={"symbol": symbol, "productType": PRODUCT,
                             "granularity": GRAN, "limit": limit}, timeout=15)
    data = r.json().get("data") or []
    data.sort(key=lambda x: int(x[0]))
    return data[:-1]  # oluşmakta olan mumu at


def volume_profile(cs):
    """Mum hacmini high-low aralığındaki dilimlere eşit dağıtır -> (POC, VAH, VAL)."""
    lo = min(float(c[3]) for c in cs)
    hi = max(float(c[2]) for c in cs)
    if hi <= lo:
        return None
    step = (hi - lo) / BINS
    vols = [0.0] * BINS
    for c in cs:
        h, l, v = float(c[2]), float(c[3]), float(c[5])
        i0 = min(int((l - lo) / step), BINS - 1)
        i1 = min(int((h - lo) / step), BINS - 1)
        share = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vols[i] += share
    poc_i = max(range(BINS), key=lambda i: vols[i])
    total = sum(vols)
    lo_i = hi_i = poc_i
    acc = vols[poc_i]
    while acc < total * VALUE_AREA and (lo_i > 0 or hi_i < BINS - 1):
        up = vols[hi_i + 1] if hi_i < BINS - 1 else -1
        dn = vols[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += dn
    return lo + (poc_i + 0.5) * step, lo + (hi_i + 1) * step, lo + lo_i * step


def check(symbol):
    try:
        closed = candles(symbol, VP_LOOKBACK + 4)
        if len(closed) < VP_LOOKBACK + 3:
            return None

        # profil, değerlendirilen son 3 mumu içermez
        hist = closed[-(VP_LOOKBACK + 3):-3]
        c1, c2, c3 = closed[-3], closed[-2], closed[-1]

        if sum(float(c[6]) for c in hist) < MIN_QUOTE_VOL:
            return None
        vp = volume_profile(hist)
        if not vp:
            return None
        poc, vah, val = vp
        width = vah - val
        a, b, c = float(c1[4]), float(c2[4]), float(c3[4])
        price = c
        if width / price * 100 < MIN_VA_PCT:
            return None

        avg_vol = sum(float(x[5]) for x in hist) / len(hist)
        inside = lambda x: val <= x <= vah

        kind = side = None
        tp1 = tp2 = sl = None

        # 1) VALUE AREA RE-ENTRY: dışarıda kapanış, sonra 2 kapanış içeride
        if inside(b) and inside(c):
            if a < val and price < poc:
                kind, side = "VA RE-ENTRY", "LONG"
                sl = min(float(x[3]) for x in (c1, c2, c3))
                tp1, tp2 = poc, vah
            elif a > vah and price > poc:
                kind, side = "VA RE-ENTRY", "SHORT"
                sl = max(float(x[2]) for x in (c1, c2, c3))
                tp1, tp2 = poc, val

        # 2) KABUL GÖREN KIRILIM: 2 kapanış değer alanı dışında + hacim
        if not kind:
            vol_ok = (float(c2[5]) + float(c3[5])) / 2 >= avg_vol * VOL_MULT
            if vol_ok:
                if b > vah and c > vah and a <= vah and c <= vah + 0.5 * width:
                    kind, side = "VAH KIRILIMI", "LONG"
                    sl = min(float(x[3]) for x in (c2, c3))
                    tp1, tp2 = vah + 0.5 * width, vah + width
                elif b < val and c < val and a >= val and c >= val - 0.5 * width:
                    kind, side = "VAL KIRILIMI", "SHORT"
                    sl = max(float(x[2]) for x in (c2, c3))
                    tp1, tp2 = val - 0.5 * width, val - width

        if not kind:
            return None

        # risk / ödül
        risk = abs(price - sl)
        if risk <= 0 or abs(tp1 - price) / risk < MIN_RR:
            return None
        rr = abs(tp1 - price) / risk

        # cooldown
        key = (symbol, kind, side)
        if time.time() - sent.get(key, 0) < COOLDOWN_SEC:
            return None

        # 7 günlük profil onayı (sadece aday sinyaller için çekilir)
        htf_ok = None
        h = candles(symbol, HTF_LOOKBACK + 4)
        if len(h) >= 300:
            vp7 = volume_profile(h[:-3][-HTF_LOOKBACK:])
            if vp7:
                poc7, vah7, val7 = vp7
                if kind == "VA RE-ENTRY":
                    htf_ok = val7 <= price <= vah7       # üst zaman diliminde de denge bölgesi
                elif side == "LONG":
                    htf_ok = price > poc7
                else:
                    htf_ok = price < poc7
        if REQUIRE_HTF and not htf_ok:
            return None

        sent[key] = time.time()
        return dict(symbol=symbol, kind=kind, side=side, price=price, tp1=tp1,
                    tp2=tp2, sl=sl, rr=rr, poc=poc, vah=vah, val=val, htf=htf_ok)
    except Exception as e:
        print(symbol, "hata:", e)
    return None


def telegram(text):
    if not TELEGRAM_TOKEN:
        print(text)
        return
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)


def main():
    symbols = get_symbols()
    print(f"{len(symbols)} parite taranıyor")
    max_min = float(os.environ.get("MAX_RUNTIME_MIN", "0"))
    end_time = time.time() + max_min * 60 if max_min else None
    while end_time is None or time.time() < end_time:
        start = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for s in ex.map(check, symbols):
                if s:
                    icon = "🟢" if s["side"] == "LONG" else "🔴"
                    htf = {True: "✅ onaylı", False: "❌ onaysız", None: "veri yok"}[s["htf"]]
                    telegram(
                        f"{icon} VOLUME PROFILE {s['side']} | {s['kind']}\n"
                        f"{s['symbol']} (Bitget USDT-M, {GRAN})\n"
                        f"Fiyat: {s['price']}\n"
                        f"TP1: {s['tp1']:.6g} (R/R {s['rr']:.1f})\n"
                        f"TP2: {s['tp2']:.6g}\n"
                        f"SL: {s['sl']:.6g}\n"
                        f"VAH {s['vah']:.6g} | POC {s['poc']:.6g} | VAL {s['val']:.6g}\n"
                        f"7 günlük profil: {htf}")
        time.sleep(max(1, 60 - (time.time() - start) % 60 + 2))


if __name__ == "__main__":
    main()
          
