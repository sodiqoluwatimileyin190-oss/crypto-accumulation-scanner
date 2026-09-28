"""
CRYPTO SCANNER v3 - Coil + Catalyst + Tiers   (100% FREE data, no paid APIs)
===========================================================================
Finds beaten-down coins that are quietly coiling, then checks news, events,
sector heat and futures positioning. Output is simple:
   TOP 10 coins (ranked by tier)  +  UPCOMING EVENTS  +  NEW LISTINGS  +  RED FLAGS

TIERS
  🟢 TRIGGER  coiled + (catalyst / hot sector / squeeze fuel) + momentum just confirmed -> entry zone
  🟠 HEATING  coiled + (catalyst / hot sector / squeeze fuel) -> get ready
  🟡 WATCH    coiled, nothing else yet -> keep an eye on it
  🔴 RED FLAG hack / exploit / delisting / monitoring-tag news -> avoid

USAGE
  python scanner_v3.py                  -> scan once
  python scanner_v3.py --every 4        -> keep scanning every 4 hours (VS Code must stay open)
  python scanner_v3.py --install-task 4 -> AUTOMATIC: Windows runs it every 4h in the background
                                            (use 1 for hourly, 24 for daily at 08:00)
  python scanner_v3.py --remove-task    -> stop the automatic scans
  python scanner_v3.py --sniper         -> watch Bybit/Binance listing announcements every 5 min
  python scanner_v3.py --coin GRAM      -> detailed breakdown for one coin
  python scanner_v3.py --stats          -> how past alerts actually performed (forward test)
  python scanner_v3.py --runs           -> log of every scan: date, time, duration, OK/FAILED

FILES
  latest_report.txt  -> the latest simple report      blacklist.txt  -> coins to always skip
  alerts_log.csv     -> every alert + results later   news_cache.json -> news memory (30+ days)
  run_history.csv    -> date/time of every scan run   reports/        -> copy of every past report
"""

import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import ccxt
import numpy as np
import pandas as pd
import requests

try:  # emojis must not crash Windows consoles / log files
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ================================ SETTINGS ================================
SHOW_TOP            = 10
MIN_24H_VOLUME_USDT = 1_000_000
MIN_LISTING_DAYS    = 200
MIN_DRAWDOWN        = 0.50     # must be 50%+ below its high   (backtest: filters = +7.1% edge)
MAX_EXTENSION       = 0.60     # skip if already 60%+ above its base
MAX_RECENT_RUN      = 0.30     # skip if already ran 30%+ in 3 days
MAX_BASE_RANGE      = 0.50
LOW_FLOAT_LIMIT     = 0.50     # penalty if under 50% of supply circulates
NEWS_DAYS           = 7        # bullish news counts for 7 days
RISK_DAYS           = 30       # red-flag news counts for 30 days
EVENT_DAYS_AHEAD    = 45

# Telegram credentials live in config.json (never committed) or environment variables.
def _load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
_CFG = _load_config()
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")   or _CFG.get("telegram_token", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or str(_CFG.get("telegram_chat_id", ""))

RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://www.theblock.co/rss.xml",
    "https://news.bitcoin.com/feed/",
    "https://u.today/rss",
    "https://cryptopotato.com/feed/",
]
BLACKLIST_FILE, NEWS_FILE, LOG_FILE, REPORT_FILE = \
    "blacklist.txt", "news_cache.json", "alerts_log.csv", "latest_report.txt"
RUNS_FILE, REPORTS_DIR = "run_history.csv", "reports"
SKIP_BASES = {"USDC", "USDE", "DAI", "FDUSD", "TUSD", "PYUSD", "USDD", "EUR", "BUSD", "USD1", "BTC"}
# Tickers that are also normal words (only matched with a $ sign in front)
WORD_TICKERS = {"THE", "FOR", "AND", "ONE", "NOW", "ALL", "NEW", "TOP", "BIG", "CAT", "DOG", "GAS",
                "SUN", "ACT", "KEY", "WIN", "FUN", "HOT", "ETF", "SEC", "CEO", "USA", "API", "NFT",
                "DAO", "GPU", "RWA", "APE", "MOVE", "COOK", "BANK", "REAL", "LINK", "NEAR", "OPEN",
                "PEOPLE", "MAGIC", "PORTAL", "HOOK", "MASK", "ID", "IO", "OM", "S", "G", "T"}
# ==========================================================================

BULL = [r"\b(will )?list(s|ing|ed)?\b", r"\blaunch(es|ed|ing)?\b", r"\bmainnet\b", r"\bpartner",
        r"\bintegrat", r"\bupgrade", r"\betf\b", r"\bapprov", r"\badopt", r"\bburn", r"\bbuyback",
        r"\bacquir", r"\bstaking\b", r"\bairdrop", r"\blaunchpool\b", r"\bcollaborat",
        r"\braises?\b", r"\binvest(s|ment)\b", r"\breserve\b", r"\btreasury\b"]
RED = [r"\bhack(ed|er|s)?\b", r"\bexploit", r"\bdrain(ed|s)?\b", r"\bdelist", r"monitoring tag",
       r"\bdepeg", r"\bbankrupt", r"\brug ?pull", r"\bsuspend(s|ed)? (trading|withdraw|deposit)",
       r"\binsolven", r"\bstolen\b"]
BEAR = [r"\bunlock", r"\blawsuit", r"\bsued?\b", r"\bcharged?\b", r"\binvestigat", r"\boutage",
        r"\bdelay(ed|s)?\b", r"\bsell-?off\b", r"\bdump(s|ed|ing)?\b"]
EVENT = [r"\bmainnet\b", r"\bupgrade", r"\bhard ?fork", r"\blaunch", r"\bunlock", r"\blist(ing)?\b",
         r"\bairdrop", r"\bsnapshot", r"\bvote\b", r"\bconference\b", r"\bsummit\b", r"\bburn",
         r"\bhalving\b", r"\btestnet\b", r"\btoken generation\b|\btge\b", r"\bweek\b"]
BULL_RE, RED_RE, BEAR_RE, EVENT_RE = (re.compile("|".join(x), re.I) for x in (BULL, RED, BEAR, EVENT))
# Headlines where a coin is only a bystander of someone else's hack (e.g. "hacker swaps ETH")
RED_IGNORE = re.compile(r"hacker[s]? (swaps?|moves?|sends?|converts?|launders?|dumps?|bridges?|transfers?)"
                        r"|stolen \$?\w+|\bafter\b.{0,40}\b(hack|exploit)|\bresumes?\b|\brecover", re.I)
RED_NEAR = 45   # red word must be within this many characters of the coin's name
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                          "jul", "aug", "sep", "oct", "nov", "dec"])}
DATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
                     r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) crypto-scanner/3"}
HOST = "bybit.com"
TIER_RANK = {"TRIGGER": 3, "HEATING": 2, "WATCH": 1}
TIER_ICON = {"TRIGGER": "🟢", "HEATING": "🟠", "WATCH": "🟡", "RED": "🔴"}


def now_ms():
    return int(time.time() * 1000)


# =============================== market data ===============================
def connect(market_type="spot"):
    global HOST
    last = None
    for host in ("bybit.com", "bytick.com"):
        ex = ccxt.bybit({"enableRateLimit": True, "hostname": host, "options": {"defaultType": market_type}})
        try:
            ex.load_markets()
            HOST = host
            return ex
        except Exception as e:
            last = e
    raise SystemExit("Can't reach Bybit. Check internet / DNS 1.1.1.1 / VPN.\n" + str(last)[:200])


def fetch(ex, symbol, tf, limit):
    for _ in range(3):
        try:
            d = ex.fetch_ohlcv(symbol, tf, limit=limit)
            if d:
                return pd.DataFrame(d, columns=["ts", "open", "high", "low", "close", "volume"])
        except Exception:
            time.sleep(2)
    return None


def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            f.write("# One coin per line. Lines starting with # are ignored.\nRESOLV\nJASMY\n")
    with open(BLACKLIST_FILE, encoding="utf-8") as f:
        return {x.strip().upper() for x in f if x.strip() and not x.startswith("#")}


def futures_data(exf, base, h4):
    if exf is None or f"{base}/USDT:USDT" not in exf.markets:
        return None
    sym = f"{base}/USDT:USDT"
    try:
        funding = exf.fetch_funding_rate(sym).get("fundingRate")
        oi = sorted(exf.fetch_open_interest_history(sym, "4h", limit=50), key=lambda x: x.get("timestamp") or 0)
        vals = [v for v in (x.get("openInterestAmount") or x.get("openInterestValue") for x in oi) if v]
        return {"funding": funding,
                "oi_chg": vals[-1] / vals[-43] - 1 if len(vals) >= 43 else None,
                "price_chg_7d": h4["close"].iloc[-1] / h4["close"].iloc[-43] - 1}
    except Exception:
        return None


# ============================ CoinGecko (free) ============================
def cg_get(path, **params):
    for _ in range(2):
        try:
            r = requests.get("https://api.coingecko.com/api/v3/" + path, params=params, headers=UA, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(30)
        except Exception:
            pass
    return None


def load_coingecko():
    """Names, float %, hot sectors, trending. Everything optional."""
    info, hot, trending = {}, {}, set()
    for page in (1, 2, 3, 4):
        data = cg_get("coins/markets", vs_currency="usd", order="market_cap_desc", per_page=250, page=page)
        if not data:
            break
        for c in data:
            s = (c.get("symbol") or "").upper()
            if s in info:
                continue
            circ, sup = c.get("circulating_supply") or 0, c.get("max_supply") or c.get("total_supply") or 0
            info[s] = {"name": c.get("name") or "", "float": circ / sup if sup else None}
        time.sleep(4)
    cats = cg_get("coins/categories") or []
    cats = [c for c in cats if (c.get("market_cap") or 0) > 3e8 and c.get("market_cap_change_24h") is not None]
    for c in sorted(cats, key=lambda c: c["market_cap_change_24h"], reverse=True)[:5]:
        time.sleep(4)
        for coin in cg_get("coins/markets", vs_currency="usd", category=c["id"], per_page=100) or []:
            hot.setdefault((coin.get("symbol") or "").upper(),
                           f"{c['name']} {c['market_cap_change_24h']:+.1f}%")
    t = cg_get("search/trending") or {}
    trending = {(x.get("item", {}).get("symbol") or "").upper() for x in t.get("coins", [])}
    print(f"CoinGecko: {len(info)} coins, {len(hot)} in hot sectors, {len(trending)} trending"
          if info else "CoinGecko unavailable (sector/float checks skipped)")
    return info, hot, trending


# ================================ news (free) ================================
def parse_ts(s):
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return now_ms()


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_rss():
    items = []
    for url in RSS_FEEDS:
        try:
            root = ET.fromstring(requests.get(url, headers=UA, timeout=15).content)
            src = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            entries = list(root.iter("item")) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
            for it in entries:
                g = lambda tag: it.findtext(tag) or it.findtext("{http://www.w3.org/2005/Atom}" + tag) or ""
                link = g("link") or (it.find("{http://www.w3.org/2005/Atom}link").get("href")
                                     if it.find("{http://www.w3.org/2005/Atom}link") is not None else "")
                items.append({"title": clean(g("title")), "desc": clean(g("description") or g("summary"))[:400],
                              "link": link.strip(), "ts": parse_ts(g("pubDate") or g("published") or g("updated")),
                              "source": src, "event_ts": None})
        except Exception:
            continue
    return items


def fetch_announcements():
    """Bybit (official public endpoint) + Binance (public page data). Both optional."""
    items = []
    try:
        r = requests.get(f"https://api.{HOST}/v5/announcements/index",
                         params={"locale": "en-US", "limit": 50}, headers=UA, timeout=15).json()
        for a in r.get("result", {}).get("list", []):
            start = a.get("startDateTimestamp")
            items.append({"title": clean(a.get("title")), "desc": clean(a.get("description"))[:400],
                          "link": a.get("url", ""), "ts": int(a.get("dateTimestamp") or a.get("publishTime") or now_ms()),
                          "source": "Bybit", "event_ts": int(start) if start else None})
    except Exception:
        pass
    try:
        r = requests.get("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                         params={"type": 1, "pageNo": 1, "pageSize": 30}, headers=UA, timeout=15).json()
        for cat in (r.get("data") or {}).get("catalogs", []):
            for a in cat.get("articles", []):
                items.append({"title": clean(a.get("title")), "desc": "",
                              "link": f"https://www.binance.com/en/support/announcement/{a.get('code', '')}",
                              "ts": int(a.get("releaseDate") or now_ms()), "source": "Binance", "event_ts": None})
    except Exception:
        pass
    return items


def update_news_cache(new_items):
    cache = []
    if os.path.exists(NEWS_FILE):
        try:
            with open(NEWS_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = []
    seen = {c["title"] for c in cache}
    cache += [n for n in new_items if n["title"] and n["title"] not in seen]
    cutoff = now_ms() - 45 * 86_400_000
    cache = [c for c in cache if c["ts"] >= cutoff]
    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return cache


def event_date(item):
    """Future date mentioned in the headline (e.g. 'mainnet on Oct 5'), as ms timestamp."""
    if item.get("event_ts"):
        return item["event_ts"]
    text = f"{item['title']} {item['desc']}"
    pub = datetime.fromtimestamp(item["ts"] / 1000, timezone.utc)
    low = text.lower()
    if "tomorrow" in low:
        return int((pub + timedelta(days=1)).timestamp() * 1000)
    m = DATE_RE.search(text)
    if not m:
        return None
    mon = MONTHS[(m.group(1) or m.group(4))[:3].lower()]
    day = int(m.group(2) or m.group(3))
    try:
        d = datetime(pub.year, mon, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    if d < pub - timedelta(days=60):
        d = d.replace(year=pub.year + 1)
    return int(d.timestamp() * 1000)


def build_matchers(bases, cg_info):
    out = {}
    for b in bases:
        pats = [re.compile(r"\$" + re.escape(b) + r"\b")]
        if b not in WORD_TICKERS and len(b) >= 3:
            pats.append(re.compile(r"(?<![A-Za-z0-9$])" + re.escape(b) + r"(?![A-Za-z0-9])"))
        name = (cg_info.get(b) or {}).get("name", "")
        if len(name) >= 4 and name.upper() not in WORD_TICKERS:
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.I))
        out[b] = pats
    return out


def is_red_for(title, pats):
    """Red flag only if the hack/delist word is close to THIS coin and the coin isn't a bystander."""
    if RED_IGNORE.search(title):
        return False
    reds = [m.start() for m in RED_RE.finditer(title)]
    spots = [m.start() for p in pats for m in p.finditer(title)]
    return any(abs(r - c) <= RED_NEAR for r in reds for c in spots)


def news_by_coin(news, matchers):
    """-> {base: {'bull': [...], 'red': [...], 'bear': [...], 'events': [...]}}"""
    now = now_ms()
    today_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    res = {}
    for n in news:
        text = f"{n['title']} {n['desc']}"
        coins = [b for b, pats in matchers.items() if any(p.search(n["title"]) for p in pats)]
        if not coins and n["source"] in ("Bybit", "Binance"):
            coins = [b for b, pats in matchers.items() if any(p.search(text) for p in pats)]
        if not coins or len(coins) > 4:          # skip generic market wrap-ups
            continue
        age_days = (now - n["ts"]) / 86_400_000
        ev_ts = event_date(n) if EVENT_RE.search(text) else None
        if ev_ts and not n.get("event_ts") and ev_ts < n["ts"] + 20 * 3_600_000:
            ev_ts = None                         # date = publish day -> already happened, not upcoming
        for b in coins:
            r = res.setdefault(b, {"bull": [], "red": [], "bear": [], "events": []})
            if age_days <= RISK_DAYS and is_red_for(n["title"], matchers[b]):
                r["red"].append(n)
            elif BEAR_RE.search(text) and age_days <= NEWS_DAYS:
                r["bear"].append(n)
            elif BULL_RE.search(text) and age_days <= NEWS_DAYS:
                r["bull"].append(n)
            if ev_ts and today_start <= ev_ts <= now + EVENT_DAYS_AHEAD * 86_400_000:
                r["events"].append({**n, "event_ts": ev_ts})
    return res


# ================================ indicators ================================
def macd(close):
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    return line, line - line.ewm(span=9, adjust=False).mean()


def supertrend_up(df, period=10, mult=3.0):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    pc = np.concatenate([[c[0]], c[:-1]])
    atr = pd.Series(np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))).ewm(alpha=1 / period, adjust=False).mean().values
    ub, lb = (h + l) / 2 + mult * atr, (h + l) / 2 - mult * atr
    fub, flb, up = ub.copy(), lb.copy(), np.ones(len(c), dtype=bool)
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        up[i] = c[i] >= flb[i] if up[i - 1] else c[i] > fub[i]
    return up


# ================================= scoring =================================
def analyze(daily, h4, fut=None, news=None, sector=None, trending=False, tok=None, verbose=False):
    def no(msg):
        return {"tier": None, "score": 0, "boxes": {}, "notes": [msg], "stats": {}} if verbose else None

    if daily is None or h4 is None or len(daily) < MIN_LISTING_DAYS or len(h4) < 120:
        return no("not enough history")
    news = news or {"bull": [], "red": [], "bear": [], "events": []}
    close = h4["close"].iloc[-1]
    drawdown = 1 - close / daily["high"].max()
    base_low = daily.iloc[-67:-7]["low"].min()
    ext = close / base_low - 1
    run3 = close / h4["low"].iloc[-18:].min() - 1
    stats = {"price": close, "drawdown": drawdown, "extension": ext, "run_3d": run3}
    notes, boxes = [], {}

    boxes["filters"] = drawdown >= MIN_DRAWDOWN and ext <= MAX_EXTENSION and run3 <= MAX_RECENT_RUN
    if not boxes["filters"]:
        notes.append(f"filters fail (down {drawdown:.0%}, {ext:+.0%} vs base, {run3:+.0%} in 3d)")
        if not verbose:
            return None

    # --- COIL (max 50): squeeze + base length (the factors the backtest liked) ---
    width = 4 * daily["close"].rolling(20).std() / daily["close"].rolling(20).mean()
    hist_w = width.iloc[-100:-10].dropna()
    sq = (hist_w < width.iloc[-10:].min()).mean() if len(hist_w) else 1
    sq_pts = 25 if sq <= 0.15 else 12 if sq <= 0.30 else 0
    base_days, base_pts = 0, 0
    for n, pts in ((120, 25), (90, 20), (60, 15), (30, 8)):
        if len(daily) >= n + 7:
            w = daily.iloc[-(n + 7):-7]
            if w["high"].max() / w["low"].min() - 1 <= MAX_BASE_RANGE:
                base_days, base_pts = n, pts
                break
    coil = sq_pts + base_pts
    boxes["coil"] = coil >= 25
    if base_days:
        notes.append(f"{base_days}d base" + (" + tight squeeze" if sq_pts == 25 else " + squeezing" if sq_pts else ""))
    elif sq_pts:
        notes.append("volatility squeeze")

    # --- CATALYST (15) ---
    boxes["catalyst"] = bool(news["bull"] or news["events"]) and not news["red"]
    if news["bull"]:
        notes.append("📰 " + news["bull"][0]["title"][:70])
    elif news["events"]:
        notes.append("📅 " + news["events"][0]["title"][:70])
    if news["bear"]:
        notes.append("⚠️ " + news["bear"][0]["title"][:60])

    # --- NARRATIVE (10) ---
    boxes["narrative"] = bool(sector or trending)
    if sector:
        notes.append(f"🔥 hot sector: {sector}")
    if trending:
        notes.append("🔥 trending on CoinGecko")

    # --- FUEL (10): shorts piling in while price is flat ---
    fuel = False
    if fut:
        if fut.get("funding") is not None and fut["funding"] < 0:
            fuel = True
            notes.append(f"⚡ negative funding {fut['funding']:.3%}")
        if fut.get("oi_chg") is not None and fut["oi_chg"] >= 0.15 and fut["price_chg_7d"] <= 0.15:
            fuel = True
            notes.append(f"⚡ open interest {fut['oi_chg']:+.0%} in 7d")
    boxes["fuel"] = fuel

    # --- TRIGGER (15): momentum confirmation = entry timing ---
    st = supertrend_up(h4)[-1]
    line, hist = macd(h4["close"])
    qv = h4["volume"] * h4["close"]
    vr = qv.iloc[-12:].mean() / max(qv.iloc[-72:-12].mean(), 1e-12)
    ema_d = close / daily["close"].ewm(span=200, adjust=False).mean().iloc[-1] - 1
    checks = [st, line.iloc[-1] > 0 and hist.iloc[-1] > 0, 1.3 <= vr <= 4, -0.05 <= ema_d <= 0.10]
    boxes["trigger"] = bool(st and sum(checks) >= 3)
    if boxes["trigger"]:
        notes.append(f"▶ trigger ON (vol {vr:.1f}x, {ema_d:+.0%} vs 200 EMA)")
    stats.update(squeeze_pct=sq, base_days=base_days, volume_ratio=vr, ema200_dist=ema_d)

    score = coil + 15 * boxes["catalyst"] + 10 * boxes["narrative"] + 10 * fuel + 15 * boxes["trigger"]
    if tok and tok.get("float") is not None and tok["float"] < LOW_FLOAT_LIMIT:
        score -= 10
        notes.append(f"⚠️ low float {tok['float']:.0%} (unlocks ahead)")
    score = max(0, min(100, score))

    extra = boxes["catalyst"] or boxes["narrative"] or fuel
    if news["red"]:
        tier = "RED"
        notes.insert(0, "🔴 " + news["red"][0]["title"][:70])
    elif not boxes["filters"] or not boxes["coil"]:
        tier = None
    elif extra and boxes["trigger"]:
        tier = "TRIGGER"
    elif extra:
        tier = "HEATING"
    else:
        tier = "WATCH"
    return {"tier": tier, "score": score, "boxes": boxes, "notes": notes, "stats": stats}


# ============================= output / alerts =============================
def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 3900],
                                "disable_web_page_preview": True}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def box_line(b):
    names = [("coil", "Coil"), ("catalyst", "News"), ("narrative", "Sector"), ("fuel", "Fuel"), ("trigger", "Trigger")]
    return " ".join(("✅" if b.get(k) else "⬜") + n for k, n in names)


def fmt_date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d %b")


def log_alerts(top, tickers, btc_price):
    """Forward test: record each alert once a day, then fill in results 3/7/14 days later."""
    cols = ["time", "symbol", "tier", "score", "price", "btc", "ret_3d", "ret_7d", "ret_14d", "btc_3d", "btc_7d", "btc_14d"]
    log = pd.read_csv(LOG_FILE) if os.path.exists(LOG_FILE) else pd.DataFrame(columns=cols)
    now = datetime.now()
    for H in (3, 7, 14):                                   # fill in matured results
        for i, r in log.iterrows():
            if pd.isna(r[f"ret_{H}d"]) and now - datetime.fromisoformat(r["time"]) >= timedelta(days=H):
                p = (tickers.get(r["symbol"]) or {}).get("last")
                if p:
                    log.at[i, f"ret_{H}d"] = p / r["price"] - 1
                    log.at[i, f"btc_{H}d"] = btc_price / r["btc"] - 1
    recent = log[pd.to_datetime(log["time"]) >= now - timedelta(hours=20)] if len(log) else log
    for c in top:
        if not ((recent["symbol"] == c["symbol"]) & (recent["tier"] == c["tier"])).any():
            log.loc[len(log)] = [now.isoformat(timespec="minutes"), c["symbol"], c["tier"], c["score"],
                                 c["stats"]["price"], btc_price] + [None] * 6
    log.to_csv(LOG_FILE, index=False)


def show_stats():
    if not os.path.exists(LOG_FILE):
        sys.exit("No alerts logged yet. Run some scans first.")
    log = pd.read_csv(LOG_FILE)
    print(f"FORWARD TEST - {len(log)} alerts logged since {log['time'].min()}\n")
    for H in (3, 7, 14):
        done = log.dropna(subset=[f"ret_{H}d"])
        if done.empty:
            print(f"{H}d: no results yet (need alerts older than {H} days)")
            continue
        print(f"=== {H}-DAY RESULTS ===")
        for tier in ("TRIGGER", "HEATING", "WATCH"):
            s = done[done["tier"] == tier]
            if len(s):
                beat = (s[f"ret_{H}d"] > s[f"btc_{H}d"]).mean()
                print(f"  {TIER_ICON[tier]} {tier:8s} n={len(s):3d}  avg {s[f'ret_{H}d'].mean():+.1%}  "
                      f"win {(s[f'ret_{H}d'] > 0).mean():.0%}  beat BTC {beat:.0%}")
    print("\nAfter 4-6 weeks: if TRIGGER beats BTC more than ~55% of the time, the scanner has a real edge.")


# ================================== scan ==================================
def scan():
    t0 = time.time()
    now = datetime.now()
    blacklist = load_blacklist()
    ex = connect("spot")
    try:
        exf = connect("swap")
    except SystemExit:
        exf = None
    tickers = ex.fetch_tickers()
    bases = {m["base"]: s for s, m in ex.markets.items()
             if m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)}
    cg_info, hot, trending = load_coingecko()
    news = update_news_cache(fetch_rss() + fetch_announcements())
    coin_news = news_by_coin(news, build_matchers([b for b in bases if b not in SKIP_BASES], cg_info))
    print(f"News: {len(news)} headlines in memory, {len(coin_news)} coins mentioned")

    btc = fetch(ex, "BTC/USDT", "1d", 400)
    bull = btc["close"].iloc[-1] > btc["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    universe = [b for b, s in bases.items() if b not in SKIP_BASES and b not in blacklist
                and not b.endswith(("3L", "3S", "2L", "2S"))
                and ((tickers.get(s) or {}).get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT]
    print(f"Scanning {len(universe)} coins...")

    results, red_flags = [], []
    for i, b in enumerate(universe, 1):
        sym, cn = bases[b], coin_news.get(b)
        if cn and cn["red"]:
            red_flags.append((b, cn["red"][0]))
            continue
        daily = fetch(ex, sym, "1d", 1000)
        if daily is None or len(daily) < MIN_LISTING_DAYS:
            continue
        h4 = fetch(ex, sym, "4h", 200)
        first = analyze(daily, h4, news=cn, sector=hot.get(b), trending=b in trending, tok=cg_info.get(b))
        if not first or not first["tier"]:
            continue
        out = analyze(daily, h4, futures_data(exf, b, h4), cn, hot.get(b), b in trending, cg_info.get(b))
        if out and out["tier"]:
            results.append({"symbol": sym, "base": b, **out})
        if i % 50 == 0:
            print(f"  {i}/{len(universe)}")

    results.sort(key=lambda r: (TIER_RANK.get(r["tier"], 0), r["score"]), reverse=True)
    top = results[:SHOW_TOP]
    counts = {t: sum(r["tier"] == t for r in results) for t in TIER_RANK}

    # ---------------------------- simple report ----------------------------
    L = [f"🔎 CRYPTO SCAN  {now:%d %b %H:%M}",
         f"BTC: {'🟢 BULL (above 200 EMA)' if bull else '🔴 BEAR (below 200 EMA) - be careful'}",
         f"🟢 Trigger {counts['TRIGGER']}  🟠 Heating {counts['HEATING']}  🟡 Watch {counts['WATCH']}  🔴 Flags {len(red_flags)}",
         "", f"🏆 TOP {len(top)} COINS"]
    if not top:
        L.append("  Nothing passes all filters right now. Patience = profit.")
    for n, r in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(n, f"{n}.")
        L.append(f"{medal} {TIER_ICON[r['tier']]} {r['tier']:8s} {r['base']:<7s} ${r['stats']['price']:.6g}  score {r['score']}")
        L.append(f"   {box_line(r['boxes'])}")
        for note in r["notes"][:3]:
            L.append(f"   • {note}")
        if n in (3, 5):
            L.append("   " + "-" * 30)

    events = []
    for b, cn in coin_news.items():
        for e in cn["events"]:
            events.append((e["event_ts"], b, e))
    seen, ev_lines = set(), []
    for ts, b, e in sorted(events, key=lambda x: x[0]):
        key = (b, e["title"][:40])
        if key in seen:
            continue
        seen.add(key)
        star = "⭐" if b in {r["base"] for r in top} else ""
        ev_lines.append(f"• {fmt_date(ts)}  {b}{star}: {e['title'][:75]} ({e['source']})")
    L += ["", f"📅 UPCOMING EVENTS (next {EVENT_DAYS_AHEAD} days)"] + (ev_lines[:15] or ["  None detected in the news right now."])

    cutoff = now_ms() - 2 * 86_400_000
    lst = [n for n in news if n["source"] in ("Bybit", "Binance") and n["ts"] >= cutoff
           and re.search(r"\blist|launchpool|launchpad|delist", n["title"], re.I)]
    L += ["", "📢 EXCHANGE LISTINGS (last 48h)"] + \
         ([f"• {n['source']}: {n['title'][:85]}" for n in lst[:8]] or ["  None."])

    L += ["", "🔴 RED FLAGS (auto-skipped)"] + \
         ([f"• {b}: {n['title'][:75]}" for b, n in red_flags[:8]] or ["  None."])
    L += ["", "⭐ = also in Top list | Not financial advice. Check chart + use a stop-loss."]
    report = "\n".join(L)

    print("\n" + report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    os.makedirs(REPORTS_DIR, exist_ok=True)                      # keep every past report
    with open(os.path.join(REPORTS_DIR, f"report_{now:%Y-%m-%d_%H%M}.txt"), "w", encoding="utf-8") as f:
        f.write(report)
    pd.DataFrame([{"symbol": r["symbol"], "tier": r["tier"], "score": r["score"], **r["stats"],
                   **{"box_" + k: v for k, v in r["boxes"].items()}, "notes": " | ".join(r["notes"])}
                  for r in results]).to_csv(f"scan_v3_{now:%Y%m%d_%H%M}.csv", index=False)
    log_alerts(top, tickers, btc["close"].iloc[-1])
    send_telegram(report)
    print(f"\nDone in {(time.time() - t0) / 60:.1f} min. Saved {REPORT_FILE}")
    return {"coins_scanned": len(universe), "trigger": counts["TRIGGER"], "heating": counts["HEATING"],
            "watch": counts["WATCH"], "red_flags": len(red_flags),
            "top3": " ".join(r["base"] for r in top[:3]), "btc": "BULL" if bull else "BEAR"}


# ================================ run log ================================
def run_logged(source="manual"):
    """Runs one scan and records date/time, duration and result in run_history.csv."""
    start = datetime.now()
    row = {"date": f"{start:%Y-%m-%d}", "start": f"{start:%H:%M:%S}", "end": "", "minutes": "",
           "status": "FAILED", "source": source, "coins_scanned": "", "trigger": "", "heating": "",
           "watch": "", "red_flags": "", "top3": "", "btc": "", "error": ""}
    try:
        row.update(scan() or {})
        row["status"] = "OK"
    except BaseException as e:            # also catches the "can't reach Bybit" exit
        row["error"] = str(e).replace("\n", " ")[:150]
        print("Scan failed:", row["error"])
    end = datetime.now()
    row["end"], row["minutes"] = f"{end:%H:%M:%S}", round((end - start).total_seconds() / 60, 1)
    pd.DataFrame([row]).to_csv(RUNS_FILE, mode="a", header=not os.path.exists(RUNS_FILE), index=False)


def show_runs(n=20):
    if not os.path.exists(RUNS_FILE):
        sys.exit("No runs logged yet.")
    runs = pd.read_csv(RUNS_FILE).fillna("")
    print(f"LAST {min(n, len(runs))} OF {len(runs)} RUNS  "
          f"(OK: {(runs['status'] == 'OK').sum()}, FAILED: {(runs['status'] == 'FAILED').sum()})\n")
    for _, r in runs.tail(n).iterrows():
        icon = "✅" if r["status"] == "OK" else "❌"
        i = lambda v: str(int(float(v))) if str(v) != "" else "-"
        info = (f"{i(r['trigger'])}🟢 {i(r['heating'])}🟠 {i(r['watch'])}🟡  top: {r['top3']}" if r["status"] == "OK"
                else f"error: {r['error']}")
        print(f"{icon} {r['date']} {r['start']}  {r['minutes']}min  [{r['source']}]  {info}")
    if os.name == "nt":
        q = subprocess.run(["schtasks", "/query", "/tn", "CryptoScannerV3", "/v", "/fo", "list"],
                           capture_output=True, text=True).stdout
        keep = [ln for ln in q.splitlines() if ln.split(":")[0].strip() in ("Next Run Time", "Last Run Time", "Status")]
        if keep:
            print("\nWindows task:\n  " + "\n  ".join(keep))


# ============================== other modes ==============================
def check_one(coin):
    ex = connect("spot")
    b = coin.upper()
    sym = f"{b}/USDT"
    try:
        exf = connect("swap")
    except SystemExit:
        exf = None
    cg_info, hot, trending = load_coingecko()
    news = update_news_cache(fetch_rss() + fetch_announcements())
    cn = news_by_coin(news, build_matchers([b], cg_info)).get(b)
    daily, h4 = fetch(ex, sym, "1d", 1000), fetch(ex, sym, "4h", 200)
    out = analyze(daily, h4, futures_data(exf, b, h4), cn, hot.get(b), b in trending, cg_info.get(b), verbose=True)
    print(f"\n{sym}:  tier {out['tier'] or 'none'}  |  score {out['score']}")
    print("  " + box_line(out["boxes"]) + ("  ✅Filters" if out["boxes"].get("filters") else "  ⬜Filters"))
    for n in out["notes"]:
        print("  •", n)
    for k, v in out["stats"].items():
        print(f"    {k}: {v:.4g}")
    if cn:
        for kind in ("bull", "bear", "red", "events"):
            for n in cn[kind][:3]:
                print(f"  [{kind}] {n['title'][:90]}")


def sniper():
    print("Listing sniper running (checks every 5 min). Ctrl+C to stop.")
    connect("spot")
    seen = {n["title"] for n in fetch_announcements()}
    while True:
        time.sleep(300)
        for n in fetch_announcements():
            if n["title"] not in seen:
                seen.add(n["title"])
                if re.search(r"\blist|launchpool|launchpad|delist", n["title"], re.I):
                    msg = f"🚨 {n['source']} ANNOUNCEMENT\n{n['title']}\n{n['link']}"
                    print(f"[{datetime.now():%H:%M}] {msg}\n")
                    send_telegram(msg)


def install_task(hours):
    folder = os.path.dirname(os.path.abspath(__file__))
    bat = os.path.join(folder, "run_scanner.bat")
    with open(bat, "w") as f:
        f.write(f'@echo off\ncd /d "{folder}"\n"{sys.executable}" "{os.path.abspath(__file__)}" --auto >> scanner_log.txt 2>&1\n')
    sched = ["/sc", "daily", "/st", "08:00"] if hours >= 24 else ["/sc", "hourly", "/mo", str(int(hours))]
    cmd = ["schtasks", "/create", "/tn", "CryptoScannerV3", "/tr", f'"{bat}"', *sched, "/f"]
    if os.name != "nt":
        print("Automatic task setup only works on Windows. Command would be:\n" + " ".join(cmd))
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if r.returncode == 0:
        print(f"✅ Automatic scans set: {'daily at 08:00' if hours >= 24 else f'every {int(hours)}h'}. "
              f"Laptop must be ON (sleep pauses it). Output -> latest_report.txt, scanner_log.txt, Telegram.")


def remove_task():
    r = subprocess.run(["schtasks", "/delete", "/tn", "CryptoScannerV3", "/f"], capture_output=True, text=True)
    print(r.stdout or r.stderr)


if __name__ == "__main__":
    a = sys.argv[1:]
    val = lambda k, d: a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else d
    if "--coin" in a:
        check_one(val("--coin", "BTC"))
    elif "--stats" in a:
        show_stats()
    elif "--runs" in a:
        show_runs(int(val("--runs", 20)) if val("--runs", "20").isdigit() else 20)
    elif "--sniper" in a:
        sniper()
    elif "--install-task" in a:
        install_task(float(val("--install-task", 4)))
    elif "--remove-task" in a:
        remove_task()
    elif "--every" in a:
        hrs = float(val("--every", 4))
        while True:
            run_logged("loop")
            print(f"Next scan in {hrs:g}h...\n")
            time.sleep(hrs * 3600)
    else:
        run_logged("auto" if "--auto" in a else "manual")
