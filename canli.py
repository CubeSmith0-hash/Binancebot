import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ---------------- AYARLAR (değiştirilmedi) ----------------
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))  # ne kadar süre çalışsın
POLL_SEC = 1               # kaç saniyede bir taransın
WINDOW_SEC = 10           # son kaç saniyeye bakılsın
MIN_PCT = 5.0              # pencere içindeki dipten en az % kaç yükseliş
MAX_PCT = 999.0            # pencere içindeki dipten en fazla % kaç yükseliş
VOL_MULT = 13.0            # pencere hacmi, normal hacmin en az kaç katı olmalı
VOL_MAX = 999.0             # pencere hacmi, normal hacmin en fazla kaç katı olabilir
MIN_WINDOW_VOL = 100_000     # pencerede en az bu kadar USDT hacim olmalı
MIN_24H_VOL = 50_000       # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
COOLDOWN_MIN = 5           # aynı coin için tekrar sinyal bekleme süresi (dk)
HISTORY_SEC = WINDOW_SEC + 30
TZ = timezone(timedelta(hours=3))
STATE_FILE = "canli_state.json"
# -----------------------------------------------------------

# GeckoTerminal (DEX) - ücretsiz API limiti: dakikada 30 istek
HOST = "https://api.geckoterminal.com"
ENDPOINTS = [
    "/api/v2/networks/trending_pools",   # tüm ağlarda trend havuzlar (20 havuz)
    "/api/v2/networks/new_pools",        # tüm ağlarda yeni havuzlar (20 havuz)
]
HEADERS = {"accept": "application/json;version=20230302"}
MIN_CALL_GAP = 2.1         # iki istek arası min saniye (30/dk limitini aşmamak için)
MIN_LIQ_USD = 10_000       # havuz likiditesi bunun altındaysa atla (rug/sahte havuz filtresi, 0 = kapalı)

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
    "USDT", "USDS", "PYUSD", "USDD", "FRAX", "LUSD", "GUSD", "USDB",
}
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
    "DELL", "IBM", "NOW", "CRM", "IREN", "BX", "HPE", "CRWD",
}
# memecoin olmayan majör / sarmalanmış varlıklar
MAJORS = {
    "BTC", "WBTC", "CBBTC", "ETH", "WETH", "STETH", "WSTETH", "SOL", "WSOL",
    "BNB", "WBNB", "MATIC", "POL", "AVAX", "WAVAX", "PAXG", "XAUT",
}

HISTORY = {}      # havuz -> deque[(zaman, fiyat, 24s_usd_hacim)]
LAST_ALERT = {}   # havuz -> son sinyal zamanı
META = {}         # havuz -> (isim, ağ, havuz_adresi)
_last_call = 0.0


def keep(base):
    base = base.upper()
    if base in STABLES or base in MAJORS or base in STOCKS:
        return False
    if base.endswith("X") and base[:-1] in STOCKS:   # tokenize hisseler (AAPLX vb.)
        return False
    return True


def fmt(p):
    if p < 1:
        return f"{p:.10f}".rstrip("0").rstrip(".")
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
            return {k: v for k, v in data.items() if v > limit}
        except (OSError, ValueError):
            pass
    return {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(LAST_ALERT, f)
    except OSError as e:
        print("state yazılamadı", e)


def throttle():
    """GeckoTerminal ücretsiz limitini (30/dk) aşmamak için istekleri aralıklar."""
    global _last_call
    wait = MIN_CALL_GAP - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def parse_pool(p):
    """Havuz kaydını (anahtar, fiyat, 24s_hacim, isim, ağ, adres) olarak döndürür."""
    a = p["attributes"]
    pool_id = p["id"]                                  # örn. solana_ABC...
    net = p["relationships"]["network"]["data"]["id"]
    addr = a.get("address") or pool_id.split("_", 1)[-1]
    name = a.get("name", "?")
    base = name.split("/")[0].strip()
    if not keep(base):
        return None
    price = float(a["base_token_price_usd"])
    vol24 = float(a["volume_usd"]["h24"])
    liq = float(a.get("reserve_in_usd") or 0)
    if price <= 0 or liq < MIN_LIQ_USD:
        return None
    return pool_id, price, vol24, name, net, addr


def fetch_snapshot():
    """(durum_kodu, {havuz: (fiyat, 24s_usd_hacim)}) döndürür."""
    out = {}
    for path in ENDPOINTS:
        throttle()
        try:
            r = requests.get(HOST + path, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            print("hata", e)
            return 0, None
        if r.status_code != 200:
            print("HTTP", r.status_code, path)
            return r.status_code, None
        try:
            body = r.json()
        except ValueError:
            print("json okunamadı")
            return 0, None
        for p in body.get("data", []):
            try:
                res = parse_pool(p)
            except (KeyError, TypeError, ValueError):
                continue
            if not res:
                continue
            key, price, vol24, name, net, addr = res
            META[key] = (name, net, addr)
            out[key] = (price, vol24)
    return 200, out


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
    if mult < VOL_MULT or mult > VOL_MAX:
        return None
    return last_p, pct, vol, mult


def process(snapshot, now):
    """Anlık veriyi geçmişe ekler, şartı sağlayan coinleri döndürür."""
    signals = []
    for sym, d in snapshot.items():
        if d[1] < MIN_24H_VOL:
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
    # listeden düşen havuzların eski geçmişini temizle
    for sym in [s for s, h in HISTORY.items() if not h or h[-1][0] < now - HISTORY_SEC]:
        del HISTORY[sym]
    return signals


def alert(sym, vol24, price, pct, vol, mult):
    LAST_ALERT[sym] = time.time()
    save_state()
    name, net, addr = META.get(sym, (sym, "?", ""))
    t = datetime.now(TZ).strftime("%H:%M:%S")
    send(
        f"⚡ ANLIK PUMP {name} (GeckoTerminal DEX)\n"
        f"Ağ: {net}\n"
        f"Saat: {t}\n"
        f"Fiyat: ${fmt(price)}\n"
        f"Son {WINDOW_SEC} sn: +{pct:.1f}%\n"
        f"Bu sürede hacim: {fmt_vol(vol)} USDT ({mult:.1f}x normal)\n"
        f"24s hacim: {fmt_vol(vol24)}\n"
        f"https://www.geckoterminal.com/{net}/pools/{addr}"
    )


def main():
    LAST_ALERT.update(load_state())
    save_state()   # dosya her durumda oluşsun

    status, snap = fetch_snapshot()
    if not snap:
        print("GeckoTerminal verisi alınamadı")
        return
    print(f"{len(snap)} DEX havuzu izleniyor")

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

        time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))
    print("süre doldu, çıkılıyor")


if __name__ == "__main__":
    main()
    
