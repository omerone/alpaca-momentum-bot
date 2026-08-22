"""Alpaca paper trading broker integration."""

import logging
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetCalendarRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderType
from alpaca.data.historical import StockHistoricalDataClient

from config import Config
from timeutil import ET

logger = logging.getLogger(__name__)


class AlpacaBroker:
    """Wrapper for Alpaca paper trading API."""

    def __init__(self, cfg: Config):
        if not cfg.api_key or not cfg.secret_key:
            raise ValueError(
                "Missing Alpaca API keys. Copy .env.example to .env and add your paper trading keys.\n"
                "Get free keys at: https://app.alpaca.markets/paper/dashboard/overview"
            )

        self.cfg = cfg
        self.trading = TradingClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
            paper=cfg.paper,
        )
        self.data = StockHistoricalDataClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
        )

        mode = "PAPER" if cfg.paper else "LIVE"
        self.account_number = ""
        try:
            self.account_number = str(self.trading.get_account().account_number)
        except Exception as e:
            logger.warning("Could not read account number: %s", e)
        logger.info("Connected to Alpaca (%s trading) — account %s", mode, self.account_number or "?")

    def get_account(self) -> dict:
        account = self.trading.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "account_number": str(account.account_number),
            "paper": self.cfg.paper,
        }

    def get_open_positions(self) -> list[dict]:
        positions = self.trading.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
            }
            for p in positions
        ]

    def get_position_entry_time(self, symbol: str, qty: float | None = None):
        """When the currently open position in `symbol` was opened.

        Walks filled orders newest -> oldest and takes the oldest BUY of the
        current streak (a SELL means the previous position was already closed).
        Returns a tz-aware datetime, or None when no fill history is found.
        """
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=200,
                direction="desc",
            )
            orders = self.trading.get_orders(req)
        except Exception as e:
            logger.warning("Order history failed for %s: %s", symbol, e)
            return None

        entry_time = None
        accumulated = 0.0
        for order in orders:
            if not getattr(order, "filled_at", None):
                continue
            side = str(order.side).lower()
            filled = float(order.filled_qty or 0)
            if filled <= 0:
                continue
            if "sell" in side:
                break                      # older fills belong to a closed position
            entry_time = order.filled_at   # keep walking back: oldest buy wins
            accumulated += filled
            if qty is not None and accumulated >= qty - 1e-6:
                break
        return entry_time

    def buy(self, symbol: str, qty: float) -> dict | None:
        try:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            result = self.trading.submit_order(order)
            logger.info("BUY order submitted: %s x %.4f (order_id=%s)", symbol, qty, result.id)
            return {"id": str(result.id), "symbol": symbol, "qty": qty, "side": "buy"}
        except Exception as e:
            logger.error("BUY failed for %s: %s", symbol, e)
            return None

    def sell(self, symbol: str, qty: float) -> dict | None:
        try:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            result = self.trading.submit_order(order)
            logger.info("SELL order submitted: %s x %.4f (order_id=%s)", symbol, qty, result.id)
            return {"id": str(result.id), "symbol": symbol, "qty": qty, "side": "sell"}
        except Exception as e:
            logger.error("SELL failed for %s: %s", symbol, e)
            return None

    def get_fill_price(self, order_id: str, retries: int = 6, wait: float = 0.5) -> float | None:
        """Actual average fill price of an order. Market orders on paper fill
        within a second — poll briefly. None if not (yet) filled."""
        import time as _time
        for _ in range(retries):
            try:
                o = self.trading.get_order_by_id(order_id)
                if o.filled_avg_price is not None and float(o.filled_qty or 0) > 0:
                    return float(o.filled_avg_price)
            except Exception as e:
                logger.warning("Fill lookup failed for %s: %s", order_id, e)
                return None
            _time.sleep(wait)
        return None

    def find_exit_fill(self, symbol: str, min_qty: float = 0.0) -> dict | None:
        """The most recent filled SELL for a symbol: {qty, price, at}.

        Needed to journal an exit that happened while the bot was down — a
        parked stop order that fired. Without it the trade is lost to the
        record, and every statistic built on the journal is quietly wrong.
        """
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100, symbols=[symbol])
            orders = self.trading.get_orders(req)
        except Exception as e:
            logger.warning("Could not look up exit fill for %s: %s", symbol, e)
            return None

        best = None
        for o in orders:
            if str(getattr(o.side, "value", o.side)).lower() != "sell":
                continue
            if not getattr(o, "filled_at", None) or o.filled_avg_price is None:
                continue
            qty = float(o.filled_qty or 0)
            if qty < min_qty - 1e-6:
                continue
            if best is None or o.filled_at > best["at"]:
                best = {"qty": qty, "price": float(o.filled_avg_price), "at": o.filled_at}
        return best

    def wait_until_order_done(self, order_id: str, retries: int = 10, wait: float = 0.4) -> bool:
        """Block until an order leaves Alpaca's open book.

        A filled order is not immediately *closed* — fractional orders linger in
        a settling state, and while they do, Alpaca rejects any opposite-side
        order on the symbol as a "potential wash trade" (code 40310000). That is
        what left fresh positions without a broker stop for ~35 seconds, until
        the next monitor pass retried. Waiting here closes that window.
        """
        import time as _time
        DONE = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
        for _ in range(retries):
            try:
                status = str(getattr(self.trading.get_order_by_id(order_id), "status", "")).lower()
            except Exception as e:
                logger.warning("Order status lookup failed for %s: %s", order_id, e)
                return False
            if any(d in status for d in DONE):
                return True
            _time.sleep(wait)
        logger.warning("Order %s still open after %.1fs — placing the stop anyway",
                       order_id, retries * wait)
        return False

    # ---- protective stop orders parked at the broker -------------------
    #
    # The in-memory trailing stop only works while the bot is running. These
    # orders keep the position covered when it is not. Alpaca rejects stop
    # orders on fractional quantities, so only the whole-share part is covered.

    def submit_stop(self, symbol: str, qty: float, stop_price: float) -> dict | None:
        whole = math.floor(qty + 1e-9)
        if whole < 1:
            logger.debug("No broker stop for %s: %.4f shares is below 1", symbol, qty)
            return None
        try:
            order = StopOrderRequest(
                symbol=symbol,
                qty=whole,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,   # must survive overnight
                stop_price=round(stop_price, 2),
            )
            result = self.trading.submit_order(order)
            logger.info("Protective stop placed: %s %d sh @ $%.2f", symbol, whole, stop_price)
            return {"id": str(result.id), "symbol": symbol, "qty": whole, "stop_price": round(stop_price, 2)}
        except Exception as e:
            # A sell stop must sit below the market. Being above it means the
            # stop was already breached while nothing was watching — the right
            # answer is to sell, not to park an order, and monitor_stops will
            # do exactly that at the next tick of regular trading hours.
            if "stop price must be less than current price" in str(e):
                logger.warning(
                    "%s is already below its stop ($%.2f) — no order parked; "
                    "it will be sold when regular trading resumes",
                    symbol, stop_price,
                )
            else:
                logger.error("Stop order failed for %s: %s", symbol, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.trading.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.warning("Cancel order %s failed: %s", order_id, e)
            return False

    def get_open_stops(self) -> dict[str, dict]:
        """symbol -> {id, qty, stop_price} for live protective sell stops."""
        try:
            orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        except Exception as e:
            logger.warning("Could not read open orders: %s", e)
            return {}

        stops = {}
        for o in orders:
            if getattr(o, "order_type", None) != OrderType.STOP:
                continue
            if "sell" not in str(o.side).lower():
                continue
            stops[o.symbol] = {
                "id": str(o.id),
                "qty": float(o.qty or 0),
                "stop_price": float(o.stop_price or 0),
            }
        return stops

    def cancel_stops_for(self, symbol: str) -> int:
        """Release shares reserved by protective stops before selling."""
        cancelled = 0
        for sym, stop in self.get_open_stops().items():
            if sym == symbol and self.cancel_order(stop["id"]):
                cancelled += 1
        return cancelled

    def get_calendar(self, days: int = 14) -> list[dict]:
        """Upcoming trading sessions, tz-aware.

        Straight from Alpaca so holidays and half-days (early closes) are
        handled — hardcoding 09:30-16:00 would be wrong a dozen days a year.
        """
        from datetime import date, timedelta
        try:
            sessions = self.trading.get_calendar(
                GetCalendarRequest(start=date.today(), end=date.today() + timedelta(days=days))
            )
        except Exception as e:
            logger.warning("Calendar fetch failed: %s", e)
            return []

        out = []
        for s in sessions:
            try:
                out.append({
                    "open": ET.localize(s.open) if s.open.tzinfo is None else s.open.astimezone(ET),
                    "close": ET.localize(s.close) if s.close.tzinfo is None else s.close.astimezone(ET),
                })
            except Exception:
                continue
        return out

    def cancel_all_orders(self) -> int:
        try:
            responses = self.trading.cancel_orders()
            logger.info("Cancelled %d open order(s)", len(responses or []))
            return len(responses or [])
        except Exception as e:
            logger.error("Cancel-all failed: %s", e)
            return 0

    def close_all_positions(self) -> list[dict]:
        """Liquidate everything at market. Cancels resting orders first, since
        open sell stops reserve the shares and would block the exit."""
        try:
            responses = self.trading.close_all_positions(cancel_orders=True)
        except Exception as e:
            logger.error("Close-all failed: %s", e)
            return []

        out = []
        for r in responses or []:
            ok = getattr(r, "status", None) in (200, "200", None)
            out.append({"symbol": getattr(r, "symbol", "?"), "ok": bool(ok)})
            logger.info("Close order for %s: %s", getattr(r, "symbol", "?"), "sent" if ok else "FAILED")
        return out

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> list[dict]:
        """Account equity over time, straight from Alpaca.

        Unlike the trade journal this covers the account as a whole — including
        positions opened by hand — so it works even with an empty journal.
        """
        try:
            resp = self.trading.get_portfolio_history(
                GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
            )
        except Exception as e:
            logger.warning("Portfolio history failed (%s/%s): %s", period, timeframe, e)
            return []

        stamps = resp.timestamp or []
        equity = resp.equity or []
        pl = resp.profit_loss or []
        pl_pct = resp.profit_loss_pct or []

        points = []
        for i, ts in enumerate(stamps):
            value = float(equity[i] or 0)
            if value <= 0:
                continue          # padding before the account was funded
            points.append({
                "ms": int(ts) * 1000,
                "equity": round(value, 2),
                "pl": round(float(pl[i] or 0), 2) if i < len(pl) else 0.0,
                "pl_pct": round(float(pl_pct[i] or 0) * 100, 3) if i < len(pl_pct) else 0.0,
            })
        return points

    def get_available_cash(self) -> float:
        """Cash available for new buys without going negative."""
        cash = self.get_account()["cash"]
        reserve = self.cfg.cash_reserve_usd
        return max(0.0, cash - reserve)

    def calculate_affordable_qty(self, price: float, target_usd: float) -> tuple[float, float]:
        """
        Calculate qty and cost capped by available cash.
        Returns (qty, estimated_cost). Cost will never exceed available cash.
        """
        if price <= 0:
            return 0.0, 0.0

        available = self.get_available_cash()
        if available <= 0:
            return 0.0, 0.0

        usd_amount = min(target_usd, available)
        qty = round(usd_amount / price, 4)
        cost = round(qty * price, 2)

        if cost > available:
            qty = round(available / price, 4)
            cost = round(qty * price, 2)

        if cost > available or qty <= 0:
            return 0.0, 0.0

        return qty, cost

    def calculate_qty(self, symbol: str, price: float, usd_amount: float) -> float:
        qty, _ = self.calculate_affordable_qty(price, usd_amount)
        return qty

    def sync_positions(self) -> list[str]:
        """Return list of symbols with open broker positions."""
        return [p["symbol"] for p in self.get_open_positions()]
