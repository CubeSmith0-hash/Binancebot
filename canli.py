import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "15m"
INTERVAL_MIN = 15
INTERVAL_MS = INTERVAL_MIN * 60000
MIN_24H_VOL = 50_000       # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
TZ = timezone(timedelta(hours=3))
STATE_FILE = "pump15_state.json"
WORKERS = 4
REQ_SLEEP = 0.2

# --- PUMP AYARLARI ---
MIN_CANDLE_PCT = 5.0       # kapanan mum en az % kaç yükselmeli (kapanış/açılış)
PREV_CANDLES = 30          # önceki kaç mumda %5 artış olmamalı
PREV_MAX_PCT = 5.0         # önceki mumlardan hiçbiri bu yükselişe ulaşmamalı

# --- ÇALIŞMA ---
SCAN_SECONDS = 30          # kaç saniyede bir taransın
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "345"))  # workflow timeout'tan (355 dk) kısa olmalı

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


def check(symbol):
    time.sleep(REQ_SLEEP)
    k = get("/api/v2/mix/market/candles",
            {"symbol": symbol, "productType": PRODUCT,
             "granularity": INTERVAL, "limit": PREV_CANDLES + 5})
    if not k:
        return None
    try:
        # [ts, open, high, low, close, baseVol, quoteVol]
        rows = sorted(
            ({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]),
              "l": float(c[3]), "c": float(c[4])} for c in k),
            key=lambda x: x["ts"])
    except (IndexError, ValueError, TypeError):
        return None

    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if r["ts"] + INTERVAL_MS <= now_ms]   # sadece kapanmış mumlar
    if len(closed) < PREV_CANDLES + 1:
        return None

    last = closed[-1]                        # az önce kapanan mum
    cur_start = now_ms // INTERVAL_MS * INTERVAL_MS
    if last["ts"] != cur_start - INTERVAL_MS:
        return None                          # hemen önceki mum değilse (eski) bakma
    if last["o"] <= 0:
        return None

    candle_pct = (last["c"] / last["o"] - 1) * 100
    if candle_pct < MIN_CANDLE_PCT:
        return None

    prev = closed[-(PREV_CANDLES + 1):-1]    # ondan önceki 30 kapanmış mum
    for r in prev:
        if r["o"] <= 0:
            return None
        if (r["c"] / r["o"] - 1) * 100 >= PREV_MAX_PCT:
            return None

    return (symbol, last["c"], last["o"], last["h"], candle_pct, last["ts"])


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
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                raw = json.load(f)
            limit = time.time() * 1000 - 24 * 3600 * 1000
            return {
                k: v for k, v in raw.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > limit
            }
        except (OSError, ValueError, AttributeError):
            pass
    return {}


def save_state(data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except OSError as e:
        print("state yazılamadı", e)


def main(sent_data):
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
    print(f"{len(symbols)} coin taranıyor")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = [r for r in ex.map(check, symbols) if r]
    results.sort(key=lambda x: -x[4])
    print(f"{len(results)} sinyal bulundu")

    limit = time.time() * 1000 - 24 * 3600 * 1000
    for k in [k for k, v in sent_data.items() if v < limit]:
        del sent_data[k]

    for symbol, price, open_p, high_p, candle_pct, ts in results:
        key = f"{symbol}_{ts}"              # aynı mum için tek mesaj
        if key in sent_data:
            continue

        t = datetime.now(TZ).strftime("%H:%M:%S")
        send(
            f"⚡ ANLIK PUMP {symbol} (Bitget Vadeli)\n"
            f"Saat: {t}\n"
            f"Kapanış: {fmt(price)}\n"
            f"Kapanan {INTERVAL} mum: +{candle_pct:.1f}% ({fmt(open_p)} -> {fmt(price)})\n"
            f"Mum tepesi: {fmt(high_p)}\n"
            f"Önceki {PREV_CANDLES} mumda %{PREV_MAX_PCT:g} artış yok\n"
            f"24s hacim: {fmt_vol(vol24.get(symbol, 0))}"
        )

        sent_data[key] = int(time.time() * 1000)
        save_state(sent_data)
        time.sleep(0.5)


if __name__ == "__main__":
    end = time.time() + RUN_MINUTES * 60
    sent = load_state()
    save_state(sent)   # dosya her durumda oluşsun
    print("bot başladı")
    while True:
        try:
            main(sent)
        except Exception as e:
            print("hata:", e)
        wait = SCAN_SECONDS - (time.time() % SCAN_SECONDS)
        if time.time() + wait > end:
            break
        time.sleep(wait)
    print("süre doldu, workflow yeniden başlayacak")
