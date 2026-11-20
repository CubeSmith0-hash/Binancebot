"""
Memecoin DEX pump botu (GeckoTerminal public API, anahtar gerekmez)
Bitget vadeli yerine DEX havuzlarini (Solana, Base...) tarar.

NOT: Public API ~60 sn cache'li ve limitli (~10-30 istek/dk).
Bu yuzden 1 sn'lik tarama anlamsiz; veriyi 60 sn'de bir cekiyoruz.
Hazir 5 dk (m5) hacim/fiyat/islem verisi API'den geliyor.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))

# ---------------- AYARLAR (sert pump) ----------------
NETWORKS = ["solana", "base"]        # "bsc", "eth" ekleyebilirsiniz (istek sayisi artar)
POLL_SEC = 60                        # cache 60 sn, daha sik cekmek anlamsiz
SOURCES = ["trending", "new", "top"] # hangi listeler taransin

MIN_PCT_M5 = 8.0          # son 5 dk fiyat artisi en az %
MAX_PCT_M5 = 150.0        # bunun ustu genelde gec / manipulasyon
MAX_PCT_H1 = 400.0        # son 1 saatte zaten cok kosmussa atla
VOL_MULT = 8.0            # 5 dk hacmi, 6 saatlik ortalamanin en az kac kati
MIN_M5_VOL = 10_000       # 5 dk'da en az bu kadar USD hacim
MIN_LIQ = 30_000          # havuz likiditesi en az USD
MIN_24H_VOL = 50_000      # 24s hacim en az USD
MIN_BUY_RATIO = 0.55      # 5 dk'da alim orani (alis / toplam islem)
MIN_BUYERS = 15           # 5 dk'da benzersiz alici sayisi
MIN_AGE_MIN = 30          # havuz en az kac dk'lik olsun (cok yeni = rug riski)
MAX_VOL_LIQ = 15.0        # 24s hacim / likidite bunun ustundeyse sahte hacim suphesi
COOLDOWN_MIN = 20
# ------------------------------------------------------

TZ = timezone(timedelta(hours=3))
STATE_FILE = "memecoin_state.json"
API = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302"}

LAST_ALERT = {}


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fmt_usd(v):
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def fmt_price(p):
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.10f}".rstrip("0").rstrip(".")


def send(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
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
            with open(STATE_FILE) as fh:
                data = json.load(fh)
            limit = time.time() - COOLDOWN_MIN * 60
            return {k: v for k, v in data.items() if v > limit}
        except (OSError, ValueError):
            pass
    return {}


def save_state():
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump(LAST_ALERT, fh)
    except OSError as e:
        print("state yazilamadi", e)


def url_for(net, source):
    if source == "trending":
        return f"{API}/networks/{net}/trending_pools", {"duration": "1h"}
    if source == "new":
        return f"{API}/networks/{net}/new_pools", {}
    return f"{API}/networks/{net}/pools", {"sort": "h24_volume_usd_desc", "page": 1}


def fetch(net, source):
    """(durum, [havuz, ...]) doner."""
    url, params = url_for(net, source)
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print("hata", e)
        return 0, []
    if r.status_code != 200:
        print("HTTP", r.status_code, net, source)
        return r.status_code, []
    try:
        return 200, r.json().get("data", [])
    except ValueError:
        return 0, []


def check(pool):
    """Sartlari saglarsa (ozet dict), yoksa None."""
    a = pool.get("attributes", {})
    pid = pool.get("id", "")
    if "_" not in pid:
        return None
    net, addr = pid.split("_", 1)

    pc = a.get("price_change_percentage", {}) or {}
    vol = a.get("volume_usd", {}) or {}
    tx5 = (a.get("transactions", {}) or {}).get("m5", {}) or {}

    pct5 = f(pc.get("m5"))
    pct1h = f(pc.get("h1"))
    if pct5 < MIN_PCT_M5 or pct5 > MAX_PCT_M5 or pct1h > MAX_PCT_H1:
        return None

    liq = f(a.get("reserve_in_usd"))
    v5, v6, v24 = f(vol.get("m5")), f(vol.get("h6")), f(vol.get("h24"))
    if liq < MIN_LIQ or v5 < MIN_M5_VOL or v24 < MIN_24H_VOL:
        return None
    if liq > 0 and v24 / liq > MAX_VOL_LIQ:
        return None

    normal = v6 / 72 if v6 > 0 else 0   # 6 saatin 5 dk'lik ortalamasi
    mult = v5 / normal if normal > 0 else 999.0
    if mult < VOL_MULT:
        return None

    buys, sells = f(tx5.get("buys")), f(tx5.get("sells"))
    buyers = f(tx5.get("buyers"))
    total = buys + sells
    if total <= 0 or buys / total < MIN_BUY_RATIO or buyers < MIN_BUYERS:
        return None

    created = a.get("pool_created_at")
    age_min = None
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        except ValueError:
            pass
    if MIN_AGE_MIN and age_min is not None and age_min < MIN_AGE_MIN:
        return None

    return {
        "key": pid, "net": net, "addr": addr,
        "name": a.get("name", "?"),
        "price": f(a.get("base_token_price_usd")),
        "pct5": pct5, "pct1h": pct1h, "liq": liq,
        "v5": v5, "v24": v24, "mult": mult,
        "buy_ratio": buys / total, "buyers": int(buyers),
        "mcap": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")),
        "age_min": age_min,
    }


def alert(s):
    LAST_ALERT[s["key"]] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    age = "?" if s["age_min"] is None else (
        f"{s['age_min'] / 60:.1f} sa" if s["age_min"] >= 120 else f"{s['age_min']:.0f} dk")
    send(
        f"🚀 MEMECOIN PUMP {s['name']} ({s['net']})\n"
        f"Saat: {t}\n"
        f"Fiyat: ${fmt_price(s['price'])}\n"
        f"5 dk: +{s['pct5']:.1f}% | 1 sa: {s['pct1h']:+.1f}%\n"
        f"5 dk hacim: {fmt_usd(s['v5'])}$ ({s['mult']:.1f}x normal)\n"
        f"Alim orani: %{s['buy_ratio'] * 100:.0f} | Alici: {s['buyers']}\n"
        f"Likidite: {fmt_usd(s['liq'])}$ | MCap/FDV: {fmt_usd(s['mcap'])}$\n"
        f"24s hacim: {fmt_usd(s['v24'])}$ | Yas: {age}\n"
        f"https://www.geckoterminal.com/{s['net']}/pools/{s['addr']}\n"
        f"https://dexscreener.com/{s['net']}/{s['addr']}\n"
        f"⚠️ Girmeden once kontrat/honeypot kontrolu yapin (RugCheck, GoPlus)."
    )


def scan_once():
    seen = {}
    rate_limited = False
    for net in NETWORKS:
        for src in SOURCES:
            status, pools = fetch(net, src)
            if status == 429:
                rate_limited = True
                break
            for p in pools:
                seen[p.get("id")] = p
            time.sleep(1.5)   # istekler arasi nefes
        if rate_limited:
            break

    signals = []
    for pid, p in seen.items():
        last = LAST_ALERT.get(pid)
        if last and time.time() - last < COOLDOWN_MIN * 60:
            continue
        s = check(p)
        if s:
            signals.append(s)
    return rate_limited, len(seen), sorted(signals, key=lambda x: -x["mult"])


def main():
    LAST_ALERT.update(load_state())
    save_state()
    end = time.time() + LOOP_MINUTES * 60
    print(f"tarama basladi: {NETWORKS} {SOURCES} her {POLL_SEC} sn", flush=True)
    backoff = 0
    while time.time() < end:
        t0 = time.time()
        limited, n, signals = scan_once()
        print(f"{n} havuz tarandi, {len(signals)} sinyal", flush=True)
        for s in signals:
            try:
                alert(s)
            except Exception as e:
                print("sinyal hatasi", e)
        if limited:
            backoff = min(backoff + 60, 300)
            print(f"429 limit, {backoff} sn ek bekleme", flush=True)
        else:
            backoff = 0
        time.sleep(max(0.0, POLL_SEC + backoff - (time.time() - t0)))
    print("sure doldu, cikiliyor")


if __name__ == "__main__":
    main()
                                 
