"""Configuration for the momentum trading bot."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Alpaca credentials
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true")

    # Momentum scanner
    min_momentum_pct: float = 0.01          # Min tick up since last scan
    min_price: float = 5.0
    max_price: float = 2000.0
    scan_universe: list[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD",
        "NFLX", "CRM", "ORCL", "INTC", "QCOM", "AVGO", "MU", "AMAT",
        "COIN", "PLTR", "SOFI", "RIVN", "LCID", "NIO", "BABA", "JD",
        "SPY", "QQQ", "IWM", "ARKK", "SMCI", "MSTR", "MARA", "RIOT",
        "NBIS",
    ])

    # Volume filters
    min_volume_ratio: float = 1.2           # Latest bar volume vs avg of prior bars
    volume_lookback_bars: int = 20          # Bars for average volume

    # VWAP filters
    require_above_vwap: bool = True         # Price must be above VWAP to buy
    min_vwap_distance_pct: float = 0.2      # Min % above VWAP (0 = any amount above)

    # Position sizing
    max_positions: int = 5
    position_size_usd: float = 25000.0
    cash_reserve_usd: float = 100.0           # Min cash to keep (never go below)

    # Stop loss / trailing stop
    initial_stop_loss_pct: float = 1.0
    trailing_stop_pct: float = 0.5
    min_profit_to_trail_pct: float = 0.2

    # Research-backed improvements
    # 1) Entry time window: momentum edge is concentrated in the open;
    #    midday (11:30-14:00 ET) is chop that harvests stops.
    entry_window_enabled: bool = True
    entry_start_et: str = "09:35"          # no new entries before (ET)
    entry_end_et: str = "11:00"            # no new entries after (ET)
    flat_eod: bool = True                  # close everything before the bell
    eod_close_et: str = "15:55"
    close_stale_at_open: bool = True       # positions from previous days are cleared
                                           # at the open — they hold no fresh signal
                                           # and block slots for today's picks

    # 2) Volatility-scaled (ATR) stops: fixed 1% is inside the noise band
    #    for volatile names and too wide for calm ones.
    atr_period_days: int = 14
    atr_stop_mult: float = 0.5             # stop = entry - 0.5 * daily ATR
    atr_trail_mult: float = 0.35           # trail = high - 0.35 * daily ATR
    atr_trail_activate: float = 0.25       # trail arms after profit >= 0.25 * stop distance

    # 3) Risk-based sizing: risk a fixed % of equity to the stop
    #    instead of a fixed $ position.
    risk_pct_per_trade: float = 1.0
    max_position_value_usd: float = 25000.0

    # 4) Safety net: a real stop order parked at the broker, so positions stay
    #    protected when the bot is off / the machine sleeps / the process dies.
    #    Alpaca rejects fractional stop orders -> only whole shares get covered.
    broker_stop_enabled: bool = True
    stop_state_file: str = "data/stops.json"   # trailing-stop state survives restarts

    # 5) Trade journal: every closed trade appended to SQLite for real stats.
    trade_log_file: str = "data/trades.db"

    # Bot loop
    scan_interval_seconds: int = 3
    monitor_interval_seconds: int = 3
    report_interval_seconds: int = 60      # Holdings summary every minute


config = Config()
