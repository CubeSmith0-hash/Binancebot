import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "15m"
CANDLES = 8
TZ = timezone(timedelta(hours=3))

COINS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LINK",
    "DOT", "LTC", "BCH", "ATOM", "NEAR", "APT", "ARB", "OP", "SUI", "INJ",
    "FIL", "ETC", "XLM", "HBAR", "ICP", "AAVE", "UNI", "RUNE", "ALGO", "SEI",
    "TIA", "PEPE", "SHIB", "WLD", "FET", "RNDR", "IMX", "GALA", "SAND", "ROSE",
]

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def get_klines(symbol):
    for host in HOSTS:
        try:
            r = requests.get(
                f"{host}/api/v3/klines",
                params={"symbol": symbol, "interval": INTERVAL, "limit": CANDLES + 4},
                timeout=15,
            )
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
    return None


def color(c):
    o, cl = float(c[1]), float(c[4])
    if cl > o:
        return "G"
    if cl < o:
        return "R"
    return "N"


def check(symbol):
    k = get_klines(symbol)
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
    return side, float(last[-1][4]), close_ms


def fmt(p):
    return f"{p:.6f}".rstrip("0").rstrip(".") if p < 1 else f"{p:.4f}".rstrip("0").rstrip(".")


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=15,
    )


def main():
    for coin in COINS:
        symbol = f"{coin}USDT"
        res = check(symbol)
        if not res:
            continue
        side, price, close_ms = res
        t = datetime.fromtimestamp(close_ms / 1000, TZ).strftime("%H:%M")
        late = int((time.time() * 1000 - close_ms) / 60000)
        note = f"\n(Bildirim {late} dk gecikmeli geldi)" if late >= 3 else ""
        icon = "🟢" if side == "LONG" else "🔴"
        send(
            f"{icon} {symbol} {side}\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}{note}"
        )
        time.sleep(0.3)


if __name__ == "__main__":
    main()
