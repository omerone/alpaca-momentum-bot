"""Local web dashboard for the momentum bot.

Run:  python server.py   ->  open http://localhost:5050

Start/stop the automation with a button, pick which symbols it focuses on,
watch account, positions and live log.
"""

import collections
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from flask import Flask, jsonify, request, send_file

from config import config
from broker import AlpacaBroker
from journal import TradeJournal
from main import MomentumBot
from strategy import TrailingStopManager

from timeutil import (
    DISPLAY_TZ, ET, IL, et_range_to_local, fmt_local,
    install_log_timezone, now_local, to_local,
)

install_log_timezone()          # every log line on the user clock

ROOT = Path(__file__).resolve().parent
DEFAULT_UNIVERSE = list(config.scan_universe)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

FOCUS_FILE = ROOT / "focus.json"


def _load_focus() -> list[str]:
    try:
        return json.loads(FOCUS_FILE.read_text())
    except Exception:
        return []


state: dict = {"bot": None, "thread": None, "focus": _load_focus()}
log_buffer: collections.deque = collections.deque(maxlen=300)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            log_buffer.append(self.format(record))
        except Exception:
            pass


_handler = BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_handler)

# standalone broker for account info while the bot is off
_account_broker = AlpacaBroker(config)
_journal = TradeJournal(config.trade_log_file, account=_account_broker.account_number)


def _stops_view() -> dict:
    """Stops as the dashboard should show them: live from the bot when it runs,
    otherwise the state persisted on disk."""
    if state["bot"]:
        return {s: state["bot"].stops.get_position(s) for s in state["bot"].stops.active_symbols()}
    mgr = TrailingStopManager(config, config.stop_state_file, account=_account_broker.account_number)
    mgr.load(quiet=True)
    return {s: mgr.get_position(s) for s in mgr.active_symbols()}


# symbol -> (qty, entry_time). Walking order history costs an API call, and the
# entry time cannot change while the position is open — so cache it.
_entry_time_cache: dict[str, tuple[float, datetime]] = {}


def _entry_time(broker, symbol: str, qty: float):
    cached = _entry_time_cache.get(symbol)
    if cached and abs(cached[0] - qty) < 1e-6:
        return cached[1]
    found = broker.get_position_entry_time(symbol, qty)
    if found:
        _entry_time_cache[symbol] = (qty, found)
    return found


def _when(dt) -> dict:
    """Hebrew 'when' labels. Israel time, always."""
    if dt is None:
        return {"label": "לא ידוע", "full": "", "ms": None}
    local = to_local(dt)
    days = (now_local().date() - local.date()).days
    if days <= 0:
        word = "היום"
    elif days == 1:
        word = "אתמול"
    elif days == 2:
        word = "לפני יומיים"
    elif days < 7:
        word = f"לפני {days} ימים"
    elif days < 14:
        word = "לפני שבוע"
    else:
        word = local.strftime("%d/%m")
    return {
        "label": f"{word} {local.strftime('%H:%M')}",
        "full": local.strftime("%d/%m/%Y %H:%M"),
        "ms": int(dt.timestamp() * 1000),
    }


# The trading calendar changes once a day at most — fetch it once, not per poll.
_calendar_cache: dict = {"day": None, "sessions": []}


def _sessions() -> list[dict]:
    today = now_local().date()
    if _calendar_cache["day"] != today or not _calendar_cache["sessions"]:
        sessions = _account_broker.get_calendar(14)
        if sessions:
            _calendar_cache.update(day=today, sessions=sessions)
    return _calendar_cache["sessions"]


def next_market_event() -> dict:
    """When does the market next open (or close, if it is open right now)?

    Uses Alpaca's calendar, so weekends, holidays and early closes are right.
    """
    now = datetime.now(pytz.UTC)
    for s in _sessions():
        if now < s["open"]:
            return {
                "type": "open",
                "ms": int(s["open"].timestamp() * 1000),
                "at": _when(s["open"])["label"],
                "session_end": fmt_local(s["close"], "%H:%M"),
            }
        if now < s["close"]:
            return {
                "type": "close",
                "ms": int(s["close"].timestamp() * 1000),
                "at": _when(s["close"])["label"],
                "session_end": fmt_local(s["close"], "%H:%M"),
            }
    return {"type": None, "ms": None, "at": "", "session_end": ""}


def bot_running() -> bool:
    return state["thread"] is not None and state["thread"].is_alive()


def market_open_now() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)


def market_session() -> str:
    """Prices keep moving outside 09:30-16:00 — the dashboard has to say so,
    otherwise 'market closed' next to a ticking P/L looks like a bug."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return "closed"
    hm = (now.hour, now.minute)
    if (9, 30) <= hm < (16, 0):
        return "open"
    if (4, 0) <= hm < (9, 30):
        return "pre"
    if (16, 0) <= hm < (20, 0):
        return "after"
    return "closed"


def apply_focus(symbols: list[str]):
    """Set the scan universe: chosen symbols + any open broker positions
    (so existing positions keep getting price updates for their stops)."""
    open_syms = [p["symbol"] for p in _account_broker.get_open_positions()]
    config.scan_universe = sorted(set(symbols) | set(open_syms))
    state["focus"] = symbols
    try:
        FOCUS_FILE.write_text(json.dumps(symbols))
    except Exception:
        pass


@app.get("/")
def index():
    return send_file(ROOT / "dashboard.html")


@app.get("/api/status")
def status():
    payload = {
        "running": bot_running(),
        "market_open": market_open_now(),
        "session": market_session(),
        "focus": state["focus"],
        "universe": DEFAULT_UNIVERSE,
        "time_et": datetime.now(ET).strftime("%H:%M:%S"),
        "time_il": datetime.now(IL).strftime("%H:%M:%S"),
        "entry_window": et_range_to_local(config.entry_start_et, config.entry_end_et),
        "market_hours_local": et_range_to_local("09:30", "16:00"),
        "next_event": next_market_event(),
        "now_ms": int(datetime.now(pytz.UTC).timestamp() * 1000),
        "logs": list(log_buffer)[-80:],
        "account": None,
        "positions": [],
    }
    try:
        broker = state["bot"].broker if state["bot"] else _account_broker
        payload["account"] = broker.get_account()
        positions = broker.get_open_positions()
        stops = _stops_view()
        live_stops = broker.get_open_stops() if config.broker_stop_enabled else {}

        unprotected = 0
        now_utc = datetime.now(pytz.UTC)
        for p in positions:
            stop_pos = stops.get(p["symbol"])
            guard = live_stops.get(p["symbol"])
            p["stop"] = round(stop_pos.stop_loss, 2) if stop_pos else None
            p["broker_stop"] = round(guard["stop_price"], 2) if guard else None
            p["broker_stop_qty"] = guard["qty"] if guard else 0
            p["protected"] = guard is not None
            if not guard:
                unprotected += 1

            opened = _entry_time(broker, p["symbol"], p["qty"])
            p["opened"] = _when(opened)
            p["held_seconds"] = (now_utc - opened.astimezone(pytz.UTC)).total_seconds() if opened else None

        for gone in set(_entry_time_cache) - {p["symbol"] for p in positions}:
            _entry_time_cache.pop(gone, None)

        payload["positions"] = positions
        payload["unprotected"] = unprotected
        payload["broker_stop_enabled"] = config.broker_stop_enabled
    except Exception as e:
        payload["error"] = str(e)
    return jsonify(payload)


PERIODS = {"today": 0, "week": 7, "month": 30, "all": None}


@app.get("/api/trades")
def trades():
    """Closed-trade journal + aggregate performance for a time window."""
    period = request.args.get("period", "all")
    days = PERIODS.get(period, None)

    since = None
    if days is not None:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = (start - timedelta(days=days)).isoformat()

    rows = _journal.recent(100, since=since)
    for t in rows:
        try:
            t["closed"] = _when(datetime.fromisoformat(t["exit_time"]))
        except Exception:
            t["closed"] = {"label": "—", "full": "", "ms": None}

    return jsonify({
        "ok": True,
        "period": period,
        "stats": _journal.stats(since=since),
        "trades": rows,
    })


# dashboard period -> (journal lookback days, Alpaca portfolio-history args)
PERF_PERIODS = {
    "today": (0, "1D", "5Min"),
    "week": (7, "1W", "1H"),
    "month": (30, "1M", "1D"),
    "all": (None, "1A", "1D"),
}


@app.get("/api/performance")
def performance():
    """Everything needed to answer 'when were we making money, and when not'."""
    period = request.args.get("period", "month")
    days, hist_period, hist_tf = PERF_PERIODS.get(period, PERF_PERIODS["month"])

    since = None
    if days is not None:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = (start - timedelta(days=days)).isoformat()

    broker = state["bot"].broker if state["bot"] else _account_broker
    equity = broker.get_portfolio_history(hist_period, hist_tf)

    payload = {
        "ok": True,
        "period": period,
        "equity": equity,
        "stats": _journal.stats(since=since),
        # entry hours in Israel time, matching every other timestamp on the page
        "breakdown": _journal.breakdown(since=since, tz=DISPLAY_TZ),
    }

    if equity:
        first, last = equity[0]["equity"], equity[-1]["equity"]
        payload["equity_summary"] = {
            "start": first,
            "now": last,
            "change": round(last - first, 2),
            "change_pct": round((last - first) / first * 100, 2) if first else 0.0,
            "peak": max(p["equity"] for p in equity),
            "trough": min(p["equity"] for p in equity),
            "max_drawdown_pct": _max_drawdown(equity),
            "from": _when(datetime.fromtimestamp(equity[0]["ms"] / 1000, pytz.UTC)),
        }
    return jsonify(payload)


def _max_drawdown(points: list[dict]) -> float:
    """Worst peak-to-trough fall in the equity curve, in percent."""
    peak, worst = None, 0.0
    for p in points:
        value = p["equity"]
        peak = value if peak is None else max(peak, value)
        if peak:
            worst = min(worst, (value - peak) / peak * 100)
    return round(worst, 2)


@app.post("/api/protect")
def protect():
    """Park protective stop orders at Alpaca for every open position.

    Runs whether or not the automation is on — that is the point: it is the
    cover for when the bot is NOT watching.
    """
    if not config.broker_stop_enabled:
        return jsonify({"ok": False, "error": "broker stops disabled in config"}), 400

    log = logging.getLogger("server")
    if state["bot"]:
        try:
            state["bot"].protect_all()
            return jsonify({"ok": True, "placed": len(state["bot"].stops.active_symbols())})
        except Exception as e:
            log.error("Protect failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 502

    # Bot is off: work straight off the persisted stop state.
    mgr = TrailingStopManager(config, config.stop_state_file, account=_account_broker.account_number)
    mgr.load(quiet=True)
    placed, skipped = 0, []
    try:
        broker_positions = {p["symbol"]: p for p in _account_broker.get_open_positions()}
        live = _account_broker.get_open_stops()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    for symbol, p in broker_positions.items():
        pos = mgr.get_position(symbol)
        if pos is None:
            # No saved stop (e.g. position opened before this feature existed)
            pos = mgr.open_position(symbol, p["entry_price"], p["qty"])
        if abs(pos.quantity - p["qty"]) > 1e-6:
            pos.quantity = p["qty"]

        existing = live.get(symbol)
        if existing and abs(existing["stop_price"] - round(pos.stop_loss, 2)) < 0.01:
            continue
        _account_broker.cancel_stops_for(symbol)
        order = _account_broker.submit_stop(symbol, pos.quantity, pos.stop_loss)
        if order:
            pos.broker_stop_id = order["id"]
            placed += 1
        else:
            skipped.append(symbol)

    mgr.save()
    log.info("Protective stops placed: %d (skipped: %s)", placed, ", ".join(skipped) or "none")
    return jsonify({"ok": True, "placed": placed, "skipped": skipped})


def _pick_timeframe(span: timedelta) -> tuple[TimeFrame, str]:
    """Keep the chart readable: coarser bars the longer the position is held."""
    hours = span.total_seconds() / 3600
    if hours <= 8:
        return TimeFrame(1, TimeFrameUnit.Minute), "1 דקה"
    if hours <= 40:
        return TimeFrame(5, TimeFrameUnit.Minute), "5 דקות"
    if hours <= 24 * 10:
        return TimeFrame(15, TimeFrameUnit.Minute), "15 דקות"
    return TimeFrame(1, TimeFrameUnit.Hour), "שעה"


@app.get("/api/history/<symbol>")
def history(symbol: str):
    """Bars from the moment the position was opened until now."""
    symbol = symbol.strip().upper()
    broker = state["bot"].broker if state["bot"] else _account_broker

    try:
        pos = next((p for p in broker.get_open_positions() if p["symbol"] == symbol), None)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if pos is None:
        return jsonify({"ok": False, "error": f"אין פוזיציה פתוחה ב-{symbol}"}), 404

    now = datetime.now(pytz.UTC)
    entry_time = broker.get_position_entry_time(symbol, pos["qty"])
    if entry_time is None:
        start = now - timedelta(hours=7)          # fallback: roughly this session
    else:
        entry_time = entry_time.astimezone(pytz.UTC)
        start = entry_time - timedelta(minutes=20)   # a little context before entry

    timeframe, tf_label = _pick_timeframe(now - start)

    bars = []
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=now,
            feed=DataFeed.IEX,      # free plan: IEX is the only real-time feed
        )
        resp = broker.data.get_stock_bars(req)
        for b in resp.data.get(symbol, []):
            ts = b.timestamp.astimezone(DISPLAY_TZ)
            bars.append({
                "ms": int(b.timestamp.timestamp() * 1000),
                "t": ts.strftime("%H:%M"),
                "date": ts.strftime("%d/%m"),
                "o": float(b.open), "h": float(b.high),
                "l": float(b.low), "c": float(b.close),
                "v": float(b.volume),
            })
    except Exception as e:
        logging.getLogger("server").warning("History failed for %s: %s", symbol, e)
        return jsonify({"ok": False, "error": f"שגיאת נתונים: {e}"}), 502

    stop_pos = _stops_view().get(symbol)
    guard = _account_broker.get_open_stops().get(symbol) if config.broker_stop_enabled else None
    entry_local = entry_time.astimezone(DISPLAY_TZ) if entry_time else None

    # how the stop moved over the life of the position
    stop_history = []
    if stop_pos:
        for iso, level in getattr(stop_pos, "stop_history", []) or []:
            try:
                stop_history.append([int(datetime.fromisoformat(iso).timestamp() * 1000), round(level, 2)])
            except Exception:
                continue

    return jsonify({
        "ok": True,
        "symbol": symbol,
        "qty": pos["qty"],
        "entry_price": pos["entry_price"],
        "current_price": pos["current_price"],
        "unrealized_pl": pos["unrealized_pl"],
        "unrealized_plpc": pos["unrealized_plpc"],
        "stop": round(stop_pos.stop_loss, 2) if stop_pos else None,
        "stop_history": stop_history,
        "broker_stop": round(guard["stop_price"], 2) if guard else None,
        "broker_stop_qty": guard["qty"] if guard else 0,
        "highest_seen": round(stop_pos.highest_price, 2) if stop_pos else None,
        "entry_ms": int(entry_time.timestamp() * 1000) if entry_time else None,
        "entry_label": entry_local.strftime("%d/%m %H:%M") if entry_local else "לא ידוע",
        "opened": _when(entry_time),
        "entry_exact": entry_time is not None,
        "interval": tf_label,
        "bars": bars,
    })


AUTOSTART_FILE = ROOT / "autostart.json"


def _set_autostart(on: bool):
    try:
        AUTOSTART_FILE.write_text(json.dumps({"on": on}))
    except Exception:
        pass


def _start_bot(symbols: list[str]) -> bool:
    if bot_running():
        return False
    apply_focus(symbols)
    bot = MomentumBot()
    thread = threading.Thread(target=bot.run, daemon=True, name="bot-loop")
    state["bot"] = bot
    state["thread"] = thread
    thread.start()
    logging.getLogger("server").info("Automation STARTED, focus: %s", ", ".join(symbols))
    return True


@app.post("/api/start")
def start():
    if bot_running():
        return jsonify({"ok": False, "error": "already running"}), 400

    symbols = [s.strip().upper() for s in (request.json or {}).get("symbols", []) if s.strip()]
    if not symbols:
        return jsonify({"ok": False, "error": "no symbols selected"}), 400

    _start_bot(symbols)
    _set_autostart(True)          # survive server restarts
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    if not bot_running():
        return jsonify({"ok": False, "error": "not running"}), 400
    state["bot"].stop()
    state["thread"].join(timeout=15)
    stopped = not state["thread"].is_alive()
    if stopped:
        state["bot"] = None
        state["thread"] = None
    _set_autostart(False)         # an explicit stop stays stopped
    logging.getLogger("server").info("Automation STOPPED (positions stay open)")
    return jsonify({"ok": stopped})


@app.post("/api/focus")
def focus():
    symbols = [s.strip().upper() for s in (request.json or {}).get("symbols", []) if s.strip()]
    if not symbols:
        return jsonify({"ok": False, "error": "no symbols selected"}), 400
    apply_focus(symbols)
    logging.getLogger("server").info("Focus updated: %s", ", ".join(symbols))
    return jsonify({"ok": True})


@app.post("/api/simulate")
def simulate_endpoint():
    """Run the long/short/both backtest simulator (examples/backtest_shorts.py)."""
    body = request.json or {}
    symbols = [s.strip().upper() for s in body.get("symbols", []) if s.strip()]
    start, end = body.get("start", ""), body.get("end", "")
    if not symbols or not start or not end:
        return jsonify({"ok": False, "error": "צריך מניות + טווח תאריכים"}), 400
    if len(symbols) > 8:
        return jsonify({"ok": False, "error": "עד 8 מניות בסימולציה אחת"}), 400

    import sys as _sys
    examples_dir = str(ROOT / "examples")
    if examples_dir not in _sys.path:
        _sys.path.insert(0, examples_dir)
    try:
        from backtest_shorts import simulate
        result = simulate(symbols, start, end)
    except SystemExit as e:          # fetch_range sys.exits on no data
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logging.getLogger("server").warning("Simulation failed: %s", e)
        return jsonify({"ok": False, "error": f"הסימולציה נכשלה: {e}"}), 500

    result["ok"] = True
    return jsonify(result)


def _maybe_autostart():
    """If the automation was ON before the server went down, bring it back —
    server restarts must not silently disarm the bot."""
    try:
        if json.loads(AUTOSTART_FILE.read_text()).get("on") and state["focus"]:
            _start_bot(state["focus"])
            logging.getLogger("server").info("Automation auto-resumed after server restart")
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.getLogger("server").warning("Autostart failed: %s", e)


if __name__ == "__main__":
    _maybe_autostart()
    print("\n  Dashboard:  http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=False)
