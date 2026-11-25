"""Haber tabanli LONG/SHORT sinyal botu (API anahtari gerektirmez).
Calistir:  python bot.py                (surekli)
           python bot.py --minutes 345  (N dakika calisip kapanir, GitHub Actions)
           python bot.py --stats        (gecmis sinyallerin basari orani)
Yatirim tavsiyesi degildir.
"""
import os, sys, re, time, sqlite3, hashlib, logging
from collections import defaultdict

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
CP_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
MIN_CONF = int(os.getenv("MIN_CONFIDENCE", "65"))
POLL = int(os.getenv("POLL_SECONDS", "60"))
COOLDOWN = int(os.getenv("COOLDOWN_MIN", "30")) * 60
DB_PATH = "bot.db"

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
}

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
           "XRP": "XRPUSDT", "GOLD": "PAXGUSDT"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

# ======================= KURAL TABANLI ANALIZ =======================
ASSET_RE = {
    "BTC": re.compile(r"\b(bitcoin|btc)\b"),
    "ETH": re.compile(r"\b(ethereum|ether|eth)\b"),
    "SOL": re.compile(r"\b(solana|sol)\b"),
    "XRP": re.compile(r"\b(xrp|ripple)\b"),
    "GOLD": re.compile(r"\b(gold|bullion)\b"),
    "OIL": re.compile(r"\b(oil|crude|brent|wti|opec)\b"),
}
GENERIC_CRYPTO = re.compile(r"\b(crypto|cryptocurrency|cryptocurrencies|digital assets?)\b")

POS = {
    "approve": 2, "approves": 2, "approved": 2, "approval": 2, "bullish": 2,
    "adopt": 1.5, "adopts": 1.5, "adopted": 1.5, "adoption": 1, "buys": 1.5,
    "bought": 1.5, "buy": 1, "accumulate": 1.5, "accumulates": 1.5,
    "partnership": 1, "launches": 1, "launch": 1, "upgrade": 1.5,
    "inflow": 1.5, "inflows": 1.5, "backs": 1.5, "wins": 1.5, "win": 1.5,
    "positive": 1.5, "optimistic": 1.5, "praises": 1.5, "embraces": 1.5,
    "invests": 1, "integrates": 1, "legalize": 2, "legalizes": 2, "legalized": 2,
    "greenlights": 2, "supportive": 1.5, "favorable": 1.5, "boost": 1,
}
NEG = {
    "ban": -2.5, "bans": -2.5, "banned": -2.5, "crackdown": -2.5,
    "lawsuit": -2, "sues": -2, "sued": -2, "hack": -2.5, "hacked": -2.5,
    "hacker": -2, "exploit": -2, "exploited": -2, "stolen": -2, "theft": -2,
    "fraud": -2.5, "scam": -2, "bearish": -2, "selloff": -2, "dump": -2,
    "dumps": -2, "outflow": -1.5, "outflows": -1.5, "reject": -2,
    "rejects": -2, "rejected": -2, "denies": -1.5, "delay": -1.5,
    "delays": -1.5, "delayed": -1.5, "warns": -1.5, "warning": -1.5,
    "crash": -2, "collapse": -2.5, "collapses": -2.5, "bankrupt": -3,
    "bankruptcy": -3, "insolvent": -3, "investigation": -1.5, "probe": -1.5,
    "charged": -1.5, "indicted": -2.5, "criticizes": -1.5, "slams": -1.5,
    "negative": -1.5, "fear": -1, "fears": -1, "threatens": -1.5,
    "restrict": -1.5, "restricts": -1.5, "restrictions": -1.5, "liquidated": -1.5,
    "liquidations": -1.5, "shutdown": -2, "freezes": -2, "seizes": -2,
}
LEXICON = {**POS, **NEG}
PHRASES = {
    "all-time high": 2, "record high": 2, "bitcoin reserve": 2.5,
    "strategic reserve": 2, "legal tender": 2, "etf approval": 2,
    "drops charges": 2, "dropped charges": 2, "sell-off": -2, "sell off": -2,
    "shut down": -2, "all-time low": -2, "record low": -2,
}
NEGATORS = {"not", "no", "never", "without", "unlikely", "fails", "fail"}

# Makro olaylar: (regex, {varlik: etki}, etiket)
MACRO = [
    (re.compile(r"\b(war|airstrike|airstrikes|missile|missiles|invasion|invades|bombing|escalat\w+)\b"),
     {"GOLD": 2, "OIL": 2, "BTC": -1, "ETH": -1}, "savas/gerilim"),
    (re.compile(r"ceasefire|peace deal|truce|de-escalat"),
     {"GOLD": -1.5, "OIL": -2, "BTC": 1, "ETH": 1}, "ates kes/baris"),
    (re.compile(r"rate cuts?|cuts? (interest )?rates|dovish"),
     {"BTC": 2, "ETH": 2, "GOLD": 2}, "faiz indirimi"),
    (re.compile(r"rate hikes?|raises? (interest )?rates|hawkish|higher for longer"),
     {"BTC": -2, "ETH": -2, "GOLD": -1.5}, "faiz artisi"),
    (re.compile(r"inflation (rises|jumps|surges|heats)|hotter[- ]than[- ]expected"),
     {"BTC": -1.5, "ETH": -1.5}, "yuksek enflasyon"),
    (re.compile(r"inflation (falls|cools|eases|slows)|cooler[- ]than[- ]expected"),
     {"BTC": 1.5, "ETH": 1.5, "GOLD": 1}, "dusen enflasyon"),
    (re.compile(r"tariff pause|tariff relief|tariffs? (delay|delayed|cut|cuts|lifted)|trade deal"),
     {"BTC": 1.5, "ETH": 1.5, "OIL": 1}, "ticaret gerginligi azaldi"),
    (re.compile(r"tariffs?|trade war"),
     {"BTC": -1.5, "ETH": -1.5, "GOLD": 1, "OIL": -1}, "gumruk vergisi"),
    (re.compile(r"opec\+? (cuts?|reduces?|slashes)"), {"OIL": 2.5}, "OPEC uretim kesintisi"),
    (re.compile(r"opec\+? (raises?|increases?|boosts?)"), {"OIL": -2.5}, "OPEC uretim artisi"),
]
ACTORS = re.compile(r"trump|musk|powell|\bfed\b|\bsec\b|blackrock|binance|coinbase|"
                    r"saylor|white house|treasury|opec|putin|xi jinping")
HEDGE = re.compile(r"\b(could|may|might|would|should|analysts?|predict\w*|forecast\w*|"
                   r"expects?|opinion|analysis|why|how|what)\b|\?")
MOVED = re.compile(r"\b(surges?|soars?|plunges?|tumbles?|jumps?|rallies|rally|slides?|"
                   r"drops?|falls?|sinks?|slumps?|skyrockets?)\b")


def sentiment(text):
    low = text.lower().replace("\u2019", "'")
    score, terms = 0.0, []
    for ph, w in PHRASES.items():
        if ph in low:
            score += w
            terms.append(ph)
    tokens = re.findall(r"[a-z][a-z\-']*", low)
    for i, t in enumerate(tokens):
        w = LEXICON.get(t)
        if w is None:
            continue
        if any(x in NEGATORS or x.endswith("n't") for x in tokens[max(0, i - 3):i]):
            w = -0.8 * w
        score += w
        terms.append(t)
    return score, terms


def analyze(title, summary=""):
    t = title.lower()
    s_title, terms_t = sentiment(title)
    s_sum, _ = sentiment(summary)
    total = s_title + 0.4 * s_sum
    scores, why = defaultdict(float), defaultdict(list)

    for asset, rx in ASSET_RE.items():
        if rx.search(t) and total:
            scores[asset] += total
            why[asset] += terms_t
    if GENERIC_CRYPTO.search(t) and total:
        for a in ("BTC", "ETH"):
            if not ASSET_RE[a].search(t):
                scores[a] += 0.7 * total
                why[a] += terms_t
    for rx, effects, label in MACRO:
        if rx.search(t):
            for a, w in effects.items():
                scores[a] += w
                why[a].append(label)

    bonus = 10 if ACTORS.search(t) else 0
    penalty = (15 if HEDGE.search(t) else 0) + (10 if MOVED.search(t) else 0)
    out = []
    for asset, sc in scores.items():
        if abs(sc) < 1.5:
            continue
        conf = int(min(92, 40 + 13 * min(abs(sc), 3.5) + bonus - penalty))
        uniq = list(dict.fromkeys(why[asset]))[:3]
        out.append({"asset": asset, "direction": "LONG" if sc > 0 else "SHORT",
                    "confidence": conf,
                    "reason": ("Olumlu" if sc > 0 else "Olumsuz") + " haber: " + ", ".join(uniq)})
    out.sort(key=lambda x: -x["confidence"])
    return out[:2]
# ====================================================================


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen(uid TEXT PRIMARY KEY)")
    con.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, asset TEXT, direction TEXT,
        confidence INTEGER, reason TEXT, title TEXT, source TEXT, link TEXT,
        entry REAL, p15 REAL, p60 REAL)""")
    return con


def get_price(asset):
    sym = SYMBOLS.get(asset)
    if not sym:
        return None
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price",
                         params={"symbol": sym}, timeout=10)
        return float(r.json()["price"])
    except Exception as e:
        log.warning("Fiyat alinamadi %s: %s", asset, e)
        return None


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        log.info("[Telegram ayarsiz] %s", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text,
                            "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        log.warning("Telegram hatasi: %s", e)


def fetch_items():
    items = []
    for name, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0")
            for e in feed.entries[:30]:
                items.append((name, e.get("title", ""),
                              re.sub(r"<[^>]+>", "", e.get("summary", ""))[:500],
                              e.get("link", "")))
        except Exception as ex:
            log.warning("RSS hatasi %s: %s", name, ex)
    if CP_TOKEN:
        try:
            r = requests.get("https://cryptopanic.com/api/v1/posts/",
                             params={"auth_token": CP_TOKEN, "public": "true",
                                     "filter": "important"}, timeout=10).json()
            for p in r.get("results", []):
                items.append(("CryptoPanic", p.get("title", ""), "", p.get("url", "")))
        except Exception as ex:
            log.warning("CryptoPanic hatasi: %s", ex)
    return items


def process(con, first_run):
    new_items = []
    for src, title, summ, link in fetch_items():
        uid = hashlib.sha1((link or title).encode()).hexdigest()
        if con.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone():
            continue
        con.execute("INSERT INTO seen VALUES(?)", (uid,))
        if not first_run:
            new_items.append((src, title, summ, link))
    con.commit()

    for src, title, summ, link in new_items:
        for s in analyze(title, summ):
            conf, asset, direction = s["confidence"], s["asset"], s["direction"]
            if conf < MIN_CONF:
                continue
            last = con.execute(
                "SELECT ts FROM signals WHERE asset=? AND direction=? ORDER BY ts DESC LIMIT 1",
                (asset, direction)).fetchone()
            if last and time.time() - last[0] < COOLDOWN:
                continue
            entry = get_price(asset)
            con.execute(
                "INSERT INTO signals(ts,asset,direction,confidence,reason,title,source,link,entry) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), asset, direction, conf, s["reason"], title, src, link, entry))
            con.commit()
            emoji = "🟢" if direction == "LONG" else "🔴"
            price_txt = f"\nGiris fiyati: {entry:,.2f}" if entry else ""
            send_telegram(
                f"{emoji} {asset} {direction} | Guven: %{conf}\n"
                f"Sebep: {s['reason']}\n"
                f"Haber: {title}\nKaynak: {src}{price_txt}\n{link}\n"
                f"⚠️ Yatirim tavsiyesi degildir.")
            log.info("SINYAL %s %s %%%s - %s", asset, direction, conf, title)


def update_outcomes(con):
    now = time.time()
    for col, secs in (("p15", 900), ("p60", 3600)):
        rows = con.execute(
            f"SELECT id, asset FROM signals WHERE {col} IS NULL AND entry IS NOT NULL AND ts<=?",
            (now - secs,)).fetchall()
        for sid, asset in rows:
            p = get_price(asset)
            if p:
                con.execute(f"UPDATE signals SET {col}=? WHERE id=?", (p, sid))
    con.commit()


def stats():
    con = db()
    for col, label in (("p15", "15 dk"), ("p60", "60 dk")):
        rows = con.execute(
            f"SELECT direction, entry, {col} FROM signals WHERE {col} IS NOT NULL").fetchall()
        if not rows:
            print(f"{label}: henuz veri yok")
            continue
        wins, total_ret = 0, 0.0
        for d, e, p in rows:
            ret = (p - e) / e * 100 * (1 if d == "LONG" else -1)
            total_ret += ret
            wins += ret > 0
        print(f"{label}: {len(rows)} sinyal, dogru yon %{wins/len(rows)*100:.0f}, "
              f"ort. getiri %{total_ret/len(rows):.2f} (kaldiracsiz, komisyonsuz)")


def main():
    if "--stats" in sys.argv:
        return stats()
    deadline = None
    if "--minutes" in sys.argv:
        deadline = time.time() + float(sys.argv[sys.argv.index("--minutes") + 1]) * 60
    con = db()
    first_run = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    if first_run:
        log.info("Ilk calistirma: mevcut haberler 'goruldu' olarak isaretleniyor.")
    if not deadline:
        send_telegram("✅ Sinyal botu basladi.")
    while True:
        try:
            process(con, first_run)
            first_run = False
            update_outcomes(con)
        except Exception as e:
            log.exception("Dongu hatasi: %s", e)
        if deadline and time.time() + POLL > deadline:
            break
        time.sleep(POLL)


if __name__ == "__main__":
    main()
