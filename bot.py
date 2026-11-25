"""Haber tabanli LONG/SHORT sinyal botu.
Calistir:  python bot.py          (botu baslatir)
           python bot.py --stats  (gecmis sinyallerin basari orani)
Yatirim tavsiyesi degildir. Once sadece bildirim modunda test et.
"""
import os, sys, re, json, time, sqlite3, hashlib, logging
from datetime import datetime, timezone

import feedparser
import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
CP_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
MODEL = os.getenv("MODEL", "claude-haiku-5-5")
MIN_CONF = int(os.getenv("MIN_CONFIDENCE", "70"))
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

# Binance sembolleri (fiyat takibi icin). GOLD icin PAXG altin proxy'si.
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
           "XRP": "XRPUSDT", "GOLD": "PAXGUSDT"}
ASSETS = ["BTC", "ETH", "SOL", "XRP", "GOLD", "OIL"]

# API maliyetini dusurmek icin on filtre
KEYWORDS = re.compile(
    r"trump|musk|bitcoin|btc|ethereum|ether\b|crypto|solana|xrp|sec\b|etf|"
    r"fed\b|powell|rate cut|rate hike|inflation|cpi|tariff|sanction|"
    r"gold|oil|opec|war|strike|missile|attack|ceasefire|iran|israel|russia|"
    r"ukraine|china|hack|exploit|binance|coinbase|blackrock|treasury", re.I)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")
client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

SYSTEM = f"""Sen bir piyasa haber analistisin. Verilen haber basligi/ozetini oku ve
bunun {', '.join(ASSETS)} varliklarindan hangisini kisa vadede (dakikalar-saatler) etkileyebilecegini degerlendir.
Kurallar:
- Sadece haber gercekten fiyat hareketi yaratabilecekse sinyal uret. Belirsiz/eski/onemsiz haberde bos liste don.
- Yorum, analiz yazilari, fiyat tahmini haberleri ve zaten gerceklesmis buyuk hareketleri haberlestiren icerikler icin dusuk guven ver.
- confidence 0-100 arasi tam sayi. Abartma.
- reason: Turkce, tek cumle.
SADECE su JSON'u dondur, baska hicbir sey yazma:
{{"signals":[{{"asset":"BTC","direction":"LONG","confidence":75,"reason":"..."}}]}}
direction sadece LONG veya SHORT olabilir."""


# ---------- Veritabani ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen(uid TEXT PRIMARY KEY)")
    con.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, asset TEXT, direction TEXT,
        confidence INTEGER, reason TEXT, title TEXT, source TEXT, link TEXT,
        entry REAL, p15 REAL, p60 REAL)""")
    return con


# ---------- Yardimcilar ----------
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
    """Tum kaynaklardan (kaynak, baslik, ozet, link) listesi dondurur."""
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
                items.append(("CryptoPanic", p.get("title", ""), "",
                              p.get("url", "")))
        except Exception as ex:
            log.warning("CryptoPanic hatasi: %s", ex)
    return items


def analyze(title, summary):
    if not client:
        return []
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content": f"Baslik: {title}\nOzet: {summary}"}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = []
        for s in data.get("signals", []):
            if s.get("asset") in ASSETS and s.get("direction") in ("LONG", "SHORT"):
                out.append(s)
        return out
    except Exception as e:
        log.warning("Analiz hatasi: %s", e)
        return []


# ---------- Ana mantik ----------
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
        if not KEYWORDS.search(title + " " + summ):
            continue
        for s in analyze(title, summ):
            conf = int(s.get("confidence", 0))
            if conf < MIN_CONF:
                continue
            asset, direction = s["asset"], s["direction"]
            # Cooldown: ayni varlik+yon icin tekrar sinyal verme
            last = con.execute(
                "SELECT ts FROM signals WHERE asset=? AND direction=? ORDER BY ts DESC LIMIT 1",
                (asset, direction)).fetchone()
            if last and time.time() - last[0] < COOLDOWN:
                continue
            entry = get_price(asset)
            con.execute(
                "INSERT INTO signals(ts,asset,direction,confidence,reason,title,source,link,entry) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), asset, direction, conf, s.get("reason", ""),
                 title, src, link, entry))
            con.commit()
            emoji = "🟢" if direction == "LONG" else "🔴"
            price_txt = f"\nGiris fiyati: {entry:,.2f}" if entry else ""
            send_telegram(
                f"{emoji} {asset} {direction} | Guven: %{conf}\n"
                f"Sebep: {s.get('reason','')}\n"
                f"Haber: {title}\nKaynak: {src}{price_txt}\n{link}\n"
                f"⚠️ Yatirim tavsiyesi degildir.")
            log.info("SINYAL %s %s %%%s - %s", asset, direction, conf, title)


def update_outcomes(con):
    """Sinyalden 15dk ve 60dk sonraki fiyati kaydeder (backtest icin)."""
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
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY eksik (.env dosyasini kontrol et)")
    # --minutes N: N dakika calisip kapanir (GitHub Actions icin)
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
                         
