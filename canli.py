import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))  # ne kadar süre çalışsın
POLL_SEC = 1               # kaç saniyede bir taransın
WINDOW_SEC = 80            # son kaç saniyeye bakılsın
MIN_PCT = 4.0              # pencere içindeki dipten en az % kaç yükseliş
MAX_PCT = 5.0              # pencere içindeki dipten en fazla % kaç yükseliş
VOL_MULT = 5.0             # pencere hacmi, normal hacmin en az kaç katı olmalı
MIN_WINDOW_VOL = 5_000     # pencerede en az bu kadar USDT hacim olmalı
MIN_24H_VOL = 50_000       # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
COOLDOWN_MIN = 5           # aynı coin için tekrar sinyal bekleme süresi (dk)
HISTORY_SEC = WINDOW_SEC + 30
TZ = timezone(timedelta(hours=3))
STATE_FILE = "canli_state.json"

# Bitget USDT-M Futures
HOST = "https://api.bitget.com"
TICKER_PATH = "/api/v2/mix/market/tickers"
TICKER_PARAMS = {"productType": "USDT-FUTURES"}

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

HISTORY = {}      # sembol -> deque[(zaman, fiyat, 24s_usdt_hacim)]
LAST_ALERT = {}   # sembol -> son sinyal zamanı
WATCH = set()     # taranacak semboller


def keep(sym):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in STABLES or base in NON_CRYPTO or base in STOCKS:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    return True


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
                data = json.load(f)
            limit = time.time() - COOLDOWN_MIN * 60
            return {
                k: v for k, v in data.items()
                if isinstance(v, (int, float)) and v > limit
            }
        except (OSError, ValueError, AttributeError):
            pass
    return {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(LAST_ALERT, f)
    except OSError as e:
        print("state yazılamadı", e)


def fetch_snapshot():
    """(durum_kodu, {sembol: (fiyat, 24s_usdt_hacim)}) döndürür."""
    try:
        r = requests.get(HOST + TICKER_PATH, params=TICKER_PARAMS, timeout=15)
    except requests.RequestException as e:
        print("hata", e)
        return 0, None
    if r.status_code != 200:
        print("HTTP", r.status_code, TICKER_PATH)
        if r.status_code in (403, 451):
            print("bu sunucunun IP'si Bitget tarafından engelli")
        return r.status_code, None
    try:
        body = r.json()
    except ValueError:
        print("json okunamadı")
        return 0, None
    if body.get("code") != "00000":
        print("API hatası:", body.get("code"), body.get("msg"))
        return 0, None
    out = {}
    for t in body.get("data", []):
        try:
            vol = t.get("usdtVolume") or t.get("quoteVolume")
            out[t["symbol"]] = (float(t["lastPr"]), float(vol))
        except (KeyError, TypeError, ValueError):
            continue
    return 200, out


def refresh_symbols(snap):
    WATCH.clear()
    for sym, (_, vol24) in snap.items():
        if keep(sym) and vol24 >= MIN_24H_VOL:
            WATCH.add(sym)
    print(f"{len(WATCH)} Bitget vadeli coin izleniyor")


def evaluate(hist, now):
    pts = [p for p in hist if p[0] >= now - WINDOW_SEC]
    if len(pts) < 3 or now - pts[0][0] < WINDOW_SEC * 0.6:
        return None   # henüz yeterli geçmiş yok
    low = min(p[1] for p in pts)
    last_p, last_q = pts[-1][1], pts[-1][2]
    if low <= 0 or last_p <= low:
        return None
    pct = (last_p / low - 1) * 100
    if pct < MIN_PCT or pct > MAX_PCT:
        return None
    vol = last_q - pts[0][2]          # penceredeki yaklaşık USDT hacmi
    if vol < MIN_WINDOW_VOL:
        return None
    normal = last_q / 1440 * (WINDOW_SEC / 60)   # aynı sürede normal hacim
    if normal <= 0:
        return None
    mult = vol / normal
    if mult < VOL_MULT:
        return None
    return last_p, pct, vol, mult


def process(snapshot, now):
    """Anlık veriyi geçmişe ekler, şartı sağlayan coinleri döndürür."""
    signals = []
    for sym in WATCH:
        d = snapshot.get(sym)
        if not d:
            continue
        h = HISTORY.setdefault(sym, deque())
        h.append((now, d[0], d[1]))
        while h and h[0][0] < now - HISTORY_SEC:
            h.popleft()
        res = evaluate(h, now)
        if not res:
            continue
        last = LAST_ALERT.get(sym)
        if last and now - last < COOLDOWN_MIN * 60:
            continue
        signals.append((sym, d[1]) + res)
    return signals


def alert(sym, vol24, price, pct, vol, mult):
    LAST_ALERT[sym] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    send(
        f"⚡ ANLIK PUMP {sym} (Bitget Vadeli)\n"
        f"Saat: {t}\n"
        f"Fiyat: {fmt(price)}\n"
        f"Son {WINDOW_SEC} sn: +{pct:.1f}%\n"
        f"Bu sürede hacim: {fmt_vol(vol)} USDT ({mult:.1f}x normal)\n"
        f"24s hacim: {fmt_vol(vol24)}"
    )


def main():
    LAST_ALERT.update(load_state())
    save_state()   # dosya her durumda oluşsun

    status, snap = fetch_snapshot()
    if not snap:
        print("Borsa verisi alınamadı")
        return
    refresh_symbols(snap)
    last_refresh = time.time()

    end = time.time() + LOOP_MINUTES * 60
    fails = 0
    print(f"canlı tarama başladı: {POLL_SEC} sn aralık, {WINDOW_SEC} sn pencere", flush=True)
    while time.time() < end:
        t0 = time.time()
        status, snap = fetch_snapshot()
        if snap is None:
            fails += 1
            wait = 60 if status in (418, 429) else min(5 * fails, 30)
            print(f"veri alınamadı ({status}), {wait} sn bekleniyor", flush=True)
            time.sleep(wait)
            continue
        fails = 0

        for sig in sorted(process(snap, time.time()), key=lambda x: -x[3]):
            try:
                alert(*sig)
            except Exception as e:
                print("sinyal hatası", e)

        if time.time() - last_refresh > 1800:
            refresh_symbols(snap)
            last_refresh = time.time()

        time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))
    print("süre doldu, çıkılıyor")


if __name__ == "__main__":
    main()
            
