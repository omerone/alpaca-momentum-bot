"""Momentum scanner with Volume and VWAP confirmation."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from config import Config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class MomentumCandidate:
    symbol: str
    price: float
    tick_change_pct: float
    bar_change_pct: float
    rising_bars: int
    vwap: float
    price_vs_vwap_pct: float
    volume_ratio: float
    score: float


def calculate_vwap(bars: list) -> float:
    """VWAP = sum(typical_price * volume) / sum(volume)"""
    total_pv = 0.0
    total_v = 0.0
    for bar in bars:
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
        vol = float(bar.volume)
        total_pv += typical * vol
        total_v += vol
    return total_pv / total_v if total_v > 0 else 0.0


def calculate_volume_ratio(bars: list, lookback: int) -> float:
    """Latest bar volume vs average of prior bars."""
    if len(bars) < 2:
        return 0.0
    latest_vol = float(bars[-1].volume)
    prior = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
    if not prior:
        return 0.0
    avg_vol = sum(float(b.volume) for b in prior) / len(prior)
    return latest_vol / avg_vol if avg_vol > 0 else 0.0


class MomentumScanner:
    def __init__(self, data_client: StockHistoricalDataClient, cfg: Config):
        self.client = data_client
        self.cfg = cfg
        self._last_prices: dict[str, float] = {}

    def _market_open_today(self) -> datetime:
        now_et = datetime.now(ET)
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_et < open_et:
            open_et -= timedelta(days=1)
        return open_et

    def _get_all_prices(self) -> dict[str, float]:
        try:
            request = StockLatestQuoteRequest(
                symbol_or_symbols=self.cfg.scan_universe,
                feed=DataFeed.IEX,
            )
            quotes = self.client.get_stock_latest_quote(request)
            prices = {}
            for symbol in self.cfg.scan_universe:
                if symbol not in quotes:
                    continue
                q = quotes[symbol]
                ask, bid = float(q.ask_price), float(q.bid_price)
                if ask > 0 and bid > 0:
                    mid = (ask + bid) / 2
                    # wide spread = unreliable quote (after hours / stale) -> skip
                    if (ask - bid) / mid > 0.02:
                        continue
                    prices[symbol] = mid
            return prices
        except Exception as e:
            logger.error("Failed to fetch quotes: %s", e)
            return {}

    def _get_intraday_bars(self) -> dict[str, list]:
        """Fetch 1-min bars from today's market open for VWAP + volume."""
        start = self._market_open_today()
        end = datetime.now(ET)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=self.cfg.scan_universe,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            bars = self.client.get_stock_bars(request)
            return bars.data
        except Exception as e:
            logger.error("Failed to fetch intraday bars: %s", e)
            return {}

    def _bar_stats(self, symbol_bars: list) -> tuple[int, float]:
        if not symbol_bars or len(symbol_bars) < 2:
            return 0, 0.0

        latest, prev = symbol_bars[-1], symbol_bars[-2]
        bar_change = ((float(latest.close) - float(prev.close)) / float(prev.close)) * 100

        rising = 0
        for i in range(len(symbol_bars) - 1, 0, -1):
            if float(symbol_bars[i].close) > float(symbol_bars[i - 1].close):
                rising += 1
            else:
                break

        return rising, bar_change

    def scan(self) -> list[MomentumCandidate]:
        candidates = []
        current_prices = self._get_all_prices()
        intraday_bars = self._get_intraday_bars()

        if not current_prices:
            return []

        for symbol, price in current_prices.items():
            if price < self.cfg.min_price or price > self.cfg.max_price:
                continue

            last_price = self._last_prices.get(symbol)
            if last_price is None or last_price <= 0:
                continue

            tick_change = ((price - last_price) / last_price) * 100
            if tick_change < self.cfg.min_momentum_pct:
                continue

            bars = intraday_bars.get(symbol, [])
            if not bars:
                continue

            vwap = calculate_vwap(bars)
            if vwap <= 0:
                continue

            price_vs_vwap = ((price - vwap) / vwap) * 100

            if self.cfg.require_above_vwap and price <= vwap:
                continue

            if price_vs_vwap < self.cfg.min_vwap_distance_pct:
                continue

            vol_ratio = calculate_volume_ratio(bars, self.cfg.volume_lookback_bars)
            if vol_ratio < self.cfg.min_volume_ratio:
                continue

            rising_bars, bar_change = self._bar_stats(bars)

            score = (
                tick_change * 10
                + bar_change * 3
                + rising_bars
                + price_vs_vwap * 2
                + vol_ratio * 5
            )

            candidates.append(MomentumCandidate(
                symbol=symbol,
                price=price,
                tick_change_pct=round(tick_change, 4),
                bar_change_pct=round(bar_change, 4),
                rising_bars=rising_bars,
                vwap=round(vwap, 2),
                price_vs_vwap_pct=round(price_vs_vwap, 4),
                volume_ratio=round(vol_ratio, 2),
                score=round(score, 4),
            ))

        self._last_prices.update(current_prices)

        candidates.sort(key=lambda c: c.score, reverse=True)
        if candidates:
            logger.info("VWAP+Volume momentum: %d stocks", len(candidates))
            for c in candidates[:5]:
                logger.info(
                    "  %s: tick=+%.3f%% vwap=$%.2f (+%.2f%%) vol=%.1fx @ $%.2f",
                    c.symbol, c.tick_change_pct, c.vwap, c.price_vs_vwap_pct,
                    c.volume_ratio, c.price,
                )

        return candidates

    def get_current_price(self, symbol: str) -> float | None:
        return self._get_all_prices().get(symbol)

    def get_daily_atrs(self) -> dict[str, float]:
        """14-day ATR per symbol from daily bars (for volatility-scaled stops)."""
        end = datetime.now(ET)
        start = end - timedelta(days=self.cfg.atr_period_days * 2 + 15)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=self.cfg.scan_universe,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            data = self.client.get_stock_bars(request).data
        except Exception as e:
            logger.error("Failed to fetch daily bars for ATR: %s", e)
            return {}

        atrs = {}
        for symbol, bars in data.items():
            if len(bars) < 6:
                continue
            trs = []
            for prev, cur in zip(bars, bars[1:]):
                high, low, prev_close = float(cur.high), float(cur.low), float(prev.close)
                trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            window = trs[-self.cfg.atr_period_days:]
            atrs[symbol] = sum(window) / len(window)
        logger.info("Daily ATR loaded for %d symbols", len(atrs))
        return atrs
