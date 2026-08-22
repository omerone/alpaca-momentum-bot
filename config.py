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
    # Ceiling on the trail distance, as a % of entry price. None = ATR only.
    # REJECTED 19/08/2026 — kept only as a documented dead end.
    # First pass (examples/backtest_trail_cap.py, 10 symbols x 4 periods) made
    # 1.2% look like the answer: the only cap that helped in all four periods
    # (+2,219 / +1,254 / +635 / +211). But a cap is a tuned number, so it also
    # has to sit on a PLATEAU, not a spike. Mapping its neighbours killed it:
    #   1.6% -> fails 1 period      1.4% -> fails 2      [1.2% -> passes]
    #   1.1% -> fails 2             1.0% -> fails 1
    # A setting whose immediate neighbours lose money is fitted to noise, not a
    # discovered edge. Same verdict, same shape, as the 0.25 trail multiple
    # (12/08) and the 3-position sizing spike (19/08).
    # Rule learned: "helps in every period" is necessary, never sufficient —
    # always map the neighbours before adopting a number.
    trail_cap_pct: float | None = None

    # 3) Risk-based sizing: risk a fixed % of equity to the stop
    #    instead of a fixed $ position.
    risk_pct_per_trade: float = 1.0
    max_position_value_usd: float = 25000.0
    # Floor on a new position. Leftover cash used to buy a fraction of a share
    # (once: NVDA 0.74 sh for $168) — Alpaca cannot park a stop order on less
    # than one whole share, so such a position is unprotectable when the bot is
    # off, and it pollutes the journal with noise-sized trades.
    min_position_usd: float = 1000.0

    # 4) Safety net: a real stop order parked at the broker, so positions stay
    #    protected when the bot is off / the machine sleeps / the process dies.
    #    Alpaca rejects fractional stop orders -> only whole shares get covered.
    broker_stop_enabled: bool = True
    stop_state_file: str = "data/stops.json"   # trailing-stop state survives restarts

    # 5) Trade journal: every closed trade appended to SQLite for real stats.
    trade_log_file: str = "data/trades.db"

    # Dashboard server. host 0.0.0.0 exposes it to the local network (phone);
    # anything not coming from this machine must present the access token.
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "0.0.0.0"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_PORT", "5050")))
    dashboard_token_file: str = "data/dashboard_token.txt"

    # 6) Opening intelligence (opening_intel.py): premarket-derived context.
    #    DISPLAY ONLY. Backtested as entry filters on the megacap focus list
    #    (2026-08-12) and they HURT (+1,308 -> +7 with RVOL gate; +526 with
    #    vetoes only): the stocks-in-play edge needs a wide universe to select
    #    from, and the gap-after-gap veto removed post-earnings winners.
    #    Filters stay off unless re-validated on a wide scanning universe.
    intel_enabled: bool = False
    rvol_min: float = 1.2                  # first-5-min volume vs 14-day average
    premarket_vol_min: float = 200_000     # shares; gates "empty" gaps (SIP pass only)
    gap_veto_pct: float = 5.0              # skip stocks that gapped this hard yesterday

    # 8) Earnings-day guard: no NEW entries in a stock on its earnings-reaction
    #    day (report day if before open, next day if after close). Risk rule —
    #    own-data diagnostic: earnings-day trades netted ~-$1,100 on ~18 trades.
    earnings_day_veto: bool = True

    # 9) Daily post-mortem (postmortem.py): after the close, replay every trade
    #    against that day's minute bars — what it left on the table, what the
    #    bot's own rules would have done, which settings would have paid better.
    #    Produces RECOMMENDATIONS ONLY; it never edits this file. Runs at 16:20
    #    ET rather than at the close because the free plan embargoes the last
    #    ~15 minutes of SIP data, and a partial session measures nothing.
    research_enabled: bool = True
    research_run_et: str = "16:20"

    # 7) Market gate: no new longs while SPY trades below its intraday VWAP.
    #    Edge-tested 2026-08-12: improved BOTH test periods (+1,308->+2,777 and
    #    -627->+59); the alternative 30-min-return definition failed December —
    #    use the VWAP definition only.
    spy_gate_enabled: bool = True

    # 9) Regime-routed shorts (validated 2026-08-13: COMBO positive in all 5
    #    test periods, incl. both periods where longs lost). SPY below its VWAP
    #    -> short signals instead of longs. OBSERVER stage first: signals are
    #    logged to data/short_observations.jsonl but NOT executed, until a few
    #    days of live observation confirm the simulation.
    shorts_enabled: bool = True
    shorts_observe_only: bool = True
    short_risk_pct: float = 0.5            # half the long risk, per the literature
    short_ssr_guard_pct: float = -9.0      # never short a stock already down this much

    # Bot loop
    # A transient Alpaca connection drop used to kill the bot thread outright
    # (18/08/2026: died 01:56, unnoticed until 17:32 — a whole entry window).
    # The loop retries now; this is the give-up threshold for real, persistent
    # failures.
    max_consecutive_errors: int = 20
    scan_interval_seconds: int = 3
    monitor_interval_seconds: int = 3
    report_interval_seconds: int = 60      # Holdings summary every minute


config = Config()
