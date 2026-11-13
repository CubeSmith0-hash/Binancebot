import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "330"))
POLL_SEC = 1

# ================= ANINDA BAŞLANGIÇ SİNYALİ (YENİ) =================
# Pump daha başlar başlamaz, kısa pencerede hacim patlaması + fiyat ivmesi.
START_ENABLED = True
# (pencere_sn, min_yükseliş_%, min_hacim_katı, min_pencere_hacmi_USDT)
START_TIERS = [
    (15, 1.2, 8.0, 10_000),    # çok hızlı: 15 sn'de %1.2 + 8x hacim
    (30, 2.0, 6.0, 20_000),    # hızlı:     30 sn'de %2   + 6x hacim
    (60, 3.0, 4.0, 30_000),    # normal:    60 sn'de %3   + 4x hacim
]
START_MAX_PCT = 6.0        # pencerede bundan fazla yükselmişse geç kalınmış, atla
START_PULLBACK_PCT = 0.4   # fiyat pencere tepesinden en fazla % bu kadar düşmüş olabilir
START_UP_TICKS = 2         # son N saniyede fiyat düşmemiş olmalı (ivme devam ediyor)
START_BASE_MIN_SEC = 600   # normal hacim hesabı için en az bu kadar geçmiş (10 dk)
START_BASE_GAP_SEC = 90    # baz hesabında son 90 sn dışarıda tutulur
START_BASE_FLOOR = 0.3     # baz hacim, 24s ortalamanın en az %30'u sayılır
START_COOLDOWN_MIN = 10    # aynı coin için tekrar başlangıç sinyali (dk)
START_LOG_FILE = "start_log.jsonl"
# açık pozisyon (OI) = gerçek para girişi göstergesi
START_OI_WINDOW_SEC = 45   # OI değişimine bakılan süre
START_OI_PCT = 0.5         # bu kadar % artmışsa "güçlü" sinyal (💰)
START_REQUIRE_OI = True    # fiyat + hacim + OI aynı anda artmalı (False: OI'siz de sinyal gelir)

# ---- PUMP sinyali (pump başladıktan sonra, geç ama teyitli) ----
WINDOW_SEC = 120
MIN_PCT = 8.0
VOL_MULT = 3.0             # %300 (10 idi)
MIN_WINDOW_VOL = 30_000    # 5K çok düşüktü
MIN_24H_VOL = 500_000      # 50K çok düşüktü (gürültü)
COOLDOWN_MIN = 5
SUPPRESS_PUMP_AFTER_START = True   # başlangıç sinyali verildiyse geç pump sinyalini gönderme

# ---- ERKEN UYARI ----
EARLY_ENABLED = True
EARLY_CHECK_SEC = 5
EARLY_WINDOW_SEC = 300
EARLY_FLAT_PCT = 1.0
EARLY_VOL_MULT = 8.0
EARLY_OI_PCT = 5.0
EARLY_FUNDING = -0.0005
SQUEEZE_SEC = 1200
SQUEEZE_RANGE_PCT = 2.5
BREAKOUT_PCT = 0.3
SQUEEZE_VOL_MULT = 3.0
EARLY_MAX_MOVE_PCT = 3.0
EARLY_MIN_SCORE = 3
EARLY_COOLDOWN_MIN = 30
EARLY_LOG_FILE = "erken_log.jsonl"

HISTORY_SEC = max(WINDOW_SEC, EARLY_WINDOW_SEC, 60) + 30
COARSE_STEP = 10
SLOW_SEC = 1800            # normal hacim tabanı için 30 dk geçmiş
TZ = timezone(timedelta(hours=3))
STATE_FILE = "canli_state.json"

HOST = "https://api.bitget.com"
TICKER_PATH = "/api/v2/mix/market/tickers"
TICKER_PARAMS = {"productType": "USDT-FUTURES"}

STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
    "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD",
}
FIAT = {"BRL", "JPY", "CNH", "CNY", "EUR", "GBP", "TRY", "RUB", "INR",
        "KRW", "MXN", "CAD", "AUD", "CHF", "SGD", "IDR", "NGN", "ARS"}
STOCKS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
    "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
    "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
    "DELL", "IBM", "NOW", "CRM", "IREN", "BX", "HPE", "CRWD",
}
NON_CRYPTO = {"PAXG", "XAUT"}

HISTORY = {}      # sembol -> deque[(zaman, fiyat, 24s_hacim, açık_pozisyon)]
COARSE = {}       # sembol -> deque[(zaman, fiyat)]
SLOW = {}         # sembol -> deque[(zaman, 24s_hacim)] (10 sn aralıklı, 30 dk)
LAST_ALERT = {}   # "sembol" pump, "E:sembol" erken, "S:sembol" başlangıç
WATCH = set()
EARLY_T = [0.0]


def keep(sym):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in STABLES or base in NON_CRYPTO or base in STOCKS:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    if base.startswith("USD") and len(base) == 6 and base[3:] in FIAT:
        return False   # USDBRL, USDJPY gibi döviz pariteleri
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
            limit = time.time() - max(COOLDOWN_MIN, EARLY_COOLDOWN_MIN,
                                      START_COOLDOWN_MIN) * 60
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


def window(hist, now, sec):
    """Son 'sec' saniyedeki noktalar (eskiden yeniye)."""
    out = []
    for p in reversed(hist):
        if p[0] < now - sec:
            break
        out.append(p)
    out.reverse()
    return out


# ------------------------------------------------- ANINDA BAŞLANGIÇ (YENİ)

def base_rate(slow, now, vol24):
    """Normal hacim akışı (USDT/sn): son 30 dk, son 90 sn hariç."""
    day_rate = vol24 / 86400.0
    floor = day_rate * START_BASE_FLOOR
    if len(slow) < 2 or now - slow[0][0] < START_BASE_MIN_SEC:
        return max(day_rate, 0.0)   # yeterli geçmiş yok, 24s ortalaması
    old = slow[0]
    recent = None
    for p in reversed(slow):
        if p[0] <= now - START_BASE_GAP_SEC:
            recent = p
            break
    if not recent or recent[0] <= old[0]:
        return max(day_rate, 0.0)
    rate = (recent[1] - old[1]) / (recent[0] - old[0])
    return max(rate, floor)


def oi_change(hist, now):
    """Son START_OI_WINDOW_SEC içinde açık pozisyon % değişimi (yoksa None)."""
    pts = window(hist, now, START_OI_WINDOW_SEC)
    if len(pts) < 3 or now - pts[0][0] < START_OI_WINDOW_SEC * 0.7:
        return None
    o0, o1 = pts[0][3], pts[-1][3]
    if not o0 or not o1 or o0 <= 0:
        return None
    return (o1 / o0 - 1) * 100


def start_eval(hist, slow, now, vol24):
    last_t, last_p, last_q = hist[-1][0], hist[-1][1], hist[-1][2]
    rate = base_rate(slow, now, vol24)
    if rate <= 0:
        return None

    # ivme: son START_UP_TICKS saniyede fiyat düşmemiş olmalı
    recent = window(hist, now, START_UP_TICKS + 1)
    if len(recent) >= 2 and recent[-1][1] < recent[0][1]:
        return None

    best = None
    for sec, min_pct, min_mult, min_vol in START_TIERS:
        pts = window(hist, now, sec)
        if len(pts) < 3 or now - pts[0][0] < sec * 0.7:
            continue
        low = min(p[1] for p in pts)
        high = max(p[1] for p in pts)
        if low <= 0 or last_p <= low:
            continue
        pct = (last_p / low - 1) * 100
        if pct < min_pct or pct > START_MAX_PCT:
            continue
        if (high / last_p - 1) * 100 > START_PULLBACK_PCT:
            continue   # tepeden dönmüş
        vol = last_q - pts[0][2]
        if vol < min_vol:
            continue
        span = max(now - pts[0][0], 1.0)
        mult = (vol / span) / rate
        if mult < min_mult:
            continue
        cand = (sec, last_p, pct, vol, mult)
        if best is None or mult > best[4]:
            best = cand
    if best is None:
        return None
    oi = oi_change(hist, now)
    if START_REQUIRE_OI and (oi is None or oi < START_OI_PCT):
        return None
    return best + (oi,)


def alert_start(sym, vol24, sec, price, pct, vol, mult, oi):
    LAST_ALERT["S:" + sym] = time.time()
    save_state()
    t = datetime.now(TZ).strftime("%H:%M:%S")
    if oi is None:
        oi_line = "Açık pozisyon: veri yok"
        head = "⚡ ANLIK PUMP"
    elif oi >= START_OI_PCT:
        oi_line = f"💰 Açık pozisyon {START_OI_WINDOW_SEC} sn: {oi:+.2f}% (para girişi VAR)"
        head = "⚡ ANLIK PUMP"
    else:
        oi_line = f"Açık pozisyon {START_OI_WINDOW_SEC} sn: {oi:+.2f}% (para girişi yok, zayıf)"
        head = "⚡ ANLIK PUMP"
    send(
        f"{head} {sym} (Bitget Vadeli)\n"
        f"Saat: {t}\n"
        f"Fiyat: {fmt(price)}\n"
        f"Son {sec} sn: +{pct:.2f}%\n"
        f"Hacim: {fmt_vol(vol)} USDT ({mult:.1f}x normal)\n"
        f"{oi_line}\n"
        f"24s hacim: {fmt_vol(vol24)}"
    )
    try:
        with open(START_LOG_FILE, "a") as f:
            f.write(json.dumps({
                "t": int(time.time()), "sym": sym, "price": price,
                "sec": sec, "pct": round(pct, 3), "vol": round(vol),
                "mult": round(mult, 2),
                "oi": None if oi is None else round(oi, 3),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ PUMP

def evaluate(hist, now):
    pts = [p for p in hist if p[0] >= now - WINDOW_SEC]
    if len(pts) < 3 or now - pts[0][0] < WINDOW_SEC * 0.6:
        return None
    low = min(p[1] for p in pts)
    last_p, last_q = pts[-1][1], pts[-1][2]
    if low <= 0 or last_p <= low:
        return None
    pct = (last_p / low - 1) * 100
    if pct < MIN_PCT:
        return None
    vol = last_q - pts[0][2]
    if vol < MIN_WINDOW_VOL:
        return None
    normal = last_q / 1440 * (WINDOW_SEC / 60)
    if normal <= 0:
        return None
    mult = vol / normal
    if mult < VOL_MULT:
        return None
    return last_p, pct, vol, mult


def process(snapshot, now):
    """Geçmişi günceller. (pump_sinyalleri, başlangıç_sinyalleri) döner."""
    pumps, starts = [], []
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

        s = SLOW.setdefault(sym, deque())
        if not s or now - s[-1][0] >= COARSE_STEP:
            s.append((now, d[1]))
            while s and s[0][0] < now - SLOW_SEC:
                s.popleft()

        if START_ENABLED:
            ls = LAST_ALERT.get("S:" + sym)
            if not (ls and now - ls < START_COOLDOWN_MIN * 60):
                res = start_eval(h, s, now, d[1])
                if res:
                    starts.append((sym, d[1]) + res)

        res = evaluate(h, now)
        if res:
            last = LAST_ALERT.get(sym)
            if last and now - last < COOLDOWN_MIN * 60:
                continue
            ls = LAST_ALERT.get("S:" + sym)
            if (SUPPRESS_PUMP_AFTER_START and ls
                    and now - ls < START_COOLDOWN_MIN * 60):
                continue
            pumps.append((sym, d[1]) + res)
    return pumps, starts


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
            return None

        vol = last_q - pts[0][2]
        normal = last_q / 1440 * (EARLY_WINDOW_SEC / 60)
        if normal > 0:
            mult = vol / normal

        if (abs(move) <= EARLY_FLAT_PCT and vol >= MIN_WINDOW_VOL
                and mult >= EARLY_VOL_MULT):
            flags.append(
                f"Hacim {mult:.1f}x arttı, fiyat sakin ({move:+.2f}%)"
            )

        o0, o1 = pts[0][3], pts[-1][3]
        if o0 and o1 and o0 > 0:
            oi_chg = (o1 / o0 - 1) * 100
            if oi_chg >= EARLY_OI_PCT and move < MIN_PCT:
                flags.append(
                    f"Açık pozisyon {EARLY_WINDOW_SEC // 60} dk'da +{oi_chg:.1f}%"
                )

    if funding is not None and funding <= EARLY_FUNDING:
        flags.append(f"Funding negatif: {funding * 100:.3f}%")

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
            continue
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
    save_state()

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
    print(f"canlı tarama başladı: {POLL_SEC} sn aralık", flush=True)
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
        pumps, starts = process(snap, now)

        # önce en hızlı sinyal: başlangıç
        for sig in sorted(starts, key=lambda x: -x[6]):
            try:
                alert_start(*sig)
            except Exception as e:
                print("başlangıç sinyal hatası", e)

        for sig in sorted(pumps, key=lambda x: -x[3]):
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
