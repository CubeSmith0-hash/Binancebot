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
WINDOW_SEC = 100           # son kaç saniyeye bakılsın
MIN_PCT = 5.0              # pencere içindeki dipten en az % kaç yükseliş
MAX_PCT = 5.0              # pencere içindeki dipten en fazla % kaç yükseliş (üst sınır)
VOL_MULT = 50.0             # pencere hacmi, normal hacmin en az kaç katı olmalı
VOL_MAX = 999.0             # pencere hacmi, normal hacmin en fazla kaç katı olabilir (üst sınır)
MIN_WINDOW_VOL = 5_000     # pencerede en az bu kadar USD hacim olmalı
MIN_24H_VOL = 50_000       # 24 saatlik USD hacmi bunun altındaki coinler sinyal vermez
COOLDOWN_MIN = 5           # aynı coin için tekrar sinyal bekleme süresi (dk)
HISTORY_SEC = WINDOW_SEC + 30
TZ = timezone(timedelta(hours=3))
STATE_FILE = "canli_meme_state.json"

# --- Memecoin'e özel yeni ayarlar ---
MIN_LIQ = 5_000            # havuz likiditesi (USD) bunun altındaysa sinyal verme (rug/ince likidite)
MAX_WATCH = 90             # aynı anda izlenecek en fazla havuz (DexScreener limiti için)
DISCOVER_SEC = 60          # izleme listesi kaç saniyede bir yenilensin

# Veri kaynakları (Axiom'un resmi API'si yok):
# 1) GeckoTerminal -> trending / yeni / hacimli Solana havuzları (aday listesi)
# 2) DexScreener   -> adayların anlık fiyat + hacim + likidite verisi
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
GT_HEADERS = {"Accept": "application/json;version=20230302"}
DS = "https://api.dexscreener.com/latest/dex/pairs/solana/"

SKIP_SYMBOLS = {
    "SOL", "WSOL", "USDC", "USDT", "PYUSD", "USDS", "USD1", "USDE",
    "JITOSOL", "MSOL", "BSOL", "HSOL", "JUPSOL", "INF", "JLP", "WBTC", "WETH",
}

HISTORY = {}      # havuz adresi -> deque[(zaman, fiyat, 24s_usd_hacim)]
LAST_ALERT = {}   # havuz adresi -> son sinyal zamanı
META = {}         # havuz adresi -> (sembol, mint, likidite, mcap)
WATCH = []        # taranacak havuz adresleri


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
                json={"chat_id": CHAT_ID, "text": text,
                      "disable_web_page_preview": True},
                timeout=15,
            )
            print("telegram:", r.status_code)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return
        except requests.RequestException:
            time.sleep(2)


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def load_state():
    """State dosyasını okur. Değer liste/bozuk olsa bile çökmez, sadece geçerli
    sayısal zamanları alır."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    limit = time.time() - COOLDOWN_MIN * 60
    out = {}
    for k, v in data.items():
        if isinstance(v, (list, tuple)):
            nums = [x for x in v if _is_num(x)]
            v = max(nums) if nums else None
        if _is_num(v) and v > limit:
            out[k] = float(v)
    return out


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(LAST_ALERT, f)
    except OSError as e:
        print("state yazılamadı", e)


def gt_pools(path, params=None):
    """GeckoTerminal'den havuz adreslerini döndürür (hata olursa boş liste)."""
    try:
        r = requests.get(GT + path, params=params, headers=GT_HEADERS, timeout=15)
    except requests.RequestException as e:
        print("GT hata", e)
        return []
    if r.status_code != 200:
        print("GT HTTP", r.status_code, path)
        return []
    try:
        data = r.json().get("data", [])
    except ValueError:
        return []
    out = []
    for d in data:
        addr = (d.get("attributes") or {}).get("address")
        if addr:
            out.append(addr)
    return out


def discover():
    """Trending (5m, 1h), yeni ve hacimli havuzlardan izleme listesi kurar."""
    sources = [
        ("/trending_pools", {"duration": "5m", "page": 1}),
        ("/trending_pools", {"duration": "5m", "page": 2}),
        ("/trending_pools", {"duration": "1h", "page": 1}),
        ("/new_pools", {"page": 1}),
        ("/pools", {"sort": "h24_volume_usd_desc", "page": 1}),
    ]
    seen = []
    for path, params in sources:
        for a in gt_pools(path, params):
            if a not in seen:
                seen.append(a)
        time.sleep(0.3)
    if not seen:
        return False
    WATCH[:] = seen[:MAX_WATCH]
    for k in list(HISTORY):
        if k not in WATCH:
            del HISTORY[k]
    print(f"{len(WATCH)} Solana memecoin havuzu izleniyor")
    return True


def fetch_snapshot():
    """(durum_kodu, {havuz: (fiyat, 24s_hacim)}) döndürür; META'yı da günceller."""
    out = {}
    status = 200
    for i in range(0, len(WATCH), 30):
        chunk = WATCH[i:i + 30]
        try:
            r = requests.get(DS + ",".join(chunk), timeout=10)
        except requests.RequestException as e:
            print("hata", e)
            status = 0
            continue
        if r.status_code != 200:
            print("DS HTTP", r.status_code)
            status = r.status_code
            if r.status_code == 429:
                break
            continue
        try:
            pairs = r.json().get("pairs") or []
        except ValueError:
            continue
        for p in pairs:
            try:
                addr = p["pairAddress"]
                price = float(p["priceUsd"])
                vol24 = float((p.get("volume") or {}).get("h24") or 0)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                mc = float(p.get("marketCap") or p.get("fdv") or 0)
                base = p.get("baseToken") or {}
                META[addr] = (base.get("symbol", "?"), base.get("address", ""), liq, mc)
                out[addr] = (price, vol24)
            except (KeyError, TypeError, ValueError):
                continue
    if out:
        return 200, out
    return status or 0, None


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
    vol = last_q - pts[0][2]          # penceredeki yaklaşık USD hacmi
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
    for addr in WATCH:
        d = snapshot.get(addr)
        if not d:
            continue
        h = HISTORY.setdefault(addr, deque())
        h.append((now, d[0], d[1]))
        while h and h[0][0] < now - HISTORY_SEC:
            h.popleft()
        sym, mint, liq, mc = META.get(addr, ("?", "", 0, 0))
        if sym.upper() in SKIP_SYMBOLS:
            continue
        if liq < MIN_LIQ or d[1] < MIN_24H_VOL:
            continue
        res = evaluate(h, now)
        if not res:
            continue
        last = LAST_ALERT.get(addr)
        if last and now - last < COOLDOWN_MIN * 60:
            continue
        signals.append((addr, d[1]) + res)
    return signals


def alert(addr, vol24, price, pct, vol, mult):
    LAST_ALERT[addr] = time.time()
    save_state()
    sym, mint, liq, mc = META.get(addr, ("?", "", 0, 0))
    t = datetime.now(TZ).strftime("%H:%M:%S")
    send(
        f"⚡ ANLIK PUMP {sym} (Solana Memecoin)\n"
        f"Saat: {t}\n"
        f"Fiyat: ${fmt(price)}\n"
        f"Son {WINDOW_SEC} sn: +{pct:.1f}%\n"
        f"Bu sürede hacim: ${fmt_vol(vol)} ({mult:.1f}x normal)\n"
        f"24s hacim: ${fmt_vol(vol24)} | Likidite: ${fmt_vol(liq)} | MC: ${fmt_vol(mc)}\n"
        f"CA (Axiom'a yapıştır): {mint}\n"
        f"https://dexscreener.com/solana/{addr}"
    )


def main():
    LAST_ALERT.update(load_state())
    save_state()   # dosya her durumda oluşsun

    if not discover():
        print("aday listesi alınamadı")
        return
    status, snap = fetch_snapshot()
    if not snap:
        print("veri alınamadı, durum:", status)
        return

    end = time.time() + LOOP_MINUTES * 60
    last_discover = time.time()
    fails = 0
    while time.time() < end:
        t0 = time.time()
        status, snap = fetch_snapshot()
        if snap:
            fails = 0
            now = time.time()
            if now - last_discover > DISCOVER_SEC:
                discover()
                last_discover = now
            for sig in process(snap, now):
                alert(*sig)
        else:
            fails += 1
            if fails >= 30:
                print("art arda 30 hata, çıkılıyor")
                break
            time.sleep(10 if status == 429 else 5)
        wait = POLL_SEC - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)

    save_state()


if __name__ == "__main__":
    main()
            
