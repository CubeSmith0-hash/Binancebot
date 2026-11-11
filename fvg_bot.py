import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "55"))
POLL_SEC = 1                 # fiyat kontrol sıklığı (saniyelik)
TF = "15m"                   # zaman dilimi
TF_SEC = 15 * 60
CANDLE_LIMIT = 150           # taranacak mum sayısı
MIN_GAP_PCT = 0.3            # minimum FVG büyüklüğü (%)
MAX_AGE_CANDLES = 96         # bundan eski FVG'ler atılır (15m x 96 = 24 saat)
MIN_24H_VOL = 500_000        # 24s USDT hacim filtresi
CANDLE_SLEEP = 0.08          # mum istekleri arası bekleme (rate limit)
TZ = timezone(timedelta(hours=3))
STATE_FILE = "fvg_state.json"

HOST = "https://api.bitget.com"
TICKER_PATH = "/api/v2/mix/market/tickers"
CANDLE_PATH = "/api/v2/mix/market/candles"
PRODUCT = "USDT-FUTURES"

# Kendi STABLES listeni buraya yapıştır
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "GBP", "BRL"}


def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException as e:
        print("telegram hata:", e)


def get_tickers():
    try:
        r = requests.get(HOST + TICKER_PATH,
                         params={"productType": PRODUCT}, timeout=10)
        data = r.json().get("data") or []
    except (requests.RequestException, ValueError) as e:
        print("ticker hata:", e)
        return {}
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLES:
            continue
        try:
            price = float(t["lastPr"])
            vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        if vol >= MIN_24H_VOL and price > 0:
            out[sym] = price
    return out


def get_candles(symbol):
    try:
        r = requests.get(
            HOST + CANDLE_PATH,
            params={"symbol": symbol, "productType": PRODUCT,
                    "granularity": TF, "limit": CANDLE_LIMIT},
            timeout=10,
        )
        rows = r.json().get("data") or []
    except (requests.RequestException, ValueError):
        return []
    candles = []
    for c in rows:
        try:
            candles.append((int(c[0]), float(c[2]), float(c[3])))  # ts, high, low
        except (IndexError, ValueError, TypeError):
            continue
    candles.sort()
    return candles


def find_bullish_fvgs(candles):
    """Kapanmış mumlardan, henüz dokunulmamış bullish FVG'leri döndürür."""
    now_ms = int(time.time() * 1000)
    closed = [c for c in candles if c[0] + TF_SEC * 1000 <= now_ms]
    n = len(closed)
    fvgs = []
    for i in range(2, n):
        bottom = closed[i - 2][1]   # 1. mumun high'ı
        top = closed[i][2]          # 3. mumun low'u
        if bottom >= top:
            continue
        if (top - bottom) / top * 100 < MIN_GAP_PCT:
            continue
        if n - i > MAX_AGE_CANDLES:
            continue
        # sonraki mumlar boşluğa girdiyse artık taze değil
        touched = any(closed[j][2] <= top for j in range(i + 1, n))
        if touched:
            continue
        fvgs.append({"ts": closed[i][0], "bottom": bottom, "top": top})
    return fvgs


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"fvgs": {}, "sent": []}


def save_state(state):
    state["sent"] = state["sent"][-2000:]
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print("state yazılamadı:", e)


def refresh_fvgs(state, symbols):
    for k, sym in enumerate(sorted(symbols), 1):
        candles = get_candles(sym)
        if candles:
            state["fvgs"][sym] = find_bullish_fvgs(candles)
        time.sleep(CANDLE_SLEEP)
        if k % 25 == 0:  # tarama sırasında da saniyelik kontrol aksamasın
            live = get_tickers()
            if live:
                check_prices(state, live)
    # listeden çıkmış coinleri temizle
    for sym in list(state["fvgs"]):
        if sym not in symbols:
            del state["fvgs"][sym]
    save_state(state)


def check_prices(state, prices):
    for sym, price in prices.items():
        zones = state["fvgs"].get(sym)
        if not zones:
            continue
        keep = []
        for z in zones:
            key = f"{sym}:{z['ts']}"
            if price < z["bottom"]:
                continue                      # tamamen doldu, sil
            if price <= z["top"]:
                if key not in state["sent"]:
                    state["sent"].append(key)
                    t = datetime.fromtimestamp(z["ts"] / 1000, TZ)
                    send(
                        f"🟢 <b>{sym}</b> bullish FVG bölgesine girdi\n"
                        f"Fiyat: {price}\n"
                        f"FVG: {z['bottom']} - {z['top']} ({TF})\n"
                        f"Oluşma: {t:%d.%m %H:%M}"
                    )
                continue                      # ilk dokunuştan sonra sil
            keep.append(z)
        state["fvgs"][sym] = keep


def next_candle_close_ts():
    now = int(time.time())
    return (now // TF_SEC + 1) * TF_SEC + 5   # kapanıştan 5 sn sonra


def main():
    state = load_state()
    end = time.time() + LOOP_MINUTES * 60
    prices = get_tickers()
    print(f"{len(prices)} coin taranıyor...")
    refresh_fvgs(state, set(prices))
    next_refresh = next_candle_close_ts()

    while time.time() < end:
        prices = get_tickers()
        if prices:
            check_prices(state, prices)
            save_state(state)
        if time.time() >= next_refresh:
            refresh_fvgs(state, set(prices))
            next_refresh = next_candle_close_ts()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
      
