#!/usr/bin/env python3
"""
NSE 44-EMA candle scanner (runs in GitHub Actions, publishes to GitHub Pages).

For each timeframe it checks the LAST COMPLETED candle of every stock:
  ABOVE: close > EMA44 and >= 50% of the candle body (|close-open|) is above the EMA
  BELOW: close < EMA44 and >= 50% of the candle body is below the EMA

Timeframes
  5m, 15m, 30m, 1h, 75m  -> Nifty 500 stocks (built from Yahoo 5-minute data)
  1D, 1W                 -> all NSE equities (EQ series)

Output: <out>/index.html (copied from this folder) and <out>/data.json
"""
import argparse
import datetime as dt
import io
import json
import os
import shutil
import sys
import time

import pandas as pd

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EMA_LEN = 44
MIN_BODY = 50.0
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
INTRADAY = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "75m": 75}
NSE = "https://archives.nseindia.com/content"
SOURCES = {
    "all": (f"{NSE}/equities/EQUITY_L.csv", "EQUITY_L.csv"),
    "n500": (f"{NSE}/indices/ind_nifty500list.csv", "ind_nifty500list.csv"),
    "n50": (f"{NSE}/indices/ind_nifty50list.csv", "ind_nifty50list.csv"),
}


def now_ist():
    return dt.datetime.now(IST)


# ------------------------------------------------------------------ symbol lists
def _read_list(key):
    """Download an NSE list; fall back to a copy uploaded to the repo's symbols/ folder."""
    import requests
    url, fname = SOURCES[key]
    text = None
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        r.raise_for_status()
        text = r.text
    except Exception as e:
        print(f"  NSE download failed for {fname}: {e}")
        local = os.path.join(HERE, "symbols", fname)
        if os.path.exists(local):
            print(f"  using repo copy symbols/{fname}")
            text = open(local, encoding="utf-8", errors="ignore").read()
    if not text:
        return {}
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().upper() for c in df.columns]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    name_col = "NAME OF COMPANY" if "NAME OF COMPANY" in df.columns else "COMPANY NAME"
    return dict(zip(df["SYMBOL"].astype(str).str.strip(), df[name_col].astype(str).str.strip()))


def load_universe():
    n50, n500, allq = _read_list("n50"), _read_list("n500"), _read_list("all")
    names = {**allq, **n500, **n50}
    if not names:
        sys.exit("No symbol list available. Upload NSE CSVs to the symbols/ folder (see README).")
    tier = {s: 3 for s in names}
    for s in n500:
        tier[s] = 2
    for s in n50:
        tier[s] = 1
    intraday = sorted(n500 or n50 or names)
    return names, tier, intraday


# ------------------------------------------------------------------ rule
def evaluate(bars):
    """bars: completed OHLCV candles, oldest first. Returns a result dict or None."""
    bars = bars.dropna(subset=["Open", "High", "Low", "Close"])
    if len(bars) < EMA_LEN + 6:
        return None
    ema = bars["Close"].ewm(span=EMA_LEN, adjust=False).mean()
    o, h, l, c = (float(bars[k].iloc[-1]) for k in ("Open", "High", "Low", "Close"))
    e = float(ema.iloc[-1])
    body = abs(c - o)
    if body <= 0:
        return None
    top, bot = max(o, c), min(o, c)
    above = max(0.0, top - max(bot, e)) / body * 100
    below = max(0.0, min(top, e) - bot) / body * 100
    if c > e and above >= MIN_BODY:
        side, pct = "ABOVE", above
    elif c < e and below >= MIN_BODY:
        side, pct = "BELOW", below
    else:
        return None
    pc, pe = float(bars["Close"].iloc[-2]), float(ema.iloc[-2])
    e5 = float(ema.iloc[-6])
    vol = float(bars["Volume"].iloc[-1] or 0)
    avg = float(bars["Volume"].iloc[-21:-1].mean() or 0)
    return {
        "side": side, "b": round(pct, 1),
        "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "e": round(e, 2),
        "d": round((c - e) / e * 100, 2),
        "chg": round((c - pc) / pc * 100, 2) if pc else 0,
        "f": (side == "ABOVE" and pc <= pe) or (side == "BELOW" and pc >= pe),
        "sl": round((e - e5) / e5 * 100, 2),
        "v": int(vol), "vx": round(vol / avg, 2) if avg else None,
    }


# ------------------------------------------------------------------ data
def download(symbols, chunk=100, **kw):
    """Yields (symbol, ohlcv DataFrame) using yfinance in batches."""
    import yfinance as yf
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        tick = [s + ".NS" for s in part]
        for attempt in range(3):
            try:
                data = yf.download(tick, group_by="ticker", auto_adjust=False, threads=True,
                                   progress=False, **kw)
                break
            except Exception as ex:
                print(f"  batch {i} attempt {attempt + 1} failed: {ex}")
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for s, t in zip(part, tick):
            try:
                d = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                d = d.dropna(how="all")
                if len(d):
                    yield s, d
            except KeyError:
                pass
        time.sleep(0.5)


def to_ist(idx):
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize("UTC").tz_convert(IST) if idx.tz is None else idx.tz_convert(IST)


def resample_intraday(d5, minutes):
    """Group 5m bars into N-minute candles anchored at 09:15 IST each day (TradingView style)."""
    if minutes == 5:
        out = d5.copy()
        out["end"] = out.index + pd.Timedelta(minutes=5)
        return out
    day = d5.index.normalize()
    open_t = day + pd.Timedelta(hours=9, minutes=15)
    k = ((d5.index - open_t) // pd.Timedelta(minutes=minutes)).astype(int)
    start = open_t + pd.to_timedelta(k * minutes, unit="m")
    g = d5.groupby(start)
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    close_t = out.index.normalize() + pd.Timedelta(hours=15, minutes=30)
    out["end"] = pd.Series(out.index + pd.Timedelta(minutes=minutes), index=out.index).where(
        lambda x: x <= close_t, close_t)
    return out


def scan_intraday(symbols, names, tier):
    now = now_ist()
    res = {tf: [] for tf in INTRADAY}
    scanned = failed = 0
    start = (now - dt.timedelta(days=55)).date()
    for s, d in download(symbols, interval="5m", start=str(start), prepost=False):
        scanned += 1
        try:
            d = d.copy()
            d.index = to_ist(d.index)
            d = d[(d.index.time >= dt.time(9, 15)) & (d.index.time < dt.time(15, 30))]
            d = d.dropna(subset=["Close"])
            d = d[d.index + pd.Timedelta(minutes=5) <= now]           # completed 5m bars only
            if d.empty:
                continue
            last_end = d.index[-1] + pd.Timedelta(minutes=5)
            for tf, m in INTRADAY.items():
                bars = resample_intraday(d, m)
                bars = bars[bars["end"] <= last_end]                     # completed candles only
                r = evaluate(bars)
                if r:
                    r.update(s=s, n=names.get(s, s), u=tier.get(s, 3),
                             t=bars.index[-1].strftime("%d %b %H:%M"))
                    res[tf].append(r)
        except Exception as ex:
            failed += 1
            print(f"  {s}: {ex}")
    meta = {"scanned": scanned, "requested": len(symbols), "failed": failed}
    return res, meta


def scan_daily(symbols, names, tier):
    now = now_ist()
    res = {"1D": [], "1W": []}
    scanned = failed = 0
    for s, d in download(symbols, interval="1d", period="2y"):
        scanned += 1
        try:
            d = d.copy()
            d.index = to_ist(d.index).normalize()
            d = d.dropna(subset=["Close"])
            # today's daily candle only counts after the 15:30 close
            if len(d) and d.index[-1].date() == now.date() and now.time() < dt.time(15, 30):
                d = d.iloc[:-1]
            r = evaluate(d)
            if r:
                r.update(s=s, n=names.get(s, s), u=tier.get(s, 3), t=d.index[-1].strftime("%d %b %Y"))
                res["1D"].append(r)
            w = d.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                         "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
            if len(w):
                fri_close = w.index[-1].replace(tzinfo=None) + pd.Timedelta(hours=15, minutes=30)
                if now.replace(tzinfo=None) < fri_close:                # current week still open
                    w = w.iloc[:-1]
                w.index = w.index - pd.Timedelta(days=4)                  # label weeks by Monday
            r = evaluate(w)
            if r:
                r.update(s=s, n=names.get(s, s), u=tier.get(s, 3), t="wk of " + w.index[-1].strftime("%d %b"))
                res["1W"].append(r)
        except Exception as ex:
            failed += 1
            print(f"  {s}: {ex}")
    meta = {"scanned": scanned, "requested": len(symbols), "failed": failed}
    return res, meta


# ------------------------------------------------------------------ caching
def last_cutoff(now, hh, mm):
    """Most recent weekday at hh:mm IST that is <= now."""
    c = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if c > now:
        c -= dt.timedelta(days=1)
    while c.weekday() >= 5:
        c -= dt.timedelta(days=1)
    return c


def load_cache(name):
    p = os.path.join(CACHE_DIR, name)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def save_cache(name, obj):
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(obj, open(os.path.join(CACHE_DIR, name), "w"))


def market_open(now):
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 45)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--symbols", help="comma list to test with (skips NSE lists)")
    ap.add_argument("--force", action="store_true", help="ignore cache")
    args = ap.parse_args()
    now = now_ist()
    print(f"Scan started {now:%Y-%m-%d %H:%M} IST")

    if args.symbols:
        syms = [x.strip().upper() for x in args.symbols.split(",")]
        names, tier, intraday_syms = {s: s for s in syms}, {s: 1 for s in syms}, syms
    else:
        names, tier, intraday_syms = load_universe()
    all_syms = sorted(names)
    print(f"  universe: {len(all_syms)} stocks, intraday list: {len(intraday_syms)}")

    # Daily / weekly: only changes once a day, so reuse the result until the next close
    daily = None if args.force else load_cache("daily.json")
    if not daily or daily["computed_at"] < last_cutoff(now, 15, 45).isoformat():
        print("Scanning daily/weekly ...")
        r, m = scan_daily(all_syms, names, tier)
        daily = {"computed_at": now.isoformat(), "res": r, "meta": m}
        save_cache("daily.json", daily)
    else:
        print("Daily/weekly unchanged since last close, reusing")

    # Intraday: rescan during market hours, otherwise reuse the final scan of the day
    intra = None if args.force else load_cache("intraday.json")
    if market_open(now) or not intra or intra["computed_at"] < last_cutoff(now, 15, 35).isoformat():
        print("Scanning intraday ...")
        r, m = scan_intraday(intraday_syms, names, tier)
        intra = {"computed_at": now.isoformat(), "res": r, "meta": m}
        save_cache("intraday.json", intra)
    else:
        print("Market closed, reusing last intraday scan")

    tfs = {}
    for tf in INTRADAY:
        tfs[tf] = {"rows": intra["res"][tf], "computed_at": intra["computed_at"],
                   "universe": "Nifty 500", **intra["meta"]}
    for tf in ("1D", "1W"):
        tfs[tf] = {"rows": daily["res"][tf], "computed_at": daily["computed_at"],
                   "universe": "All NSE", **daily["meta"]}

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "data.json"), "w") as f:
        json.dump({"generated_at": now.isoformat(), "ema": EMA_LEN, "min_body": MIN_BODY,
                   "counts": {"all": len(all_syms), "intraday": len(intraday_syms)},
                   "timeframes": tfs}, f, separators=(",", ":"))
    shutil.copy(os.path.join(HERE, "index.html"), os.path.join(args.out, "index.html"))
    for tf, v in tfs.items():
        a = sum(x["side"] == "ABOVE" for x in v["rows"])
        print(f"  {tf:>4}: {a} above, {len(v['rows']) - a} below")
    print("Done")


if __name__ == "__main__":
    main()
