import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "5m"
INTERVAL_MIN = 5     # mum süresi (dk), INTERVAL ile aynı olmalı
LOOKBACK = 20        # ortalama hacim için önceki mum sayısı
VOL_MULT = 5.0       # son mum hacmi ortalamanın en az kaç katı olmalı
PRICE_PCT = 3.0      # son mumda en az % kaç artış (açılış -> kapanış)
MIN_AVG_VOL = 200    # önceki mumların ortalama hacmi (USDT), bunun altı elenir
MIN_24H_VOL = 50_000  # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
TZ = timezone(timedelta(hours=3))
MAX_LATE_MIN = 3     # mum kapanışından en fazla kaç dk sonra çalışsın
AGE_GATE = True
STATE_FILE = "sent_signals.json"
WORKERS = 4          # Bitget mum limiti ~20 istek/sn
REQ_SLEEP = 0.2      # her istek sonrası bekleme (sn)

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
    """Mum yeni kapandıysa True, geç kalındıysa False döner."""
    period_ms = INTERVAL_MIN * 60000
    age_min = (time.time() * 1000 % period_ms) / 60000
    if AGE_GATE and age_min > MAX_LATE_MIN:
        print(f"mum yeni kapanmadı ({age_min:.1f} dk geçmiş), atlandı")
        return False
    return True


def get(path, params=None):
    """Bitget GET isteği, başarılıysa 'data' kısmını döndürür."""
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


def check(symbol):
    time.sleep(REQ_SLEEP)
    k = get("/api/v2/mix/market/candles",
            {"symbol": symbol, "productType": PRODUCT,
             "granularity": INTERVAL, "limit": LOOKBACK + 3})
    if not k:
        return None
    try:
        # [ts, open, high, low, close, baseVol, quoteVol]
        rows = sorted((int(c[0]), float(c[1]), float(c[4]), float(c[6])) for c in k)
    except (IndexError, ValueError, TypeError):
        return None
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if r[0] + INTERVAL_MIN * 60000 <= now_ms]
    if len(closed) < LOOKBACK + 1:
        return None
    last = closed[-1]
    prev = closed[-LOOKBACK - 1:-1]
    ts, o, cl, qv = last
    if o <= 0 or cl <= o:
        return None
    pct = (cl / o - 1) * 100
    if pct < PRICE_PCT:
        return None
    avg = sum(p[3] for p in prev) / len(prev)
    if avg < MIN_AVG_VOL:
        return None
    mult = qv / avg
    if mult < VOL_MULT:
        return None
    return symbol, cl, pct, mult, ts + INTERVAL_MIN * 60000


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

    # Hafıza dosyasını yükle (anahtar -> mum kapanış zamanı)
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

    for symbol, price, pct, mult, close_ms in results:
        signal_key = f"{symbol}_{close_ms}"
        if signal_key in sent_data:
            continue

        t = datetime.now(TZ).strftime("%H:%M")
        late = int((time.time() * 1000 - close_ms) / 60000)
        note = f"\n(Bildirim {late} dk gecikmeli geldi)" if late > 1 else ""

        send(
            f"🚀 PUMP {symbol} (Bitget Vadeli)\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"{INTERVAL_MIN} dk: +{pct:.1f}%\n"
            f"Hacim: {mult:.1f}x ortalama\n"
            f"24s hacim: {fmt_vol(vol24.get(symbol, 0))}"
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
    main()

  
