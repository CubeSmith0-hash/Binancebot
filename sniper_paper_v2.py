"""
Sniper (PAPER TRADING) v2 - 3 saatlik oturum.
Sanal bakiye 10$, islem basi 1$, en fazla 100 islem (kasa kapanan islemlerle geri doner).
Yenilikler: RugCheck filtresi, ayni token tekrar alinmaz, yuksek slippage varsayimi,
likidite dususu 2 kez dogrulanir, raporda medyan / kazanc orani / en iyi islem haric ortalama.
Gercek para / private key KULLANMAZ.
"""
import csv
import os
import statistics
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))

# ---------------- AYARLAR ----------------
NETWORKS = ["solana"]
POLL_SEC = 5
REQ_GAP = 10
INTERIM_MIN = 60            # her 60 dk'da ara rapor

START_BALANCE = 10.0
TRADE_USD = 1.0
MAX_TRADES = 100
MAX_OPEN = 10

# Giris filtresi
MIN_AGE_MIN = 0
MAX_AGE_MIN = 10
MIN_LIQ = 5_000
MIN_M5_VOL = 1_500
MIN_BUYERS = 5
MIN_BUY_RATIO = 0.55
MAX_VOL_LIQ = 60.0
MAX_FDV = 5_000_000

# RugCheck (Solana, anahtar gerekmez)
USE_RUGCHECK = True
RC_MAX_SCORE = 50           # score_normalised bunun ustundeyse alma (yuksek = riskli)
RC_IGNORE = ("liquidity", "lp provider")   # yeni tokenda her zaman cikan, elemeyecegimiz uyarilar
RC_GAP = 3                  # RugCheck istekleri arasi sn
RC_MAX_TRIES = 3            # rapor henuz yoksa en fazla kac kez tekrar denensin

# Cikis kurallari
FEE_PCT = 4.0               # alim ve satim basina ucret + slippage varsayimi (%)
LIQ_EXIT_EXTRA_SLIP = 10.0  # likidite dususunde satis icin ek kayip (%)
TRAIL_PCT = 15.0
SELL_RATIO_EXIT = 0.60
MIN_TX_FOR_PRESSURE = 6
LIQ_DROP_EXIT = 30.0        # ilk takip olcumune gore, ust uste 2 olcumde dogrulanirsa
NO_DATA_MIN = 15
# -----------------------------------------

TZ = timezone(timedelta(hours=3))
API = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302"}
RC_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
LOG_FILE = "paper_log.csv"

OPEN = {}
SEEN = set()
TRADED_MINTS = set()
RC_TRIES = {}
RESULTS = []
STATE = {"cash": START_BALANCE, "opened": 0, "rejected": 0}


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def fmt_price(p):
    return f"{p:.4f}" if p >= 1 else f"{p:.10f}".rstrip("0").rstrip(".")


def send(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return
        except requests.RequestException:
            time.sleep(2)


def log_row(row):
    new = not os.path.exists(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["kapanis", "ag", "isim", "adres", "giris_fiyat", "cikis_fiyat",
                            "net_getiri_%", "max_getiri_%", "min_getiri_%", "sebep", "sure_dk"])
            w.writerow(row)
    except OSError as e:
        print("log hatasi", e)


def get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print("hata", e)
        return 0, []
    if r.status_code != 200:
        print("HTTP", r.status_code, url)
        return r.status_code, []
    try:
        return 200, r.json().get("data", [])
    except ValueError:
        return 0, []


def rugcheck(mint):
    """(True, ozet) guvenli | (False, sebep) riskli | (None, sebep) belirsiz."""
    try:
        r = requests.get(RC_URL.format(mint), timeout=15)
    except requests.RequestException:
        return None, "baglanti"
    if r.status_code == 429:
        time.sleep(10)
        return None, "429"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except ValueError:
        return None, "json"
    if d.get("rugged"):
        return False, "rugged"
    score = d.get("score_normalised")
    if isinstance(score, (int, float)) and score > RC_MAX_SCORE:
        return False, f"risk skoru {score}"
    for risk in d.get("risks") or []:
        name = str(risk.get("name", ""))
        if str(risk.get("level", "")).lower() == "danger" and not any(w in name.lower() for w in RC_IGNORE):
            return False, name
    return True, f"skor {score}"


def entry_ok(p):
    a = p.get("attributes", {})
    pid = p.get("id", "")
    if "_" not in pid or pid in SEEN or pid in OPEN:
        return None
    base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
    mint = base.split("_", 1)[1] if "_" in base else ""
    if not mint or mint in TRADED_MINTS:
        return None
    vol = a.get("volume_usd", {}) or {}
    tx5 = (a.get("transactions", {}) or {}).get("m5", {}) or {}
    liq, v5, v24 = f(a.get("reserve_in_usd")), f(vol.get("m5")), f(vol.get("h24"))
    fdv = f(a.get("fdv_usd"))
    price = f(a.get("base_token_price_usd"))
    if price <= 0 or liq < MIN_LIQ or v5 < MIN_M5_VOL:
        return None
    if liq > 0 and v24 / liq > MAX_VOL_LIQ:
        return None
    if fdv and fdv > MAX_FDV:
        return None
    buys, sells, buyers = f(tx5.get("buys")), f(tx5.get("sells")), f(tx5.get("buyers"))
    tot = buys + sells
    if tot <= 0 or buys / tot < MIN_BUY_RATIO or buyers < MIN_BUYERS:
        return None
    try:
        dt = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except (KeyError, ValueError, AttributeError):
        return None
    if age < MIN_AGE_MIN or age > MAX_AGE_MIN:
        return None
    net, addr = pid.split("_", 1)
    return {"id": pid, "net": net, "addr": addr, "mint": mint, "name": a.get("name", "?"),
            "price": price, "liq": liq, "v5": v5, "buyers": int(buyers),
            "ratio": buys / tot, "fdv": fdv, "age": age}


def open_trade(s, rc_note):
    STATE["opened"] += 1
    STATE["cash"] -= TRADE_USD
    TRADED_MINTS.add(s["mint"])
    OPEN[s["id"]] = {"net": s["net"], "addr": s["addr"], "name": s["name"],
                     "entry": s["price"], "liq": None, "liq_strikes": 0,
                     "ts": time.time(), "seen": time.time(),
                     "max": s["price"], "min": s["price"]}
    send(
        f"🎯 SANAL ALIM #{STATE['opened']}/{MAX_TRADES} {s['name']} ({s['net']})\n"
        f"Giris: ${fmt_price(s['price'])} | {TRADE_USD:.0f}$ (sanal)\n"
        f"Yas: {s['age']:.0f} dk | Likidite: {s['liq']/1e3:.0f}K$ | FDV: {s['fdv']/1e3:.0f}K$\n"
        f"5dk hacim: {s['v5']/1e3:.1f}K$ | Alici: {s['buyers']} | Alim: %{s['ratio']*100:.0f}\n"
        f"RugCheck: {rc_note}\n"
        f"Kasa: {STATE['cash']:.2f}$\n"
        f"https://www.geckoterminal.com/{s['net']}/pools/{s['addr']}\n"
        f"https://rugcheck.xyz/tokens/{s['mint']}"
    )


def close_trade(pid, price, reason):
    t = OPEN.pop(pid)
    if price > 0 and reason == "LIKIDITE_DUSTU":
        price *= 1 - LIQ_EXIT_EXTRA_SLIP / 100
    if price <= 0:
        net = -100.0
    else:
        gross = price / t["entry"] - 1
        net = ((1 + gross) * (1 - FEE_PCT / 100) / (1 + FEE_PCT / 100) - 1) * 100
    proceeds = TRADE_USD * (1 + net / 100)
    STATE["cash"] += proceeds
    mx = (t["max"] / t["entry"] - 1) * 100
    mn = (t["min"] / t["entry"] - 1) * 100
    mins = (time.time() - t["ts"]) / 60
    RESULTS.append({"name": t["name"], "net": net, "reason": reason, "mins": mins})
    log_row([datetime.now(TZ).isoformat(timespec="seconds"), t["net"], t["name"], t["addr"],
             t["entry"], price, round(net, 2), round(mx, 2), round(mn, 2), reason, round(mins, 1)])
    send(f"{'✅' if net > 0 else '❌'} SANAL SATIS {t['name']} ({reason})\n"
         f"Net getiri: {net:+.1f}% ({proceeds:.2f}$)\n"
         f"Tepe: {mx:+.0f}% | Dip: {mn:+.0f}% | Sure: {mins:.0f} dk\n"
         f"Kasa: {STATE['cash']:.2f}$")


def exit_reason(t, price, attrs):
    tx5 = (attrs.get("transactions", {}) or {}).get("m5", {}) or {}
    buys, sells = f(tx5.get("buys")), f(tx5.get("sells"))
    tot = buys + sells
    liq = f(attrs.get("reserve_in_usd"))
    if liq > 0:
        if t["liq"] is None:
            t["liq"] = liq            # baslangic: ilk takip olcumu (ayni endpoint, tutarli)
        elif (1 - liq / t["liq"]) * 100 >= LIQ_DROP_EXIT:
            t["liq_strikes"] += 1
        else:
            t["liq_strikes"] = 0
        if t["liq_strikes"] >= 2:
            return "LIKIDITE_DUSTU"
    if (1 - price / t["max"]) * 100 >= TRAIL_PCT:
        return "ZIRVEDEN_DUSUS"
    if tot >= MIN_TX_FOR_PRESSURE and sells / tot >= SELL_RATIO_EXIT:
        return "SATIS_BASKISI"
    return None


def track_open(force=False):
    by_net = {}
    for pid, t in OPEN.items():
        by_net.setdefault(t["net"], []).append(t["addr"])
    for net, addrs in by_net.items():
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            st, data = get(f"{API}/networks/{net}/pools/multi/{','.join(chunk)}")
            time.sleep(REQ_GAP)
            if st == 429:
                time.sleep(30)
                continue
            pools = {d.get("id"): d["attributes"] for d in data if d.get("attributes")}
            for pid in [p for p in list(OPEN) if p in pools]:
                attrs, t = pools[pid], OPEN[pid]
                price = f(attrs.get("base_token_price_usd"))
                if price <= 0:
                    continue
                t["seen"] = time.time()
                t["max"], t["min"] = max(t["max"], price), min(t["min"], price)
                reason = exit_reason(t, price, attrs)
                if reason:
                    close_trade(pid, price, reason)
                elif force:
                    close_trade(pid, price, "OTURUM_BITTI")
    for pid in [p for p, t in OPEN.items() if time.time() - t["seen"] > NO_DATA_MIN * 60]:
        close_trade(pid, 0, "VERI_YOK_SIFIR")


def scan_new():
    rl = False
    for net in NETWORKS:
        st, pools = get(f"{API}/networks/{net}/new_pools")
        time.sleep(REQ_GAP)
        if st == 429:
            rl = True
            time.sleep(30)
            continue
        for p in pools:
            if STATE["opened"] >= MAX_TRADES or len(OPEN) >= MAX_OPEN or STATE["cash"] < TRADE_USD:
                break
            s = entry_ok(p)
            if not s:
                continue
            note = "kapali"
            if USE_RUGCHECK:
                ok, why = rugcheck(s["mint"])
                time.sleep(RC_GAP)
                if ok is None:                      # rapor henuz yok: sonra tekrar dene
                    RC_TRIES[s["id"]] = RC_TRIES.get(s["id"], 0) + 1
                    if RC_TRIES[s["id"]] >= RC_MAX_TRIES:
                        SEEN.add(s["id"])
                    continue
                if not ok:
                    SEEN.add(s["id"])
                    STATE["rejected"] += 1
                    print(f"RugCheck reddetti: {s['name']} ({why})", flush=True)
                    continue
                note = why
            SEEN.add(s["id"])
            open_trade(s, note)
    return rl


def report(title="SANAL SNIPER RAPORU", elapsed=None):
    n = len(RESULTS)
    rets = [r["net"] for r in RESULTS]
    wins = sum(1 for x in rets if x > 0)
    by = {}
    for r in RESULTS:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
    equity = STATE["cash"] + TRADE_USD * len(OPEN)   # acik pozisyonlar giris degerinden
    pnl = equity - START_BALANCE
    stat = ""
    if n:
        avg = sum(rets) / n
        med = statistics.median(rets)
        ex_best = (sum(rets) - max(rets)) / (n - 1) if n > 1 else 0.0
        stat = (f"Kazanc orani: %{wins / n * 100:.0f} | Ortalama: {avg:+.1f}% | Medyan: {med:+.1f}%\n"
                f"En iyi islem haric ortalama: {ex_best:+.1f}%\n")
    worst = sorted(RESULTS, key=lambda r: r["net"])[:3]
    best = sorted(RESULTS, key=lambda r: -r["net"])[:3]
    fmt = lambda r: f"{r['name']}: {r['net']:+.0f}% ({r['reason']}, {r['mins']:.0f} dk)"
    send(
        f"📊 {title}" + (f" ({elapsed:.0f} dk)" if elapsed else "") + "\n"
        f"Baslangic: {START_BALANCE:.2f}$ -> Su an: {equity:.2f}$ ({pnl:+.2f}$, {pnl / START_BALANCE * 100:+.1f}%)\n"
        f"Islem: {n} (acik: {len(OPEN)}) | Kazanan: {wins} | Kaybeden: {n - wins} | RugCheck red: {STATE['rejected']}\n"
        + stat
        + "Cikis nedenleri: " + (", ".join(f"{k}={v}" for k, v in by.items()) or "-") + "\n"
        + ("\nEn iyi 3: " + " | ".join(fmt(r) for r in best) if best else "")
        + ("\nEn kotu 3: " + " | ".join(fmt(r) for r in worst) if worst else "")
        + "\n\n⚠️ Sanal sonuc. Gercekte gecikme, slippage ve honeypot riski daha yuksek."
    )


def main():
    t_start = time.time()
    end = t_start + RUN_MINUTES * 60
    next_interim = t_start + INTERIM_MIN * 60
    send(f"🚀 Sanal sniper v2 basladi: {START_BALANCE:.0f}$ kasa, islem basi {TRADE_USD:.0f}$, "
         f"max {MAX_TRADES} islem, {RUN_MINUTES} dk, RugCheck: {'acik' if USE_RUGCHECK else 'kapali'}.")
    backoff = 0
    while time.time() < end:
        t0 = time.time()
        rl = False
        if STATE["opened"] < MAX_TRADES:
            rl = scan_new()
        if OPEN:
            track_open()
        if time.time() >= next_interim and time.time() < end - 300:
            report("ARA RAPOR", (time.time() - t_start) / 60)
            next_interim += INTERIM_MIN * 60
        print(f"acik: {len(OPEN)} | alim: {STATE['opened']}/{MAX_TRADES} | kasa: {STATE['cash']:.2f}", flush=True)
        backoff = min(backoff + 30, 60) if rl else 0
        time.sleep(max(0.0, POLL_SEC + backoff - (time.time() - t0)))
    if OPEN:
        track_open(force=True)
    for pid in list(OPEN):
        close_trade(pid, 0, "VERI_YOK_SIFIR")
    report("SANAL SNIPER RAPORU", (time.time() - t_start) / 60)


if __name__ == "__main__":
    main()
                                        
