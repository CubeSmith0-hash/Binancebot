"""
Bitget USDT vadeli tüm coinlerin 1 dk mumlarını çeker, gerçek pump'ları bulur,
pump hacim verisini pumps.csv'ye yazar ve ayar kombinasyonlarını karşılaştırır.

Kullanım:  python tara_pump.py
Ayarlar aşağıda.
"""
import builtins
import csv
import html
import os
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
MIN_PCTS = (2, 3, 5, 8)          # pencerede dipten en az % yükseliş
VOL_MULTS = (10, 20, 30)      # pencere hacmi / normal hacim
MIN_WINDOW_VOL = 5_000
COOLDOWN = 15                 # dk
HORIZONS = (5, 15, 30, 60)    # sinyalden kaç dk sonraki getiriye bakılsın
FEE_PCT = 0.12                # gidiş-dönüş tahmini komisyon (%)

# Senin işlem ayarın (marjin / ROE üzerinden)
LEV = 20
TP_ROE = 30.0
SL_ROE = 50.0
TPSL_HORIZON = 120            # TP/SL vurmazsa en fazla kaç dk beklenir
HILO = {}                     # sembol -> (yüksekler, düşükler)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
REPORT = []


def print(*args, **kw):
    """Normal print + (ilerleme satırları hariç) Telegram raporuna ekler."""
    builtins.print(*args, **kw)
    if not kw.get("flush"):
        REPORT.append(" ".join(str(a) for a in args))


def tg(method, **kwargs):
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                              timeout=30, **kwargs)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return r.status_code
        except requests.RequestException:
            time.sleep(2)
    return 0


def send_report():
    if not TOKEN or not CHAT_ID:
        builtins.print("Telegram bilgisi yok, rapor gönderilmedi")
        return
    text = "\n".join(REPORT)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        tg("sendMessage", data={"chat_id": CHAT_ID, "parse_mode": "HTML",
                                "text": "<pre>" + html.escape(ch) + "</pre>"})
        time.sleep(1)
    if os.path.exists("pumps.csv"):
        with open("pumps.csv", "rb") as f:
            tg("sendDocument", data={"chat_id": CHAT_ID}, files={"document": f})
    builtins.print("Telegram'a gönderildi")


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
            rows[int(c[0])] = (int(c[0]), float(c[4]), float(c[6]),
                               float(c[2]), float(c[3]))  # ts, close, quoteVol, high, low
        end = min(rows) - 1
        time.sleep(0.15)
    return sym, [rows[k] for k in sorted(rows)]


def tpsl_outcome(cl, hi, lo, i):
    """i. mumun kapanışında LONG aç; TP/SL hangisi önce? (aynı mumda ikisi de olursa SL)"""
    entry = cl[i]
    tp_p = entry * (1 + TP_ROE / LEV / 100)
    sl_p = entry * (1 - SL_ROE / LEV / 100)
    fee = FEE_PCT * LEV
    end = i + TPSL_HORIZON
    if end >= len(cl):
        return None   # veri yetmiyor, sonuç belirsiz
    for k in range(i + 1, end + 1):
        if lo[k] <= sl_p:
            return "SL", -SL_ROE - fee
        if hi[k] >= tp_p:
            return "TP", TP_ROE - fee
    return "ZA", (cl[end] / entry - 1) * 100 * LEV - fee


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
                HILO[sym] = ([c[3] for c in candles], [c[4] for c in candles])
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
    tpsl_rows = []
    for w in WINDOWS:
        for mp in MIN_PCTS:
            for vx in VOL_MULTS:
                caught, false, late = 0, 0, []
                fwd = {h: [] for h in HORIZONS}
                tps = []
                for sym, (ts, cl, per_w, eps) in results.items():
                    pct, mult, wvol = per_w[w]
                    last = -999
                    sigs = []
                    for i in range(w, len(cl)):
                        if (pct[i] >= mp and mult[i] >= vx and wvol[i] >= MIN_WINDOW_VOL
                                and i - last >= COOLDOWN):
                            sigs.append(i)
                            last = i
                    hi_, lo_ = HILO[sym]
                    for i in sigs:
                        o = tpsl_outcome(cl, hi_, lo_, i)
                        if o:
                            tps.append(o)
                        for h in HORIZONS:
                            if i + h < len(cl):
                                fwd[h].append((cl[i + h] / cl[i] - 1) * 100 - FEE_PCT)
                    for s, p in eps:
                        hit = next((i for i in sigs if s <= i <= p), None)
                        if hit is not None:
                            caught += 1
                            late.append((cl[hit] / cl[s] - 1) * 100)
                    inside = {i for i in sigs for s, p in eps if s <= i <= p}
                    false += len(sigs) - len(inside)
                st = []
                for h in HORIZONS:
                    v = fwd[h]
                    st.append((statistics.mean(v), 100 * sum(1 for x in v if x > 0) / len(v))
                              if v else (0.0, 0.0))
                tpsl_rows.append((w, mp, vx, tps))
                table.append((w, mp, vx, caught, false / days,
                              statistics.median(late) if late else 0, st))
    table.sort(key=lambda r: (-r[3], r[4]))
    for w, mp, vx, caught, fpd, late, st in table:
        print(f"{w:>4} dk  {mp:>3}%  {vx:>5}x | {caught:>3}/{total_eps:<3}   | "
              f"{fpd:>10.1f}        | {late:>6.1f}")
    print(f"\nSinyalde LONG açılsaydı (komisyon -%{FEE_PCT} düşülmüş): ortalama getiri / kazanma oranı")
    print("pencere  min%  hacimx | " + " | ".join(f"{h:>3} dk sonra   " for h in HORIZONS))
    for w, mp, vx, caught, fpd, late, st in table:
        cols = " | ".join(f"{a:>+6.2f}% / {b:>3.0f}%" for a, b in st)
        print(f"{w:>4} dk  {mp:>3}%  {vx:>5}x | {cols}")
    print("\nNot: ortalama > 0 ve kazanma% > 50 değilse bu ayar para kazandırmaz.")

    # --- senin TP/SL ayarın ---
    fee = FEE_PCT * LEV
    be = (SL_ROE + fee) / (TP_ROE + SL_ROE) * 100
    print(f"\nSENİN İŞLEMİN: {LEV}x, TP %{TP_ROE:.0f} / SL %{SL_ROE:.0f} (marjin), "
          f"en fazla {TPSL_HORIZON} dk, komisyon dahil")
    print(f"Başabaş için TP oranı en az ≈ %{be:.0f} olmalı")
    out = []
    for w, mp, vx, tps in tpsl_rows:
        n = len(tps)
        if n < 5:
            continue
        tp = sum(1 for o in tps if o[0] == "TP")
        sl = sum(1 for o in tps if o[0] == "SL")
        za = n - tp - sl
        avg = sum(o[1] for o in tps) / n
        out.append((w, mp, vx, n, 100 * tp / n, 100 * sl / n, 100 * za / n, avg))
    print("\nEn karlı 12 ayar (en az 5 sinyal):")
    print("pencere  min%  hacimx | sinyal |  TP%  |  SL%  | süre% | ort net marjin%/işlem")
    for r in sorted(out, key=lambda r: -r[7])[:12]:
        print(f"{r[0]:>4} dk  {r[1]:>3}%  {r[2]:>5}x | {r[3]:>6} | {r[4]:>4.0f}% | {r[5]:>4.0f}% | "
              f"{r[6]:>4.0f}% | {r[7]:>+8.1f}")
    print("\nEn yüksek TP oranı (en az 10 sinyal):")
    for r in sorted([x for x in out if x[3] >= 10], key=lambda r: -r[4])[:5]:
        print(f"{r[0]:>4} dk  {r[1]:>3}%  {r[2]:>5}x | {r[3]:>6} | {r[4]:>4.0f}% | {r[5]:>4.0f}% | "
              f"{r[6]:>4.0f}% | {r[7]:>+8.1f}")
    if not out:
        print("Yeterli sinyal yok")

    send_report()


if __name__ == "__main__":
    main()
                    
