"""
Momentum Trading Bot with Trailing Stop Loss
Connects to Alpaca Paper Trading for demo execution.
"""

import logging
import signal
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


class MomentumBot:
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
        self.active = True          # external stop switch (web dashboard)
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
                saved.quantity = pos["qty"]      # partial fill / partial stop fill
                self.stops.save()
                logger.info("Adjusted %s quantity to %.4f", symbol, pos["qty"])

    def _reconcile(self):
        """Catch positions closed behind our back (a broker stop that fired)."""
        broker_symbols = {p["symbol"] for p in self.broker.get_open_positions()}
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
        """(Re)park a protective stop at Alpaca. Cancels the previous one first."""
        if not self.cfg.broker_stop_enabled:
            return
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
        if self.broker.cancel_stops_for(symbol):
            time.sleep(0.4)      # give Alpaca a moment to free the shares
        self._last_stop_sent.pop(symbol, None)
        pos = self.stops.get_position(symbol)
        if pos:
            pos.broker_stop_id = None

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
        self._release_broker_stop(symbol)

        result = self.broker.sell(symbol, qty)
        if not result:
            self._place_broker_stop(symbol)     # sell failed: re-arm the safety net
            return False

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
            logger.info("Closed %s (%s) @ $%.2f", symbol, reason, price)
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

    def scan_and_enter(self):
        """Scan for momentum stocks and enter new positions."""
        if not self._in_entry_window():
            logger.debug("Outside entry window (%s), skipping scan",
                         et_range_to_local(self.cfg.entry_start_et, self.cfg.entry_end_et))
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

            result = self.broker.buy(candidate.symbol, qty)
            if result:
                self.stops.open_position(candidate.symbol, price, qty, stop_distance, trail_distance)
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

            action = self.stops.update_price(symbol, price)

            if action == "stop_hit":
                pos = self.stops.get_position(symbol)
                if pos:
                    self._close_position(symbol, pos.quantity, price, "stop loss")
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

        self._print_status()
        self.recover_cash_if_needed()
        self._refresh_atrs()

        last_scan = 0
        last_report = 0

        while running and self.active:
            now = time.time()

            if self._market_hours():
                self._refresh_atrs()
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
                last_report = now

            time.sleep(self.cfg.monitor_interval_seconds)

        logger.info("Bot stopped. Final report:")
        self._print_holdings_report()


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
