"""Direct market data via yfinance (Yahoo Finance) — the free source the
Markets tab falls back to when a treg-routed call misses, and the source
of the fields the catalog's quote contract doesn't carry (market cap,
52-week range, averages, fundamentals metrics).

All functions are synchronous and blocking — call them from a worker
thread, never from the GUI thread. They return plain dicts and lists (no
pandas objects) so the GUI thread can render them directly.
"""

from __future__ import annotations

import datetime as _dt
import math
import warnings

from .client import TregError


def _yf():
    """yfinance, imported lazily so the rest of the app starts even when
    it isn't installed — only the Markets tab needs it."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise TregError("yfinance is not installed — pip install yfinance") from e
    _quiet_pandas()
    return yf


def _quiet_pandas():
    """Silence yfinance's pandas deprecation noise (irrelevant in a GUI app).
    Applied after the yfinance import: yfinance inserts its own 'default'
    filter at the front, and Pandas4Warning is a DeprecationWarning, so an
    earlier filter would be shadowed."""
    try:
        from pandas.errors import Pandas4Warning
    except ImportError:
        return
    warnings.filterwarnings("ignore", category=Pandas4Warning)


def num(v) -> float | None:
    """A finite number as a plain float (numpy floats included); None for
    missing / NaN / inf values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def yf_quote(symbol: str) -> dict | None:
    """Live quote + key stats for a ticker (stock, ETF, index, or COIN-USD).
    None when Yahoo has no data for it."""
    yf = _yf()
    try:
        fi = dict(yf.Ticker(symbol).fast_info)
    except Exception:
        return None
    price = num(fi.get("lastPrice"))
    if price is None:
        return None
    prev = num(fi.get("previousClose")) or num(fi.get("regularMarketPreviousClose"))
    change = price - prev if prev else None
    return {
        "symbol": symbol,
        "price": price,
        "previous_close": prev,
        "open": num(fi.get("open")),
        "day_high": num(fi.get("dayHigh")),
        "day_low": num(fi.get("dayLow")),
        "volume": num(fi.get("lastVolume")),
        "market_cap": num(fi.get("marketCap")),
        "year_high": num(fi.get("yearHigh")),
        "year_low": num(fi.get("yearLow")),
        "fifty_day_avg": num(fi.get("fiftyDayAverage")),
        "two_hundred_day_avg": num(fi.get("twoHundredDayAverage")),
        "currency": fi.get("currency") or "",
        "exchange": fi.get("exchange") or "",
        "quote_type": fi.get("quoteType") or "",
        "change": change,
        "change_pct": (change / prev * 100) if (change is not None and prev) else None,
    }


def yf_history(symbol: str, days: int = 30) -> list[dict]:
    """Daily bars for the last `days` days, oldest first. [] on a miss."""
    yf = _yf()
    import pandas as pd

    start = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    try:
        df = yf.Ticker(symbol).history(start=start, interval="1d", auto_adjust=False)
    except Exception:
        return []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    bars = []
    for idx, row in df.iterrows():
        bars.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": num(row.get("Open")),
            "high": num(row.get("High")),
            "low": num(row.get("Low")),
            "close": num(row.get("Close")),
            "volume": num(row.get("Volume")),
        })
    return bars


def yf_fundamentals(symbol: str) -> dict | None:
    """Company identity + key metrics from Yahoo's fundamentals. None on a
    miss. Margin / yield / growth fields are fractions (0.45 = 45%)."""
    yf = _yf()
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not isinstance(info, dict) or not info:
        return None
    # dividendYield comes back as a percent number (0.31 = 0.31%) on some
    # Yahoo responses; trailingAnnualDividendYield is a real fraction.
    dy = num(info.get("trailingAnnualDividendYield"))
    if dy is None:
        dy = num(info.get("dividendYield"))
        if dy is not None and dy > 0.5:  # a percent number, not a fraction
            dy = dy / 100
    return {
        "name": info.get("longName") or info.get("shortName") or "",
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "country": info.get("country") or "",
        "website": info.get("website") or "",
        "employees": num(info.get("fullTimeEmployees")),
        "market_cap": num(info.get("marketCap")),
        "trailing_pe": num(info.get("trailingPE")),
        "forward_pe": num(info.get("forwardPE")),
        "eps": num(info.get("trailingEps")),
        "revenue": num(info.get("totalRevenue")),
        "revenue_growth": num(info.get("revenueGrowth")),
        "gross_margin": num(info.get("grossMargins")),
        "operating_margin": num(info.get("operatingMargins")),
        "profit_margin": num(info.get("profitMargins")),
        "roe": num(info.get("returnOnEquity")),
        "debt_to_equity": num(info.get("debtToEquity")),
        "dividend_yield": dy,
        "beta": num(info.get("beta")),
        "week52_change": num(info.get("52WeekChange")),
    }


def yf_crypto_quote(symbol: str) -> dict | None:
    """Live quote for a coin by ticker (BTC, ETH, …) via its <SYM>-USD pair.
    None when Yahoo has no data for it."""
    sym = symbol.upper().strip()
    if sym.endswith("-USD"):
        sym = sym[:-4]
    if not sym:
        return None
    return yf_quote(f"{sym}-USD")


#: (ticker, name) — the major coins the crypto view lists by default.
MAJOR_COINS = [
    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"), ("BNB", "BNB"),
    ("XRP", "XRP"), ("ADA", "Cardano"), ("DOGE", "Dogecoin"),
    ("AVAX", "Avalanche"), ("LINK", "Chainlink"), ("DOT", "Polkadot"),
    ("LTC", "Litecoin"), ("BCH", "Bitcoin Cash"), ("TRX", "TRON"),
    ("TON", "Toncoin"), ("NEAR", "NEAR"), ("APT", "Aptos"),
    ("ARB", "Arbitrum"), ("OP", "Optimism"), ("SUI", "Sui"), ("FIL", "Filecoin"),
]


def yf_coins(coins: list[tuple[str, str]] | None = None) -> list[dict]:
    """Latest quotes for a set of coins (defaults to MAJOR_COINS), fetched in
    one batched Yahoo download. Rows: {symbol, name, price, change_24h,
    change_7d, volume}, sorted by 24h volume. Coins Yahoo has no data for
    are dropped."""
    yf = _yf()
    import pandas as pd

    coins = list(coins) if coins else MAJOR_COINS
    tickers = " ".join(c + "-USD" for c, _ in coins)
    # yfinance prints "$SYM: No data found" to stderr for dead tickers —
    # that's noise in a GUI app, and the rows are dropped below anyway.
    import contextlib
    import io

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            df = yf.download(tickers, period="10d", progress=False)
    except Exception:
        return []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        close, vol = df["Close"], df["Volume"]
    else:  # a single ticker comes back with flat columns
        close, vol = df["Close"].to_frame(), df["Volume"].to_frame()
    rows = []
    for sym, name in coins:
        t = f"{sym}-USD"
        if t not in close.columns:
            continue
        series = close[t].dropna()
        if series.empty:
            continue
        price = num(series.iloc[-1])
        if price is None:
            continue

        def pct(n: int):
            if len(series) > n:
                old = num(series.iloc[-1 - n])
                if old:
                    return (price - old) / old * 100
            return None

        rows.append({
            "symbol": sym,
            "name": name,
            "price": price,
            "change_24h": pct(1),
            "change_7d": pct(5),
            "volume": num(vol[t].dropna().iloc[-1]),
        })
    rows.sort(key=lambda r: (r["volume"] or 0), reverse=True)
    return rows