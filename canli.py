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

# ---- PUMP sinyali (pump başladıktan sonra) ----
WINDOW_SEC = 120           # son kaç saniyeye bakılsın (2 dk)
MIN_PCT = 8.0              # pencere içindeki dipten en az % kaç yükseliş
VOL_MULT = 10.0            # pencere hacmi, normal hacmin en az kaç katı olmalı
MIN_WINDOW_VOL = 5_000     # pencerede en az bu kadar USDT hacim olmalı
MIN_24H_VOL = 50_000       # 24 saatlik USDT hacmi bunun altındaki coinler taranmaz
COOLDOWN_MIN = 5           # aynı coin için tekrar pump sinyali bekleme süresi (dk)

# ---- ERKEN UYARI (pump başlamadan önce) ----
EARLY_ENABLED = True
EARLY_CHECK_SEC = 5        # erken uyarı kontrol sıklığı (sn)
EARLY_WINDOW_SEC = 300     # hacim ve açık pozisyon değişimine bakılan süre (5 dk)
EARLY_FLAT_PCT = 1.0       # fiyat bu pencerede en fazla % kaç oynamış olmalı (sakin)
EARLY_VOL_MULT = 8.0       # (A) pencere hacmi normalin en az kaç katı
EARLY_OI_PCT = 2.0         # (B) açık pozisyon pencerede en az % kaç artmış
EARLY_FUNDING = -0.0005    # (C) funding bu değerin altındaysa negatif sayılır (-0.05%)
SQUEEZE_SEC = 1200         # (D) sıkışma için bakılan süre (20 dk)
SQUEEZE_RANGE_PCT = 2.5    # (D) bu sürede bant genişliği en fazla %
BREAKOUT_PCT = 0.3         # (D) bandın üstünü en az % kaç aşmalı
SQUEEZE_VOL_MULT = 3.0     # (D) kırılımda hacim en az kaç kat
EARLY_MAX_MOVE_PCT = 3.0   # pencerede bundan fazla yükselmişse zaten geç, erken uyarı verme
EARLY_MIN_SCORE = 2        # kaç işaret aynı anda olunca uyarı gelsin (1-4)
EARLY_COOLDOWN_MIN = 30    # aynı coin için tekrar erken uyarı bekleme süresi (dk)
EARLY_LOG_FILE = "erken_log.jsonl"   # erken uyarıların kaydı (sonradan ölçmek için)

HISTORY_SEC = max(WINDOW_SEC, EARLY_WINDOW_SEC) + 30
COARSE_STEP = 10           # sıkışma için fiyat örnekleme aralığı (sn)
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

HISTORY = {}      # sembol -> deque[(zaman, fiyat, 24s_usdt_hacim, açık_pozisyon)]
COARSE = {}       # sembol -> deque[(zaman, fiyat)] (10 sn aralıklı)
LAST_ALERT = {}   # sembol -> son sinyal zamanı ("E:sembol" -> erken uyarı)
WATCH = set()     # taranacak semboller
EARLY_T = [0.0]   # son erken uyarı kontrol zamanı


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
            limit = time.time() - max(COOLDOWN_MIN, EARLY_COOLDOWN_MIN) * 60
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


def to_float(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_snapshot():
    """(durum_kodu, {sembol: (fiyat, 24s_usdt_hacim, funding, açık_pozisyon)})."""
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
            out[t["symbol"]] = (
                float(t["lastPr"]),
                float(vol),
                to_float(t.get("fundingRate")),
                to_float(t.get("holdingAmount")),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return 200, out


def refresh_symbols(snap):
    WATCH.clear()
    for sym, d in snap.items():
        if keep(sym) and d[1] >= MIN_24H_VOL:
            WATCH.add(sym)
    print(f"{len(WATCH)} Bitget vadeli coin izleniyor")


# ------------------------------------------------------------------ PUMP

def evaluate(hist, now):
    pts = [p for p in hist if p[0] >= now - WINDOW_SEC]
    if len(pts) < 3 or now - pts[0][0] < WINDOW_SEC * 0.6:
        return None   # henüz yeterli geçmiş yok
    low = min(p[1] for p in pts)
    last_p, last_q = pts[-1][1], pts[-1][2]
    if low <= 0 or last_p <= low:
        return None
    pct = (last_p / low - 1) * 100
    if pct < MIN_PCT:
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
        h.append((now, d[0], d[1], d[3]))
        while h and h[0][0] < now - HISTORY_SEC:
            h.popleft()

        c = COARSE.setdefault(sym, deque())
        if not c or now - c[-1][0] >= COARSE_STEP:
            c.append((now, d[0]))
            while c and c[0][0] < now - SQUEEZE_SEC - 60:
                c.popleft()

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


# ------------------------------------------------------------ ERKEN UYARI

def early_eval(hist, now, funding, coarse):
    """Pump başlamadan önceki işaretleri kontrol eder.
    Döner: (işaret_listesi, fiyat) ya da None."""
    flags = []
    price = hist[-1][1]
    move = 0.0
    mult = 0.0

    pts = [p for p in hist if p[0] >= now - EARLY_WINDOW_SEC]
    if len(pts) >= 3 and now - pts[0][0] >= EARLY_WINDOW_SEC * 0.8:
        first_p, last_q = pts[0][1], pts[-1][2]
        if first_p > 0:
            move = (price / first_p - 1) * 100
        if move > EARLY_MAX_MOVE_PCT:
            return None   # zaten yükselmiş, erken değil

        vol = last_q - pts[0][2]
        normal = last_q / 1440 * (EARLY_WINDOW_SEC / 60)
        if normal > 0:
            mult = vol / normal

        # (A) fiyat sakinken hacim patlaması
        if (abs(move) <= EARLY_FLAT_PCT and vol >= MIN_WINDOW_VOL
                and mult >= EARLY_VOL_MULT):
            flags.append(
                f"Hacim {mult:.1f}x arttı, fiyat sakin ({move:+.2f}%)"
            )

        # (B) açık pozisyon artışı (fiyat henüz uçmamışken)
        o0, o1 = pts[0][3], pts[-1][3]
        if o0 and o1 and o0 > 0:
            oi_chg = (o1 / o0 - 1) * 100
            if oi_chg >= EARLY_OI_PCT and move < MIN_PCT:
                flags.append(
                    f"Açık pozisyon {EARLY_WINDOW_SEC // 60} dk'da +{oi_chg:.1f}%"
                )

    # (C) negatif funding (short kalabalık, squeeze ihtimali)
    if funding is not None and funding <= EARLY_FUNDING:
        flags.append(f"Funding negatif: {funding * 100:.3f}%")

    # (D) sıkışma sonrası yukarı kırılım
    if coarse:
        base = [p for t, p in coarse
                if now - SQUEEZE_SEC <= t < now - 60]
        need = (SQUEEZE_SEC - 60) / COARSE_STEP * 0.7
        if len(base) >= need:
            hi, lo = max(base), min(base)
            if lo > 0:
                rng = (hi / lo - 1) * 100
                if (rng <= SQUEEZE_RANGE_PCT
                        and price > hi * (1 + BREAKOUT_PCT / 100)
                        and mult >= SQUEEZE_VOL_MULT):
                    flags.append(
                        f"Dar bant ({rng:.1f}%) yukarı kırıldı, hacim {mult:.1f}x"
                    )

    if len(flags) < EARLY_MIN_SCORE:
        return None
    return flags, price


def process_early(snapshot, now):
    out = []
    for sym in WATCH:
        d = snapshot.get(sym)
        h = HISTORY.get(sym)
        if not d or not h:
            continue
        last = LAST_ALERT.get("E:" + sym)
        if last and now - last < EARLY_COOLDOWN_MIN * 60:
            continue
        lastp = LAST_ALERT.get(sym)
        if lastp and now - lastp < COOLDOWN_MIN * 60:
            continue   # zaten pump sinyali verildi
        res = early_eval(h, now, d[2], COARSE.get(sym))
        if res:
            out.append((sym, d[1], res[1], res[0]))
    return out


def alert_early(sym, vol24, price, flags):
    LAST_ALERT["E:" + sym] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    lines = "\n".join(f"• {f}" for f in flags)
    send(
        f"⚠️ ERKEN UYARI {sym} (Bitget Vadeli)\n"
        f"Saat: {t}\n"
        f"Fiyat: {fmt(price)}\n"
        f"İşaretler ({len(flags)}):\n{lines}\n"
        f"24s hacim: {fmt_vol(vol24)}"
    )
    try:
        with open(EARLY_LOG_FILE, "a") as f:
            f.write(json.dumps({
                "t": int(time.time()), "sym": sym,
                "price": price, "flags": flags,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main():
    LAST_ALERT.update(load_state())
    save_state()   # dosya her durumda oluşsun

    status, snap = fetch_snapshot()
    if not snap:
        print("Borsa verisi alınamadı")
        return
    refresh_symbols(snap)
    last_refresh = time.time()

    n_oi = sum(1 for d in snap.values() if d[3] is not None)
    n_fr = sum(1 for d in snap.values() if d[2] is not None)
    print(f"açık pozisyon verisi: {n_oi} coin, funding verisi: {n_fr} coin")
    if EARLY_ENABLED and (n_oi == 0 or n_fr == 0):
        print("UYARI: tickers verisinde holdingAmount/fundingRate yok, "
              "ilgili işaretler çalışmayacak")

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

        now = time.time()
        for sig in sorted(process(snap, now), key=lambda x: -x[3]):
            try:
                alert(*sig)
            except Exception as e:
                print("sinyal hatası", e)

        if EARLY_ENABLED and now - EARLY_T[0] >= EARLY_CHECK_SEC:
            EARLY_T[0] = now
            try:
                for e in process_early(snap, now):
                    alert_early(*e)
            except Exception as e:
                print("erken uyarı hatası", e)

        if time.time() - last_refresh > 1800:
            refresh_symbols(snap)
            last_refresh = time.time()

        time.sleep(max(0.0, POLL_SEC - (time.time() - t0)))
    print("süre doldu, çıkılıyor")


if __name__ == "__main__":
    main()
        
