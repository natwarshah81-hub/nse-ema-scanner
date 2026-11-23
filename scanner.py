#!/usr/bin/env python3
"""
NSE 44-EMA candle scanner (runs in GitHub Actions, publishes to GitHub Pages).

Rule, applied to one candle per stock and timeframe:
  ABOVE: close > EMA44 and >= 50% of the candle body (|close-open|) is above the EMA
  BELOW: close < EMA44 and >= 50% of the candle body is below the EMA

Two modes are published:
  data.json  CLOSED  - the last completed candle (a signal that can no longer change)
  live.json  LIVE    - the candle that is still forming right now (what TradingView shows)

Timeframes
  5m, 15m, 30m, 1h, 75m  -> Nifty 500 stocks (built from Yahoo 5-minute data)
  1D, 1W                 -> all NSE equities (1 year of daily candles; 5 years of weekly candles so the
                            weekly EMA has enough history to match TradingView)
Plus BTC and XAU (gold) on every timeframe.

Slope level (1-5): how steep the EMA is, measured as the EMA's move over the last 5 candles
divided by the average candle range (ATR 14), so the same scale works for every stock and timeframe.
  1 flat (< 0.1x)   2 mild (< 0.3x)   3 moderate (< 0.6x)   4 strong (< 1.0x)   5 very strong
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
UTC = dt.timezone.utc
EMA_LEN = 44
ALPHA = 2 / (EMA_LEN + 1)
MIN_BODY = 50.0
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
INTRADAY = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "75m": 75}
SLOPE_LEVELS = (0.1, 0.3, 0.6, 1.0)  # upper bounds of levels 1-4 in "x ATR"; edit to taste
LIVE_DW_EVERY_MIN = 10          # refresh live Daily/Weekly for all NSE at most this often
NSE = "https://archives.nseindia.com/content"
SOURCES = {
    "all": (f"{NSE}/equities/EQUITY_L.csv", "EQUITY_L.csv"),
    "n500": (f"{NSE}/indices/ind_nifty500list.csv", "ind_nifty500list.csv"),
    "n50": (f"{NSE}/indices/ind_nifty50list.csv", "ind_nifty50list.csv"),
}
GLOBAL = {
    "BTC": {"label": "Bitcoin", "tickers": ["BTC-USD"], "tv": "BITSTAMP:BTCUSD", "week_days": 7},
    "XAU": {"label": "Gold", "tickers": ["XAUUSD=X", "GC=F"], "tv": "OANDA:XAUUSD", "week_days": 5},
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
    return names, tier, sorted(n500 or n50 or names)


# ------------------------------------------------------------------ rule
def num(x):
    try:
        x = float(x)
        return 0.0 if x != x else x          # NaN -> 0
    except (TypeError, ValueError):
        return 0.0


def slope_level(x):
    for i, bound in enumerate(SLOPE_LEVELS):
        if x < bound:
            return i + 1
    return 5


def judge(o, h, l, c, e, pc, pe, e5, vol, avg, atr):
    """Applies the 44-EMA body rule to one candle. side is ABOVE, BELOW or NONE."""
    o, h, l, c, e, pc, pe, e5, vol, avg, atr = (num(x) for x in (o, h, l, c, e, pc, pe, e5, vol, avg, atr))
    sx = abs(e - e5) / atr if atr else 0.0
    body = abs(c - o)
    top, bot = max(o, c), min(o, c)
    above = max(0.0, top - max(bot, e)) / body * 100 if body else 0.0
    below = max(0.0, min(top, e) - bot) / body * 100 if body else 0.0
    if body and c > e and above >= MIN_BODY:
        side, pct = "ABOVE", above
    elif body and c < e and below >= MIN_BODY:
        side, pct = "BELOW", below
    else:
        side, pct = "NONE", (above if c >= e else below)
    return {
        "side": side, "b": round(pct, 1),
        "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "e": round(e, 2),
        "d": round((c - e) / e * 100, 2),
        "chg": round((c - pc) / pc * 100, 2) if pc else 0,
        "f": (side == "ABOVE" and pc <= pe) or (side == "BELOW" and pc >= pe),
        "sl": round((e - e5) / e5 * 100, 2) if e5 else 0,
        "sx": round(sx, 2), "lv": slope_level(sx),
        "v": int(vol), "vx": round(vol / avg, 2) if avg else None,
    }


def atr14(bars):
    """Average true range of the last 14 candles."""
    h, l, c = bars["High"], bars["Low"], bars["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-14:].mean())


def metrics(bars):
    """Judges the LAST row of bars (oldest first). Returns dict or None if too little history."""
    bars = bars.dropna(subset=["Open", "High", "Low", "Close"])
    if len(bars) < EMA_LEN + 6:
        return None
    ema = bars["Close"].ewm(span=EMA_LEN, adjust=False).mean()
    last = bars.iloc[-1]
    vol = bars["Volume"].fillna(0) if "Volume" in bars else pd.Series(0, index=bars.index)
    return judge(float(last.Open), float(last.High), float(last.Low), float(last.Close),
                 float(ema.iloc[-1]), float(bars["Close"].iloc[-2]), float(ema.iloc[-2]),
                 float(ema.iloc[-6]), float(vol.iloc[-1]), float(vol.iloc[-21:-1].mean()), atr14(bars))


def ema_state(bars):
    """What is needed to judge the NEXT (still forming) candle without the full history."""
    bars = bars.dropna(subset=["Close"])
    if len(bars) < EMA_LEN + 6:
        return None
    ema = bars["Close"].ewm(span=EMA_LEN, adjust=False).mean()
    vol = bars["Volume"].fillna(0) if "Volume" in bars else pd.Series(0, index=bars.index)
    return {"e": float(ema.iloc[-1]), "c": float(bars["Close"].iloc[-1]), "e5": float(ema.iloc[-5]),
            "av": num(vol.iloc[-20:].mean()), "atr": atr14(bars), "last": bars.index[-1].strftime("%Y-%m-%d")}


def judge_next(st, o, h, l, c, vol):
    """Judges a forming candle that follows the state's last completed candle."""
    e = st["e"] + ALPHA * (c - st["e"])
    return judge(o, h, l, c, e, st["c"], st["e"], st["e5"], vol, st["av"], st.get("atr", 0))


def keep(r):
    return r is not None and r["side"] != "NONE"


def evaluate(bars):
    """Stock rule: the metrics, only when the candle qualifies ABOVE or BELOW."""
    r = metrics(bars)
    return r if keep(r) else None


# ------------------------------------------------------------------ data
def download(symbols, chunk=100, suffix=".NS", **kw):
    """Yields (symbol, ohlcv DataFrame) using yfinance in batches."""
    import yfinance as yf
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        tick = [s + suffix for s in part]
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


def in_tz(d, tz):
    d = d.copy()
    idx = pd.DatetimeIndex(d.index)
    d.index = idx.tz_localize("UTC").tz_convert(tz) if idx.tz is None else idx.tz_convert(tz)
    return d


def as_dates(d):
    """Daily bars -> naive session dates (in the exchange's own timezone)."""
    d = d.copy()
    idx = pd.DatetimeIndex(d.index)
    d.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    return d[~d.index.duplicated(keep="last")]


def resample_intraday(d5, minutes, open_at=pd.Timedelta(hours=9, minutes=15),
                      close_at=pd.Timedelta(hours=15, minutes=30)):
    """Groups 5m bars into N-minute candles anchored at the session open each day (TradingView style).
    The index timezone decides the day: IST for NSE, UTC for BTC/XAU."""
    if minutes == 5:
        out = d5[["Open", "High", "Low", "Close", "Volume"]].copy()
        out["end"] = out.index + pd.Timedelta(minutes=5)
        return out
    open_t = d5.index.normalize() + open_at
    k = ((d5.index - open_t) // pd.Timedelta(minutes=minutes)).astype(int)
    start = open_t + pd.to_timedelta(k * minutes, unit="m")
    g = d5.groupby(start)
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    close_t = out.index.normalize() + close_at
    end = pd.Series(out.index + pd.Timedelta(minutes=minutes), index=out.index)
    out["end"] = end.where(end <= close_t, close_t)
    return out


def weekly_bars(w):
    """Yahoo weekly candles -> one row per week labelled by its Monday (merges any split rows)."""
    w = as_dates(w.dropna(subset=["Close"]))
    mon = w.index - pd.to_timedelta(w.index.weekday, unit="D")
    g = w.groupby(mon)
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    out.index.name = None
    return out


def weekly(dd):
    """Daily bars (naive dates) -> weekly bars labelled by Monday; 'wend' is the week's Friday."""
    w = dd.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                  "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    w["wend"] = w.index
    w.index = w.index - pd.Timedelta(days=4)
    return w


def week_closed(friday, now):
    return now.replace(tzinfo=None) >= friday + pd.Timedelta(hours=15, minutes=30)


# ------------------------------------------------------------------ NSE intraday (Nifty 500)
def scan_intraday(symbols, names, tier):
    now = now_ist()
    closed = {tf: [] for tf in INTRADAY}
    live = {tf: [] for tf in INTRADAY}
    scanned = failed = 0
    start = (now - dt.timedelta(days=55)).date()
    for s, d in download(symbols, interval="5m", start=str(start), prepost=False):
        scanned += 1
        try:
            d = in_tz(d, IST).dropna(subset=["Close"])
            d = d[(d.index.time >= dt.time(9, 15)) & (d.index.time < dt.time(15, 30))]
            d = d[d.index <= now]
            if d.empty:
                continue
            done = d[d.index + pd.Timedelta(minutes=5) <= now]         # finished 5m bars
            last_end = done.index[-1] + pd.Timedelta(minutes=5) if len(done) else None
            info = dict(s=s, n=names.get(s, s), u=tier.get(s, 3))
            for tf, m in INTRADAY.items():
                allbars = resample_intraday(d, m)
                r = metrics(allbars)                                    # live: includes forming candle
                if keep(r):
                    forming = allbars["end"].iloc[-1] > now
                    r.update(info, t=allbars.index[-1].strftime("%d %b %H:%M") + (" · live" if forming else ""))
                    live[tf].append(r)
                if last_end is not None:
                    bars = allbars[allbars["end"] <= last_end]          # closed: finished candles only
                    r = metrics(bars)
                    if keep(r):
                        r.update(info, t=bars.index[-1].strftime("%d %b %H:%M"))
                        closed[tf].append(r)
        except Exception as ex:
            failed += 1
            print(f"  {s}: {ex}")
    return closed, live, {"scanned": scanned, "requested": len(symbols), "failed": failed}


# ------------------------------------------------------------------ NSE daily / weekly (all NSE)
def scan_daily(symbols, names, tier):
    """Closed Daily (1 year of daily candles) and Weekly (5 years of weekly candles) + EMA state for live mode."""
    now = now_ist()
    res = {"1D": [], "1W": []}
    state = {}
    scanned = failed = 0
    for s, d in download(symbols, interval="1d", period="1y"):
        scanned += 1
        try:
            d = as_dates(in_tz(d, IST).dropna(subset=["Close"]))
            if len(d) and d.index[-1].date() == now.date() and now.time() < dt.time(15, 30):
                d = d.iloc[:-1]                                           # today's candle still open
            st = ema_state(d)
            if st:
                state[s] = {"D": st}
            r = evaluate(d)
            if r:
                r.update(s=s, n=names.get(s, s), u=tier.get(s, 3), t=d.index[-1].strftime("%d %b %Y"))
                res["1D"].append(r)
        except Exception as ex:
            failed += 1
            print(f"  {s} daily: {ex}")
    for s, w in download(symbols, interval="1wk", period="5y"):
        try:
            w = weekly_bars(in_tz(w, IST))
            st = state.get(s)
            if not len(w) or st is None:
                continue
            if not week_closed(w.index[-1] + pd.Timedelta(days=4), now):
                p = w.iloc[-1]                                            # this week so far
                st["P"] = {"o": num(p.Open), "h": num(p.High), "l": num(p.Low), "c": num(p.Close),
                           "v": num(p.Volume), "wk": w.index[-1].strftime("%Y-%m-%d")}
                w = w.iloc[:-1]
            st["W"] = ema_state(w)
            r = evaluate(w)
            if r:
                r.update(s=s, n=names.get(s, s), u=tier.get(s, 3), t="wk of " + w.index[-1].strftime("%d %b"))
                res["1W"].append(r)
        except Exception as ex:
            failed += 1
            print(f"  {s} weekly: {ex}")
    state = {s: st for s, st in state.items() if st.get("D") and st.get("W")}
    return res, state, {"scanned": scanned, "requested": len(symbols), "failed": failed}


def live_daily_weekly(state, closed_res, names, tier, fetch_today):
    """Forming Daily and Weekly candles for all NSE, using the saved EMA state + today's bar.
    Where nothing is forming (before open, after close, weekend, holiday) the last closed candle stands."""
    now = now_ist()
    today = {}
    if fetch_today:
        for s, d in download(sorted(state), interval="1d", period="5d"):
            try:
                d = as_dates(in_tz(d, IST).dropna(subset=["Close"]))
                if len(d) and d.index[-1].date() == now.date():
                    today[s] = d.iloc[-1]
            except Exception:
                pass
    monday = (pd.Timestamp(now.date()) - pd.Timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    closed_d = {r["s"]: r for r in closed_res["1D"]}
    closed_w = {r["s"]: r for r in closed_res["1W"]}
    out = {"1D": [], "1W": []}
    for s, st in state.items():
        info = dict(s=s, n=names.get(s, s), u=tier.get(s, 3))
        t = today.get(s)
        is_new = t is not None and st["D"]["last"] < now.strftime("%Y-%m-%d")
        # Daily
        if is_new:
            r = judge_next(st["D"], num(t.Open), num(t.High), num(t.Low), num(t.Close), num(t.Volume))
            if keep(r):
                r.update(info, t=now.strftime("%d %b %Y") + " · live")
                out["1D"].append(r)
        elif s in closed_d:
            out["1D"].append(closed_d[s])
        # Weekly: this week's days so far (+ today), judged against last completed week's EMA
        p = st.get("P") if (st.get("P") or {}).get("wk") == monday else None
        if p or is_new:
            o, h, l, c, v = (p["o"], p["h"], p["l"], p["c"], p["v"]) if p else \
                (num(t.Open), num(t.High), num(t.Low), num(t.Close), 0.0)
            if is_new:
                h, l, c = max(h, num(t.High)), min(l, num(t.Low)), num(t.Close)
                v += num(t.Volume)
            r = judge_next(st["W"], o, h, l, c, v)
            if keep(r):
                r.update(info, t="wk of " + pd.Timestamp(monday).strftime("%d %b") + " · live")
                out["1W"].append(r)
        elif s in closed_w:
            out["1W"].append(closed_w[s])
    return out


# ------------------------------------------------------------------ BTC / XAU
def _fetch_one(tickers, **kw):
    """First ticker in the list that returns data wins (gold falls back to COMEX futures)."""
    for t in tickers:
        for _, d in download([t], suffix="", **kw):
            if len(d.dropna(subset=["Close"])) > EMA_LEN + 6:
                return t, d
    return None, None


def scan_global():
    """BTC and XAU on every timeframe, closed and live, whether or not the candle qualifies."""
    now = dt.datetime.now(UTC)
    out = {}
    for key, cfg in GLOBAL.items():
        item = {"label": cfg["label"], "tv": cfg["tv"], "ticker": None, "closed": {}, "live": {}}
        try:
            t5, d5 = _fetch_one(cfg["tickers"], interval="5m", start=str((now - dt.timedelta(days=55)).date()))
            if d5 is not None:
                item["ticker"] = t5
                d5 = in_tz(d5, UTC).dropna(subset=["Close"])
                d5 = d5[d5.index <= now]
                done = d5[d5.index + pd.Timedelta(minutes=5) <= now]
                for tf, m in INTRADAY.items():
                    allbars = resample_intraday(d5, m, pd.Timedelta(0), pd.Timedelta(hours=24))
                    r = metrics(allbars)
                    if r:
                        r["t"] = allbars.index[-1].strftime("%d %b %H:%M") + " UTC"
                    item["live"][tf] = r
                    if len(done):
                        bars = allbars[allbars["end"] <= done.index[-1] + pd.Timedelta(minutes=5)]
                        r = metrics(bars)
                        if r:
                            r["t"] = bars.index[-1].strftime("%d %b %H:%M") + " UTC"
                        item["closed"][tf] = r
            td, dd = _fetch_one(cfg["tickers"], interval="1d", period="1y")
            if dd is not None:
                item["ticker"] = item["ticker"] or td
                dd = as_dates(dd.dropna(subset=["Close"]))
                forming = len(dd) and dd.index[-1].date() >= now.date()
                for mode, bars in (("live", dd), ("closed", dd.iloc[:-1] if forming else dd)):
                    r = metrics(bars)
                    if r:
                        r["t"] = bars.index[-1].strftime("%d %b %Y")
                    item[mode]["1D"] = r
            tw, wk = _fetch_one(cfg["tickers"], interval="1wk", period="5y")
            if wk is not None:
                w = weekly_bars(wk)
                week_open = len(w) and now.replace(tzinfo=None) < w.index[-1] + pd.Timedelta(days=cfg["week_days"])
                for mode, bars in (("live", w), ("closed", w.iloc[:-1] if week_open else w)):
                    r = metrics(bars)
                    if r:
                        r["t"] = "wk of " + bars.index[-1].strftime("%d %b")
                    item[mode]["1W"] = r
        except Exception as ex:
            print(f"  {key}: {ex}")
        out[key] = item
    return out


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
    try:
        return json.load(open(p)) if os.path.exists(p) else None
    except Exception:
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

    # Closed Daily / Weekly change once a day: reuse until the next close
    daily = None if args.force else load_cache("daily.json")
    if not daily or daily.get("v") != 3 or daily["computed_at"] < last_cutoff(now, 15, 45).isoformat():
        print("Scanning daily (1y) / weekly (5y) ...")
        r, st, m = scan_daily(all_syms, names, tier)
        daily = {"v": 3, "computed_at": now.isoformat(), "res": r, "state": st, "meta": m}
        save_cache("daily.json", daily)
    else:
        print("Closed daily/weekly unchanged since last close, reusing")

    # Live Daily / Weekly: today's forming candle for all NSE, refreshed every few minutes in market hours
    ldw = None if args.force else load_cache("live_dw.json")
    stale = not ldw or ldw.get("base") != daily["computed_at"] or (
        market_open(now) and ldw["computed_at"] < (now - dt.timedelta(minutes=LIVE_DW_EVERY_MIN - 1)).isoformat())
    if stale:
        print("Building live daily/weekly ...")
        try:
            res = live_daily_weekly(daily["state"], daily["res"], names, tier, fetch_today=market_open(now))
        except Exception as ex:                       # never lose the whole scan over live mode
            print(f"  live daily/weekly failed, showing closed candles instead: {ex}")
            res = daily["res"]
        ldw = {"computed_at": now.isoformat(), "base": daily["computed_at"], "res": res}
        save_cache("live_dw.json", ldw)

    # Intraday: rescan during market hours, otherwise reuse the final scan of the day
    intra = None if args.force else load_cache("intraday.json")
    if market_open(now) or not intra or intra.get("v") != 3 or \
            intra["computed_at"] < last_cutoff(now, 15, 35).isoformat():
        print("Scanning intraday ...")
        c, l, m = scan_intraday(intraday_syms, names, tier)
        intra = {"v": 3, "computed_at": now.isoformat(), "closed": c, "live": l, "meta": m}
        save_cache("intraday.json", intra)
    else:
        print("Market closed, reusing last intraday scan")

    print("Scanning BTC / XAU ...")
    try:
        glob = scan_global()
    except Exception as ex:
        print(f"  BTC/XAU failed: {ex}")
        glob = {}

    os.makedirs(args.out, exist_ok=True)
    for mode, fname in (("closed", "data.json"), ("live", "live.json")):
        tfs = {}
        for tf in INTRADAY:
            tfs[tf] = {"rows": intra[mode][tf], "computed_at": intra["computed_at"],
                       "universe": "Nifty 500", **intra["meta"]}
        for tf in ("1D", "1W"):
            src = daily["res"] if mode == "closed" else ldw["res"]
            tfs[tf] = {"rows": src[tf], "computed_at": daily["computed_at"] if mode == "closed" else ldw["computed_at"],
                       "universe": "All NSE", **daily["meta"]}
        g = {k: {"label": v["label"], "tv": v["tv"], "ticker": v["ticker"], "tf": v[mode]} for k, v in glob.items()}
        with open(os.path.join(args.out, fname), "w") as f:
            json.dump({"mode": mode, "generated_at": now.isoformat(), "ema": EMA_LEN, "min_body": MIN_BODY,
                       "counts": {"all": len(all_syms), "intraday": len(intraday_syms)},
                       "timeframes": tfs, "global": g, "global_at": now.isoformat()}, f, separators=(",", ":"))
        print(f" {mode}: " + ", ".join(f"{tf} {len(v['rows'])}" for tf, v in tfs.items()))
    shutil.copy(os.path.join(HERE, "index.html"), os.path.join(args.out, "index.html"))
    print("Done")


if __name__ == "__main__":
    main()
