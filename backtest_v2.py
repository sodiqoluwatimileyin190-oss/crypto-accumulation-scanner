"""
BACKTEST v2 - for accumulation_scanner_v2.py
=============================================
Measures whether the scanner actually finds winners, and WHICH factors matter.

What makes it fairer than the first backtest:
  1. Rolling test dates: scores every coin once a week over the past ~4 months
     (thousands of samples instead of 64).
  2. Point-in-time liquidity: coins are picked by their volume AT THAT DATE, not today's.
  3. Excess return: every coin is compared to the median coin on the same date,
     so a market-wide rally can't fake good results.
  4. Several horizons: 3, 7 and 14 days after the signal.
  5. Factor report: shows which of the factors came before real outperformance.

Needs accumulation_scanner_v2.py in the SAME folder.

USAGE
  python backtest_v2.py              -> 16 weekly test dates (default)
  python backtest_v2.py --weeks 10   -> fewer dates, faster
  python backtest_v2.py --from backtest_v2_XXXX.csv -> rebuild report from saved data (seconds)
Takes roughly 15-30 minutes. Results are saved to backtest_v2_*.csv and backtest_v2_report_*.txt
"""

import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import accumulation_scanner_v2 as sc

HORIZONS = (3, 7, 14)
HIT = {3: 0.15, 7: 0.20, 14: 0.30}    # "hit" = max gain reached within the horizon
BIG_PUMP = 0.50                        # 14-day max gain that counts as a big pump
UNIVERSE_MIN_VOL_TODAY = 50_000        # only skips totally dead markets; real filter is point-in-time
TOP_PICKS = 5                          # "buy the top 5 scores each week" strategy
FACTORS = ["obv", "ema", "squeeze", "volume", "updown", "rs", "base", "higher_lows", "trend"]
DAY = 86_400_000


def universe(ex):
    tickers = ex.fetch_tickers()
    out = []
    for sym, m in ex.markets.items():
        if not (m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)):
            continue
        base = m.get("base", "")
        if base in sc.SKIP_BASES or base.endswith(("3L", "3S", "2L", "2S")):
            continue
        if ((tickers.get(sym) or {}).get("quoteVolume") or 0) >= UNIVERSE_MIN_VOL_TODAY:
            out.append(sym)
    return out


def collect(weeks):
    ex = sc.connect("spot")
    btc = sc.fetch(ex, "BTC/USDT", "1d", 1000)
    today_open = int(btc["ts"].iloc[-1])                  # today's (incomplete) daily candle
    dates = [today_open - (14 + 7 * k) * DAY for k in range(weeks)]
    symbols = universe(ex)
    print(f"Collecting data for {len(symbols)} coins x {len(dates)} test dates "
          f"({datetime.fromtimestamp(dates[-1] / 1000, timezone.utc):%d %b} to "
          f"{datetime.fromtimestamp(dates[0] / 1000, timezone.utc):%d %b})...")

    rows = []
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        d = sc.fetch(ex, sym, "1d", 1000)
        h = sc.fetch(ex, sym, "4h", 1000)
        if d is None or h is None:
            continue
        done = d[d["ts"] < today_open]                    # completed days only
        for T in dates:
            past_d = d[d["ts"] < T]
            past_h = h[h["ts"] < T].iloc[-200:]
            if len(past_d) < sc.MIN_LISTING_DAYS or len(past_h) < 120:
                continue
            if past_h["ts"].iloc[-1] < T - 8 * 3_600_000:  # data gap / halted
                continue
            last7 = past_d.iloc[-7:]
            if (last7["volume"] * last7["close"]).mean() < sc.MIN_24H_VOLUME_USDT:
                continue                                   # point-in-time liquidity filter
            fwd = done[done["ts"] >= T]
            if len(fwd) < max(HORIZONS):
                continue

            out = sc.analyze(past_d, past_h, btc[btc["ts"] < T], verbose=True)
            if not out["stats"]:
                continue
            entry = past_h["close"].iloc[-1]
            row = {"symbol": sym, "date": T, "score": out["score"],
                   "raw_score": max(0, sum(out["parts"].values())),
                   "passed": out["stats"].get("passed", 0)}
            for f in FACTORS:
                row["f_" + f] = int(out["parts"].get(f, 0) > 0)
            for H in HORIZONS:
                w = fwd.iloc[:H]
                row[f"ret_{H}"] = w["close"].iloc[-1] / entry - 1
                row[f"max_{H}"] = w["high"].max() / entry - 1
                row[f"dd_{H}"] = w["low"].min() / entry - 1
            rows.append(row)
        if i % 25 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(symbols)} coins  ({el / 60:.1f} min, ~{el / i * (len(symbols) - i) / 60:.0f} min left)")

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for H in HORIZONS:  # excess return vs the median coin on the same date
        df[f"exc_{H}"] = df[f"ret_{H}"] - df.groupby("date")[f"ret_{H}"].transform("median")
    return df


def pct(x):
    return f"{x * 100:+.1f}%"


def report(df):
    out = []
    p = out.append
    n_dates = df["date"].nunique()
    p(f"BACKTEST v2 REPORT  -  {datetime.now():%Y-%m-%d %H:%M}")
    p(f"Samples: {len(df)}  |  coins: {df['symbol'].nunique()}  |  test dates: {n_dates}")
    p("Excess = return vs the median coin on the same date (removes market-wide rallies).\n")

    # 1. Score buckets
    bins = [-1, 29, 49, 64, 79, 100]
    labels = ["<30", "30-49", "50-64", "65-79", "80+"]
    df["bucket"] = pd.cut(df["score"], bins, labels=labels)
    for H in HORIZONS:
        p(f"=== {H}-DAY HORIZON (hit = touched +{HIT[H]:.0%}) ===")
        p(f"{'bucket':8s}{'n':>6s}{'avg excess':>12s}{'median exc':>12s}{'beat mkt':>10s}{'hit rate':>10s}{'avg dip':>10s}")
        for lab in labels + ["ALL"]:
            s = df if lab == "ALL" else df[df["bucket"] == lab]
            if len(s) == 0:
                continue
            p(f"{lab:8s}{len(s):6d}{pct(s[f'exc_{H}'].mean()):>12s}{pct(s[f'exc_{H}'].median()):>12s}"
              f"{(s[f'exc_{H}'] > 0).mean() * 100:9.0f}%{(s[f'max_{H}'] >= HIT[H]).mean() * 100:9.0f}%"
              f"{pct(s[f'dd_{H}'].mean()):>10s}")
        corr = df["score"].rank().corr(df[f"exc_{H}"].rank())   # Spearman without scipy
        p(f"Score vs excess correlation: {corr:+.3f}  (>+0.05 = useful, ~0 = no edge, <0 = backwards)\n")

    # 2. Strategy: top N scores each week
    p(f"=== STRATEGY: buy the top {TOP_PICKS} scores each test date ===")
    top = df[df["passed"] == 1].sort_values("score", ascending=False).groupby("date").head(TOP_PICKS)
    for H in HORIZONS:
        p(f"{H:>3d}d: avg excess {pct(top[f'exc_{H}'].mean())} | beat market {(top[f'exc_{H}'] > 0).mean() * 100:.0f}% "
          f"| hit rate {(top[f'max_{H}'] >= HIT[H]).mean() * 100:.0f}% (all coins: {(df[f'max_{H}'] >= HIT[H]).mean() * 100:.0f}%)")
    p("")

    # 3. Factor report (14d main)
    H = 14
    p(f"=== FACTOR REPORT ({H}-day) - which signals came before outperformance ===")
    p(f"{'factor':13s}{'fired n':>8s}{'exc fired':>11s}{'exc not':>10s}{'LIFT':>9s}{'hit fired':>11s}{'hit not':>9s}")
    lifts = {}
    for f in FACTORS + ["passed"]:
        col = "f_" + f if f != "passed" else "passed"
        on, off = df[df[col] == 1], df[df[col] == 0]
        if len(on) < 20 or len(off) < 20:
            p(f"{f:13s}{len(on):8d}   (too few samples)")
            continue
        lift = on[f"exc_{H}"].mean() - off[f"exc_{H}"].mean()
        hit_on = (on[f"max_{H}"] >= HIT[H]).mean()
        hit_off = (off[f"max_{H}"] >= HIT[H]).mean()
        lifts[f] = (lift, hit_on - hit_off)
        p(f"{f:13s}{len(on):8d}{pct(on[f'exc_{H}'].mean()):>11s}{pct(off[f'exc_{H}'].mean()):>10s}"
          f"{pct(lift):>9s}{hit_on * 100:10.0f}%{hit_off * 100:8.0f}%")
    p("LIFT > 0 = coins with this signal beat coins without it.\n")

    # 4. Big pump fingerprint
    pumps = df[df["max_14"] >= BIG_PUMP]
    p(f"=== BIG PUMP FINGERPRINT: {len(pumps)} cases of +{BIG_PUMP:.0%} within 14 days ===")
    if len(pumps):
        p(f"{'factor':13s}{'in pumps':>10s}{'in all':>9s}{'ratio':>8s}")
        for f in FACTORS:
            a, b = pumps["f_" + f].mean(), df["f_" + f].mean()
            p(f"{f:13s}{a * 100:9.0f}%{b * 100:8.0f}%{(a / b if b else 0):8.2f}x")
        p("Ratio > 1.2x = this signal shows up more often before big pumps.")
        p("Avg score before big pumps: "
          f"{pumps['score'].mean():.0f} (all samples: {df['score'].mean():.0f})\n")

    # 5. Suggested weights
    p("=== SUGGESTED WEIGHTS (data-driven, use with judgment) ===")
    good = {f: max(0, l) + max(0, h) * 0.5 for f, (l, h) in lifts.items()
            if f != "passed" and l > 0 and h > 0}
    if good:
        total = sum(good.values())
        for f in FACTORS:
            w = round(good.get(f, 0) / total * 100) if f in good else 0
            p(f"  {f:13s} {w:3d}   {'(drop or reduce)' if w == 0 else ''}")
        p("Only factors that improved BOTH excess return and hit rate get weight.")
    else:
        p("  No factor showed a clear positive edge in this sample.")
    return "\n".join(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    weeks = int(args[args.index("--weeks") + 1]) if "--weeks" in args else 16
    weeks = max(4, min(weeks, 16))   # 4H history limits how far back we can go
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if "--from" in args:             # rebuild the report from a saved CSV (no re-download)
        df = pd.read_csv(args[args.index("--from") + 1])
    else:
        df = collect(weeks)
        if df.empty:
            sys.exit("No samples collected.")
        df.to_csv(f"backtest_v2_{stamp}.csv", index=False)
    text = report(df)
    print("\n" + text)
    with open(f"backtest_v2_report_{stamp}.txt", "w") as f:
        f.write(text)
    print(f"\nSaved: backtest_v2_{stamp}.csv and backtest_v2_report_{stamp}.txt")
