import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INTERVAL = "1m"
INTERVAL_MIN = 1
MIN_24H_VOL = 50_000
TZ = timezone(timedelta(hours=3))
STATE_FILE = "sent_signals.json"

# --- TARAMA ZAMANLAMASI ---
SCAN_OFFSET = 4      # her dakikanın kaçıncı saniyesinde tarama başlasın (mum kapanışı otursun)

# --- HIZ AYARLARI ---
WORKERS = 8           # aynı anda kaç istek
RATE_PER_SEC = 15     # saniyede en fazla kaç istek (Bitget limiti ~20/sn, güvenli pay bırakıldı)
CONTRACTS_REFRESH = 600   # coin listesi kaç saniyede bir yenilensin

# --- SİNYAL AYARLARI ---
GREEN_COUNT = 8       # 8. kapanmış mum yeşil kapanınca sinyal
MIN_GAIN_PCT = 0.0    # serinin toplam yükselişi en az %kaç olsun (0 = filtre yok)
MIN_BODY_PCT = 0.0    # her mumun gövdesi (açılış->kapanış) en az %kaç olsun (0 = filtre yok; grafikte çok ince mumlar nötr görünüyorsa 0.02 gibi bir değer dene)
CANDLE_LIMIT = 40

# --- ÇALIŞMA ---
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
    "ADBE", "ABNB", "AMAT", "ASML", "BA", "BAC", "CSCO", "CVX", "GS", "HD",
    "HON", "INTU", "JNJ", "LLY", "LMT", "LRCX", "MCD", "MRK", "PFE", "PG",
    "QCOM", "RBLX", "ROKU", "SBUX", "SNOW", "SOFI", "TXN", "UNH", "UPS",
    "VZ", "WFC", "XOM", "RDDT", "DKNG", "RIOT", "CLSK", "SMCI", "OKLO",
    "RKLB", "ASTS", "NBIS", "CRWV", "SNDK", "WDC", "CVNA",
    "TQQQ", "SQQQ", "IWM", "GLD", "SLV", "USO", "EWY", "SMH",
}
NON_CRYPTO = {"PAXG", "XAUT"}

# Listede olmayan bir hisse/varlık sinyal atarsa buraya coin adını (USDT'siz) ekle
EXTRA_EXCLUDE = set()


class RateLimiter:
    def __init__(self, per_sec):
        self.interval = 1.0 / per_sec
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            t = max(now, self.next)
            self.next = t + self.interval
        if t > now:
            time.sleep(t - now)


limiter = RateLimiter(RATE_PER_SEC)


def get(path, params=None):
    for attempt in range(3):
        limiter.wait()
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
    # Bitget'in tokenize hisse/RWA işaretlediği kontratlar (alan varsa)
    if str(c.get("isRwa", "")).upper() in ("YES", "TRUE", "1"):
        return False
    if base in STABLES or base in NON_CRYPTO or base in STOCKS or base in EXTRA_EXCLUDE:
        return False
    # tokenize hisse takıları: TSLAB, TSLAX, TSLAON gibi
    for suf in ("B", "X", "ON"):
        if base.endswith(suf) and base[:-len(suf)] in STOCKS:
            return False
    return True


def check(symbol, cutoff_ms):
    k = get("/api/v2/mix/market/candles",
            {"symbol": symbol, "productType": PRODUCT,
             "granularity": INTERVAL, "limit": CANDLE_LIMIT})
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

    # sadece kapanmış mumlar (oluşan mum sayılmaz); tüm coinler aynı kapanış anına göre değerlendirilir
    rows = [r for r in rows if r["ts"] + INTERVAL_MIN * 60000 <= cutoff_ms]
    if len(rows) < GREEN_COUNT:
        return None

    # sondan geriye doğru üst üste yeşil mum serisi
    # mumlar ardışık olmalı: arada işlem olmayan (eksik) dakika varsa seri kopar
    step = INTERVAL_MIN * 60000
    streak = 0
    prev_ts = None
    for r in reversed(rows):
        if prev_ts is not None and prev_ts - r["ts"] != step:
            break
        if r["o"] > 0 and r["c"] > r["o"] and (r["c"] / r["o"] - 1) * 100 >= MIN_BODY_PCT:
            streak += 1
            prev_ts = r["ts"]
        else:
            break
    # serinin son mumu, kapanan son dakika olmalı (eski/duran veri sinyal üretmesin)
    if rows[-1]["ts"] + step != cutoff_ms:
        return None
    if streak != GREEN_COUNT:   # sadece 8. mum yeşil kapanınca
        return None

    series = rows[-streak:]
    start_open = series[0]["o"]
    last_close = series[-1]["c"]
    gain_pct = (last_close / start_open - 1) * 100
    if gain_pct < MIN_GAIN_PCT:
        return None

    bodies = " ".join(f"{(x['c'] / x['o'] - 1) * 100:+.2f}" for x in series)
    return (symbol, last_close, streak, gain_pct, start_open, series[0]["ts"], bodies)


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
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)
        limit = time.time() * 1000 - 24 * 3600 * 1000
        return {
            k: v for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > limit
        }
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(sent_data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sent_data, f)
    except OSError:
        pass


def load_symbols():
    contracts = get("/api/v2/mix/market/contracts", {"productType": PRODUCT})
    if not contracts:
        return None
    return [c["symbol"] for c in contracts if keep(c)]


def scan(symbols, sent_data):
    tick = get("/api/v2/mix/market/tickers", {"productType": PRODUCT}) or []
    vol24 = {}
    for t in tick:
        try:
            vol24[t["symbol"]] = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except (KeyError, ValueError, TypeError):
            continue

    syms = symbols
    if vol24:
        syms = [s for s in symbols if vol24.get(s, 0) >= MIN_24H_VOL]

    # bu taramanın "şu an" referansı: son tamamlanan dakikanın sınırı
    cutoff_ms = int(time.time() // 60) * 60000
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = [r for r in ex.map(lambda s: check(s, cutoff_ms), syms) if r]
    results.sort(key=lambda x: -x[3])

    changed = False
    for (symbol, price, streak, gain_pct, start_open, start_ts, bodies) in results:
        key = f"{symbol}_{start_ts}"        # her yeşil seri için tek mesaj
        if key in sent_data:
            continue

        t = datetime.now(TZ).strftime("%H:%M:%S")
        send(
            f"🟢 {symbol} PUMP sinyali: {streak} yeşil mum (1dk)\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"Seri yükselişi: +{gain_pct:.2f}% ({fmt(start_open)} -> {fmt(price)})\n"
            f"Mumlar (%): {bodies}\n"
            f"24s hacim: {fmt_vol(vol24.get(symbol, 0))}"
        )
        sent_data[key] = int(time.time() * 1000)
        changed = True
        time.sleep(0.5)

    if changed:
        save_state(sent_data)
    return len(syms), len(results)


if __name__ == "__main__":
    end = time.time() + RUN_MINUTES * 60
    print("bot başladı")
    sent_data = load_state()
    symbols, symbols_ts = [], 0

    while True:
        # bir sonraki dakika başına (+ offset) kadar bekle
        wait = 60 - ((time.time() - SCAN_OFFSET) % 60)
        if time.time() + wait > end:
            break
        time.sleep(wait)

        scan_start = time.time()
        try:
            if not symbols or time.time() - symbols_ts > CONTRACTS_REFRESH:
                new = load_symbols()
                if new:
                    symbols, symbols_ts = new, time.time()
                elif not symbols:
                    print("Borsa verisi alınamadı")
                    continue

            total, found = scan(symbols, sent_data)
            took = time.time() - scan_start
            print(f"{total} coin tarandı, {found} sinyal, {took:.1f} sn")
            if took > 55:
                print("UYARI: tarama 1 dakikaya sığmıyor, RATE_PER_SEC/WORKERS artırın")

            limit = time.time() * 1000 - 24 * 3600 * 1000
            for k in [k for k, v in sent_data.items() if v <= limit]:
                del sent_data[k]
        except Exception as e:
            print("hata:", e)

    print("süre doldu, workflow yeniden başlayacak")
          
