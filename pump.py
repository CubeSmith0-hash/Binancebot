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
PRICE_PCT = 3.0      # son mumda en az % kaç artış (açılış -> kapanış)
MIN_AVG_VOL = 200    # önceki mumların ortalama hacmi (USDT), bunun altı elenir
TZ = timezone(timedelta(hours=3))
MAX_LATE_MIN = 4
AGE_GATE = True

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
    age_min = (time.time() * 1000 % 900000) / 60000
    if AGE_GATE and age_min > MAX_LATE_MIN:
        print(f"mum yeni kapanmadı ({age_min:.0f} dk geçmiş), atlandı")
        return
        
        
    



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
    if o <= 0 or cl <= o:
        return None
    pct = (cl / o - 1) * 100
    if pct < PRICE_PCT:
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


def main():
    global BASE, PREFIX
    mode = "futures"
    info = get(FAPI, "/fapi/v1/exchangeInfo")
    if not info:
        print("fapi erişilemedi, spot'a geçiliyor")
        BASE, PREFIX, mode = SPOT, "/api/v3", "spot"
        info = get(SPOT, "/api/v3/exchangeInfo")
    if not info:
        print("Borsa verisi alınamadı")
        return

    all_syms = info.get("symbols", [])
    symbols = [s["symbol"] for s in all_syms if keep(s)]
    print(f"[{mode}] borsada {len(all_syms)} parite var, {len(symbols)} coin taranıyor")
    if not symbols and all_syms:
        print("örnek parite:", all_syms[0])

    tick = get(BASE, f"{PREFIX}/ticker/24hr") or []
    vol24 = {t["symbol"]: float(t["quoteVolume"]) for t in tick}

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = [r for r in ex.map(check, symbols) if r]
    print(f"{len(results)} pump sinyali")

    import json
    import os

    STATE_FILE = "sent_signals.json"
    
    # Hafıza dosyasını yükle
    sent_data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                sent_data = json.load(f)
        except:
            sent_data = {}

    for symbol, price, pct, mult, close_ms in sorted_results:
        # Benzersiz anahtar (Coin + Mum Zamanı)
        signal_key = f"{symbol}_{close_ms}"
        
        # Eğer bu mum için daha önce bildirim atıldıysa atla!
        if signal_key in sent_data:
            continue
            
        t = datetime.fromtimestamp(close_ms / 1000)
        late = int((time.time() * 1000 - close_ms) / 60000)
        note = f"\n(Bildirim {late} dk gecikmeli geldi)" if late > 2 else ""
        
        send(
            f"🚀 PUMP {symbol}\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"15 dk: +{pct:.1f}%\n"
            f"Hacim: {mult:.1f}x ortalama\n"
            f"24s hacim: {fmt_vol(vol24.get(symbol, 0))}"
            f"{note}"
        )
        
        # Gönderildi olarak kaydet
        sent_data[signal_key] = True
        time.sleep(0.5)

    # Güncel hafızayı dosyaya kaydet
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sent_data, f)
    except:
        pass
        


if __name__ == "__main__":
    main()
