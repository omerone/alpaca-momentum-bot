"""Trailing stop loss position management."""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)

MAX_STOP_HISTORY = 500


def _stamp() -> str:
    """Timezone-aware local timestamp — a naive one cannot be lined up with bars."""
    return datetime.now().astimezone().isoformat()


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    stop_loss: float
    highest_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    # ATR mode: absolute $ distances; None -> percent-based config values
    trail_distance: float | None = None
    trail_activate_profit: float = 0.0
    broker_stop_id: str | None = None       # protective stop order parked at Alpaca
    # [[iso_time, level], ...] — every level the stop has held, for the chart
    stop_history: list = field(default_factory=list)

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((self.highest_price - self.entry_price) / self.entry_price) * 100

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        try:
            d["entry_time"] = datetime.fromisoformat(d["entry_time"])
        except Exception:
            d["entry_time"] = datetime.now()
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


class TrailingStopManager:
    """Manages positions with initial stop loss and trailing stop.

    State is mirrored to disk so a restart does not reset a stop that has
    already trailed up — reloading from the broker alone would silently drop
    it back to entry - initial_stop.
    """

    def __init__(self, cfg: Config, state_file: str | None = None, account: str = ""):
        self.cfg = cfg
        self.positions: dict[str, Position] = {}
        # No state_file -> in-memory only. Backtests must never touch live state.
        self.state_path = Path(state_file) if state_file else None
        self.account = account or ""

    # ---- persistence ----------------------------------------------------

    def save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now().isoformat(),
                "account": self.account,
                "positions": [p.to_dict() for p in self.positions.values()],
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.state_path)      # atomic: never leave a half-written file
        except Exception as e:
            logger.warning("Could not save stop state: %s", e)

    def load(self, quiet: bool = False) -> int:
        if self.state_path is None or not self.state_path.exists():
            return 0
        try:
            payload = json.loads(self.state_path.read_text())
        except Exception as e:
            logger.warning("Could not read stop state: %s", e)
            return 0

        # Stop levels belong to one account. Loading another account's levels
        # would park protective orders at prices that mean nothing here.
        saved_account = payload.get("account") or ""
        if saved_account and self.account and saved_account != self.account:
            logger.warning(
                "Stop state belongs to account %s but we are on %s — discarding it",
                saved_account, self.account,
            )
            self.state_path.unlink(missing_ok=True)
            return 0

        for raw in payload.get("positions", []):
            try:
                pos = Position.from_dict(raw)
                self.positions[pos.symbol] = pos
            except Exception as e:
                logger.warning("Skipping bad saved position %s: %s", raw.get("symbol"), e)

        if self.positions and not quiet:
            logger.info(
                "Restored %d stop(s) from %s: %s",
                len(self.positions), self.state_path,
                ", ".join(f"{s} @ ${p.stop_loss:.2f}" for s, p in self.positions.items()),
            )
        return len(self.positions)

    def open_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: float,
        stop_distance: float | None = None,
        trail_distance: float | None = None,
    ) -> Position:
        if stop_distance is not None and stop_distance > 0:
            stop_loss = entry_price - stop_distance
            trail_activate = self.cfg.atr_trail_activate * stop_distance
        else:
            stop_loss = entry_price * (1 - self.cfg.initial_stop_loss_pct / 100)
            trail_activate = entry_price * self.cfg.min_profit_to_trail_pct / 100
            trail_distance = None

        pos = Position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            highest_price=entry_price,
            trail_distance=trail_distance,
            trail_activate_profit=trail_activate,
        )
        pos.stop_history = [[_stamp(), round(stop_loss, 4)]]
        self.positions[symbol] = pos
        self.save()
        logger.info(
            "Opened %s: entry=$%.2f qty=%.4f stop=$%.2f (-%.2f%%%s)",
            symbol, entry_price, quantity, stop_loss,
            (entry_price - stop_loss) / entry_price * 100,
            ", ATR-scaled" if stop_distance else "",
        )
        return pos

    def update_price(self, symbol: str, current_price: float) -> str | None:
        """
        Update trailing stop based on current price.
        Returns 'stop_hit' when the stop triggered, 'stop_raised' when the
        trail moved up (the broker-side stop then needs re-placing), else None.
        """
        pos = self.positions.get(symbol)
        if not pos:
            return None

        raised = False
        if current_price > pos.highest_price:
            pos.highest_price = current_price

            profit = current_price - pos.entry_price

            if profit >= pos.trail_activate_profit:
                trail_amount = (
                    pos.trail_distance
                    if pos.trail_distance is not None
                    else current_price * self.cfg.trailing_stop_pct / 100
                )
                new_stop = current_price - trail_amount
                if new_stop > pos.stop_loss:
                    old_stop = pos.stop_loss
                    pos.stop_loss = new_stop
                    raised = True
                    pos.stop_history.append([_stamp(), round(new_stop, 4)])
                    del pos.stop_history[:-MAX_STOP_HISTORY]
                    logger.info(
                        "Trailing stop raised for %s: $%.2f -> $%.2f (price=$%.2f, high=$%.2f)",
                        symbol, old_stop, new_stop, current_price, pos.highest_price,
                    )
            self.save()

        if current_price <= pos.stop_loss:
            logger.info(
                "Stop loss hit for %s: price=$%.2f stop=$%.2f entry=$%.2f",
                symbol, current_price, pos.stop_loss, pos.entry_price,
            )
            return "stop_hit"

        return "stop_raised" if raised else None

    def close_position(self, symbol: str) -> Position | None:
        pos = self.positions.pop(symbol, None)
        if pos:
            self.save()
        return pos

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def active_symbols(self) -> list[str]:
        return list(self.positions.keys())

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)
