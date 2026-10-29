import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "15m"
CANDLES = 8
TZ = timezone(timedelta(hours=3))
MAX_LATE_MIN = 4
AGE_GATE = True
# 24 saatlik USDT hacmi bu değerin altındaki coinler taranmaz.
# Hepsini taramak için 0 yap.
MIN_VOLUME = 5_000_000

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
# Kripto olmayanlar (hisse tokenları, altın) taranmaz
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
}
NON_CRYPTO = {"PAXG", "XAUT"}


def is_excluded(base):
    if base in NON_CRYPTO:
        return True
    return base.endswith("B") and base[:-1] in STOCKS

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def api_get(path, params=None):
    for host in HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
    return None



def get_symbols():
    info = api_get("/api/v3/exchangeInfo")
    if not info:
        return []
    
    syms = []
    for s in info["symbols"]:
        if s["quoteAsset"] == "USDT" \
           and s["status"] == "TRADING" \
           and s["baseAsset"] not in STABLES \
           and not is_excluded(s["baseAsset"]):
            syms.append(s["symbol"])
            
    if MIN_VOLUME > 0:
        tick = api_get("/api/v3/ticker/24hr") or []
        vols = {t["symbol"]: float(t["quoteVolume"]) for t in tick}
        syms = [s for s in syms if vols.get(s, 0) > MIN_VOLUME]
        
    return syms
    




def color(c):
    o, cl = float(c[1]), float(c[4])
    if cl > o:
        return "G"
    if cl < o:
        return "R"
    return "N"


def check(symbol):
    try:
        k = api_get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": INTERVAL, "limit": CANDLES + 4},
        )
        if not k:
            return None
        now_ms = int(time.time() * 1000)
        closed = [c for c in k if int(c[6]) < now_ms]
        if len(closed) < CANDLES + 1:
            return None
        last = closed[-CANDLES:]
        prev = closed[-CANDLES - 1]
        cols = [color(c) for c in last]
        if all(x == "G" for x in cols):
            side = "LONG"
            if color(prev) == "G":
                return None
        elif all(x == "R" for x in cols):
            side = "SHORT"
            if color(prev) == "R":
                return None
        else:
            return None
        close_ms = int(last[-1][6]) + 1
        return symbol, side, float(last[-1][4]), close_ms
    except Exception:
        return None


def fmt(p):
    if p < 1:
        return f"{p:.8f}".rstrip("0").rstrip(".")
    return f"{p:.4f}".rstrip("0").rstrip(".")


def send(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
                timeout=15,
            )
            print("telegram:", r.status_code, r.text[:200])
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return
        except requests.RequestException:
            time.sleep(2)


def main():
        age_min = (time.time() * 1000 % 900000) / 60000
        if AGE_GATE and age_min > MAX_LATE_MIN:
            print(f"mum yeni kapanmadı ({age_min:.0f} dk geçmiş), atlandı")
            return
    symbols = get_symbols()
    print(f"{len(symbols)} coin taranıyor")
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = [r for r in ex.map(check, symbols) if r]

    for symbol, side, price, close_ms in results:
        t = datetime.fromtimestamp(close_ms / 1000, TZ).strftime("%H:%M")
        late = int((time.time() * 1000 - close_ms) / 60000)
        note = f"\n(Bildirim {late} dk gecikmeli geldi)" if late >= 3 else ""
        icon = "🟢" if side == "LONG" else "🔴"
        send(f"{icon} {symbol} {side}\nSaat: {t}\nFiyat: {fmt(price)}{note}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
