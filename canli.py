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

# ---- Denenecek sinyal kombinasyonları (hepsi aynı anda test edilir, üst sınır yok) ----
WINDOWS = [10, 20, 30, 60, 100, 250]            # pencere (sn)
PCTS = [1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]     # pencere içindeki dipten en az % yükseliş
VOLS = [5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0]  # en az hacim çarpanı
MIN_WINDOW_VOL = 5_000     # pencerede en az bu kadar USDT hacim
MIN_24H_VOL = 0            # 0 = hacim filtresi yok, tüm USDT-M pariteleri taranır
COOLDOWN_MIN = 5           # aynı kombinasyon + coin için bekleme (dk)

# ---- Sanal işlem ayarları (margin üzerinden %) ----
LEVERAGE = 20
TP_MARGIN = 30.0           # kâr al: margin üzerinden +%30
SL_MARGIN = 50.0           # zarar durdur: margin üzerinden -%50
FEE_PCT = 0.12             # gidiş-dönüş komisyon (fiyat üzerinden %)
TRACK_SEC = 900            # bir işlem en fazla kaç saniye izlensin

# ---- Kombinasyon onayı ----
NEED = 5                   # üst üste kaç işlem TP olursa kombinasyon onaylansın
MIN_RATE = 0.9             # onaylı kombinasyon son 10 işlemde en az bu oranda TP olmalı

REPORT_MIN = 30            # kaç dakikada bir rapor
SAVE_SEC = 300             # kaç saniyede bir state dosyasına yazılsın
MIN_TRADES = 5             # rapora girmek için en az işlem sayısı
TOP_N = 10                 # raporda kaç kombinasyon gösterilsin

TP_PRICE = TP_MARGIN / LEVERAGE       # fiyatta gereken hareket (%)
SL_PRICE = SL_MARGIN / LEVERAGE
FEE_MARGIN = FEE_PCT * LEVERAGE       # komisyonun margin'e etkisi (%)
HISTORY_SEC = max(WINDOWS) + 30
TZ = timezone(timedelta(hours=3))

# State dosyası: workflow "data" klasörünü state branch'inden hazırlar
STATE_FILE = os.environ.get("STATE_FILE", "data/canli_state.json")
OLD_STATE_FILE = "canli_state.json"   # eski konum (yedek olarak okunur)

# ---- Son araştırma raporundan devam etmek için başlangıç verisi ----
# Bu veri state dosyasına sadece BİR KEZ uygulanır ("seeded" bayrağı ile).
SEED_TOTAL = 9285
# kombinasyon: (işlem, TP, SL, süre, işlem başı ort. kâr %)
SEED_STATS = {
    "100|4.0|50.0": (13, 12, 1, 0, 21.4),
    "250|4.0|50.0": (9, 8, 1, 0, 18.7),
    "250|4.0|15.0": (15, 13, 2, 0, 16.9),
    "100|4.0|30.0": (15, 13, 2, 0, 16.9),
    "60|3.0|30.0": (12, 10, 1, 1, 16.1),
    "60|3.0|20.0": (18, 15, 2, 1, 15.5),
    "250|4.0|5.0": (13, 11, 2, 0, 15.3),
    "60|3.0|15.0": (17, 14, 2, 1, 14.8),
    "100|5.0|30.0": (12, 10, 2, 0, 14.3),
    "100|5.0|20.0": (12, 10, 2, 0, 14.3),
}
SEED_LIVE = ["20|4.0|50.0", "60|2.0|100.0"]   # onaylı kombinasyonlar

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
NON_CRYPTO = {"PAXG", "XAUT", "XAU", "XAG", "XPT", "XPD", "CL", "BZ", "NG", "HG"}

HISTORY = {}     # sembol -> deque[(zaman, fiyat, 24s_usdt_hacim)]
LAST = {}        # (kombinasyon, sembol) -> son sanal giriş zamanı
LIVE_LAST = {}   # sembol -> son gerçek sinyal zamanı
WATCH = set()
PENDING = []     # izlenen sanal işlemler
STATS = {}       # kombinasyon -> [n, tp, sl, zaman, toplam_pnl_margin]
RESULTS = {}     # kombinasyon -> [[sembol, sonuç(1=TP,2=SL,3=süre)], ...] son 20
LIVE = set()     # onaylanmış (gerçek sinyal veren) kombinasyonlar
BANNED = set()   # onayı düşmüş, bir daha taranmayacak kombinasyonlar
QUALIFIED = []   # bu turda onaylanan kombinasyonlar (tek mesajda gönderilir)
DEMOTED = []     # bu turda onayı düşenler
META = {"base": 0, "seeded": False}   # base: STATS'ta olmayan eski işlem sayısı


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


def send(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
                timeout=15,
            )
            print("telegram:", r.status_code, flush=True)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return
        except requests.RequestException:
            time.sleep(2)


def load_state():
    path = STATE_FILE if os.path.exists(STATE_FILE) else OLD_STATE_FILE
    if not os.path.exists(path):
        print("state dosyası yok, sıfırdan başlanıyor", flush=True)
        return
    try:
        with open(path, "r") as f:
            data = json.load(f)
        STATS.update(data.get("stats", {}))
        RESULTS.update(data.get("results", {}))
        LIVE.update(data.get("live", []))
        BANNED.update(data.get("banned", []))
        META["base"] = int(data.get("base", 0))
        META["seeded"] = bool(data.get("seeded", False))
        print(f"state yüklendi ({path}): {len(STATS)} kombinasyon, "
              f"{len(LIVE)} onaylı, {len(BANNED)} yasaklı", flush=True)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        print("STATE OKUNAMADI:", e, flush=True)


def seed_state():
    """Son araştırma raporundaki verileri bir kez state'e işler."""
    if META["seeded"]:
        return
    for k, (n, tp, sl, z, avg) in SEED_STATS.items():
        STATS[k] = [n, tp, sl, z, round(avg * n, 2)]
    for k in SEED_LIVE:
        LIVE.add(k)
        BANNED.discard(k)
    known = sum(s[0] for s in STATS.values())
    META["base"] = max(0, SEED_TOTAL - known)
    META["seeded"] = True
    print(f"seed uygulandı: {len(SEED_STATS)} kombinasyon, {len(SEED_LIVE)} onaylı, "
          f"base={META['base']}", flush=True)


def save_state():
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"stats": STATS, "results": RESULTS, "live": sorted(LIVE),
                       "banned": sorted(BANNED), "base": META["base"],
                       "seeded": META["seeded"]}, f)
        os.replace(tmp, STATE_FILE)   # yarım yazılmış dosya commit'lenmesin
    except OSError as e:
        print("state yazılamadı", e, flush=True)


def fetch_snapshot():
    try:
        r = requests.get(HOST + TICKER_PATH, params=TICKER_PARAMS, timeout=15)
    except requests.RequestException as e:
        print("hata", e)
        return 0, None
    if r.status_code != 200:
        print("HTTP", r.status_code, TICKER_PATH)
        return r.status_code, None
    try:
        body = r.json()
    except ValueError:
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
    print(f"{len(WATCH)} Bitget vadeli coin izleniyor", flush=True)


def metrics(hist, now, window):
    """Pencere için (son fiyat, yükseliş %, pencere hacmi, çarpan) ya da None."""
    pts = [p for p in hist if p[0] >= now - window]
    if len(pts) < 3 or now - pts[0][0] < window * 0.6:
        return None
    low = min(p[1] for p in pts)
    last_p, last_q = pts[-1][1], pts[-1][2]
    if low <= 0 or last_p <= low:
        return None
    pct = (last_p / low - 1) * 100
    vol = last_q - pts[0][2]
    normal = last_q / 1440 * (window / 60)
    if normal <= 0:
        return None
    return last_p, pct, vol, vol / normal


def key_of(w, p, v):
    return f"{w}|{p}|{v}"


def label(k):
    w, p, v = k.split("|")
    return f"{w}sn %{p} {v}x"


def process(snapshot, now):
    """Her kombinasyon için şartı sağlayan coinlere sanal işlem açar.
    Yeni açılan işlemleri (sembol, fiyat, pct, çarpan, kombinasyon) listesi olarak döndürür."""
    hits = []
    for sym in WATCH:
        d = snapshot.get(sym)
        if not d:
            continue
        h = HISTORY.setdefault(sym, deque())
        h.append((now, d[0], d[1]))
        while h and h[0][0] < now - HISTORY_SEC:
            h.popleft()
        for w in WINDOWS:
            m = metrics(h, now, w)
            if not m:
                continue
            last_p, pct, vol, mult = m
            if vol < MIN_WINDOW_VOL:
                continue
            for p in PCTS:
                if pct < p:
                    continue
                for v in VOLS:
                    if mult < v:
                        continue
                    k = key_of(w, p, v)
                    if k in BANNED:
                        continue
                    last = LAST.get((k, sym))
                    if last and now - last < COOLDOWN_MIN * 60:
                        continue
                    LAST[(k, sym)] = now
                    PENDING.append({"key": k, "sym": sym, "entry": last_p, "t0": now})
                    hits.append((sym, last_p, pct, mult, k))
    return hits


def check_status(k):
    """Kombinasyon onaylanmış mı / onayı düşmeli mi kontrol eder."""
    if k in BANNED:
        return
    res = RESULTS.get(k, [])
    if k not in LIVE:
        last = res[-NEED:]
        if (len(last) == NEED and all(r[1] == 1 for r in last)
                and len({r[0] for r in last}) == NEED):
            LIVE.add(k)
            QUALIFIED.append((k, [r[0] for r in last]))
    else:
        last = res[-10:]
        if len(last) >= 6:
            tp = sum(1 for r in last if r[1] == 1)
            if tp / len(last) < MIN_RATE:
                LIVE.discard(k)
                BANNED.add(k)
                PENDING[:] = [x for x in PENDING if x["key"] != k]
                DEMOTED.append((k, tp, len(last)))


def flush_alerts():
    """Aynı turda onaylanan / düşen kombinasyonları tek mesajda bildirir."""
    if QUALIFIED:
        lines = [f"{label(k)} ({', '.join(c.replace('USDT', '') for c in coins)})"
                 for k, coins in QUALIFIED[:10]]
        more = f"\n... ve {len(QUALIFIED) - 10} kombinasyon daha" if len(QUALIFIED) > 10 else ""
        send(
            f"🚨 ONAYLI KOMBİNASYON BULUNDU ({len(QUALIFIED)} adet)\n"
            f"Her biri üst üste {NEED} farklı coinde TP oldu:\n"
            + "\n".join(lines) + more +
            f"\nBunlardan artık gerçek sinyal göndereceğim.\n"
            f"Not: {NEED} işlem az bir örnek, sonuçlar izlenecek. "
            f"Benzer kombinasyonlar aynı pump'lara dayandığı için birlikte onaylanır."
        )
        QUALIFIED.clear()
    if DEMOTED:
        lines = [f"{label(k)}: son {n} işlemin {tp}'i TP" for k, tp, n in DEMOTED[:10]]
        send(f"⚠️ ONAYI DÜŞEN KOMBİNASYON ({len(DEMOTED)} adet)\n" + "\n".join(lines)
             + "\nBunlardan sinyal göndermeyi durdurdum ve bir daha taramayacağım.")
        DEMOTED.clear()


def track(snapshot, now):
    """Açık sanal işlemleri TP, SL ya da süre dolana kadar izler."""
    for t in PENDING[:]:
        if t not in PENDING:
            continue
        d = snapshot.get(t["sym"])
        if not d:
            continue
        up = (d[0] / t["entry"] - 1) * 100
        if up >= TP_PRICE:
            res, pnl = 1, TP_MARGIN - FEE_MARGIN
        elif up <= -SL_PRICE:
            res, pnl = 2, -SL_MARGIN - FEE_MARGIN
        elif now - t["t0"] >= TRACK_SEC:
            res, pnl = 3, up * LEVERAGE - FEE_MARGIN
        else:
            continue
        PENDING.remove(t)
        k = t["key"]
        s = STATS.setdefault(k, [0, 0, 0, 0, 0.0])
        s[0] += 1
        s[res] += 1
        s[4] += pnl
        lst = RESULTS.setdefault(k, [])
        lst.append([t["sym"], res])
        del lst[:-20]
        check_status(k)


def send_live(hits, now):
    """Onaylı kombinasyonlardan gelen sinyalleri coin başına tek mesajla gönderir."""
    by_sym = {}
    for sym, price, pct, mult, k in hits:
        if k in LIVE:
            by_sym.setdefault(sym, []).append((price, pct, mult, k))
    for sym, lst in by_sym.items():
        if now - LIVE_LAST.get(sym, 0) < COOLDOWN_MIN * 60:
            continue
        LIVE_LAST[sym] = now
        price, pct, mult, k = lst[0]
        res = RESULTS.get(k, [])[-10:]
        tp = sum(1 for r in res if r[1] == 1)
        extra = f"\n+ {len(lst) - 1} onaylı kombinasyon daha" if len(lst) > 1 else ""
        t = datetime.now(TZ).strftime("%H:%M:%S")
        send(
            f"🟢 SİNYAL {sym} (Bitget Vadeli, {LEVERAGE}x)\n"
            f"Saat: {t}\n"
            f"Fiyat: {fmt(price)}\n"
            f"Yükseliş: +{pct:.1f}%, hacim {mult:.1f}x\n"
            f"TP: {fmt(price * (1 + TP_PRICE / 100))} "
            f"(fiyatta +{TP_PRICE:.1f}% = margin +{TP_MARGIN:.0f}%)\n"
            f"SL: {fmt(price * (1 - SL_PRICE / 100))} "
            f"(fiyatta -{SL_PRICE:.1f}% = margin -{SL_MARGIN:.0f}%)\n"
            f"Kombinasyon: {label(k)} (son {len(res)} işlemin {tp}'i TP){extra}"
        )


def report():
    rows = []
    for k, (n, tp, sl, z, pnl) in STATS.items():
        if n >= MIN_TRADES:
            rows.append((pnl / n, k, n, tp, sl, z))
    rows.sort(reverse=True)
    total = sum(s[0] for s in STATS.values()) + META["base"]
    head = (f"🔬 ARAŞTIRMA RAPORU\n"
            f"TP +{TP_MARGIN:.0f}% / SL -{SL_MARGIN:.0f}% margin ({LEVERAGE}x)\n"
            f"Toplam sanal işlem: {total}, açık: {len(PENDING)}\n"
            f"Onaylı kombinasyon: {len(LIVE)}, yasaklanan: {len(BANNED)}"
            + (f" ({', '.join(label(k) for k in sorted(LIVE))})" if LIVE else "") + "\n")
    if not rows:
        send(head + f"Henüz hiçbir kombinasyon {MIN_TRADES} işleme ulaşmadı.")
        return
    lines = []
    for avg, k, n, tp, sl, z in rows[:TOP_N]:
        done = tp + sl
        rate = f"%{tp / done * 100:.0f}" if done else "-"
        lines.append(f"{label(k)}: {n} işlem, TP {tp}/SL {sl}/süre {z}, "
                     f"TP oranı {rate}, ort {avg:+.1f}%")
    send(head + f"En iyi {len(lines)} kombinasyon (işlem başı ort. kâr, margin %):\n"
         + "\n".join(lines))


def main():
    load_state()
    seed_state()
    save_state()
    status, snap = fetch_snapshot()
    if not snap:
        print("Borsa verisi alınamadı")
        return
    refresh_symbols(snap)
    last_refresh = last_report = last_save = time.time()
    combos = len(WINDOWS) * len(PCTS) * len(VOLS)
    send(f"🔬 Araştırma modu başladı: {combos} kombinasyon aynı anda test ediliyor.\n"
         f"TP: margin +{TP_MARGIN:.0f}% (fiyatta +{TP_PRICE:.1f}%), "
         f"SL: margin -{SL_MARGIN:.0f}% (fiyatta -{SL_PRICE:.1f}%), {LEVERAGE}x.\n"
         f"Bir kombinasyon üst üste {NEED} farklı coinde TP olursa seni uyarıp "
         f"o kombinasyondan sinyal göndereceğim.\n"
         f"Şu an onaylı kombinasyon: {len(LIVE)}")

    end = time.time() + LOOP_MINUTES * 60
    fails = 0
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
        now = time.time()
        track(snap, now)
        flush_alerts()
        send_live(process(snap, now), now)

        if now - last_report >= REPORT_MIN * 60:
            report()
            last_report = now
        if now - last_save >= SAVE_SEC:
            save_state()
            last_save = now
        if now - last_refresh > 1800:
            refresh_symbols(snap)
            last_refresh = now

        time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))
    report()
    save_state()
    print("süre doldu, çıkılıyor")


if __name__ == "__main__":
    main()
