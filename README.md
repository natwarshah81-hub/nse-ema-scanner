# NSE 44 EMA Scanner

A web page listing NSE stocks whose last completed candle closed above or below the 44 EMA
with at least 50% of the candle body on that side. It runs entirely on GitHub, so nothing is
installed on your computer. A scan runs about every 5 minutes during market hours, and the page
reloads itself every 5 minutes.

Timeframes: 5m, 15m, 30m, 1h and 75m (Nifty 500), plus Daily and Weekly (all NSE).

## Setup (about 10 minutes, all in the browser)

1. Sign up for a free account at https://github.com.
2. Click **+** (top right), then **New repository**. Name it `nse-ema-scanner`, choose **Public**, and click **Create repository**.
3. Click the **uploading an existing file** link. Drag in everything from this folder:
   `scanner.py`, `index.html`, `requirements.txt`, `README.md` and the `symbols` and `.github` folders.
   Then click **Commit changes**.
   - On a Mac, Finder hides the `.github` folder. Press **Cmd + Shift + .** in Finder to show it.
   - If it still won't upload, click **Add file**, then **Create new file**. Type
     `.github/workflows/scan.yml` as the name, paste in that file's contents, and commit.
4. Go to **Settings**, then **Pages**. Under **Source**, choose **GitHub Actions**.
5. Open the **Actions** tab (click **enable** if GitHub asks). Choose **NSE 44 EMA scan**, then **Run workflow**.
   The first scan takes about 5–10 minutes.
6. Open your page at `https://YOUR-USERNAME.github.io/nse-ema-scanner/` and bookmark it.
   It works on your phone too.

## Using it
- Pick a timeframe from the buttons. **Refresh now** loads the latest scan immediately.
- To start a brand-new scan outside the schedule, use the **Run a fresh scan on GitHub** link at the bottom of the page.
- Use the filters for Nifty 50 / Nifty 500 / All NSE, above or below, a minimum body %, fresh crosses only, and EMA slope.

## Good to know
- Scheduled scans run Monday to Friday, 08:30–16:25 IST, plus one at 18:00 IST for the final daily candle.
  GitHub sometimes starts scheduled runs a few minutes late during busy periods.
- The repository must stay **Public**. GitHub Actions minutes are free and unlimited for public repositories,
  while private ones have a monthly cap that this would exceed.
- If the repository has no activity for 60 days, GitHub pauses the schedule.
  Re-enable it from the **Actions** tab.
- If a scan log says **NSE download failed**, follow the instructions in `symbols/README.md`.
- Data comes from Yahoo Finance. This is not investment advice.
