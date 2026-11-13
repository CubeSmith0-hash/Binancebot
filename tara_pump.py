"""
Bitget USDT vadeli tüm coinlerin 1 dk mumlarını çeker, gerçek pump'ları bulur,
pump hacim verisini pumps.csv'ye yazar ve ayar kombinasyonlarını karşılaştırır.

Kullanım:  python tara_pump.py
Ayarlar aşağıda.
"""
import csv
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

PAGES = 2             # 2 sayfa x 1000 dk = ~33 saat veri
PUMP_PCT = 20.0       # 60 dk içinde dipten en az % kaç yükselirse "pump" sayılır
MIN_24H_VOL = 50_000  # bu hacmin altındaki coinler atlanır
WORKERS = 4           # eşzamanlı istek sayısı (rate limit için düşük tut)
TZ = timezone(timedelta(hours=3))

HOST = "https://api.bitget.com"
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1",
           "AEUR", "EUR", "EURI", "TRY", "GBP", "BRL", "RUB", "UAH", "XUSD"}
STOCKS = {"AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
          "COIN", "MSTR", "CRCL", "HOOD", "SPY", "QQQ", "NFLX", "AMD",
          "INTC", "BABA", "PLTR", "ORCL", "AVGO", "JPM", "V", "MA", "DIS",
          "PYPL", "UBER", "SHOP", "SQ", "TSM", "NKE", "KO", "PEP", "WMT",
          "DELL", "IBM", "NOW", "CRM", "IREN", "BX", "HPE", "CRWD"}
NON_CRYPTO = {"PAXG", "XAUT"}

WINDOWS = (2, 5, 10)          # dakika
MIN_PCTS = (2, 3, 5)          # pencerede dipten en az % yükseliş
VOL_MULTS = (10, 20, 30)      # pencere hacmi / normal hacim
MIN_WINDOW_VOL = 5_000
COOLDOWN = 15                 # dk


def keep(sym):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if base in STABLES or base in NON_CRYPTO or base in STOCKS:
        return False
    if base.endswith("B") and base[:-1] in STOCKS:
        return False
    return True


def get(path, params):
    for _ in range(4):
        try:
            r = requests.get(HOST + path, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2)
                continue
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1)
    return {}


def hm(ts):
    return datetime.fromtimestamp(ts / 1000, TZ).strftime("%d.%m %H:%M")


def fetch_symbol(sym):
    end = int(time.time() * 1000)
    rows = {}
    for _ in range(PAGES):
        data = get("/api/v2/mix/market/candles", {
            "symbol": sym, "productType": "USDT-FUTURES",
            "granularity": "1m", "endTime": end, "limit": 1000,
        }).get("data") or []
        if not data:
            break
        for c in data:
            rows[int(c[0])] = (int(c[0]), float(c[4]), float(c[6]))  # ts, close, quoteVol
        end = min(rows) - 1
        time.sleep(0.15)
    return sym, [rows[k] for k in sorted(rows)]


def sliding_min(vals, w):
    """out[i] = min(vals[i-w:i+1])"""
    dq, out = deque(), []
    for i, v in enumerate(vals):
        while dq and vals[dq[-1]] >= v:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - w - 1:
            dq.popleft()
        out.append(vals[dq[0]])
    return out


def analyze(candles):
    ts = [c[0] for c in candles]
    cl = [c[1] for c in candles]
    qv = [c[2] for c in candles]
    n = len(cl)
    pre = [0.0]
    for v in qv:
        pre.append(pre[-1] + v)

    def base_mean(i, w):
        lo = max(0, i - 1440)
        hi = i - w + 1
        if hi - lo < 60:
            return 0
        return (pre[hi] - pre[lo]) / (hi - lo)

    per_w = {}
    for w in WINDOWS:
        mn = sliding_min(cl, w)
        pct, mult, wvol = [0.0] * n, [0.0] * n, [0.0] * n
        for i in range(w, n):
            bm = base_mean(i, w)
            if bm <= 0 or mn[i] <= 0:
                continue
            vol = pre[i + 1] - pre[i + 1 - w]
            pct[i] = (cl[i] / mn[i] - 1) * 100
            wvol[i] = vol
            mult[i] = vol / (bm * w)
        per_w[w] = (pct, mult, wvol)

    # pump bölgeleri: 60 dk içinde dipten >= PUMP_PCT
    mn60 = sliding_min(cl, 60)
    episodes, i = [], 61
    while i < n:
        if mn60[i] > 0 and (cl[i] / mn60[i] - 1) * 100 >= PUMP_PCT:
            first = i
            last = i
            j = i
            while j < n and j - last <= 60:
                if mn60[j] > 0 and (cl[j] / mn60[j] - 1) * 100 >= PUMP_PCT:
                    last = j
                j += 1
            s0 = max(0, first - 60)
            start = min(range(s0, first + 1), key=lambda k: cl[k])
            peak = max(range(first, min(n, last + 1)), key=lambda k: cl[k])
            if peak > start:
                episodes.append((start, peak))
            i = last + 60
        else:
            i += 1
    return ts, cl, per_w, episodes


def main():
    tk = get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"}).get("data") or []
    syms = []
    for t in tk:
        try:
            vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        except ValueError:
            continue
        if keep(t["symbol"]) and vol >= MIN_24H_VOL:
            syms.append(t["symbol"])
    print(f"{len(syms)} coin taranacak, birkaç dakika sürebilir...", flush=True)
    if not syms:
        print("Ticker verisi alınamadı")
        return

    results = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for k, (sym, candles) in enumerate(ex.map(fetch_symbol, syms), 1):
            if len(candles) >= 300:
                results[sym] = analyze(candles)
            if k % 50 == 0:
                print(f"  {k}/{len(syms)}", flush=True)

    # --- pump verisi ---
    rows = []
    for sym, (ts, cl, per_w, eps) in results.items():
        for s, p in eps:
            total = (cl[p] / cl[s] - 1) * 100
            row = {"coin": sym, "baslangic": hm(ts[s]), "zirve": hm(ts[p]),
                   "toplam_%": round(total, 1), "sure_dk": p - s}
            for th in (2, 3, 5):
                hit = next((k for k in range(s + 1, p + 1)
                            if (cl[k] / cl[s] - 1) * 100 >= th), None)
                for w in WINDOWS:
                    pct, mult, wvol = per_w[w]
                    key = f"%{th}_{w}dk"
                    row[key + "_dk_sonra"] = (hit - s) if hit else ""
                    row[key + "_hacimx"] = round(mult[hit], 1) if hit else ""
                    row[key + "_hacim_usdt"] = round(wvol[hit]) if hit else ""
            rows.append(row)
    rows.sort(key=lambda r: -r["toplam_%"])
    if rows:
        with open("pumps.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)
    print(f"\n{len(rows)} pump bulundu (60 dk'da %{PUMP_PCT:.0f}+). Detay: pumps.csv")

    print("\nPump'lar: dipten %3 olduğu anda 5 dk hacim çarpanı")
    print("coin            toplam%  süre  %3'e kaç dk sonra  hacimx")
    for r in rows[:30]:
        print(f"{r['coin']:<14} {r['toplam_%']:>7} {r['sure_dk']:>5}  "
              f"{str(r['%3_5dk_dk_sonra']):>16}  {str(r['%3_5dk_hacimx']):>7}")
    mx = [r["%3_5dk_hacimx"] for r in rows if r["%3_5dk_hacimx"] != ""]
    if mx:
        print(f"\n%3 anında 5 dk hacim çarpanı: medyan {statistics.median(mx):.1f}x, "
              f"min {min(mx):.1f}x, max {max(mx):.1f}x")

    # --- ayar karşılaştırması ---
    days = statistics.mean(len(v[0]) for v in results.values()) / 1440
    total_eps = sum(len(v[3]) for v in results.values())
    print(f"\nAyar taraması ({len(results)} coin, ~{days:.1f} gün, {total_eps} pump)")
    print("pencere  min%  hacimx | yakalanan | yanlış sinyal/gün | sinyalde pump'ın ne kadarı geçmişti(medyan %)")
    table = []
    for w in WINDOWS:
        for mp in MIN_PCTS:
            for vx in VOL_MULTS:
                caught, false, late = 0, 0, []
                for sym, (ts, cl, per_w, eps) in results.items():
                    pct, mult, wvol = per_w[w]
                    last = -999
                    sigs = []
                    for i in range(w, len(cl)):
                        if (pct[i] >= mp and mult[i] >= vx and wvol[i] >= MIN_WINDOW_VOL
                                and i - last >= COOLDOWN):
                            sigs.append(i)
                            last = i
                    for s, p in eps:
                        hit = next((i for i in sigs if s <= i <= p), None)
                        if hit is not None:
                            caught += 1
                            late.append((cl[hit] / cl[s] - 1) * 100)
                    inside = {i for i in sigs for s, p in eps if s <= i <= p}
                    false += len(sigs) - len(inside)
                table.append((w, mp, vx, caught, false / days,
                              statistics.median(late) if late else 0))
    table.sort(key=lambda r: (-r[3], r[4]))
    for w, mp, vx, caught, fpd, late in table:
        print(f"{w:>4} dk  {mp:>3}%  {vx:>5}x | {caught:>3}/{total_eps:<3}   | "
              f"{fpd:>10.1f}        | {late:>6.1f}")


if __name__ == "__main__":
    main()
  
