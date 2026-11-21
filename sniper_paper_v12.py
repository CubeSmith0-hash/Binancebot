"""
Sniper (PAPER TRADING) v12 - PUMP.FUN (PumpPortal websocket)
Her yeni coine aninda 1$ ile girer. Ayni coine bir daha girmez.
Cikis: TP 500x, SL -%50 veya 10 dk hareketsizlik. Kasa bossa hemen siradaki yeni coine girer.
Sure dolunca acikler son fiyattan kapanir, 10$ ne kadar oldu raporlanir.
Gercek para / private key KULLANMAZ.

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
from datetime import datetime, timezone, timedelta

import requests
import websockets

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "180"))   # 3 saat

# ---------------- AYARLAR ----------------
WS_URL = "wss://pumpportal.fun/api/data"
INTERIM_MIN = 30

START_BALANCE = 10.0
TRADE_USD = 1.0

STOP_LOSS_PCT = 50.0
TAKE_PROFIT_X = 500
TAKE_PROFIT_PCT = (TAKE_PROFIT_X - 1) * 100
FEE_PCT = 4.0               # alim ve satim basina ucret + slippage (%)

IDLE_MINUTES = 10           # bu sure hareket yoksa kapat
IDLE_MOVE_PCT = 5.0         # "hareket" sayilmasi icin gereken minimum fiyat degisimi (%)
# -----------------------------------------

TZ = timezone(timedelta(hours=3))
LOG_FILE = "paper_log.csv"
TRADED_FILE = "traded_mints.txt"

OPEN = {}
TRADED_MINTS = set()
TRADED_NAMES = set()
UNSUB = []
RESULTS = []
STATE = {"cash": START_BALANCE, "opened": 0}
CLOCK = {"start": 0.0, "next_interim": 0.0, "end": 0.0}

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


def net_pct(entry, price):
    if price <= 0:
        return -100.0
    gross = price / entry - 1
    return ((1 + gross) * (1 - FEE_PCT / 100) / (1 + FEE_PCT / 100) - 1) * 100


def equity():
    # acik pozisyonlar son fiyattan (ucret dusulmus) degerlenir
    return STATE["cash"] + sum(o["size"] * (1 + net_pct(o["entry"], o["last"]) / 100)
                                for o in OPEN.values())


load_traded()


def open_trade(mint, name, price, mcap):
    size = TRADE_USD
    STATE["opened"] += 1
    STATE["cash"] -= size
    TRADED_MINTS.add(mint)
    TRADED_NAMES.add(base_name(name))
    save_traded(mint, base_name(name))
    now = time.time()
    OPEN[mint] = {"name": name, "entry": price, "size": size, "ts": now,
                  "last": price, "max": price, "min": price,
                  "ref": price, "last_move": now}
    send(
        f"🎯 SANAL ALIM #{STATE['opened']} {name}\n"
        f"Giris: {price:.3e} SOL | {size:.2f}$ | MCap: {mcap:.0f} SOL\n"
        f"Acik: {len(OPEN)} | Kasa: {STATE['cash']:.2f}$\n"
        f"https://pump.fun/coin/{mint}"
    )


def close_trade(mint, price, reason):
    o = OPEN.pop(mint)
    UNSUB.append(mint)
    net = net_pct(o["entry"], price)
    proceeds = o["size"] * (1 + net / 100)
    STATE["cash"] += proceeds
    mx = (o["max"] / o["entry"] - 1) * 100
    mn = (o["min"] / o["entry"] - 1) * 100
    mins = (time.time() - o["ts"]) / 60
    RESULTS.append({"name": o["name"], "net": net, "reason": reason, "mins": mins})
    log_row([datetime.now(TZ).isoformat(timespec="seconds"), mint, o["name"], o["entry"], price,
             round(o["size"], 2), round(net, 2), round(mx, 2), round(mn, 2), reason, round(mins, 1)])
    send(f"{'✅' if net > 0 else '❌'} SANAL SATIS {o['name']} ({reason})\n"
         f"Net: {net:+.1f}% ({proceeds:.2f}$) | Sure: {mins:.0f} dk\n"
         f"Acik: {len(OPEN)} | Kasa: {STATE['cash']:.2f}$")


def check_exit(mint, price):
    o = OPEN[mint]
    o["last"] = price
    o["max"], o["min"] = max(o["max"], price), min(o["min"], price)
    # hareket takibi: referans fiyattan yeterince saptiysa sayaci sifirla
    if abs(price / o["ref"] - 1) * 100 >= IDLE_MOVE_PCT:
        o["ref"] = price
        o["last_move"] = time.time()
    gain = (price / o["entry"] - 1) * 100
    if gain <= -STOP_LOSS_PCT:
        close_trade(mint, price, "STOP_LOSS")
    elif gain >= TAKE_PROFIT_PCT:
        close_trade(mint, price, "TAKE_PROFIT")


async def on_message(ws, m):
    tx, mint = m.get("txType"), m.get("mint")
    if not mint or not tx:
        return
    vs, vt = f(m.get("vSolInBondingCurve")), f(m.get("vTokensInBondingCurve"))
    price = vs / vt if vs > 0 and vt > 0 else 0.0

    if tx == "create":
        if mint in OPEN or mint in TRADED_MINTS or STATE["cash"] < TRADE_USD or price <= 0:
            return
        name = m.get("name") or m.get("symbol") or "?"
        if base_name(name) in TRADED_NAMES:
            return
        mcap = f(m.get("marketCapSol")) or price * 1e9
        open_trade(mint, name, price, mcap)
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        return

    if tx in ("buy", "sell") and mint in OPEN and price > 0:
        check_exit(mint, price)


async def periodic(ws):
    global UNSUB
    now = time.time()
    # hareketsiz pozisyonlari kapat (islem akisi olmasa bile calisir)
    for mint in [k for k, o in OPEN.items() if now - o["last_move"] >= IDLE_MINUTES * 60]:
        close_trade(mint, OPEN[mint]["last"], "HAREKETSIZ")
    if UNSUB:
        keys, UNSUB = UNSUB, []
        for i in range(0, len(keys), 100):
            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": keys[i:i + 100]}))
    if now >= CLOCK["next_interim"] and now < CLOCK["end"] - 300:
        report("ARA RAPOR", (now - CLOCK["start"]) / 60)
        CLOCK["next_interim"] += INTERIM_MIN * 60
    print(f"acik: {len(OPEN)} | alim: {STATE['opened']} | kapanan: {len(RESULTS)} "
          f"| kasa: {STATE['cash']:.2f} | toplam: {equity():.2f}", flush=True)


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
                keys = list(OPEN)
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
        stat = (f"Kazanc orani: %{wins / n * 100:.0f} | Ortalama: {sum(rets) / n:+.1f}% | "
                f"Medyan: {statistics.median(rets):+.1f}%\n"
                f"En iyi islem haric ortalama: "
                f"{((sum(rets) - max(rets)) / (n - 1) if n > 1 else 0.0):+.1f}%\n")
    best = sorted(RESULTS, key=lambda r: -r["net"])[:3]
    fmt = lambda r: f"{r['name']}: {r['net']:+.0f}% ({r['reason']})"
    send(
        f"📊 {title}" + (f" ({elapsed:.0f} dk)" if elapsed else "") + "\n"
        f"💰 {START_BALANCE:.2f}$ -> {eq:.2f}$ ({pnl:+.2f}$, {pnl / START_BALANCE * 100:+.1f}%)\n"
        f"Toplam alim: {STATE['opened']} | Kapanan: {n} (acik: {len(OPEN)}) | "
        f"Kazanan: {wins} | Kaybeden: {n - wins}\n"
        + stat
        + "Cikis nedenleri: " + (", ".join(f"{k}={v}" for k, v in by.items()) or "-")
        + ("\nEn iyi 3: " + " | ".join(fmt(r) for r in best) if best else "")
        + "\n\n⚠️ Sanal sonuc. Gercekte gecikme, slippage ve rug riski daha yuksek."
    )


def main():
    CLOCK["start"] = time.time()
    CLOCK["end"] = CLOCK["start"] + RUN_MINUTES * 60
    CLOCK["next_interim"] = CLOCK["start"] + INTERIM_MIN * 60
    send(f"🚀 Sanal sniper v12 basladi: {START_BALANCE:.0f}$ kasa, islem basi {TRADE_USD:.0f}$, "
         f"SL -%{STOP_LOSS_PCT:.0f}, TP {TAKE_PROFIT_X}x, {IDLE_MINUTES} dk hareketsizlik cikisi, "
         f"sure {RUN_MINUTES} dk.")
    asyncio.run(run())
    for mint in list(OPEN):
        close_trade(mint, OPEN[mint]["last"], "OTURUM_BITTI")
    report("SONUC", (time.time() - CLOCK["start"]) / 60)
    Q.join()


if __name__ == "__main__":
    main()
