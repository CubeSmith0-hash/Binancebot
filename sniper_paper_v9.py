"""
Sniper (PAPER TRADING) v9 - PUMP.FUN (PumpPortal websocket) - GeckoTerminal YOK.
Sanal bakiye 10$, her islem sabit 1$, pozisyon sayisi sinirsiz (kasa yettigi kadar), 10 saat calisir.
Cikis SADECE: TP 1000x veya SL -%50 (her islemde anlik kontrol); oturum bitince acikler son fiyattan kapanir.
GIRIS: yeni cikan coinde hacim patlamasi (son 30 sn hacmi, onceki ortalama hizin 2 kati + saglik filtreleri).
Ayni coin (mint ve isim) bir daha alinmaz. Gercek para / private key KULLANMAZ.

Kurulum: pip install requests websockets
"""
import asyncio
import csv
import json
import os
import queue
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
import websockets

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "600"))

# ---------------- AYARLAR ----------------
WS_URL = "wss://pumpportal.fun/api/data"
INTERIM_MIN = 60

START_BALANCE = 10.0
TRADE_USD = 1.0             # her islem sabit 1$

# Cikis
STOP_LOSS_PCT = 50.0        # girise gore -%50
TAKE_PROFIT_X = 1000        # 1000x (+%1000 istiyorsan 10 yaz)
TAKE_PROFIT_PCT = (TAKE_PROFIT_X - 1) * 100
FEE_PCT = 4.0               # alim ve satim basina ucret + slippage varsayimi (%)

# Giris: hacim patlamasi (hacim SOL cinsinden)
MIN_AGE_SEC = 20            # cok taze (ilk saniyeler) coine girme
MAX_AGE_MIN = 10            # bundan eski coin yeni sayilmaz
VOL_WINDOW_SEC = 30         # hacim penceresi
VOL_MIN_SOL = 4.0           # pencerede en az bu kadar SOL hacim
VOL_ACCEL = 2.0             # pencere hizi, onceki ortalama hizin en az kac kati
MIN_BUYERS = 6              # pencerede en az kac farkli alici cuzdan
MIN_BUY_RATIO = 0.60        # pencere hacminin en az %60'i alim
MAX_TOP_WALLET = 0.35       # tek cuzdan pencere hacminin en fazla %35'i (wash trading filtresi)
MIN_MCAP_SOL = 25           # market cap (SOL) alt/ust sinir; ust sinir graduation'a yakin coini eler
MAX_MCAP_SOL = 150
MAX_WATCH = 400             # ayni anda islem akisi izlenen coin sayisi
# -----------------------------------------

TZ = timezone(timedelta(hours=3))
LOG_FILE = "paper_log.csv"
TRADED_FILE = "traded_mints.txt"

WATCH = {}                  # mint -> coin durumu
OPEN = {}                   # mint -> acik pozisyon
TRADED_MINTS = set()
TRADED_NAMES = set()
RESULTS = []
STATE = {"cash": START_BALANCE, "opened": 0}
CLOCK = {"start": 0.0, "next_interim": 0.0, "end": 0.0}

# ---------- Telegram (ayri thread: websocket'i bloklamasin) ----------
Q = queue.Queue()


def _tg_worker():
    while True:
        text = Q.get()
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
                break
            except requests.RequestException:
                time.sleep(2)
        Q.task_done()


threading.Thread(target=_tg_worker, daemon=True).start()


def send(text):
    Q.put(text)


# ---------- yardimcilar ----------
def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def base_name(name):
    return str(name).split(" / ")[0].strip().lower()


def load_traded():
    try:
        with open(TRADED_FILE) as fh:
            for line in fh:
                mint, _, nm = line.rstrip("\n").partition("\t")
                if mint:
                    TRADED_MINTS.add(mint)
                if nm:
                    TRADED_NAMES.add(nm)
    except OSError:
        pass


def save_traded(mint, nm):
    try:
        with open(TRADED_FILE, "a") as fh:
            fh.write(f"{mint}\t{nm}\n")
    except OSError as e:
        print("dosya hatasi", e)


def log_row(row):
    new = not os.path.exists(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["kapanis", "mint", "isim", "giris_fiyat_sol", "cikis_fiyat_sol",
                            "boyut_usd", "net_getiri_%", "max_getiri_%", "min_getiri_%",
                            "sebep", "sure_dk"])
            w.writerow(row)
    except OSError as e:
        print("log hatasi", e)


def equity():
    return STATE["cash"] + TRADE_USD * len(OPEN)   # acik pozisyonlar giris degerinden


load_traded()


# ---------- islem mantigi ----------
def open_trade(mint, t, rate, prior_rate, n_buyers):
    size = min(TRADE_USD, STATE["cash"])
    if size < TRADE_USD:
        return
    STATE["opened"] += 1
    STATE["cash"] -= size
    TRADED_MINTS.add(mint)
    TRADED_NAMES.add(base_name(t["name"]))
    save_traded(mint, base_name(t["name"]))
    now = time.time()
    OPEN[mint] = {"name": t["name"], "entry": t["price"], "size": size, "ts": now,
                  "seen": now, "last": t["price"], "max": t["price"], "min": t["price"]}
    send(
        f"🎯 SANAL ALIM #{STATE['opened']} {t['name']} (pump.fun)\n"
        f"Giris: {t['price']:.3e} SOL | {size:.2f}$ (sanal)\n"
        f"Stop: -%{STOP_LOSS_PCT:.0f} | Kar al: {TAKE_PROFIT_X}x\n"
        f"Hacim patlamasi: {rate:.1f} SOL/dk (onceki ort. {prior_rate:.1f}) | Alici: {n_buyers}\n"
        f"Yas: {(now - t['created']):.0f} sn | MCap: {t['mcap']:.0f} SOL\n"
        f"Kasa: {STATE['cash']:.2f}$\n"
        f"https://pump.fun/coin/{mint}"
    )


def close_trade(mint, price, reason):
    o = OPEN.pop(mint)
    if price <= 0:
        net = -100.0
    else:
        gross = price / o["entry"] - 1
        net = ((1 + gross) * (1 - FEE_PCT / 100) / (1 + FEE_PCT / 100) - 1) * 100
    proceeds = o["size"] * (1 + net / 100)
    STATE["cash"] += proceeds
    mx = (o["max"] / o["entry"] - 1) * 100
    mn = (o["min"] / o["entry"] - 1) * 100
    mins = (time.time() - o["ts"]) / 60
    RESULTS.append({"name": o["name"], "net": net, "reason": reason, "mins": mins})
    log_row([datetime.now(TZ).isoformat(timespec="seconds"), mint, o["name"], o["entry"], price,
             round(o["size"], 2), round(net, 2), round(mx, 2), round(mn, 2), reason, round(mins, 1)])
    send(f"{'✅' if net > 0 else '❌'} SANAL SATIS {o['name']} ({reason})\n"
         f"Net getiri: {net:+.1f}% ({proceeds:.2f}$)\n"
         f"Tepe: {mx:+.0f}% | Dip: {mn:+.0f}% | Sure: {mins:.0f} dk\n"
         f"Kasa: {STATE['cash']:.2f}$")


def check_exit(mint, price):
    o = OPEN[mint]
    o["seen"] = time.time()
    o["last"] = price
    o["max"], o["min"] = max(o["max"], price), min(o["min"], price)
    gain = (price / o["entry"] - 1) * 100
    if gain <= -STOP_LOSS_PCT:
        close_trade(mint, price, "STOP_LOSS")
    elif gain >= TAKE_PROFIT_PCT:
        close_trade(mint, price, "TAKE_PROFIT")


def maybe_enter(mint, t, now):
    if STATE["cash"] < TRADE_USD:      # tek sinir: kasada 1$ olmasi
        return
    age = now - t["created"]
    if age < MIN_AGE_SEC or age > MAX_AGE_MIN * 60:
        return
    if t["dev_sold"] or not (MIN_MCAP_SOL <= t["mcap"] <= MAX_MCAP_SOL):
        return
    win = [x for x in t["trades"] if now - x[0] <= VOL_WINDOW_SEC]
    vol_win = sum(x[1] for x in win)
    if vol_win < VOL_MIN_SOL:
        return
    buyers = {x[3] for x in win if x[2]}
    if len(buyers) < MIN_BUYERS:
        return
    if sum(x[1] for x in win if x[2]) / vol_win < MIN_BUY_RATIO:
        return
    per = {}
    for _, sol, _, wallet in win:
        per[wallet] = per.get(wallet, 0.0) + sol
    if max(per.values()) / vol_win > MAX_TOP_WALLET:
        return
    rate = vol_win / (VOL_WINDOW_SEC / 60)
    prior_vol = max(t["vol_total"] - vol_win, 0.0)
    prior_rate = prior_vol / (max(age - VOL_WINDOW_SEC, 30) / 60)
    if rate < VOL_ACCEL * prior_rate:
        return
    open_trade(mint, t, rate, prior_rate, len(buyers))


async def on_message(ws, m):
    tx, mint = m.get("txType"), m.get("mint")
    if not mint or not tx:
        return
    now = time.time()
    if tx == "create":
        if mint in WATCH or mint in TRADED_MINTS or len(WATCH) >= MAX_WATCH:
            return
        name = m.get("name") or m.get("symbol") or "?"
        if base_name(name) in TRADED_NAMES:          # ayni isimli klon / tekrar
            return
        WATCH[mint] = {"name": name, "created": now, "dev": m.get("traderPublicKey", ""),
                       "dev_sold": False, "trades": deque(), "vol_total": 0.0,
                       "price": 0.0, "mcap": 0.0}
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        return
    if tx not in ("buy", "sell"):
        return
    t = WATCH.get(mint)
    if not t:
        return
    vs, vt = f(m.get("vSolInBondingCurve")), f(m.get("vTokensInBondingCurve"))
    if vs <= 0 or vt <= 0:
        return
    price = vs / vt
    sol = f(m.get("solAmount"))
    trader = m.get("traderPublicKey", "")
    is_buy = tx == "buy"
    t["price"] = price
    t["mcap"] = f(m.get("marketCapSol")) or price * 1e9
    t["vol_total"] += sol
    t["trades"].append((now, sol, is_buy, trader))
    while t["trades"] and now - t["trades"][0][0] > 120:
        t["trades"].popleft()
    if not is_buy and trader == t["dev"]:
        t["dev_sold"] = True
    if mint in OPEN:
        check_exit(mint, price)
    else:
        maybe_enter(mint, t, now)


async def periodic(ws):
    now = time.time()
    # yasi gecen ve acik olmayan coinlerin akisini birak
    old = [k for k, t in WATCH.items() if k not in OPEN and now - t["created"] > MAX_AGE_MIN * 60 + 60]
    for k in old:
        del WATCH[k]
    for i in range(0, len(old), 100):
        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": old[i:i + 100]}))
    if now >= CLOCK["next_interim"] and now < CLOCK["end"] - 300:
        report("ARA RAPOR", (now - CLOCK["start"]) / 60)
        CLOCK["next_interim"] += INTERIM_MIN * 60
    print(f"izlenen: {len(WATCH)} | acik: {len(OPEN)} | alim: {STATE['opened']} "
          f"| kasa: {STATE['cash']:.2f}", flush=True)


async def periodic_loop(ws):
    while time.time() < CLOCK["end"]:
        await asyncio.sleep(5)
        await periodic(ws)
    await ws.close()


async def run():
    while time.time() < CLOCK["end"]:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 22) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                keys = list(set(WATCH) | set(OPEN))
                for i in range(0, len(keys), 100):
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys[i:i + 100]}))
                task = asyncio.create_task(periodic_loop(ws))
                try:
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(m, dict):
                            await on_message(ws, m)
                finally:
                    task.cancel()
        except (websockets.WebSocketException, OSError) as e:
            print("ws hatasi:", e, flush=True)
            await asyncio.sleep(3)


def report(title="SANAL SNIPER RAPORU", elapsed=None):
    n = len(RESULTS)
    rets = [r["net"] for r in RESULTS]
    wins = sum(1 for x in rets if x > 0)
    by = {}
    for r in RESULTS:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
    eq = equity()
    pnl = eq - START_BALANCE
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
        f"Baslangic: {START_BALANCE:.2f}$ -> Su an: {eq:.2f}$ ({pnl:+.2f}$, {pnl / START_BALANCE * 100:+.1f}%)\n"
        f"Islem: {n} (acik: {len(OPEN)}) | Kazanan: {wins} | Kaybeden: {n - wins}\n"
        + stat
        + "Cikis nedenleri: " + (", ".join(f"{k}={v}" for k, v in by.items()) or "-") + "\n"
        + ("\nEn iyi 3: " + " | ".join(fmt(r) for r in best) if best else "")
        + ("\nEn kotu 3: " + " | ".join(fmt(r) for r in worst) if worst else "")
        + "\n\n⚠️ Sanal sonuc. Gercekte gecikme, slippage ve rug riski daha yuksek."
    )


def main():
    CLOCK["start"] = time.time()
    CLOCK["end"] = CLOCK["start"] + RUN_MINUTES * 60
    CLOCK["next_interim"] = CLOCK["start"] + INTERIM_MIN * 60
    send(f"🚀 Sanal sniper v9 (pump.fun) basladi: {START_BALANCE:.0f}$ kasa, islem basi {TRADE_USD:.0f}$, "
         f"SL -%{STOP_LOSS_PCT:.0f}, TP {TAKE_PROFIT_X}x, {RUN_MINUTES} dk, pozisyon sinirsiz (kasa yettigi kadar).")
    asyncio.run(run())
    for mint in list(OPEN):
        close_trade(mint, OPEN[mint]["last"], "OTURUM_BITTI")
    report("SANAL SNIPER RAPORU", (time.time() - CLOCK["start"]) / 60)
    Q.join()


if __name__ == "__main__":
    main()
    
