"""Download and cache 1-minute bars from Alpaca (SIP)."""

import logging
from pathlib import Path

import pandas as pd
import pytz
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")
CACHE_DIR = Path("data/cache/minute")


def _bars_to_df(bars: list) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    rows = [{
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
        "timestamp": pd.Timestamp(b.timestamp).tz_convert(ET),
    } for b in bars]
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df[~df.index.duplicated(keep="last")]


def _cache_file(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.parquet"


def merge_into_cache(symbol: str, fresh: pd.DataFrame) -> pd.DataFrame:
    """Add bars to a symbol's cache without losing what is already there.

    force=True used to overwrite the file with just the requested window, so
    asking for three days of NVDA silently threw away three months of it. Any
    caller that needs a *newer* range must merge, not replace.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_file(symbol)
    if fresh.empty:
        return pd.read_parquet(path) if path.exists() else fresh
    merged = fresh
    if path.exists():
        old = pd.read_parquet(path)
        if not old.empty:
            merged = pd.concat([old, fresh]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
    merged.to_parquet(path)
    logger.info("  %s: cache now holds %d minute bars", symbol, len(merged))
    return merged


def fetch_range(
    client: StockHistoricalDataClient,
    symbol: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Download a window straight from Alpaca. Never touches the cache."""
    start_ts = pd.Timestamp(start, tz=ET)
    end_ts = pd.Timestamp(end, tz=ET)
    chunks: list[pd.DataFrame] = []
    cursor = start_ts

    while cursor < end_ts:
        chunk_end = min(cursor + pd.DateOffset(months=1), end_ts)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=cursor.to_pydatetime(),
                end=chunk_end.to_pydatetime(),
                feed=DataFeed.SIP,
            )
            resp = client.get_stock_bars(req)
            symbol_bars = resp.data.get(symbol, [])
            df_chunk = _bars_to_df(symbol_bars)
            if not df_chunk.empty:
                chunks.append(df_chunk)
        except Exception as e:
            logger.warning("  %s %s->%s failed: %s", symbol, cursor.date(), chunk_end.date(), e)
        cursor = chunk_end

    if not chunks:
        logger.warning("  %s: no minute data", symbol)
        return pd.DataFrame()

    df = pd.concat(chunks).sort_index()
    return df[~df.index.duplicated(keep="last")]


def download_symbol(
    client: StockHistoricalDataClient,
    symbol: str,
    start: str,
    end: str,
    force: bool = False,
) -> pd.DataFrame:
    """Cached minute bars. `force` refetches the window and MERGES it in, so a
    narrow request can never shrink a wide cache."""
    cache_file = _cache_file(symbol)
    if cache_file.exists() and not force:
        df = pd.read_parquet(cache_file)
        logger.info("  %s: loaded %d bars from cache", symbol, len(df))
        return df

    fresh = fetch_range(client, symbol, start, end)
    if fresh.empty and cache_file.exists():
        return pd.read_parquet(cache_file)
    return merge_into_cache(symbol, fresh)


def load_all(symbols: list[str], start: str, end: str, force: bool = False) -> dict[str, pd.DataFrame]:
    client = StockHistoricalDataClient(config.api_key, config.secret_key)
    data = {}
    for symbol in symbols:
        df = download_symbol(client, symbol, start, end, force=force)
        if not df.empty:
            data[symbol] = df
    return data
