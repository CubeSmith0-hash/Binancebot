import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
GREEN_COUNT = 8          # kaç ardışık yeşil mum
GRANULARITY = "1m"
WORKERS = 8              # eşzamanlı istek (Bitget limitine takılmamak için düşük tut)

sent = {}  # symbol -> son sinyal verilen mumun zamanı


def get_symbols():
    r = requests.get(f"{BASE}/api/v2/mix/market/contracts",
                     params={"productType": PRODUCT}, timeout=15)
    r.raise_for_status()
    return [c["symbol"] for c in r.json()["data"]
            if c.get("symbolStatus", "normal") == "normal"]


def check(symbol):
    try:
        r = requests.get(f"{BASE}/api/v2/mix/market/candles",
                         params={"symbol": symbol, "productType": PRODUCT,
                                 "granularity": GRANULARITY,
                                 "limit": GREEN_COUNT + 2}, timeout=15)
        data = r.json().get("data") or []
        # [ts, open, high, low, close, baseVol, quoteVol], eskiden yeniye
        data.sort(key=lambda x: int(x[0]))
        closed = data[:-1]  # son mum henüz oluşuyor, çıkar
        if len(closed) < GREEN_COUNT + 1:
            return None
        last8 = closed[-GREEN_COUNT:]
        prev = closed[-GREEN_COUNT - 1]
        all_green = all(float(c[4]) > float(c[1]) for c in last8)
        prev_green = float(prev[4]) > float(prev[1])
        # seri tam 8'de başlamışsa sinyal (9., 10. mumda tekrar atmaz)
        if all_green and not prev_green:
            ts = int(last8[-1][0])
            if sent.get(symbol) == ts:
                return None
            sent[symbol] = ts
            first_open = float(last8[0][1])
            last_close = float(last8[-1][4])
            pct = (last_close - first_open) / first_open * 100
            return symbol, last_close, pct
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
    max_min = float(os.environ.get("MAX_RUNTIME_MIN", "0"))  # 0 = sınırsız
    end_time = time.time() + max_min * 60 if max_min else None
    while end_time is None or time.time() < end_time:
        start = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for res in ex.map(check, symbols):
                if res:
                    sym, price, pct = res
                    telegram(f"🟢 PUMP LONG sinyali\n{sym} (Bitget USDT-M, 1dk)\n"
                             f"Son {GREEN_COUNT} mum yeşil kapandı\n"
                             f"Fiyat: {price}\nSerideki yükseliş: %{pct:.2f}")
        # bir sonraki dakika başına kadar bekle
        time.sleep(max(1, 60 - (time.time() - start) % 60 + 2))


if __name__ == "__main__":
    main()
    
