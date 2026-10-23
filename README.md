# treg console

A native Linux desktop app (GTK3) that uses [treg](https://treg.to) as its
backend. The simple path is the **Browse** tab: search the web, click a
result, and read the page. Under the hood it's a full console — X (Twitter),
Reddit, Hacker News, GitHub, OLX.pt and eBay frontends, a news front page,
market data, a 4,000+ endpoint catalog, live calls with cost, and an **AI**
tab whose model can call all of it as tools. **Ctrl+K** opens a command palette that
jumps to any tab, searches a source, or hands the query straight to the AI.

## The goal

The treg console is meant to become the **everything app** — one native
window that is your gateway to the internet: search the web and read pages,
browse social platforms, code and marketplaces (X, Reddit, Hacker News, GitHub,
OLX.pt and eBay today, more as treg's catalog grows), read the news, watch
markets, and call any of treg's 4,000+ endpoints — all with live cost, without
leaving the desktop. An **AI interaction** tab ties it together: the local
model can use every one of those data sources as a tool, so one question can
search the catalog, read a page, scan news, check markets, skim Hacker News,
compare GitHub repos and price a used phone on OLX and eBay in a single turn.
A **Ctrl+K**
palette makes that the front door: type a question, a search or a repo name
and it routes to the right tab or to the AI. treg's catalog (113 platforms,
new providers all the time) is the backend that makes this possible: every
platform or service treg can reach is a frontend this app can add next.

## Doctrine — how features are added

This app is **AI-centred**. The AI is not a bolt-on: it is the intended
primary way to use the app. Every screen also exists as a tool, so the model
can do by itself whatever you can do by clicking. Keep that true for anything
you add.

1. **The AI is local.** The model is an OpenAI-compatible server on your own
   machine — `AI_URL` (default `http://localhost:5000/v1`), `AI_API_KEY`,
   `TREG_AI_MODEL`. New features must work against that local endpoint; do
   not make part of the app depend on a hosted model, and do not send the
   user's data off-box. The point is that it runs on your hardware.
2. **Every tab has at least one AI feature.** A new tab is not finished until
   it can hand off to the AI. At minimum an **Ask AI** action that explains
   the current selection with the on-screen context; when the tab shows a list
   or a result set, also an **AI digest** / analysis action for the whole
   view (see the News and Hacker News tabs). Wire it through
   `MainWindow._ai_ask(...)` so the turn lands in the AI tab.
3. **Every data source is a tool.** Whatever the app can fetch, the model must
   be able to fetch. Add it to the tool registry in `ai.py`
   (`build_registry`), give it a clear description that says whether it is
   free or paid, add it to the `tool_summary` groups, and mention it in
   `system_prompt()`. A data source that the AI cannot reach is an
   incomplete feature.
4. **Prefer free tools; be honest about cost.** Free, direct sources (RSS,
   the official Hacker News API, GitHub, OLX.pt's public JSON API, yfinance)
   come first; treg-routed calls are paid. eBay is deliberately treg-routed:
   eBay has no keyless official API and blocks non-browser clients, so its
   search and listing detail are paid SerpApi calls ($0.015 each).
   Descriptions and the system prompt must say which is which, and the UI
   must show charges in the status bar. Never hide a cost.
5. **Pass context, don't re-fetch.** When a tab hands off to the AI it should
   include the rows and detail that are already on screen (as hidden context),
   so the model reasons about the current view. It may then call tools for
   anything more recent or missing.
6. **Keep the tool surface coherent.** Name tools by area with a shared prefix
   (`news_*`, `hn_*`, `github_*`, `olx_*`, `market_*`, `crypto_*`, `x_*`,
   `reddit_*`, `treg_*`),
   return plain JSON-able dicts (the registry truncates oversized results),
   raise `TregError` for expected failures, and stay synchronous/blocking —
   the GUI always calls these from a worker thread.


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
- an OpenAI-compatible chat server for the AI tab (default
  `http://localhost:5000/v1`; native tool calling is used when the server
  supports it)
- optional `GITHUB_TOKEN` for the GitHub tab — the public API works without
  one (60 requests/hour); a token raises that to 5,000/hour
- no key for the OLX tab — OLX.pt's public listings JSON API is free and
  needs no token
- the eBay tab needs the same treg token and is **paid**: search and full
  listing detail are $0.015 per successful call, charged to the team

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
  (one search + one page read, ~$0.0002). **Ask AI** hands the current
  results to the **AI** tab.
- **AI** — AI interaction: a chat with the local model (default
  `http://localhost:5000/v1`, model `qwen3.8-27B-3.5bpw`). The model has
  **tools for everything in the app** and calls them itself: search treg's
  catalog and call *any* endpoint (`treg_call`, async tasks polled), search
  the web and read pages, search X and load replies/profiles, search Reddit
  and load comments/profiles, read the news (free RSS) or search it through
  treg, read Hacker News from its free official API (`hn_stories`,
  `hn_search`, `hn_item`, `hn_comments`), search GitHub and read repos,
  READMEs, issues and releases (`github_*`), search OLX.pt classifieds and
  open any listing (`olx_*`), search eBay and open any listing (`ebay_*`,
  paid through treg), and pull stock quotes, history, fundamentals and crypto
  from yfinance. Tool calls appear inline
  (`→ tool_name(...)` / `← result`) and
  answers render as Markdown with clickable links; the conversation is kept
  for follow-ups until **New chat**. Native OpenAI tool calling is used when
  the server supports it, otherwise the agent falls back to a fenced-JSON
  tool protocol. Configure with `AI_URL`, `AI_API_KEY` and `TREG_AI_MODEL`
  (the old `SUMMARY_URL` / `SUMMARY_API_KEY` / `TREG_SUMMARY_MODEL` names
  still work).
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
  **Replies** loads the thread, **Profile** loads the author's profile,
  **Ask AI** hands the post to the AI.
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
  bar. **Ask AI** hands the post to the AI.
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
  library. **Ask AI** analyzes whatever is on screen.
- **News** — a news front page. **Top**, **Tech** and **Business**
  aggregate public RSS feeds directly (BBC, NPR, Al Jazeera, Guardian, NYT,
  Ars Technica, TechCrunch, Wired, Hacker News, CNBC, MarketWatch, FT):
  duplicates across outlets are merged, and stories are ranked by
  corroboration, recency and feed position. No treg calls, no charges,
  works without a token. **Search** runs an arbitrary query through treg's
  routed news search. Select a story for its snippet and image,
  **Read article** fetches the full text with treg's web extract, **Open in
  browser** hands it to the desktop, **AI digest** summarizes the current
  list in the AI tab, and **Ask AI** explains the selected story.
- **Hacker News** — a Hacker News frontend backed by the **free official
  API** (`hacker-news.firebaseio.com`) and the **free Algolia search API** —
  no treg calls, no charges, works without a token. Seven modes: **Top**,
  **New**, **Best**, **Ask HN**, **Show HN**, **Jobs**, and **Search** (the
  free Algolia index). Select a story for its points, author, site and text,
  **Read article** fetches the linked page with treg's web extract, **Open
  link** hands it to the desktop, **Comments** loads the discussion as a
  nested tree from the free API, **AI digest** summarizes the current list in
  the AI tab, and **Ask AI** explains the selected story and its thread.
- **GitHub** — a GitHub frontend backed by the **free public API** — no treg
  calls, no charges, works without a token (export `GITHUB_TOKEN` to lift the
  anonymous 60/hour rate limit to 5,000/hour). Five modes: **Search**
  repositories, **Trending** (repos created in the last 7 days, by stars),
  **Repo** (stats, topics, license and the README rendered as Markdown),
  **Issues** (open issues *and* pull requests, with labels, author, comments
  and body) and **Releases** (tag, author, notes). Select a row for detail,
  **Open on GitHub** in the browser, **AI digest** summarizes the current
  list, and **Ask AI** explains the selected repo or issue.
- **OLX** — an OLX.pt classifieds frontend backed by the site's **free public
  JSON API** — no treg calls, no charges, works without a token. Two modes:
  **Search** (keyword, category, price range and relevance/cheapest/most
  expensive order; an empty query browses the chosen category or the latest
  listings) and **Listing** (a numeric id or a full olx.pt URL, resolved
  through the listing page). Select a row for the price, location, seller,
  all listing attributes, the full description and photo links, **Open on
  OLX** in the browser, **AI digest** summarizes the current list, and **Ask
  AI** judges the selected listing and can compare similar ones with
  `olx_search`.
- **eBay** — an eBay.com frontend routed through treg's paid SerpApi eBay
  endpoints — **$0.015 per successful search and $0.015 per full listing
  detail**, charged to the team and shown in the status bar. Two modes:
  **Search** (keyword, category filters eBay offered for that query, and
  relevance/cheapest/most-expensive order applied to the fetched page) and
  **Listing detail** (a product id or eBay URL). Select a row for its price,
  condition, shipping, location, seller feedback and returns; **Full
  listing** fetches the item specifics, short description and photo links
  (the second paid call), **Open on eBay** hands it to the browser, **AI
  digest** summarizes the current list, and **Ask AI** judges the selected
  listing and can compare similar ones with `ebay_search` (paid).
- **Ctrl+K** — the command palette. One box to jump to any tab, search the
  web/catalog/news/Hacker News/GitHub/OLX/eBay, open a repo by `owner/name`
  or an OLX/eBay listing by id/URL, look up a ticker, or **Ask AI** the query.
  Arrow keys move, Enter runs, Escape closes. It is also a **Search / ask**
  button in the top bar.
- **Alt+1 … Alt+0** — switch tabs from anywhere, including while typing in a
  search field: Alt+1–9 for the first nine tabs, Alt+0 for the tenth (News),
  and Alt+Shift+1/2/3/4 for Hacker News, GitHub, OLX and eBay. Each tab's
  tooltip shows its shortcut, and the palette lists it too.

Every tab carries at least one AI action — that is the app's doctrine; see
**Doctrine — how features are added**.

## Credentials

Read from `~/.treg/config.json` (what `treg login` writes), overridable with
`TREG_TOKEN` / `TREG_URL`.

The AI endpoint is configured with `AI_URL` / `AI_API_KEY` / `TREG_AI_MODEL`
(and falls back to the older `SUMMARY_*` names).

## Layout

```
meson.build            meson project (package, command, .desktop, icon)
pyproject.toml         meson-python build config (pip install .)
data/                  .desktop entry + launcher script
treg_console/
  app.py               the GUI (tabs, worker threads, charts, AI tab wiring)
  ai.py                AI agent: tool registry (treg/web/X/Reddit/news/HN/
                       GitHub/OLX/markets) and the native + fallback
                       tool-calling loop
  client.py            synchronous REST client for the treg API + async polling
  parsing.py           response normalization shared by the tabs and the tools
  markets.py           yfinance data source for the Markets tab and AI tools
  news.py              RSS aggregation, dedupe/ranking and treg search for News
  hackernews.py        Hacker News source (official API + Algolia) for the
                       Hacker News tab and the AI tools
  github.py            GitHub public API source (repos, trending, issues,
                       releases) for the GitHub tab and the AI tools
  olx.py               OLX.pt public JSON API source (search, categories,
                       listings) for the OLX tab and the AI tools
  ebay.py              eBay source through treg's paid SerpApi endpoints
                       (search, listing detail) for the eBay tab and tools
  desktop.py           .desktop + icon installation (user XDG dirs)
  icon.png             app icon
```

Tests for the feed/API normalization (News, Hacker News, GitHub, OLX, eBay)
and the AI agent live in `tests/`:

```sh
python3 -m unittest discover -s tests -v
```