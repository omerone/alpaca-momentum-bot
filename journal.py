"""Closed-trade journal.

The live log is a ring buffer — it forgets. This keeps every closed trade in
SQLite so the question "is the bot actually making money?" has an answer.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    qty          REAL    NOT NULL,
    entry_price  REAL    NOT NULL,
    exit_price   REAL    NOT NULL,
    entry_time   TEXT,
    exit_time    TEXT    NOT NULL,
    held_seconds REAL,
    pnl_usd      REAL    NOT NULL,
    pnl_pct      REAL    NOT NULL,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_time);
"""

# columns added after the first version shipped: (name, definition)
MIGRATIONS = [
    ("stop_price", "REAL"),      # where the stop stood when the trade closed
    ("initial_stop", "REAL"),    # where it started, before any trailing
    ("account", "TEXT"),         # which Alpaca account the trade belongs to
]


def _parse(value, tz=None):
    """Stored timestamps are local and naive; attach a zone before comparing."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(tz) if tz else dt


class TradeJournal:
    def __init__(self, db_file: str, account: str = ""):
        # Trades are scoped to an account, so swapping API keys shows a clean
        # slate instead of someone else's history mixed into the stats.
        self.account = account or ""
        self.path = Path(db_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            existing = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
            for name, decl in MIGRATIONS:
                if name not in existing:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        exit_price: float,
        reason: str,
        entry_time: datetime | None = None,
        stop_price: float | None = None,
        initial_stop: float | None = None,
    ) -> None:
        exit_time = datetime.now()
        held = (exit_time - entry_time).total_seconds() if entry_time else None
        pnl_usd = (exit_price - entry_price) * qty
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0.0
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO trades (symbol, qty, entry_price, exit_price, entry_time,"
                    " exit_time, held_seconds, pnl_usd, pnl_pct, reason, stop_price,"
                    " initial_stop, account)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        symbol, qty, entry_price, exit_price,
                        entry_time.isoformat() if entry_time else None,
                        exit_time.isoformat(), held, pnl_usd, pnl_pct, reason,
                        stop_price, initial_stop, self.account,
                    ),
                )
        except Exception as e:
            logger.warning("Could not journal %s trade: %s", symbol, e)

    def _filter(self, since: str | None) -> tuple[str, list]:
        """WHERE clause shared by every read: this account, optional date floor.
        Rows with no account (written before the column existed) always match."""
        clauses, args = [], []
        if since:
            clauses.append("exit_time >= ?")
            args.append(since)
        if self.account:
            clauses.append("(account IS NULL OR account = '' OR account = ?)")
            args.append(self.account)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def recent(self, limit: int = 100, since: str | None = None) -> list[dict]:
        where, args = self._filter(since)
        try:
            with self._conn() as c:
                rows = c.execute(
                    f"SELECT * FROM trades{where} ORDER BY id DESC LIMIT ?", (*args, limit)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("Could not read trades: %s", e)
            return []

    def breakdown(self, since: str | None = None, tz=None) -> dict:
        """Slice realized P/L by day, symbol, entry hour and exit reason.

        Done in Python rather than SQL: timestamps are stored as local naive
        strings, and the entry hour is only meaningful in market time.
        """
        rows = self.merge_fragments(self.recent(limit=100_000, since=since))
        rows.sort(key=lambda r: r.get("exit_time") or "")   # oldest first, for the cumulative curve

        by_day: dict[str, dict] = {}
        by_symbol: dict[str, dict] = {}
        by_hour: dict[int, dict] = {}
        by_reason: dict[str, dict] = {}
        curve: list[dict] = []
        running = 0.0

        def bucket(store, key):
            return store.setdefault(key, {"key": key, "trades": 0, "wins": 0, "pnl": 0.0})

        def add(store, key, pnl):
            b = bucket(store, key)
            b["trades"] += 1
            b["pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1

        for r in rows:
            pnl = r["pnl_usd"]
            exit_dt = _parse(r["exit_time"], tz)
            entry_dt = _parse(r["entry_time"], tz)

            if exit_dt:
                add(by_day, exit_dt.strftime("%Y-%m-%d"), pnl)
                running += pnl
                curve.append({
                    "ms": int(exit_dt.timestamp() * 1000),
                    "cum": round(running, 2),
                    "pnl": round(pnl, 2),
                    "symbol": r["symbol"],
                })
            add(by_symbol, r["symbol"], pnl)
            add(by_reason, r["reason"] or "—", pnl)
            if entry_dt:
                add(by_hour, entry_dt.hour, pnl)

        def finish(store, sort_key):
            out = list(store.values())
            for b in out:
                b["pnl"] = round(b["pnl"], 2)
                b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
            out.sort(key=sort_key)
            return out

        return {
            "by_day": finish(by_day, lambda b: b["key"]),
            "by_symbol": finish(by_symbol, lambda b: -b["pnl"]),
            "by_hour": finish(by_hour, lambda b: b["key"]),
            "by_reason": finish(by_reason, lambda b: -b["pnl"]),
            "curve": curve,
        }

    @staticmethod
    def merge_fragments(rows: list[dict]) -> list[dict]:
        """Collapse the fragments of one position back into a single trade.

        A broker stop sells only the whole-share part, so the crumb is closed
        and journalled separately: 151 shares and 0.98 shares become two rows
        for what was one decision. Counting them separately distorts every
        ratio — a -$0.02 crumb was being scored as a full losing trade, pushing
        the win rate from 40% down to 31%.

        Same symbol + same entry price + same entry minute = one position.
        """
        merged: dict[tuple, dict] = {}
        for r in rows:
            key = (r["symbol"], round(r["entry_price"], 4), (r["entry_time"] or "")[:16])
            first = merged.get(key)
            if first is None:
                merged[key] = dict(r)
                continue
            qty = first["qty"] + r["qty"]
            # size-weighted exit price, so pnl_pct stays honest
            first["exit_price"] = ((first["exit_price"] * first["qty"]
                                    + r["exit_price"] * r["qty"]) / qty) if qty else r["exit_price"]
            first["qty"] = qty
            first["pnl_usd"] = first["pnl_usd"] + r["pnl_usd"]
            entry = first["entry_price"]
            first["pnl_pct"] = ((first["exit_price"] - entry) / entry * 100) if entry else 0.0
            # the exit that moved the real size is the one worth reporting
            if r["qty"] > first["qty"] - r["qty"]:
                first["reason"] = r["reason"]
                first["exit_time"] = r["exit_time"]
                first["held_seconds"] = r["held_seconds"]
        return list(merged.values())

    def stats(self, since: str | None = None) -> dict:
        """Aggregate performance. `since` is an ISO date, e.g. '2026-08-01'."""
        where, args = self._filter(since)
        try:
            with self._conn() as c:
                rows = c.execute(
                    f"SELECT symbol, entry_price, exit_price, qty, entry_time, exit_time,"
                    f" reason, pnl_usd, pnl_pct, held_seconds FROM trades{where}", args
                ).fetchall()
        except Exception as e:
            logger.warning("Could not aggregate trades: %s", e)
            rows = []

        rows = self.merge_fragments([dict(r) for r in rows])
        pnls = [r["pnl_usd"] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        held = [r["held_seconds"] for r in rows if r["held_seconds"]]

        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            # >1 means winners out-earn losers; the single most useful number here.
            # None = undefined (no losses yet) — inf would break JSON parsing.
            "profit_factor": (gross_win / gross_loss) if gross_loss else None,
            "expectancy": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "best": max(pnls) if pnls else 0.0,
            "worst": min(pnls) if pnls else 0.0,
            "avg_hold_min": (sum(held) / len(held) / 60) if held else 0.0,
        }
