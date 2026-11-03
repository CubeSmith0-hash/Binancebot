import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "15m"
LOOKBACK = 20        # ortalama hacim için önceki mum sayısı
VOL_MULT = 5.0       # son mum hacmi ortalamanın en az kaç katı olmalı
DROP_PCT = 5.0       # son mumda en az % kaç düşüş (açılış -> kapanış)
MIN_AVG_VOL = 200    # önceki mumların ortalama hacmi (USDT), bunun altı elenir
TZ = timezone(timedelta(hours=3))
MAX_LATE_MIN = 4
CLOSE_DELAY = 1.5     # mum kapanışından kaç sn sonra taransın
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))  # 0 = tek sefer
AGE_GATE = False
STATE_FILE = "sent_signals.json"

FAPI = "https://fapi.binance.com"
SPOT = "https://data-api.binance.vision"

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
}
NON_CRYPTO = {"PAXG", "XAUT"}
BAD_TYPES = {"EQUITY", "COMMODITY", "INDEX"}

BASE = FAPI
PREFIX = "/fapi/v1"


def mum_kontrol():
    """Mum yeni kapandıysa True, geç kalındıysa False döner."""
    age_min = (time.time() * 1000 % 900000) / 60000
    if AGE_GATE and age_min > MAX_LATE_MIN:
        print(f"mum yeni kapanmadı ({age_min:.0f} dk geçmiş), atlandı")
        return False
    return True


def get(base, path, params=None):
    try:
        r = requests.get(base + path, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        print("HTTP", r.status_code, path)
    except requests.RequestException as e:
        print("hata", path, e)
    return None


def keep(s):
    if s.get("quoteAsset") != "USDT":
        return False
    if s.get("status") != "TRADING":
        return False
    if s.get("contractType", "PERPETUAL") != "PERPETUAL":
        return False
    base = s.get("baseAsset", "")
    if base in STABLES or base in NON_CRYPTO:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    if s.get("underlyingType") in BAD_TYPES:
        return False
    if "TradFi" in (s.get("underlyingSubType") or []):
        return False
    return True


def check(symbol):
    k = get(BASE, f"{PREFIX}/klines",
            {"symbol": symbol, "interval": INTERVAL, "limit": LOOKBACK + 3})
    if not k:
        return None
    now_ms = int(time.time() * 1000)
    closed = [c for c in k if int(c[6]) < now_ms]
    if len(closed) < LOOKBACK + 1:
        return None
    last = closed[-1]
    prev = closed[-LOOKBACK - 1:-1]
    o, cl = float(last[1]), float(last[4])
    if o <= 0 or cl >= o:
        return None
    pct = (1 - cl / o) * 100
    if pct < DROP_PCT:
        return None
    avg = sum(float(c[7]) for c in prev) / len(prev)
    if avg < MIN_AVG_VOL:
        return None
    mult = float(last[7]) / avg
    if mult < VOL_MULT:
        return None
    return symbol, cl, pct, mult, int(last[6]) + 1


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
    """Gönderilmiş sinyalleri yükle, 2 günden eski kayıtları temizle."""
    data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    limit = time.time() - 2 * 86400
    return {k: v for k, v in data.items()
            if not isinstance(v, (int, float)) or v > limit or v is True}


def save_state(data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except OSError as e:
        print("state yazılamadı", e)


def get_symbols():
    global BASE, PREFIX
    mode = "futures"
    BASE, PREFIX = FAPI, "/fapi/v1"
    info = get(FAPI, "/fapi/v1/exchangeInfo")
    if not info:
        print("fapi erişilemedi, spot'a geçiliyor")
        BASE, PREFIX, mode = SPOT, "/api/v3", "spot"
        info = get(SPOT, "/api/v3/exchangeInfo")
    if not info:
        print("Borsa verisi alınamadı")
        return []
    all_syms = info.get("symbols", [])
    symbols = [s["symbol"] for s in all_syms if keep(s)]
    print(f"[{mode}] borsada {len(all_syms)} parite var, {len(symbols)} coin taranıyor")
    return symbols


def scan(symbols, sent_data):
    with ThreadPoolExecutor(max_workers=30) as ex:
        results = [r for r in ex.map(check, symbols) if r]
    print(f"{len(results)} dump sinyali")
    if not results:
        return

    tick = get(BASE, f"{PREFIX}/ticker/24hr") or []
    vol24 = {t["symbol"]: float(t["quoteVolume"]) for t in tick}

    for symbol, price, pct, mult, close_ms in results:
        signal_key = f"DUMP_{symbol}_{close_ms}"
        if signal_key in sent_data:
            continue

        # Önce kaydet, sonra gönder: hata olsa bile tekrar göndermez
        sent_data[signal_key] = int(time.time())
        save_state(sent_data)

        t = datetime.now(TZ).strftime("%H:%M")
        text = (
            f"🔻 DUMP {symbol}\nSaat: {t}\nFiyat: {fmt(price)}\n15 dk:"
            f" -{pct:.1f}%\nHacim: {mult:.1f}x ortalama\n24s hacim:"
            f" {fmt_vol(vol24.get(symbol, 0))}"
        )
        send(text)


def main():
    sent_data = load_state()
    symbols = get_symbols()
    if not symbols:
        return

    # Tek seferlik mod (eski davranış)
    if LOOP_MINUTES <= 0:
        if mum_kontrol():
            scan(symbols, sent_data)
        return

    # Sürekli mod: her 15 dk mum kapanışında anında tara
    period = 900
    end_time = time.time() + LOOP_MINUTES * 60
    last_refresh = time.time()
    while True:
        now = time.time()
        next_close = (now // period + 1) * period
        if next_close + CLOSE_DELAY > end_time:
            print("süre doldu, çıkılıyor")
            break
        time.sleep(max(0, next_close + CLOSE_DELAY - now))

        if time.time() - last_refresh > 3600:
            fresh = get_symbols()
            if fresh:
                symbols = fresh
            last_refresh = time.time()

        try:
            scan(symbols, sent_data)
        except Exception as e:
            print("tarama hatası:", e)


if __name__ == "__main__":
    main()
