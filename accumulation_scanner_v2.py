"""
ACCUMULATION BREAKOUT SCANNER v2 - Bybit (USDT pairs)
======================================================
Finds coins in quiet accumulation BEFORE the big leg up, and filters out dead coins.

Public data only. No API keys, no trading access.

USAGE
  python accumulation_scanner_v2.py                    -> scan once
  python accumulation_scanner_v2.py --loop             -> scan every 4 hours
  python accumulation_scanner_v2.py --coin GRAM        -> full breakdown for one coin
  python accumulation_scanner_v2.py --coin QNT --ago 3 -> score a coin as it looked 3 days ago
  python accumulation_scanner_v2.py --backtest 30      -> score all coins 30 days ago, check what happened after

FILES IT USES / CREATES
  blacklist.txt      -> coins to always skip (edit by hand)
  scan_history.csv   -> every scan's scores (powers the NEW / RISING tags)
  scan_*.csv         -> full results of each scan
  backtest_*.csv     -> backtest results
"""

import json
import os
import sys
import time
from datetime import datetime

import ccxt
import numpy as np
import pandas as pd
import requests

# =========================== SETTINGS ===========================
# Filters (coins failing these are skipped entirely)
MIN_24H_VOLUME_USDT = 1_000_000   # liquidity: skip easy-to-manipulate coins
MIN_LISTING_DAYS    = 200         # skip coins with less than ~7 months of history
MIN_DRAWDOWN        = 0.50        # must be at least 50% below its high
MAX_EXTENSION       = 0.60        # skip if already >60% above base low (too late)
MAX_RECENT_RUN      = 0.30        # skip if already ran >30% in last 3 days (too late)

# Scoring
MAX_BASE_RANGE      = 0.50        # base high/low range allowed when measuring base length
LOW_FLOAT_LIMIT     = 0.50        # penalty if less than 50% of supply is circulating
LOW_FLOAT_PENALTY   = 10

# Alerts
MIN_SCORE_TO_ALERT  = 65
TOP_N               = 15
LOOP_HOURS          = 4
ALERT_IN_BEAR_MARKET = False      # if BTC is below its 200-day EMA, don't send Telegram alerts

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

BLACKLIST_FILE = "blacklist.txt"
HISTORY_FILE   = "scan_history.csv"
SKIP_BASES = {"USDC", "USDE", "DAI", "FDUSD", "TUSD", "PYUSD", "USDD", "EUR", "BUSD", "USD1", "BTC"}
# ================================================================


# ------------------------------ connection ------------------------------
def connect(market_type="spot"):
    """Tries Bybit's main domain, then the backup domain (bytick.com)."""
    last_err = None
    for host in ("bybit.com", "bytick.com"):
        ex = ccxt.bybit({"enableRateLimit": True, "hostname": host,
                         "options": {"defaultType": market_type}})
        try:
            ex.load_markets()
            return ex
        except Exception as e:
            last_err = e
    raise SystemExit("\nCan't reach Bybit. Check internet, set DNS to 1.1.1.1 / 8.8.8.8, "
                     "or use a VPN.\n" + str(last_err)[:200])


def fetch(ex, symbol, timeframe, limit):
    for _ in range(3):
        try:
            data = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if data:
                return pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        except Exception:
            time.sleep(2)
    return None


def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "w") as f:
            f.write("# One coin per line (e.g. RESOLV). Lines starting with # are ignored.\n"
                    "# Add coins with a Binance Monitoring Tag, recent hacks, or delistings.\n"
                    "RESOLV\n"
                    "JASMY\n")
    with open(BLACKLIST_FILE) as f:
        return {ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")}


def liquid_symbols(ex, blacklist=frozenset()):
    tickers = ex.fetch_tickers()
    out = []
    for sym, m in ex.markets.items():
        if not (m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)):
            continue
        base = m.get("base", "")
        if base in SKIP_BASES or base in blacklist or base.endswith(("3L", "3S", "2L", "2S")):
            continue
        if ((tickers.get(sym) or {}).get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT:
            out.append(sym)
    return out


# --------------------------- external data ---------------------------
def load_tokenomics():
    """Circulating-supply % from CoinGecko's free API (top ~1000 coins). Fails gracefully."""
    out = {}
    for page in (1, 2, 3, 4):
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                             params={"vs_currency": "usd", "order": "market_cap_desc",
                                     "per_page": 250, "page": page}, timeout=20)
            if r.status_code != 200:
                break
            for c in r.json():
                sym = (c.get("symbol") or "").upper()
                if sym in out:
                    continue  # keep the biggest coin with that ticker
                circ = c.get("circulating_supply") or 0
                supply = c.get("max_supply") or c.get("total_supply") or 0
                out[sym] = {"mcap": c.get("market_cap") or 0,
                            "float": (circ / supply) if supply else None}
            time.sleep(3)
        except Exception:
            break
    print(f"Tokenomics loaded for {len(out)} coins" if out else "Tokenomics unavailable (skipping)")
    return out


def futures_data(exf, base, h4):
    """Funding rate + 7-day open interest change from Bybit perps. None if no perp."""
    if exf is None:
        return None
    sym = f"{base}/USDT:USDT"
    if sym not in exf.markets:
        return None
    try:
        funding = exf.fetch_funding_rate(sym).get("fundingRate")
        oi = sorted(exf.fetch_open_interest_history(sym, "4h", limit=50),
                    key=lambda x: x.get("timestamp") or 0)
        vals = [x.get("openInterestAmount") or x.get("openInterestValue") for x in oi]
        vals = [v for v in vals if v]
        oi_chg = vals[-1] / vals[-43] - 1 if len(vals) >= 43 else None
        price_chg = h4["close"].iloc[-1] / h4["close"].iloc[-43] - 1
        return {"funding": funding, "oi_chg": oi_chg, "price_chg_7d": price_chg}
    except Exception:
        return None


# ------------------------------ indicators ------------------------------
def macd(close, fast=12, slow=26, signal=9):
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line, line - line.ewm(span=signal, adjust=False).mean()


def supertrend_up(df, period=10, mult=3.0):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().values
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub, flb = ub.copy(), lb.copy()
    up = np.ones(len(c), dtype=bool)
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        up[i] = c[i] >= flb[i] if up[i - 1] else c[i] > fub[i]
    return up


# ------------------------------- scoring -------------------------------
def analyze(daily, h4, btc_daily, fut=None, tok=None, verbose=False):
    """Returns dict(score, reasons, stats, parts) or None if filtered out (unless verbose)."""
    def reject(msg):
        return {"score": 0, "reasons": [msg], "stats": {}, "parts": {}} if verbose else None

    if daily is None or h4 is None or len(daily) < MIN_LISTING_DAYS or len(h4) < 120:
        return reject(f"not enough history (needs {MIN_LISTING_DAYS}+ days)")

    close = h4["close"].iloc[-1]
    drawdown = 1 - close / daily["high"].max()
    base = daily.iloc[-67:-7]
    base_low = base["low"].min()
    extension = close / base_low - 1
    recent_run = close / h4["low"].iloc[-18:].min() - 1
    stats = {"price": close, "drawdown": drawdown, "extension": extension, "run_3d": recent_run}

    problems = []
    if drawdown < MIN_DRAWDOWN:
        problems.append(f"only {drawdown:.0%} below high (not beaten down)")
    if extension > MAX_EXTENSION:
        problems.append(f"already {extension:.0%} above base (late)")
    if recent_run > MAX_RECENT_RUN:
        problems.append(f"already ran {recent_run:.0%} in 3 days (late)")
    if problems and not verbose:
        return None

    stats["passed"] = int(not problems)
    parts, reasons = {}, list(problems)

    # 1. OBV accumulation (15): buying volume building while price stays flat
    w = daily.iloc[-45:]
    direction = np.sign(w["close"].diff().fillna(0))
    obv_ratio = (direction * w["volume"]).sum() / max(w["volume"].sum(), 1e-12)
    price_chg_45 = w["close"].iloc[-1] / w["close"].iloc[0] - 1
    stats["obv_ratio"] = obv_ratio
    if obv_ratio > 0.15 and price_chg_45 < 0.25:
        parts["obv"] = 15; reasons.append(f"OBV accumulation ({obv_ratio:+.0%}) while price flat")
    elif obv_ratio > 0.15:
        parts["obv"] = 10; reasons.append(f"OBV rising ({obv_ratio:+.0%})")
    elif obv_ratio > 0.05:
        parts["obv"] = 5; reasons.append(f"OBV slightly rising ({obv_ratio:+.0%})")

    # 2. Distance to daily 200 EMA (15): the "pushing the blue line" stage
    ema200 = daily["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    dist = close / ema200 - 1
    stats["ema200_dist"] = dist
    if -0.05 <= dist <= 0.10:
        parts["ema"] = 15; reasons.append(f"at/crossing 200 EMA ({dist:+.0%})")
    elif -0.15 <= dist < -0.05:
        parts["ema"] = 8; reasons.append(f"approaching 200 EMA ({dist:+.0%})")
    elif 0.10 < dist <= 0.25:
        parts["ema"] = 5; reasons.append(f"above 200 EMA ({dist:+.0%})")

    # 3. Volatility squeeze (10): Bollinger width near a 90-day low
    mid = daily["close"].rolling(20).mean()
    width = 4 * daily["close"].rolling(20).std() / mid
    recent_min = width.iloc[-10:].min()
    hist_w = width.iloc[-100:-10].dropna()
    squeeze_pct = (hist_w < recent_min).mean() if len(hist_w) else 1
    stats["squeeze_pct"] = squeeze_pct
    if squeeze_pct <= 0.15:
        parts["squeeze"] = 10; reasons.append("tight volatility squeeze")
    elif squeeze_pct <= 0.30:
        parts["squeeze"] = 5; reasons.append("volatility contracting")

    # 4. Volume waking up on 4H (10)
    qvol = h4["volume"] * h4["close"]
    v_base = qvol.iloc[-72:-12].mean()
    vr = qvol.iloc[-12:].mean() / v_base if v_base > 0 else 0
    stats["volume_ratio"] = vr
    if 1.3 <= vr <= 4:
        parts["volume"] = 10; reasons.append(f"volume waking up ({vr:.1f}x)")
    elif vr > 4:
        parts["volume"] = 3; reasons.append(f"volume spike ({vr:.1f}x), may be late")

    # 5. Up-day vs down-day volume, last 30 days (10)
    w30 = daily.iloc[-30:]
    up_v = w30.loc[w30["close"] > w30["open"], "volume"].sum()
    dn_v = w30.loc[w30["close"] < w30["open"], "volume"].sum()
    ud = up_v / dn_v if dn_v > 0 else 3
    stats["up_down_vol"] = ud
    if ud >= 1.5:
        parts["updown"] = 10; reasons.append(f"buyers dominate volume ({ud:.1f}x)")
    elif ud >= 1.2:
        parts["updown"] = 6; reasons.append(f"buyers ahead on volume ({ud:.1f}x)")

    # 6. Relative strength vs BTC, 14 days (10)
    if btc_daily is not None and len(btc_daily) > 15 and len(daily) > 15:
        rs = (daily["close"].iloc[-1] / daily["close"].iloc[-15]) - \
             (btc_daily["close"].iloc[-1] / btc_daily["close"].iloc[-15])
        stats["rs_vs_btc"] = rs
        if rs >= 0.10:
            parts["rs"] = 10; reasons.append(f"beating BTC by {rs:+.0%} (14d)")
        elif rs >= 0:
            parts["rs"] = 5; reasons.append(f"holding vs BTC ({rs:+.0%})")

    # 7. Base length + tightness (10)
    for n, pts in ((120, 10), (90, 8), (60, 6), (30, 3)):
        if len(daily) >= n + 7:
            win = daily.iloc[-(n + 7):-7]
            rng = win["high"].max() / win["low"].min() - 1
            if rng <= MAX_BASE_RANGE:
                parts["base"] = pts; stats["base_days"] = n
                reasons.append(f"{n}d+ base ({rng:.0%} range)")
                break

    # 8. Higher lows on 4H (5)
    lows = h4["low"].iloc[-60:].values
    if lows[:20].min() < lows[20:40].min() < lows[40:].min():
        parts["higher_lows"] = 5; reasons.append("higher lows")

    # 9. Trend flip + MACD on 4H (5)
    trend = 0
    if supertrend_up(h4)[-1]:
        trend += 3; reasons.append("4H trend up")
    line, hist = macd(h4["close"])
    if line.iloc[-1] > 0 and hist.iloc[-1] > 0:
        trend += 2; reasons.append("MACD above zero")
    if trend:
        parts["trend"] = trend

    # 10. Futures squeeze setup (10): shorts piling in while price is flat
    if fut:
        f_pts = 0
        if fut.get("funding") is not None:
            stats["funding"] = fut["funding"]
            if fut["funding"] < 0:
                f_pts += 4; reasons.append(f"negative funding ({fut['funding']:.4%})")
        if fut.get("oi_chg") is not None:
            stats["oi_chg_7d"] = fut["oi_chg"]
            if fut["oi_chg"] >= 0.15 and fut["price_chg_7d"] <= 0.15:
                f_pts += 6; reasons.append(f"open interest up {fut['oi_chg']:+.0%} (squeeze fuel)")
        if f_pts:
            parts["futures"] = f_pts

    # Penalty: low float = heavy unlocks ahead
    if tok and tok.get("float") is not None:
        stats["float"] = tok["float"]
        if tok["float"] < LOW_FLOAT_LIMIT:
            parts["low_float"] = -LOW_FLOAT_PENALTY
            reasons.append(f"LOW FLOAT: only {tok['float']:.0%} circulating (unlocks ahead)")

    score = max(0, sum(parts.values())) if not problems else 0
    return {"score": score, "reasons": reasons, "stats": stats, "parts": parts}


# ------------------------------ history / tags ------------------------------
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE)
        except Exception:
            pass
    return pd.DataFrame(columns=["time", "symbol", "score"])


def tags_for(hist, sym, score):
    prev = hist.loc[hist["symbol"] == sym].sort_values("time")["score"].tolist()
    tags = []
    if score >= MIN_SCORE_TO_ALERT and not any(s >= MIN_SCORE_TO_ALERT for s in prev):
        tags.append("NEW")
    if (len(prev) >= 2 and score > prev[-1] > prev[-2]) or (prev and score - prev[-1] >= 10):
        tags.append("RISING")
    return tags


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def btc_regime(btc_daily):
    ema = btc_daily["close"].ewm(span=200, adjust=False).mean().iloc[-1]
    return btc_daily["close"].iloc[-1] > ema


# --------------------------------- modes ---------------------------------
def scan():
    now = datetime.now()
    blacklist = load_blacklist()
    ex = connect("spot")
    try:
        exf = connect("swap")
    except SystemExit:
        exf = None
    tok_data = load_tokenomics()
    btc_daily = fetch(ex, "BTC/USDT", "1d", 400)
    bull = btc_regime(btc_daily) if btc_daily is not None else True
    symbols = liquid_symbols(ex, blacklist)
    print(f"[{now:%Y-%m-%d %H:%M}] BTC regime: {'BULL' if bull else 'BEAR'} | "
          f"blacklisted: {len(blacklist)} | scanning {len(symbols)} pairs...")

    results = []
    for i, sym in enumerate(symbols, 1):
        base = ex.markets[sym]["base"]
        daily = fetch(ex, sym, "1d", 1000)
        if daily is None or len(daily) < MIN_LISTING_DAYS:
            continue
        h4 = fetch(ex, sym, "4h", 200)
        quick = analyze(daily, h4, btc_daily)          # cheap pass first
        if not quick:
            continue
        fut = futures_data(exf, base, h4)              # extra calls only for survivors
        out = analyze(daily, h4, btc_daily, fut, tok_data.get(base))
        if out:
            results.append({"symbol": sym, "score": out["score"], **out["stats"],
                            "reasons": "; ".join(out["reasons"])})
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} done")

    if not results:
        print("No candidates this run.")
        return
    df = pd.DataFrame(results).sort_values("score", ascending=False)

    hist = load_history()
    df["tags"] = [" ".join(tags_for(hist, s, sc)) for s, sc in zip(df["symbol"], df["score"])]
    stamp = now.strftime("%Y-%m-%d %H:%M")
    new_rows = pd.DataFrame({"time": stamp, "symbol": df["symbol"], "score": df["score"]})
    new_rows.to_csv(HISTORY_FILE, mode="a", header=not os.path.exists(HISTORY_FILE), index=False)

    fname = f"scan_{now:%Y%m%d_%H%M}.csv"
    df.to_csv(fname, index=False)
    print(f"\nSaved all results to {fname}")

    hits = df[df["score"] >= MIN_SCORE_TO_ALERT].head(TOP_N)
    if hits.empty:
        print(f"No coins scored {MIN_SCORE_TO_ALERT}+ this run.")
        return

    lines = [f"ACCUMULATION SCANNER v2 - {now:%d %b %H:%M}",
             f"BTC regime: {'BULL' if bull else 'BEAR (be careful)'}"]
    for _, r in hits.iterrows():
        tag = f" [{r['tags']}]" if r["tags"] else ""
        lines.append(f"\n{r['symbol']}  score {r['score']}{tag}  price {r['price']:.6g}\n  {r['reasons']}")
    lines.append("\nCandidates only. Check chart + news, and use a stop-loss.")
    msg = "\n".join(lines)
    print(msg)
    if bull or ALERT_IN_BEAR_MARKET:
        send_telegram(msg)
    else:
        print("\n(BTC below 200-day EMA: Telegram alert paused. Set ALERT_IN_BEAR_MARKET = True to override.)")


def check_one(coin, ago=0):
    ex = connect("spot")
    sym = f"{coin.upper()}/USDT"
    daily = fetch(ex, sym, "1d", 1000)
    h4 = fetch(ex, sym, "4h", min(1000, 200 + ago * 6))
    btc = fetch(ex, "BTC/USDT", "1d", 400)
    fut = tok = None
    if ago:
        daily, h4, btc = daily.iloc[:-ago], h4.iloc[:-ago * 6], btc.iloc[:-ago]
    else:
        try:
            fut = futures_data(connect("swap"), coin.upper(), h4)
        except SystemExit:
            pass
        tok = load_tokenomics().get(coin.upper())
    out = analyze(daily, h4, btc, fut, tok, verbose=True)
    print(f"\n{sym} ({ago} days ago)" if ago else f"\n{sym} (now)")
    print(f"SCORE: {out['score']}/100")
    for k, v in out["parts"].items():
        print(f"  {k:12s} {v:+d}")
    print("Reasons:")
    for r in out["reasons"]:
        print("  -", r)
    print("Stats:")
    for k, v in out["stats"].items():
        print(f"  {k}: {v:.4g}")


def backtest(ago):
    ago = max(5, min(ago, 130))
    ex = connect("spot")
    btc = fetch(ex, "BTC/USDT", "1d", 1000)
    btc_past = btc.iloc[:-ago]
    btc_ret = btc["close"].iloc[-1] / btc_past["close"].iloc[-1] - 1
    symbols = liquid_symbols(ex)
    print(f"Backtest: scoring {len(symbols)} coins as of {ago} days ago "
          f"(futures + tokenomics not included)...")
    rows = []
    for i, sym in enumerate(symbols, 1):
        daily = fetch(ex, sym, "1d", 1000)
        h4 = fetch(ex, sym, "4h", 200 + ago * 6)
        if daily is None or h4 is None or len(daily) < MIN_LISTING_DAYS + ago:
            continue
        past_d, past_h = daily.iloc[:-ago], h4.iloc[:-ago * 6]
        out = analyze(past_d, past_h, btc_past)
        if not out:
            continue
        entry = past_h["close"].iloc[-1]
        fwd = daily.iloc[-ago:]
        rows.append({"symbol": sym, "score": out["score"],
                     "max_gain": fwd["high"].max() / entry - 1,
                     "max_drop": fwd["low"].min() / entry - 1,
                     "end_return": daily["close"].iloc[-1] / entry - 1})
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} done")

    df = pd.DataFrame(rows)
    if df.empty:
        print("No results.")
        return
    fname = f"backtest_{ago}d_{datetime.now():%Y%m%d_%H%M}.csv"
    df.sort_values("score", ascending=False).to_csv(fname, index=False)
    df["bucket"] = pd.cut(df["score"], [-1, 49, 64, 79, 100], labels=["<50", "50-64", "65-79", "80+"])
    summary = df.groupby("bucket", observed=True).agg(
        coins=("symbol", "count"),
        avg_max_gain=("max_gain", "mean"),
        avg_end_return=("end_return", "mean"),
        hit_30pct=("max_gain", lambda s: (s >= 0.30).mean()),
        avg_max_drop=("max_drop", "mean"))
    print(f"\nBTC over same period: {btc_ret:+.1%}")
    print((summary * [1, 100, 100, 100, 100]).round(1).rename(columns=lambda c: c + ("" if c == "coins" else " %")))
    print(f"\nHigher score buckets should show better returns. Saved details to {fname}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--coin" in args:
        c = args[args.index("--coin") + 1]
        a = int(args[args.index("--ago") + 1]) if "--ago" in args else 0
        check_one(c, a)
    elif "--backtest" in args:
        backtest(int(args[args.index("--backtest") + 1]))
    elif "--loop" in args:
        while True:
            try:
                scan()
            except Exception as e:
                print("Scan failed:", e)
            print(f"Sleeping {LOOP_HOURS}h...\n")
            time.sleep(LOOP_HOURS * 3600)
    else:
        scan()
