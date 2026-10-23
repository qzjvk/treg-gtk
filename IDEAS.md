# IDEAS

Directions for the everything app — the gateway to the internet. Where treg's
catalog already has coverage it's noted; the rest is to build.

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
- **AI over all of it** — an AI that can look at all of the above: reddit,
  markets, news, financial data. *Done — the **AI** tab's model gets every
  data source in the app as a tool: the treg catalog and any endpoint (async
  included), web search/extract, X, Reddit, News (free RSS + treg search) and
  Markets/crypto (yfinance), with native OpenAI tool calling and a JSON
  fallback. See README.* The MCP-server idea below is still open, to expose
  the same tools to any external MCP client.

## More platform frontends

Same pattern as the X tab — a frontend per platform:

- **TikTok** — 192 endpoints in the catalog (plus Douyin, 286).
- **Instagram** — 187 endpoints in the catalog.
- **WhatsApp** — not in the catalog yet.
- **Discord** — not in the catalog yet.