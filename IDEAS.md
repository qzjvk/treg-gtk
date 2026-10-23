# IDEAS

Directions for the everything app — the gateway to the internet. Where treg's
catalog already has coverage it's noted; the rest is to build.

How a feature is built — the AI-centred rules (local endpoint only, every tab
has an AI action, every data source is a tool, prefer free tools) — is in the
README under **Doctrine — how features are added**. Anything listed here
arrives as a tab plus its AI tools.

## Data & finance

- **Reddit** — a frontend tab like the X tab: search, subreddit timelines,
  post detail + comments. treg already has a Reddit platform (50 endpoints).
  *Done — the Reddit tab is in the app (see README).*
- **Markets & financial data** — quotes, prices, fundamentals, sorted and
  laid out. treg has a Stock Market Data platform (57 endpoints) and Crypto
  Market Data (14). **yfinance** as a direct data source where the catalog
  doesn't reach.
  *Done — the Markets tab is in the app, backed by yfinance (free, direct)
  with price and margin charts (see README).*
- **News** — search and aggregate news, then sort it out: dedupe, rank, and
  summarize with the local model.
  *Done — the News tab is in the app: Top/Tech/Business aggregate free RSS
  feeds (deduped and ranked), Search goes through treg, Read article fetches
  the full text, and Digest summarizes with the local model (see README).*
- **Hacker News** — a Hacker News frontend: Top/New/Best/Ask/Show/Jobs, search,
  post detail and the comment tree, then an AI hand-off.
  *Done — the Hacker News tab is in the app, backed by the free official API
  and the free Algolia search API (no treg calls, no charges). **Comments**
  loads the thread, **AI digest** summarizes the list, **Ask AI** explains the
  selected story (see README).*
- **AI over all of it** — an AI that can look at all of the above: reddit,
  markets, news, Hacker News, financial data. *Done — the **AI** tab's model
  gets every data source in the app as a tool: the treg catalog and any
  endpoint (async included), web search/extract, X, Reddit, News (free RSS +
  treg search), Hacker News (free) and Markets/crypto (yfinance), with native
  OpenAI tool calling and a JSON fallback. See README.* The MCP-server idea
  below is still open, to expose the same tools to any external MCP client.

## More platform frontends

Same pattern as the X tab — a frontend per platform:

- **TikTok** — 192 endpoints in the catalog (plus Douyin, 286).
- **Instagram** — 187 endpoints in the catalog.
- **WhatsApp** — not in the catalog yet.
- **Discord** — not in the catalog yet.