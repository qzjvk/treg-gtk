# treg console

A native Linux desktop app (GTK3) that uses [treg](https://treg.to) as its
backend. The simple path is the **Browse** tab: search the web, click a
result, and read the page. Under the hood it's a full console — X (Twitter)
and Reddit frontends, a news front page, market data, a 4,000+ endpoint
catalog, live calls with cost, and the team's call audit log.

## The goal

The treg console is meant to become the **everything app** — one native
window that is your gateway to the internet: search the web and read pages,
summarize anything with a local model, browse social platforms (X and Reddit
today, more as treg's catalog grows), and call any of treg's 4,000+ endpoints — all
with live cost, without leaving the desktop. treg's catalog (113 platforms,
new providers all the time) is the backend that makes this possible: every
platform or service treg can reach is a frontend this app can add next.

## Native look

No custom CSS. The app is a plain GTK3 application, so it follows your
desktop's GTK theme, system font and icon theme. Its GApplication id
(`treg.console`) associates the window with the `.desktop` entry, so the
desktop environment shows the right icon and groups the window with the app.

## Requirements

- Python 3.10+ with `PyGObject` (`python3-gi` on Debian/Devuan) and `httpx`
- GTK 3 (system libraries)
- `yfinance` for the Markets tab (Yahoo Finance data, free; the other tabs
  work without it)
- meson ≥ 1.8 and ninja (for installing)
- a treg login: `treg login` (writes `~/.treg/config.json`), or `TREG_TOKEN`

## Run from a checkout

```sh
./run.sh
```

## Install with meson

```sh
meson setup build --prefix ~/.local \
    -Dpython.purelibdir=lib/python3.13/site-packages
meson install -C build
```

The `-Dpython.purelibdir` flag puts the package in the PEP-standard
`lib/python3.13/site-packages` under the prefix, which is on the user's
`sys.path`. (On Debian, meson's default follows the distro's `dist-packages`
layout, which is *not* on the user path for a `~/.local` install.) Adjust the
`3.13` to your Python version. For a system-wide install use
`--prefix /usr` (needs root) and drop the flag.

That installs:

| What | Where |
|---|---|
| `treg_console` package | `~/.local/lib/python3.13/site-packages/` |
| `treg-console` command | `~/.local/bin/` |
| `.desktop` entry | `~/.local/share/applications/` |
| app icon | `~/.local/share/icons/hicolor/{256,512}x{256,512}/apps/` |

For a system-wide install use `--prefix /usr` (needs root).

### pip (meson-python)

`pip install .` also works — the same meson build rules are used (the
`-Dpython.purelibdir` flag is not needed; the build venv's standard layout is
used).

## .desktop entry (generated directly)

The app writes its own `.desktop` file and icon to your user XDG dirs on
every start (idempotent, no root needed), so it appears in the application
menu even when run from a checkout. Manage it explicitly:

```sh
treg-console --register-desktop     # (re)write the .desktop entry
treg-console --unregister-desktop   # remove it
treg-console --no-desktop           # start without touching it
```

## Tabs

- **Browse** — the simple path: type a query, hit Search, click a result. The
  page is fetched with treg's web-extract tool and shown as clean Markdown
  (one search + one page read, ~$0.0002). **Summarize** sends the current
  results to the **Summarize** tab — no treg charge.
- **Summarize** — the current Browse results (all of them, default 8) sent
  to an OpenAI-compatible `/v1/completions` endpoint (default
  `http://localhost:5000/v1`, model `qwen3.8-27B-3.5bpw`); the 3-5 sentence
  summary appears in this tab. The **Ask further** box below it is a chat:
  your question shows as a bold "You — …" line and the answer renders below
  it, so the thread stacks as you ask more. Page content, summaries and
  answers are all rendered as Markdown (the model is asked to answer in
  Markdown); links open in the default browser.

  The endpoint, key and model default to the local server; override with
  `SUMMARY_URL`, `SUMMARY_API_KEY` and `TREG_SUMMARY_MODEL` if you run it
  elsewhere.
- **Catalog** — search the catalog by task ("backlinks for a domain"),
  inspect an endpoint's parameters, price, reliability and sibling providers,
  then jump straight to a call.
- **Providers** — browse the platform catalog (113 platforms) by name or
  category; select one to see its capabilities and every endpoint under them,
  with per-endpoint cost.
- **Call** — target a catalog id or team tool URL, add query params or a JSON
  body, optional route-max-cost and idempotency key. Shows the upstream
  response verbatim plus cost / call-id / served-by headers. Async
  generation tasks are polled automatically.
- **History** — the team's call audit log; select a call to fetch its stored
  result.
- **X** — an X (Twitter) frontend. Three modes: **Search** (keyword,
  `#hashtag`, `from:user`), **User** (an account's recent posts by handle)
  and **Trends** (trending topics by country — pick one to search it).
  Select a post for its full text, media image, stats and a link out to X;
  **Replies** loads the thread, **Profile** loads the author's profile.
  Posts come from treg's routed `x.*` endpoints — each view costs a fraction
  of a cent, and the charge shows in the status bar.
- **Reddit** — a Reddit frontend. Three modes: **Search** (a keyword or
  phrase), **Subreddit** (a subreddit's posts — `r/` optional) and **User**
  (a user's posts — `u/` optional). Select a post for its full text, media
  image, stats and a link out to Reddit; **Comments** loads the comment tree
  (replies nested with ↳ arrows), **Profile** loads the user's profile.
  The post view renders from the feed row at once, then a detail call
  enriches it (Markdown body, media, flair); if that call misses, the feed
  data stays on screen. Posts come from treg-routed Reddit endpoints —
  each view costs a fraction of a cent, and the charge shows in the status
  bar.
- **Markets** — market data backed by **yfinance** (Yahoo Finance, direct
  and free — no treg calls, no charges, works without a treg token). Four
  modes: **Stock** (a live quote — price, change, day range, volume, market
  cap, 52-week range, moving averages — with a 30-day close chart),
  **History** (daily OHLCV bars for any number of days, with a period chart
  and summary; selecting a bar shows its detail), **Fundamentals** (company
  profile — sector, industry, market cap, website — plus P/E, EPS, revenue,
  margins, ROE, debt/equity, dividend yield and beta, with the margins as a
  bar chart), and **Crypto** (major coins' latest prices in one batched
  call; select a coin, or enter a ticker or name, for its quote and
  90-day chart). Charts are plain Cairo in the theme's colors — no chart
  library.
- **News** — a news front page. **Top**, **Tech** and **Business**
  aggregate public RSS feeds directly (BBC, NPR, Al Jazeera, Guardian, NYT,
  Ars Technica, TechCrunch, Wired, Hacker News, CNBC, MarketWatch, FT):
  duplicates across outlets are merged, and stories are ranked by
  corroboration, recency and feed position. No treg calls, no charges,
  works without a token. **Search** runs an arbitrary query through treg's
  routed news search. Select a story for its snippet and image,
  **Read article** fetches the full text with treg's web extract, **Open in
  browser** hands it to the desktop, and **Digest** summarizes the current
  list with the local model.

## Credentials

Read from `~/.treg/config.json` (what `treg login` writes), overridable with
`TREG_TOKEN` / `TREG_URL`.

## Layout

```
meson.build            meson project (package, command, .desktop, icon)
pyproject.toml         meson-python build config (pip install .)
data/                  .desktop entry + launcher script
treg_console/
  app.py               the GUI (tabs, worker threads, async polling, charts)
  client.py            synchronous REST client for the treg API
  markets.py           yfinance data source for the Markets tab (free, direct)
  news.py              RSS aggregation, dedupe/ranking and treg search for News
  desktop.py           .desktop + icon installation (user XDG dirs)
  icon.png             app icon
```

Tests for the News feed parsing and ranking live in `tests/`:

```sh
python3 -m unittest discover -s tests -v
```