"""
Momentum Trading Bot with Trailing Stop Loss
Connects to Alpaca Paper Trading for demo execution.
"""

import logging
import signal
import socket
import threading
import sys
import time
from datetime import datetime

from config import config
from broker import AlpacaBroker
from journal import TradeJournal
from scanner import MomentumScanner
from strategy import TrailingStopManager
from timeutil import ET, et_range_to_local, install_log_timezone, now_local

install_log_timezone()          # log lines carry Israel time, not machine time

# A half-open TCP connection — the normal result of a Wi-Fi flap or a laptop
# waking up — leaves recv() blocking with no timeout, because the alpaca SDK
# passes none to requests. On 25/08/2026 that froze the trading loop for 12
# minutes with the thread still ALIVE, so the watchdog saw nothing wrong.
# A default timeout turns an infinite hang into an ordinary exception, which
# the loop's network-retry path already knows how to handle.
socket.setdefaulttimeout(45)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

running = True


def handle_shutdown(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping bot...")
    running = False


def _is_network_error(exc: BaseException) -> bool:
    """Is this a connectivity problem rather than a bug?

    Walks the __cause__/__context__ chain: the alpaca SDK wraps a socket error
    in requests' ConnectionError, and only the innermost frame names it.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (socket.gaierror, socket.timeout, ConnectionError, TimeoutError)):
            return True
        if type(exc).__name__ in {
            "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
            "NewConnectionError", "NameResolutionError", "MaxRetryError",
            "ProtocolError", "RemoteDisconnected", "SSLError", "ChunkedEncodingError",
        }:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class MomentumBot:
    # CLASS-level, deliberately: selling and stop-placing are reachable from the
    # trading loop and from the dashboard thread, and a revived bot is a new
    # object. A per-instance lock would let a restarted bot sell alongside a
    # stuck one — on a long-only bot that means an accidental short.
    _position_lock = threading.RLock()

    def __init__(self):
        self.cfg = config
        self.broker = AlpacaBroker(self.cfg)
        self.scanner = MomentumScanner(self.broker.data, self.cfg)
        self.stops = TrailingStopManager(
            self.cfg, self.cfg.stop_state_file, account=self.broker.account_number
        )
        self.journal = TradeJournal(self.cfg.trade_log_file, account=self.broker.account_number)
        self.atrs: dict[str, float] = {}
        self._atr_date = None
        self.earnings_blocked: dict[str, list[str]] = {}
        self._earnings_date = None
        self.active = True          # external stop switch (web dashboard)
        self._lock = MomentumBot._position_lock
        self._heartbeat = time.time()   # watchdog liveness, see _daily_prep_loop
        self._last_stop_sent: dict[str, float | None] = {}
        self.stops.load()           # keep trailed stops across restarts
        self._sync_existing_positions()
        self.protect_all()

    def stop(self):
        self.active = False

    def _refresh_atrs(self):
        """Reload daily ATRs once per trading day."""
        today = datetime.now(ET).date()
        if self._atr_date != today:
            atrs = self.scanner.get_daily_atrs()
            if atrs:
                self.atrs = atrs
                self._atr_date = today

    def _refresh_earnings(self):
        """Reload earnings-reaction days once per day (disk-cached)."""
        if not self.cfg.earnings_day_veto:
            return
        today = datetime.now(ET).date()
        if self._earnings_date != today:
            from earnings_guard import load_blocked_days
            self.earnings_blocked = load_blocked_days(list(self.cfg.scan_universe))
            self._earnings_date = today

    @staticmethod
    def _parse_et(hhmm: str):
        h, m = hhmm.split(":")
        return int(h), int(m)

    def _in_entry_window(self) -> bool:
        if not self.cfg.entry_window_enabled:
            return True
        now = datetime.now(ET)
        sh, sm = self._parse_et(self.cfg.entry_start_et)
        eh, em = self._parse_et(self.cfg.entry_end_et)
        return (sh, sm) <= (now.hour, now.minute) < (eh, em)

    def _past_eod(self) -> bool:
        """True only in the closing window (eod_close_et..16:00 ET)."""
        now = datetime.now(ET)
        h, m = self._parse_et(self.cfg.eod_close_et)
        return (h, m) <= (now.hour, now.minute) < (16, 0)

    @staticmethod
    def _market_hours() -> bool:
        """Regular trading hours: Mon-Fri 09:30-16:00 ET. After-hours quotes
        have huge spreads that fire false stops — never act outside RTH."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        return (9, 30) <= (now.hour, now.minute) < (16, 0)

    def close_all_eod(self):
        """Flat before the close — no overnight gap risk."""
        for symbol in list(self.stops.active_symbols()):
            pos = self.stops.get_position(symbol)
            price = self.scanner.get_current_price(symbol) or pos.entry_price
            self._close_position(symbol, pos.quantity, price, "end of day")

    @staticmethod
    def _todays_open_local():
        """Today's 09:30 ET expressed as naive local time (entry_time's clock)."""
        open_et = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        return open_et.astimezone().replace(tzinfo=None)

    def close_stale_positions(self):
        """Positions carried over from previous days hold no fresh signal and
        block slots for today's picks — clear them once the market opens."""
        if not self.cfg.close_stale_at_open:
            return
        cutoff = self._todays_open_local()
        for symbol in list(self.stops.active_symbols()):
            pos = self.stops.get_position(symbol)
            if pos.entry_time >= cutoff:
                continue
            price = self.scanner.get_current_price(symbol) or pos.entry_price
            self._close_position(symbol, pos.quantity, price, "stale (previous day)")

    def _sync_existing_positions(self):
        """Reconcile saved stop state against what the broker actually holds."""
        broker_positions = {p["symbol"]: p for p in self.broker.get_open_positions()}

        # Gone at the broker (sold, or a protective stop fired while we were off)
        for symbol in list(self.stops.active_symbols()):
            if symbol not in broker_positions:
                closed = self.stops.close_position(symbol)
                logger.info(
                    "Position %s is gone at the broker — dropping its saved stop ($%.2f)",
                    symbol, closed.stop_loss if closed else 0.0,
                )

        for symbol, pos in broker_positions.items():
            saved = self.stops.get_position(symbol)
            if saved is None:
                self.stops.open_position(symbol, pos["entry_price"], pos["qty"])
                logger.info("Synced existing position: %s", symbol)
            elif abs(saved.quantity - pos["qty"]) > 1e-6:
                sold = saved.quantity - pos["qty"]
                # Shrunk while we were down: a parked stop order fired and sold
                # the whole-share part. Silently adjusting the quantity used to
                # erase that exit from the journal — on 2026-08-17 it lost
                # $193.60 of realized P/L across QCOM and INTC, which is exactly
                # the data the research engine reasons from.
                if sold > 1e-6 and not self.broker.get_open_stops().get(symbol):
                    self._journal_missed_exit(symbol, saved, sold)
                saved.quantity = pos["qty"]
                self.stops.save()
                logger.info("Adjusted %s quantity to %.4f", symbol, pos["qty"])

    def _journal_missed_exit(self, symbol: str, saved, sold: float):
        """Record an exit that a broker stop executed while the bot was off."""
        fill = self.broker.find_exit_fill(symbol, min_qty=sold * 0.9)
        exit_price = fill["price"] if fill else saved.stop_loss
        self.journal.record(
            symbol, sold, saved.entry_price, exit_price,
            "broker stop (בזמן שהבוט לא רץ)", saved.entry_time,
            stop_price=saved.stop_loss,
            initial_stop=saved.stop_history[0][1] if saved.stop_history else None,
        )
        logger.warning(
            "%s: %.4f מניות נמכרו בסטופ אצל הברוקר בזמן שהבוט לא רץ — נרשם ביומן @ $%.2f (%+.2f$)",
            symbol, sold, exit_price, (exit_price - saved.entry_price) * sold,
        )

    def _reconcile(self):
        """Catch positions closed behind our back (a broker stop that fired)."""
        broker_positions = {p["symbol"]: p for p in self.broker.get_open_positions()}
        broker_symbols = set(broker_positions)

        # a broker stop sells only the whole-share part — if it fired, sell the
        # fractional crumb left behind and record the full trade
        for symbol in list(self.stops.active_symbols()):
            bp = broker_positions.get(symbol)
            pos = self.stops.get_position(symbol)
            if not bp or not pos or not pos.broker_stop_id:
                continue
            if bp["qty"] < 1 and pos.quantity >= 1 and not self.broker.get_open_stops().get(symbol):
                logger.warning(
                    "%s broker stop fired — selling %.4f remainder and recording the trade",
                    symbol, bp["qty"],
                )
                res = self.broker.sell(symbol, bp["qty"])
                exit_price = (self.broker.get_fill_price(res["id"]) if res else None) or pos.stop_loss
                closed = self.stops.close_position(symbol)
                self._last_stop_sent.pop(symbol, None)
                if closed:
                    self.journal.record(
                        symbol, closed.quantity, closed.entry_price, exit_price,
                        "broker stop", closed.entry_time,
                        stop_price=closed.stop_loss,
                        initial_stop=closed.stop_history[0][1] if closed.stop_history else None,
                    )
                broker_symbols.discard(symbol)

        for symbol in list(self.stops.active_symbols()):
            if symbol in broker_symbols:
                continue
            closed = self.stops.close_position(symbol)
            self._last_stop_sent.pop(symbol, None)
            if closed:
                logger.warning(
                    "%s closed at the broker (protective stop @ $%.2f) — recording it",
                    symbol, closed.stop_loss,
                )
                self.journal.record(
                    symbol, closed.quantity, closed.entry_price,
                    closed.stop_loss, "broker stop", closed.entry_time,
                    stop_price=closed.stop_loss,
                    initial_stop=closed.stop_history[0][1] if closed.stop_history else None,
                )
        self.protect_all()

    # ---- broker-side protection ----------------------------------------

    def protect_all(self):
        """Make sure every open position has a live protective stop order."""
        if not self.cfg.broker_stop_enabled:
            return
        with self._lock:
            self._protect_all_locked()

    def _protect_all_locked(self):
        live = self.broker.get_open_stops()
        for symbol in self.stops.active_symbols():
            pos = self.stops.get_position(symbol)
            existing = live.get(symbol)
            if existing and abs(existing["stop_price"] - round(pos.stop_loss, 2)) < 0.01:
                pos.broker_stop_id = existing["id"]      # already covered
                continue
            self._place_broker_stop(symbol)
        self.stops.save()

    def _place_broker_stop(self, symbol: str, force: bool = False):
        """(Re)park a protective stop at Alpaca. Cancels the previous one first.

        Held under the same lock as selling: cancel-then-submit is not atomic,
        and two threads interleaving it would leave two live stop orders on the
        same shares — the second one sells stock we no longer own.
        """
        if not self.cfg.broker_stop_enabled:
            return
        with self._lock:
            self._place_broker_stop_locked(symbol, force)

    def _place_broker_stop_locked(self, symbol: str, force: bool = False):
        pos = self.stops.get_position(symbol)
        if not pos:
            return

        # A trailing stop can tick up many times a minute; re-sending the order
        # for every cent would burn the 200 calls/min budget for nothing.
        sent = self._last_stop_sent.get(symbol)
        if not force and sent is not None:
            if abs(pos.stop_loss - sent) < max(0.05, pos.stop_loss * 0.0015):
                return

        self.broker.cancel_stops_for(symbol)
        order = self.broker.submit_stop(symbol, pos.quantity, pos.stop_loss)
        pos.broker_stop_id = order["id"] if order else None
        self._last_stop_sent[symbol] = pos.stop_loss if order else None
        self.stops.save()

    def _release_broker_stop(self, symbol: str):
        """Open sell stops reserve the shares — cancel before selling."""
        if not self.cfg.broker_stop_enabled:
            return
        # already re-entrant via _close_position; explicit for direct callers
        if self.broker.cancel_stops_for(symbol):
            time.sleep(0.4)      # give Alpaca a moment to free the shares
        self._last_stop_sent.pop(symbol, None)
        pos = self.stops.get_position(symbol)
        if pos:
            pos.broker_stop_id = None

    def _daily_prep_loop(self):
        """Refresh ATRs and earnings dates OFF the trading path.

        Both are once-a-day network jobs, but they used to sit at the top of
        the 3-second trading loop, ahead of the scan. On 24/08/2026 the network
        flapped through the entry window and a yfinance call for ORCL hung for
        765 seconds with no timeout; the loop barely advanced, the ATRs landed
        at 18:08 and the earnings cache at 18:27 — both after the 18:00 window
        had closed. The bot never scanned once that day.

        A slow setup task must never cost a trading window. This thread owns
        the retries; the trading loop only ever reads the results.
        """
        while running and self.active:
            try:
                self._refresh_atrs()        # no-ops once it has today's data
                self._refresh_earnings()
            except Exception as e:
                logger.warning("הכנה יומית נכשלה — ינוסה שוב בעוד 30 שניות: %s", e)
            time.sleep(30)

    # ---- daily research -------------------------------------------------

    def research_if_due(self):
        """Once a day, after the close, study today's trades.

        Deliberately late (16:20 ET default): the free data plan embargoes the
        last ~15 minutes of SIP, and reviewing a session whose tail is missing
        measures the data hole instead of the trade.
        """
        if not self.cfg.research_enabled:
            return
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return
        h, m = self._parse_et(self.cfg.research_run_et)
        if (now.hour, now.minute) < (h, m):
            return
        if getattr(self, "_researched_day", None) == now.date():
            return
        self._researched_day = now.date()      # set first: a crash must not loop

        try:
            import postmortem
            result = postmortem.run(self.cfg.trade_log_file, self.journal.recent(limit=5000))
            if result.get("reviewed"):
                logger.info("חקירה יומית: נותחו %d עסקאות", result["reviewed"])
        except Exception as e:
            logger.warning("Daily research failed: %s", e)

    def _print_status(self):
        account = self.broker.get_account()
        positions = self.broker.get_open_positions()
        logger.info(
            "Account: equity=$%.2f cash=$%.2f | Positions: %d/%d",
            account["equity"], account["cash"], len(positions), self.cfg.max_positions,
        )
        for p in positions:
            stop_pos = self.stops.get_position(p["symbol"])
            stop_str = f"stop=${stop_pos.stop_loss:.2f}" if stop_pos else "no stop"
            logger.info(
                "  %s: qty=%.4f entry=$%.2f now=$%.2f P/L=%+.1f%% [%s]",
                p["symbol"], p["qty"], p["entry_price"], p["current_price"],
                p["unrealized_plpc"], stop_str,
            )

    def _print_holdings_report(self):
        """Print a formatted holdings summary with P/L every minute."""
        account = self.broker.get_account()
        positions = self.broker.get_open_positions()
        border = "*" * 50

        logger.info(border)
        logger.info("*  HOLDINGS REPORT  %s", now_local().strftime("%H:%M:%S"))
        logger.info(border)

        if not positions:
            logger.info("*  No open positions")
        else:
            total_value = 0.0
            total_pl = 0.0

            for p in positions:
                market_value = p["qty"] * p["current_price"]
                pl_usd = p["unrealized_pl"]
                pl_pct = p["unrealized_plpc"]
                total_value += market_value
                total_pl += pl_usd

                sign = "+" if pl_usd >= 0 else ""
                logger.info(
                    "*  %-6s  value=$%10.2f  P/L=%s$%.2f (%+.2f%%)",
                    p["symbol"], market_value, sign, pl_usd, pl_pct,
                )

            sign = "+" if total_pl >= 0 else ""
            logger.info("*  %s", "-" * 46)
            logger.info("*  TOTAL   value=$%10.2f  P/L=%s$%.2f", total_value, sign, total_pl)

        cash = account["cash"]
        equity = account["equity"]
        cash_sign = "+" if cash >= 0 else ""
        logger.info(
            "*  CASH=$%s%.2f  |  EQUITY=$%.2f  |  Positions: %d",
            cash_sign, cash, equity, len(positions),
        )
        logger.info(border)

    def _close_position(self, symbol: str, qty: float, price: float, reason: str):
        """Sell and remove a position from stop manager."""
        with self._lock:
            return self._close_position_locked(symbol, qty, price, reason)

    def _close_position_locked(self, symbol: str, qty: float, price: float, reason: str):
        # The caller's quantity may be stale — it was read before the lock, and
        # a broker stop may have filled in between. Selling more than we hold
        # opens a short position that nothing in this bot knows how to manage.
        held, broker_entry = qty, None
        try:
            bp = next((p for p in self.broker.get_open_positions()
                       if p["symbol"] == symbol), None)
            held = bp["qty"] if bp else 0.0
            # grab the entry price NOW — after the sale the position is gone and
            # an untracked trade would otherwise be journalled with no cost basis
            broker_entry = bp["entry_price"] if bp else None
        except Exception as e:
            logger.warning("לא ניתן לאמת כמות ל-%s לפני מכירה: %s", symbol, e)
        if held <= 0:
            logger.info("%s כבר לא מוחזקת אצל הברוקר — מדלג על המכירה", symbol)
            self.stops.close_position(symbol)
            self._last_stop_sent.pop(symbol, None)
            return False
        qty = min(qty, held)

        self._release_broker_stop(symbol)

        result = self.broker.sell(symbol, qty)
        if not result:
            self._place_broker_stop(symbol)     # sell failed: re-arm the safety net
            return False

        # journal the ACTUAL fill, not the quote that triggered the decision —
        # a transient bad quote once recorded -$160 on a trade that filled +$67
        fill = self.broker.get_fill_price(result["id"])
        if fill is not None:
            price = fill

        closed = self.stops.close_position(symbol)
        if closed:
            pnl_pct = ((price - closed.entry_price) / closed.entry_price) * 100
            self.journal.record(
                symbol, qty, closed.entry_price, price, reason, closed.entry_time,
                stop_price=closed.stop_loss,
                initial_stop=closed.stop_history[0][1] if closed.stop_history else None,
            )
            logger.info(
                "Closed %s (%s): entry=$%.2f exit=$%.2f P/L=%+.1f%%",
                symbol, reason, closed.entry_price, price, pnl_pct,
            )
        else:
            # Untracked position (opened outside the bot, or already dropped by
            # a concurrent path). It still left the account, so it still belongs
            # in the journal — a missing row silently corrupts every statistic
            # and every conclusion the research engine draws from them.
            entry_time = self.broker.get_position_entry_time(symbol, qty)
            self.journal.record(symbol, qty, broker_entry or price, price,
                                f"{reason} (לא היה במעקב)", entry_time)
            logger.warning("Closed %s (%s) @ $%.2f — לא היה במעקב, נרשם ביומן",
                           symbol, reason, price)
        return True

    def recover_cash_if_needed(self):
        """Sell a position when cash is negative or below reserve."""
        account = self.broker.get_account()
        cash = account["cash"]

        if cash >= self.cfg.cash_reserve_usd:
            return

        needed = self.cfg.cash_reserve_usd - cash
        logger.warning(
            "Cash low ($%.2f) — selling a position to recover (~$%.2f needed)",
            cash, needed,
        )

        positions = self.broker.get_open_positions()
        if not positions:
            logger.error("Cash is $%.2f but no open positions to sell", cash)
            return

        positions.sort(key=lambda p: p["qty"] * p["current_price"], reverse=True)
        target = positions[0]
        symbol = target["symbol"]
        price = target["current_price"]

        freed = target["qty"] * price
        self._close_position(symbol, target["qty"], price, "cash recovery")
        logger.info("Freed ~$%.2f from selling %s", freed, symbol)

    def _handle_weak_tape(self):
        """SPY below its VWAP: the regime that favors shorts (sim 13/08:
        COMBO positive in all 5 periods). OBSERVER stage — every short the bot
        would open is logged to data/short_observations.jsonl, nothing is
        executed, until live observation confirms the simulation."""
        candidates = self.scanner.scan_shorts()
        if not candidates:
            return

        from earnings_guard import is_blocked_today
        now = datetime.now(ET)
        throttle = getattr(self, "_short_obs_last", {})
        self._short_obs_last = throttle

        for c in candidates[: self.cfg.max_positions]:
            if self.stops.has_position(c.symbol):
                continue
            last = throttle.get(c.symbol)
            if last and (now - last).total_seconds() < 300:
                continue                      # one observation per symbol / 5 min
            if self.cfg.earnings_day_veto and is_blocked_today(c.symbol, self.earnings_blocked):
                continue
            atr = self.atrs.get(c.symbol)
            if not atr or atr <= 0:
                continue
            prev_close = getattr(self.scanner, "prev_closes", {}).get(c.symbol)
            day_chg = ((c.price - prev_close) / prev_close * 100) if prev_close else 0.0
            if day_chg <= self.cfg.short_ssr_guard_pct:
                continue                      # crashed already — SSR / squeeze zone

            stop_d = self.cfg.atr_stop_mult * atr
            try:
                equity = self.broker.get_account()["equity"]
            except Exception:
                equity = 100_000.0
            qty = int(min((equity * self.cfg.short_risk_pct / 100) / stop_d,
                          self.cfg.max_position_value_usd / c.price))
            if qty < 1:
                continue

            throttle[c.symbol] = now
            obs = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": c.symbol, "price": c.price,
                "stop": round(c.price + stop_d, 2), "qty": qty,
                "tick_pct": c.tick_change_pct, "vs_vwap_pct": c.price_vs_vwap_pct,
                "vol_ratio": c.volume_ratio, "day_chg_pct": round(day_chg, 2),
            }
            logger.info(
                "OBSERVER: would SHORT %s x%d @ $%.2f (stop $%.2f, tick %.3f%%, vwap %.2f%%, vol %.1fx)",
                c.symbol, qty, c.price, obs["stop"], c.tick_change_pct,
                c.price_vs_vwap_pct, c.volume_ratio,
            )
            try:
                import json as _json
                from pathlib import Path as _Path
                p = _Path("data/short_observations.jsonl")
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as f:
                    f.write(_json.dumps(obs) + "\n")
            except Exception as e:
                logger.warning("Could not record short observation: %s", e)

            if not self.cfg.shorts_observe_only:
                logger.warning("Short execution requested but not implemented yet — staying observer-only")

    def scan_and_enter(self):
        """Scan for momentum stocks and enter new positions."""
        if not self._in_entry_window():
            logger.debug("Outside entry window (%s), skipping scan",
                         et_range_to_local(self.cfg.entry_start_et, self.cfg.entry_end_et))
            return

        # Market gate routes DIRECTION: SPY above its VWAP -> longs;
        # below -> short signals (observer stage for now).
        # None (no data) fails OPEN for longs — a hiccup must not stop the bot.
        if self.cfg.spy_gate_enabled and self.scanner.spy_above_vwap() is False:
            if self.cfg.shorts_enabled:
                self._handle_weak_tape()
            else:
                logger.debug("SPY below its VWAP — market gate closed, no new longs")
            return

        if len(self.stops.active_symbols()) >= self.cfg.max_positions:
            logger.debug("Max positions reached, skipping scan")
            return

        candidates = self.scanner.scan()

        for candidate in candidates:
            if not running:
                break

            if len(self.stops.active_symbols()) >= self.cfg.max_positions:
                break

            if self.stops.has_position(candidate.symbol):
                continue

            if self.cfg.earnings_day_veto:
                from earnings_guard import is_blocked_today
                if is_blocked_today(candidate.symbol, self.earnings_blocked):
                    logger.debug("%s blocked today (earnings reaction day)", candidate.symbol)
                    continue

            account = self.broker.get_account()
            available = max(0.0, account["cash"] - self.cfg.cash_reserve_usd)

            if available <= 0:
                logger.warning(
                    "No cash left (cash=$%.2f) — trying to sell a position",
                    account["cash"],
                )
                self.recover_cash_if_needed()
                break

            price = self.scanner.get_current_price(candidate.symbol) or candidate.price

            # Volatility-scaled stop + risk-based sizing (fallback: old fixed %/$)
            atr = self.atrs.get(candidate.symbol)
            if atr and atr > 0:
                stop_distance = self.cfg.atr_stop_mult * atr
                trail_distance = self.cfg.atr_trail_mult * atr
                risk_usd = account["equity"] * self.cfg.risk_pct_per_trade / 100
                target_usd = min(
                    (risk_usd / stop_distance) * price,
                    self.cfg.max_position_value_usd,
                )
            else:
                stop_distance = trail_distance = None
                target_usd = self.cfg.position_size_usd

            qty, cost = self.broker.calculate_affordable_qty(price, target_usd)

            if qty <= 0:
                logger.debug("Cannot afford %s (cash=$%.2f)", candidate.symbol, account["cash"])
                continue

            if cost > account["cash"] - self.cfg.cash_reserve_usd:
                logger.warning("Blocked %s: cost $%.2f exceeds available cash $%.2f", candidate.symbol, cost, available)
                continue

            # Scraps of leftover cash buy a fraction of a share, which Alpaca
            # cannot protect with a stop order. Skip instead of opening one.
            if cost < self.cfg.min_position_usd:
                logger.info(
                    "Skipping %s: $%.2f is below the $%.0f minimum position "
                    "(too small to protect with a broker stop)",
                    candidate.symbol, cost, self.cfg.min_position_usd,
                )
                if available < self.cfg.min_position_usd:
                    break        # cash is the limit — no later candidate does better
                continue

            result = self.broker.buy(candidate.symbol, qty)
            if result:
                fill = self.broker.get_fill_price(result["id"])
                if fill is not None:
                    price = fill            # stops anchor on the real entry, not the signal quote
                self.stops.open_position(candidate.symbol, price, qty, stop_distance, trail_distance)
                # the buy must be off Alpaca's open book first, or the stop is
                # rejected as a wash trade and the position sits unprotected
                self.broker.wait_until_order_done(result["id"])
                self._place_broker_stop(candidate.symbol)
                logger.info(
                    "Entered: %s tick=+%.3f%% vwap=$%.2f (+%.2f%%) vol=%.1fx @ $%.2f (cost=$%.2f, cash left~$%.2f)",
                    candidate.symbol, candidate.tick_change_pct, candidate.vwap,
                    candidate.price_vs_vwap_pct, candidate.volume_ratio, price,
                    cost, account["cash"] - cost,
                )

    def monitor_stops(self):
        """Monitor open positions and execute trailing stop losses."""
        for symbol in list(self.stops.active_symbols()):
            if not running:
                break

            price = self.scanner.get_current_price(symbol)
            if price is None:
                continue

            # a new high must be seen twice before it may raise the trail —
            # a ghost HIGH quote would tighten the broker stop into the market
            pos = self.stops.get_position(symbol)
            if pos and price > pos.highest_price:
                confirm = self.scanner.get_current_price(symbol)
                if confirm is not None:
                    price = min(price, confirm)

            action = self.stops.update_price(symbol, price)

            if action == "stop_hit":
                pos = self.stops.get_position(symbol)
                if not pos:
                    continue
                # Zero-false-sell architecture: our quotes never liquidate.
                # The parked broker STOP order is the executor — it reacts to
                # the real consolidated market, not to our IEX feed. A ghost
                # quote here therefore cannot sell anything.
                if pos.broker_stop_id and self.broker.get_open_stops().get(symbol):
                    logger.info(
                        "%s at stop per our feed ($%.2f <= $%.2f) — deferring to the broker stop order",
                        symbol, price, pos.stop_loss,
                    )
                    continue
                # no live broker stop (fractional-only position or placement
                # failed): software fallback, double-confirmed
                confirm = self.scanner.get_current_price(symbol)
                if confirm is not None and confirm > pos.stop_loss:
                    logger.info(
                        "Stop trigger for %s not confirmed (quote $%.2f -> $%.2f) — holding",
                        symbol, price, confirm,
                    )
                    continue
                self._close_position(symbol, pos.quantity, confirm or price, "stop loss")
            elif action == "stop_raised":
                self._place_broker_stop(symbol)      # keep the safety net in step

    def run(self):
        logger.info("=" * 50)
        logger.info("Momentum Bot Started (Alpaca Paper Trading)")
        logger.info("Scan interval: %ds | Monitor interval: %ds", self.cfg.scan_interval_seconds, self.cfg.monitor_interval_seconds)
        logger.info("Initial stop: %.1f%% | Trailing stop: %.1f%%", self.cfg.initial_stop_loss_pct, self.cfg.trailing_stop_pct)
        logger.info("Entry window %s | market %s (Israel time)",
                    et_range_to_local(self.cfg.entry_start_et, self.cfg.entry_end_et),
                    et_range_to_local("09:30", "16:00"))
        logger.info("=" * 50)

        # Startup probes talk to the network too — a hiccup here must not stop
        # the bot before it has begun.
        try:
            self._print_status()
            self.recover_cash_if_needed()
            self._refresh_atrs()
        except Exception as e:
            logger.error("שגיאה באתחול הבוט (ממשיך בכל זאת): %s", e)

        threading.Thread(target=self._daily_prep_loop, daemon=True,
                         name="daily-prep").start()

        last_scan = 0
        last_report = 0
        errors = 0                  # consecutive non-network failures
        net_errors = 0              # consecutive connectivity failures

        while running and self.active:
          try:
            now = time.time()
            self._heartbeat = now       # proof of life for the watchdog

            if self._market_hours():
                # NOTE: _refresh_atrs / _refresh_earnings deliberately do NOT
                # run here — see _daily_prep_loop.
                self.close_stale_positions()
                self.recover_cash_if_needed()
                self.monitor_stops()

                if self.cfg.flat_eod and self._past_eod() and self.stops.active_symbols():
                    self.close_all_eod()

                if now - last_scan >= self.cfg.scan_interval_seconds:
                    self.scan_and_enter()
                    last_scan = now

            if now - last_report >= self.cfg.report_interval_seconds:
                # A broker stop may have fired on its own — notice it, and make
                # sure nothing is left unprotected.
                self._reconcile()
                self._print_holdings_report()
                self.research_if_due()
                last_report = now

            errors = net_errors = 0
          except Exception as e:
            # A dropped keep-alive connection to Alpaca used to kill this thread
            # outright. The server stayed up, the dashboard still read "off",
            # and the bot silently missed a whole session: it died at 01:56 on
            # 2026-08-18 and was noticed at 17:32, an hour into the entry
            # window. A transient network fault costs a retry, not the day.
            # A network fault is NOT a reason to give up. On 20/08/2026 the
            # laptop slept, DNS died for nine hours, this counter reached 20 and
            # the bot quit on purpose — guaranteeing it was dead when the link
            # came back. Connectivity always returns; logic errors do not fix
            # themselves. So they are counted separately.
            if _is_network_error(e):
                net_errors += 1
                if net_errors in (1, 5, 20) or net_errors % 50 == 0:
                    logger.error("אין תקשורת (%d ניסיונות) — ממתין וממשיך לנסות: %s",
                                 net_errors, e)
                time.sleep(min(60, 5 * net_errors))
            else:
                errors += 1
                logger.error("שגיאה בלולאת הבוט (%d ברצף) — ממשיך: %s", errors, e,
                             exc_info=(errors == 1))
                if errors >= self.cfg.max_consecutive_errors:
                    logger.critical(
                        "הבוט נעצר אחרי %d שגיאות רצופות — נדרשת בדיקה ידנית", errors)
                    break
                time.sleep(min(30, 3 * errors))   # back off, then try again

          time.sleep(self.cfg.monitor_interval_seconds)

        logger.info("Bot stopped. Final report:")
        try:
            self._print_holdings_report()
        except Exception as e:
            # this ran unguarded and produced the alarming "Exception in thread
            # bot-loop" traceback on 20/08/2026, long after the real cause
            logger.warning("לא ניתן להדפיס דוח סיום: %s", e)


def main():
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        bot = MomentumBot()
        bot.run()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
