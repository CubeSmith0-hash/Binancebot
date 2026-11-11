import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "5m"
INTERVAL_MIN = 5
LOOKBACK = 20
VOL_MULT = 5.0
PRICE_PCT = 3.0
MIN_AVG_VOL = 200
MIN_24H_VOL = 50_000
TZ = timezone(timedelta(hours=3))
MAX_LATE_MIN = 3
AGE_GATE = True
STATE_FILE = "sent_signals.json"
WORKERS = 4
REQ_SLEEP = 0.2

# --- FİLTRELER ---
REQUIRE_FVG = True
CLOSE_POS_MIN = 0.70
MIN_FVG_PCT = 0.5

# --- SERT DÜŞÜŞTEN DÖNÜŞ (sadece long) ---
DROP_LOOKBACK = 24
MIN_DROP_PCT = 8.0
RECENT_LOW_BARS = 6
NEAR_LOW_PCT = 2.0

# --- SÜREKLİ ÇALIŞMA ---
RUN_MINUTES = 345     # workflow timeout'tan (355 dk) kısa olmalı

HOST = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
    "DELL", "IBM", "NOW", "CRM", "IREN", "BX", "HPE", "CRWD",
}
NON_CRYPTO = {"PAXG", "XAUT"}


def mum_kontrol():
    period_ms = INTERVAL_MIN * 60000
    age_min = (time.time() * 1000 % period_ms) / 60000
    if AGE_GATE and age_min > MAX_LATE_MIN:
        print(f"mum yeni kapanmadı ({age_min:.1f} dk geçmiş), atlandı")
        return False
    return True


def get(path, params=None):
    for attempt in range(3):
        try:
            r = requests.get(HOST + path, params=params, timeout=20)
        except requests.RequestException as e:
            print("hata", path, e)
            time.sleep(1)
            continue
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        if r.status_code != 200:
            print("HTTP", r.status_code, path)
            if r.status_code in (403, 451):
                print("bu sunucunun IP'si Bitget tarafından engelli")
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        if body.get("code") != "00000":
            print("API hatası:", path, body.get("code"), body.get("msg"))
            return None
        return body.get("data")
    return None


def keep(c):
    sym = c.get("symbol", "")
    if not sym.endswith("USDT"):
        return False
    if c.get("symbolStatus", "normal") != "normal":
        return False
    if c.get("symbolType", "perpetual") != "perpetual":
        return False
    base = c.get("baseCoin") or sym[:-4]
    if base in STABLES or base in NON_CRYPTO or base in STOCKS:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    return True


def find_bullish_fvg(a, c):
    """a = pump'tan 2 önceki mum, c = pump mumu.
    Bullish FVG: c.low > a.high -> bölge (a.high, c.low)."""
    gap_low, gap_high = a["h"], c["l"]
    if gap_high > gap_low:
        return gap_low, gap_high
    return None


def check(symbol):
    time.sleep(REQ_SLEEP)
    need = max(LOOKBACK, DROP_LOOKBACK)
    k = get("/api/v2/mix/market/candles",
            {"symbol": symbol, "productType": PRODUCT,
             "granularity": INTERVAL, "limit": need + 3})
    if not k:
        return None
    try:
        # [ts, open, high, low, close, baseVol, quoteVol]
        rows = sorted(
            ({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
              "l": float(c[3]), "c": float(c[4]), "qv": float(c[6])}
             for c in k),
            key=lambda x: x["ts"])
    except (IndexError, ValueError, TypeError):
        return None
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if r["ts"] + INTERVAL_MIN * 60000 <= now_ms]
    if len(closed) < need + 1:
        return None

    last = closed[-1]
    prev = closed[-LOOKBACK - 1:-1]
    o, cl, h, l, qv = last["o"], last["c"], last["h"], last["l"], last["qv"]
    if o <= 0 or cl <= o:
        return None
    pct = (cl / o - 1) * 100
    if pct < PRICE_PCT:
        return None
    avg = sum(p["qv"] for p in prev) / len(prev)
    if avg < MIN_AVG_VOL:
        return None
    mult = qv / avg
    if mult < VOL_MULT:
        return None

    # Üstte kapanış
    rng = h - l
    if rng <= 0 or (cl - l) / rng < CLOSE_POS_MIN:
        return None

    # Sert düşüş -> dip -> dönüş
    win = closed[-DROP_LOOKBACK - 1:-1]
    hi_i = max(range(len(win)), key=lambda i: win[i]["h"])
    lo_i = min(range(len(win)), key=lambda i: win[i]["l"])
    hi_v, lo_v = win[hi_i]["h"], win[lo_i]["l"]
    if hi_v <= 0 or lo_v <= 0:
        return None
    drop_pct = (hi_v - lo_v) / hi_v * 100
    if drop_pct < MIN_DROP_PCT:
        return None
    if hi_i >= lo_i:
        return None
    if lo_i < len(win) - RECENT_LOW_BARS:
        return None
    if (o - lo_v) / lo_v * 100 > NEAR_LOW_PCT:
        return None

    # FVG
    fvg = find_bullish_fvg(closed[-3], last)
    if REQUIRE_FVG:
        if not fvg:
            return None
        if (fvg[1] - fvg[0]) / cl * 100 < MIN_FVG_PCT:
            return None

    return (symbol, cl, pct, mult, last["ts"] + INTERVAL_MIN * 60000,
            fvg, drop_pct, lo_v)


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


def main():
    if not mum_kontrol():
        return

    contracts = get("/api/v2/mix/market/contracts", {"productType": PRODUCT})
    if not contracts:
        print("Borsa verisi alınamadı")
        return

    tick = get("/api/v2/mix/market/tickers", {"productType": PRODUCT}) or []
    vol24 = {}
    for t in tick:
        try:
            vol24[t["symbol"]] = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except (KeyError, ValueError, TypeError):
            continue

    symbols = [c["symbol"] for c in contracts if keep(c)]
    if vol24:
        symbols = [s for s in symbols if vol24.get(s, 0) >= MIN_24H_VOL]
    print(f"Bitget vadeli: {len(contracts)} parite var, {len(symbols)} coin taranıyor")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = [r for r in ex.map(check, symbols) if r]
    results.sort(key=lambda x: -x[2])
    print(f"{len(results)} pump sinyali")

    sent_data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                raw = json.load(f)
            limit = time.time() * 1000 - 6 * 3600 * 1000
            sent_data = {
                k: v for k, v in raw.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > limit
            }
        except (OSError, ValueError, AttributeError):
            sent_data = {}

    for symbol, price, pct, mult, close_ms, fvg, drop_pct, low_v in results:
        signal_key = f"{symbol}_{close_ms}"
        if signal_key in sent_data:
            continue

        t = datetime.now(TZ).strftime("%H:%M")
        late = int((time.time() * 1000 - close_ms) / 60000)
        note = f"\n(Bildirim {late} dk gecikmeli geldi)" if late > 1 else ""

        if fvg:
            lo, hi = fvg
            fvg_txt = (
                f"\n🟩 Bullish FVG ({INTERVAL}): {fmt(lo)} - {fmt(hi)}"
                f"\nGeri çekilme bölgesi: FVG'nin içi"
                f"\nİptal seviyesi: {fmt(lo)} altı kapanış"
            )
        else:
            fvg_txt = ""

        send(
            f"🚀 LONG DİP DÖNÜŞ {symbol} (Bitget Vadeli)\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"Önceki düşüş: -{drop_pct:.1f}% (dip: {fmt(low_v)})\n"
            f"{INTERVAL_MIN} dk: +{pct:.1f}%\n"
            f"Hacim: {mult:.1f}x ortalama\n"
            f"24s hacim: {fmt_vol(vol24.get(symbol, 0))}"
            f"{fvg_txt}"
            f"{note}"
        )

        sent_data[signal_key] = close_ms
        time.sleep(0.5)

    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sent_data, f)
    except OSError:
        pass


if __name__ == "__main__":
    end = time.time() + RUN_MINUTES * 60
    period = INTERVAL_MIN * 60
    print("bot başladı")
    while True:
        wait = period - (time.time() % period) + 5  # mum kapanışından 5 sn sonra
        if time.time() + wait > end:
            break
        time.sleep(wait)
        try:
            main()
        except Exception as e:
            print("hata:", e)
    print("süre doldu, workflow yeniden başlayacak")
