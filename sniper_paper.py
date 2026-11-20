"""
Sniper (PAPER TRADING) - 1 saatlik oturum.
Sanal bakiye 10$, her islem 1$, en fazla 10 islem.
60 dk sonra (ya da 10 islem bitince) acik pozisyonlari kapatir, Telegram'a rapor atar.
Gercek para / private key KULLANMAZ.
"""
import csv
import os
import time
from datetime import datetime, timezone, timedelta

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "60"))

# ---------------- AYARLAR ----------------
NETWORKS = ["solana"]
POLL_SEC = 5
REQ_GAP = 10

START_BALANCE = 10.0        # sanal baslangic bakiyesi ($)
TRADE_USD = 1.0             # her islem ($)
MAX_TRADES = 10             # oturum boyunca toplam alim sayisi
MAX_OPEN = 10               # ayni anda acik pozisyon

# Giris filtresi
MIN_AGE_MIN = 0             # ilk dakikalardan itibaren al
MAX_AGE_MIN = 10            # sadece cok yeni havuzlar
MIN_LIQ = 5_000
MIN_M5_VOL = 1_500
MIN_BUYERS = 5
MIN_BUY_RATIO = 0.55
MAX_VOL_LIQ = 60.0
MAX_FDV = 5_000_000

# Cikis kurallari (sabit TP/SL YOK)
FEE_PCT = 1.0               # alim ve satim basina tahmini ucret+slippage (%)
TRAIL_PCT = 15.0            # zirveden %15 duserse sat (giris fiyati da ilk zirve sayilir)
SELL_RATIO_EXIT = 0.60      # son 5 dk islemlerin %60+'i satis ise sat
MIN_TX_FOR_PRESSURE = 6     # satis baskisi icin en az bu kadar islem olmali
LIQ_DROP_EXIT = 30.0        # likidite girise gore %30 duserse sat (rug sinyali)
NO_DATA_MIN = 15            # bu kadar dk veri gelmezse sifir say
# -----------------------------------------

TZ = timezone(timedelta(hours=3))
API = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302"}
LOG_FILE = "paper_log.csv"

OPEN = {}
SEEN = set()
RESULTS = []                # kapanan islemler
STATE = {"cash": START_BALANCE, "opened": 0}


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


def entry_ok(p):
    a = p.get("attributes", {})
    pid = p.get("id", "")
    if "_" not in pid or pid in SEEN or pid in OPEN:
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
    return {"id": pid, "net": net, "addr": addr, "name": a.get("name", "?"),
            "price": price, "liq": liq, "v5": v5, "buyers": int(buyers),
            "ratio": buys / tot, "fdv": fdv, "age": age}


def open_trade(s):
    SEEN.add(s["id"])
    if STATE["opened"] >= MAX_TRADES or len(OPEN) >= MAX_OPEN or STATE["cash"] < TRADE_USD:
        return
    STATE["opened"] += 1
    STATE["cash"] -= TRADE_USD
    OPEN[s["id"]] = {"net": s["net"], "addr": s["addr"], "name": s["name"],
                     "entry": s["price"], "liq": s["liq"], "ts": time.time(), "seen": time.time(),
                     "max": s["price"], "min": s["price"]}
    send(
        f"🎯 SANAL ALIM #{STATE['opened']}/{MAX_TRADES} {s['name']} ({s['net']})\n"
        f"Giris: ${fmt_price(s['price'])} | {TRADE_USD:.0f}$ (sanal)\n"
        f"Yas: {s['age']:.0f} dk | Likidite: {s['liq']/1e3:.0f}K$ | FDV: {s['fdv']/1e3:.0f}K$\n"
        f"5dk hacim: {s['v5']/1e3:.1f}K$ | Alici: {s['buyers']} | Alim: %{s['ratio']*100:.0f}\n"
        f"Cikis: limit yok, satis baskisi / zirveden -%{TRAIL_PCT:.0f} / likidite dususu\n"
        f"Kasa: {STATE['cash']:.2f}$\n"
        f"https://www.geckoterminal.com/{s['net']}/pools/{s['addr']}\n"
        f"https://axiom.trade/meme/{s['addr']}"
    )


def close_trade(pid, price, reason):
    t = OPEN.pop(pid)
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
    emoji = "✅" if net > 0 else "❌"
    send(f"{emoji} SANAL SATIS {t['name']} ({reason})\n"
         f"Net getiri: {net:+.1f}% ({proceeds:.2f}$)\n"
         f"Tepe: {mx:+.0f}% | Dip: {mn:+.0f}% | Sure: {mins:.0f} dk\n"
         f"Kasa: {STATE['cash']:.2f}$")


def exit_reason(t, price, attrs):
    """Satis baskisi, zirveden dusus veya likidite cekilmesi varsa neden doner."""
    tx5 = (attrs.get("transactions", {}) or {}).get("m5", {}) or {}
    buys, sells = f(tx5.get("buys")), f(tx5.get("sells"))
    tot = buys + sells
    liq = f(attrs.get("reserve_in_usd"))
    if t["liq"] > 0 and liq > 0 and (1 - liq / t["liq"]) * 100 >= LIQ_DROP_EXIT:
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
    # uzun suredir veri gelmeyenler: likidite cekilmis kabul et, sifir
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
            s = entry_ok(p)
            if s:
                open_trade(s)
    return rl


def report():
    n = len(RESULTS)
    wins = sum(1 for r in RESULTS if r["net"] > 0)
    by = {}
    for r in RESULTS:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
    final = STATE["cash"]
    pnl = final - START_BALANCE
    lines = [f"{'✅' if r['net'] > 0 else '❌'} {r['name']}: {r['net']:+.1f}% ({r['reason']}, {r['mins']:.0f} dk)"
             for r in RESULTS]
    send(
        f"📊 SANAL SNIPER RAPORU ({RUN_MINUTES} dk)\n"
        f"Baslangic: {START_BALANCE:.2f}$ -> Bitis: {final:.2f}$ ({pnl:+.2f}$, {pnl / START_BALANCE * 100:+.1f}%)\n"
        f"Islem: {n} | Kazanan: {wins} | Kaybeden: {n - wins}\n"
        f"Cikis nedenleri: " + (", ".join(f"{k}={v}" for k, v in by.items()) or "-") + "\n\n"
        + ("\n".join(lines) if lines else "Filtreye uyan token cikmadi, islem yapilmadi.")
        + "\n\n⚠️ Sanal sonuc. Gercek islemde gecikme, slippage ve honeypot riski daha yuksektir."
    )


def main():
    end = time.time() + RUN_MINUTES * 60
    send(f"🚀 Sanal sniper basladi: {START_BALANCE:.0f}$ kasa, islem basi {TRADE_USD:.0f}$, "
         f"max {MAX_TRADES} islem, {RUN_MINUTES} dk.")
    backoff = 0
    while time.time() < end:
        if STATE["opened"] >= MAX_TRADES and not OPEN:
            break
        t0 = time.time()
        rl = False
        if STATE["opened"] < MAX_TRADES:
            rl = scan_new()
        if OPEN:
            track_open()
        print(f"acik: {len(OPEN)} | alim: {STATE['opened']}/{MAX_TRADES} | kasa: {STATE['cash']:.2f}", flush=True)
        backoff = min(backoff + 30, 60) if rl else 0
        time.sleep(max(0.0, POLL_SEC + backoff - (time.time() - t0)))
    if OPEN:
        track_open(force=True)
    for pid in list(OPEN):          # fiyat alinamayanlar
        close_trade(pid, 0, "VERI_YOK_SIFIR")
    report()


if __name__ == "__main__":
    main()
  
