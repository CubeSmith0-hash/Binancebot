import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "1h"
LONG_AFTER_RED = 11      # üst üste bu kadar kırmızı mum kapanınca LONG
SHORT_AFTER_GREEN = 12   # üst üste bu kadar yeşil mum kapanınca SHORT
MIN_VOLUME = 1_000_000   # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
AGE_GATE = True          # sadece saat başında tara (test için False yap)
MAX_AGE_MIN = 4         # saat başından en fazla kaç dk sonra çalışırsa tarasın
TZ = timezone(timedelta(hours=3))

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

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


def get(path, params=None):
    for host in HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=20)
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
    base = s.get("baseAsset", "")
    if base in STABLES or base in NON_CRYPTO:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    return True


def color(c):
    o, cl = float(c[1]), float(c[4])
    if cl > o:
        return "G"
    if cl < o:
        return "R"
    return "N"


def check(symbol):
    k = get("/api/v3/klines",
            {"symbol": symbol, "interval": INTERVAL, "limit": SHORT_AFTER_GREEN + 5})
    if not k:
        return None
    now_ms = int(time.time() * 1000)
    closed = [c for c in k if int(c[6]) < now_ms]
    cols = [color(c) for c in closed]
    if not cols:
        return None
    price = float(closed[-1][4])
    close_ms = int(closed[-1][6]) + 1

    n = LONG_AFTER_RED
    if len(cols) >= n + 1 and all(x == "R" for x in cols[-n:]) and cols[-n - 1] != "R":
        return symbol, "LONG", price, n, "kırmızı", close_ms
    n = SHORT_AFTER_GREEN
    if len(cols) >= n + 1 and all(x == "G" for x in cols[-n:]) and cols[-n - 1] != "G":
        return symbol, "SHORT", price, n, "yeşil", close_ms
    return None


def fmt(p):
    if p < 1:
        return f"{p:.8f}".rstrip("0").rstrip(".")
    return f"{p:.4f}".rstrip("0").rstrip(".")


def fmt_vol(v):
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v / 1e3:.0f}K"


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
    now_ms = int(time.time() * 1000)
    age_min = (now_ms % 3600000) / 60000
    if AGE_GATE and age_min > MAX_AGE_MIN:
        print(f"saatlik mum yeni kapanmadı (saat başından {age_min:.0f} dk geçmiş), atlandı")
        return

    info = get("/api/v3/exchangeInfo")
    tick = get("/api/v3/ticker/24hr")
    if not info or not tick:
        print("Borsa verisi alınamadı")
        return
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in tick}
    symbols = [
        s["symbol"] for s in info["symbols"]
        if keep(s) and vols.get(s["symbol"], 0) >= MIN_VOLUME
    ]
    print(f"{len(symbols)} coin taranıyor (24s hacim >= {fmt_vol(MIN_VOLUME)})")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = [r for r in ex.map(check, symbols) if r]
    print(f"{len(results)} sinyal")

    for symbol, side, price, n, renk, close_ms in results:
        t = datetime.fromtimestamp(close_ms / 1000, TZ).strftime("%H:%M")
        icon = "🟢" if side == "LONG" else "🔴"
        send(
            f"⏰ SAATLİK İŞLEM (1s grafik)\n"
            f"{icon} {symbol} {side}\n"
            f"{n} ardışık {renk} mum kapandı\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"24s hacim: {fmt_vol(vols.get(symbol, 0))} USDT"
        )
        time.sleep(0.5)


if __name__ == "__main__":
    main()
