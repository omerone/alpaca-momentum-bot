"""Local web dashboard for the momentum bot.

Run:  python server.py   ->  open http://localhost:5050

Start/stop the automation with a button, pick which symbols it focuses on,
watch account, positions and live log.
"""

import collections
import json
import logging
import secrets
import socket
import threading
import time
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
    DISPLAY_TZ, ET, IL, et_range_to_local, et_to_local, fmt_local,
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


# ---- access control -----------------------------------------------------
#
# This dashboard can start the bot, stop it, place orders and liquidate.
# Localhost stays open (that is you, at the machine). Anything arriving over
# the network — your phone — must carry the token.

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _load_or_create_token() -> str:
    path = Path(config.dashboard_token_file)
    try:
        if path.exists():
            token = path.read_text().strip()
            if token:
                return token
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(16)
        path.write_text(token)
        path.chmod(0o600)
        return token
    except Exception as e:
        logging.getLogger("server").warning("Token file unusable (%s) — using a session token", e)
        return secrets.token_urlsafe(16)


ACCESS_TOKEN = _load_or_create_token()


def _is_local(remote: str | None) -> bool:
    return (remote or "") in LOCAL_HOSTS


@app.before_request
def _require_token():
    if _is_local(request.remote_addr):
        return None
    supplied = (
        request.args.get("token")
        or request.headers.get("X-Dashboard-Token")
        or request.cookies.get("dash_token")
    )
    if supplied and secrets.compare_digest(supplied, ACCESS_TOKEN):
        return None
    return (
        "<html lang='he' dir='rtl'><meta charset='utf-8'>"
        "<body style=\"background:#0d1117;color:#e6edf3;font-family:-apple-system,Arial;"
        "padding:40px;text-align:center\">"
        "<h2>נדרשת הרשאה</h2>"
        "<p style='color:#8b949e'>פתח את הדשבורד דרך הקישור המלא עם הטוקן.<br>"
        "הוא מודפס בטרמינל כשהשרת עולה.</p></body></html>",
        401,
    )


@app.after_request
def _remember_token(response):
    """First visit with ?token=... plants a cookie, so later taps need no URL."""
    supplied = request.args.get("token")
    if supplied and secrets.compare_digest(supplied, ACCESS_TOKEN):
        response.set_cookie(
            "dash_token", ACCESS_TOKEN,
            max_age=60 * 60 * 24 * 365, samesite="Lax", httponly=True,
        )
    return response


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


NO_EVENT = {"type": None, "ms": None, "at": "", "session_end": ""}


def next_market_event() -> dict:
    """When does the market next open (or close, if it is open right now)?

    Uses Alpaca's calendar, so weekends, holidays and early closes are right.
    Never raises: this feeds /api/status, and a broken countdown must not take
    the whole dashboard down with it.
    """
    try:
        now = datetime.now(pytz.UTC)
        for s in _sessions():
            if now < s["open"]:
                kind, when = "open", s["open"]
            elif now < s["close"]:
                kind, when = "close", s["close"]
            else:
                continue
            return {
                "type": kind,
                "ms": int(when.timestamp() * 1000),
                "at": _when(when)["label"],
                "session_end": fmt_local(s["close"], "%H:%M"),
            }
    except Exception as e:
        logging.getLogger("server").warning("Market countdown unavailable: %s", e)
    return dict(NO_EVENT)


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


def _in_entry_window() -> bool:
    if not config.entry_window_enabled:
        return True
    now = datetime.now(ET)
    sh, sm = (int(x) for x in config.entry_start_et.split(":"))
    eh, em = (int(x) for x in config.entry_end_et.split(":"))
    return (sh, sm) <= (now.hour, now.minute) < (eh, em)


def entry_gate(positions: list, account: dict | None) -> dict:
    """Why is the bot not buying right now?

    Every reason the entry path can bail out on is invisible in the log (they
    are all logger.debug), so the answer used to require reading the code. This
    returns the first blocking reason, in the same order scan_and_enter checks
    them. `state` drives the colour; `txt` is the whole explanation.
    """
    window = et_range_to_local(config.entry_start_et, config.entry_end_et)

    if not bot_running():
        # "off" and "fell over" look identical from the outside, and reading the
        # wrong one costs a whole session — say which it is.
        if crash_state["crashed_at"] and _autostart_on():
            return {"state": "blocked",
                    "txt": (f"⚠ הבוט קרס ב-{crash_state['crashed_at']} ({crash_state['reason']}) "
                            "ולא רץ כרגע. לחץ על הכפתור כדי להפעיל מחדש.")}
        return {"state": "off", "txt": "האוטומציה כבויה — לחץ על הכפתור כדי להפעיל"}

    session = market_session()
    if session != "open":
        label = {"pre": "פרה-מרקט — הבוט לא סוחר לפני הפתיחה",
                 "after": "אחרי סגירת המסחר",
                 "closed": "השוק סגור"}[session]
        return {"state": "wait", "txt": f"{label}. חלון הקנייה הוא {window}"}

    if not _in_entry_window():
        now_hm = datetime.now(IL).strftime("%H:%M")
        early = now_hm < et_to_local(config.entry_start_et)
        return {"state": "wait",
                "txt": (f"חלון הקנייה ({window}) עוד לא נפתח" if early
                        else f"חלון הקנייה ({window}) נסגר — אין כניסות חדשות היום. "
                             "הפוזיציות הקיימות ממשיכות להתנהל עד הסגירה")}

    if len(positions) >= config.max_positions:
        return {"state": "full",
                "txt": f"כל {config.max_positions} המקומות תפוסים — הבוט ייכנס רק אחרי שפוזיציה תיסגר"}

    if account is not None:
        available = account["cash"] - config.cash_reserve_usd
        if available < config.min_position_usd:
            return {"state": "full",
                    "txt": f"נשארו ${max(0, available):,.0f} פנויים — פחות מהמינימום "
                           f"לפוזיציה (${config.min_position_usd:,.0f})"}

    # SPY gate — read the bot's 60s cache only; computing it here would add an
    # API call to every dashboard poll.
    if config.spy_gate_enabled and state["bot"]:
        cached = getattr(state["bot"].scanner, "_spy_gate_cache", None)
        if cached and cached[1] is False:
            return {"state": "blocked",
                    "txt": "שער השוק סגור — SPY נסחר מתחת ל-VWAP שלו, כלומר השוק הכללי חלש. "
                           "מניה בודדת רק לעתים רחוקות מנצחת שוק יורד"}

    return {"state": "hunting",
            "txt": "סורק כל 3 שניות ומחפש מניה שעולה עם נפח חריג מעל ה-VWAP שלה"}


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
    payload["gate"] = entry_gate(payload["positions"], payload["account"])
    return jsonify(payload)


PERIODS = {"today": 0, "week": 7, "month": 30, "all": None}


def _lan_address() -> str:
    """This machine's address on the local network, as the phone sees it."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # nothing is sent; this just picks the route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


@app.get("/api/connect")
def connect_info():
    """Everything needed to open this dashboard on a phone.

    Guarded by the same token gate as the rest of the API, so a stranger on the
    Wi-Fi cannot ask the server to hand out its own access token.
    """
    ip = _lan_address()
    if not ip:
        return jsonify({"ok": False, "error": "לא נמצאה כתובת ברשת המקומית — בדוק חיבור Wi-Fi"}), 503

    port = config.dashboard_port
    url = f"http://{ip}:{port}/?token={ACCESS_TOKEN}"
    payload = {
        "ok": True,
        "url": url,
        "short_url": f"http://{ip}:{port}",
        "exposed": config.dashboard_host == "0.0.0.0",
        "qr": None,
    }
    try:
        import segno
        payload["qr"] = segno.make(url, error="m").svg_inline(
            scale=5, border=2, dark="#0d1117", light="#ffffff"
        )
    except ImportError:
        payload["qr_hint"] = "להצגת QR:  ./.venv/bin/pip install segno"
    return jsonify(payload)


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


# dashboard period -> journal lookback in days (None = the whole journal)
PERF_PERIODS = {"today": 0, "week": 7, "month": 30, "all": None}


def _realized_equity_curve(broker, curve: list[dict], since: str | None) -> list[dict]:
    """Account value counting CLOSED trades only.

    Alpaca's portfolio history marks open positions to market, so an untouched
    position drags the line up and down on its own and the chart stops being a
    statement about the strategy. Here every step is a real exit: the line moves
    when — and only when — a trade is closed.
    """
    if not curve:
        return []
    try:
        cash = broker.get_account()["cash"]
        cost_basis = sum(p["qty"] * p["entry_price"] for p in broker.get_open_positions())
    except Exception as e:
        logging.getLogger("server").warning("Realized equity base failed: %s", e)
        return []

    # Open positions are held at what we PAID, not at the last print, so no live
    # price reaches this number. (equity - unrealized_pl gives the same value but
    # jitters by a dollar or two: Alpaca samples the two at different instants.)
    realized_now = cash + cost_basis
    base = realized_now - curve[-1]["cum"]

    if since:
        start_ms = int(datetime.fromisoformat(since).astimezone().timestamp() * 1000)
    else:
        start_ms = curve[0]["ms"] - 3_600_000      # room to see the first step

    def point(ms: int, value: float) -> dict:
        return {
            "ms": ms,
            "equity": round(value, 2),
            "pl": round(value - base, 2),
            "pl_pct": round((value - base) / base * 100, 4) if base else 0.0,
        }

    points = [point(start_ms, base)]
    points += [point(c["ms"], base + c["cum"]) for c in curve]
    # flat tail to now: nothing has been realized since the last exit
    points.append(point(int(datetime.now(pytz.UTC).timestamp() * 1000), realized_now))
    return points


@app.get("/api/performance")
def performance():
    """Everything needed to answer 'when were we making money, and when not'."""
    period = request.args.get("period", "month")
    days = PERF_PERIODS.get(period, PERF_PERIODS["month"])

    since = None
    if days is not None:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = (start - timedelta(days=days)).isoformat()

    broker = state["bot"].broker if state["bot"] else _account_broker
    # entry hours in Israel time, matching every other timestamp on the page
    breakdown = _journal.breakdown(since=since, tz=DISPLAY_TZ)
    equity = _realized_equity_curve(broker, breakdown["curve"], since)

    payload = {
        "ok": True,
        "period": period,
        "equity": equity,
        "stats": _journal.stats(since=since),
        "breakdown": breakdown,
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


_research_cache: dict = {"ts": None, "payload": None}


def _build_research() -> dict:
    """Autopsies + parameter sweep + conclusions, in one payload."""
    import postmortem as pm

    journal = TradeJournal(config.trade_log_file, account=_account_broker.account_number)
    trades = journal.recent(limit=5000)
    # research shows only complete autopsies — a trade closed earlier today
    # waits for the nightly run instead of appearing as a half-baked "partial"
    reviews = [r for r in pm.load_reviews(config.trade_log_file) if r.get("data_ok")]

    sweep_rows = []
    if reviews:
        try:
            # sweep only what has been reviewed: an unreviewed trade has no bars
            # on disk yet, and would silently land in the "skipped" column
            done = {r["trade_id"] for r in reviews}
            days = sorted(r["trade_day"] for r in reviews if r.get("trade_day"))
            source = pm._BarSource(days[0], days[-1])
            sweep_rows = pm.sweep([t for t in trades if t["id"] in done], source)
        except Exception as e:
            logging.getLogger("server").warning("Sweep failed: %s", e)

    return {
        "ok": True,
        "reviews": reviews,
        "sweep": sweep_rows,
        "conclusions": pm.conclusions(reviews, sweep_rows),
        "pending": len([
            t for t in trades
            if t["id"] not in pm.reviewed_ids(config.trade_log_file)
            and (d := pm._to_et(t.get("exit_time"))) is not None
            and pm.session_complete(d.date())
        ]),
        "config": {k: getattr(config, k) for k in
                   ("atr_stop_mult", "atr_trail_mult", "atr_trail_activate")},
    }


@app.get("/api/research")
def research():
    """Cached: the sweep re-reads parquet files, which is wasted work on a poll."""
    age = (datetime.now() - _research_cache["ts"]).total_seconds() if _research_cache["ts"] else 1e9
    if age > 300 or _research_cache["payload"] is None:
        try:
            _research_cache.update(ts=datetime.now(), payload=_build_research())
        except Exception as e:
            logging.getLogger("server").warning("Research build failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(_research_cache["payload"])


@app.post("/api/research/run")
def research_run():
    """Analyse every trade that has not been reviewed yet."""
    import postmortem as pm

    force = bool((request.get_json(silent=True) or {}).get("force"))
    journal = TradeJournal(config.trade_log_file, account=_account_broker.account_number)
    try:
        result = pm.run(config.trade_log_file, journal.recent(limit=5000), force=force)
    except Exception as e:
        logging.getLogger("server").error("Research run failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    _research_cache.update(ts=None, payload=None)      # force a rebuild on next read
    return jsonify({"ok": True, **result})


@app.post("/api/close/<symbol>")
def close_position(symbol: str):
    """Sell one position now, at market, and journal it as a manual close.

    Routed through the bot when it is running so the trade is recorded with its
    real fill price and the trailing-stop state is cleaned up — selling behind
    the bot's back makes _reconcile() log the stop price as the exit instead.
    """
    symbol = symbol.upper().strip()
    log = logging.getLogger("server")
    try:
        positions = {p["symbol"]: p for p in _account_broker.get_open_positions()}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    pos = positions.get(symbol)
    if not pos:
        return jsonify({"ok": False, "error": f"אין פוזיציה פתוחה ב-{symbol}"}), 404

    try:
        if state["bot"]:
            ok = state["bot"]._close_position(symbol, pos["qty"], pos["current_price"], "סגירה ידנית")
            if not ok:
                return jsonify({"ok": False, "error": "הברוקר דחה את המכירה"}), 502
        else:
            # Bot off: sell directly — but the trade must still reach the journal
            # and the persisted stop state, or the position vanishes from every
            # statistic exactly like the $193.60 that went missing on 17/08.
            _account_broker.cancel_stops_for(symbol)
            order = _account_broker.sell(symbol, pos["qty"])
            if not order:
                return jsonify({"ok": False, "error": "הברוקר דחה את המכירה"}), 502
            exit_price = _account_broker.get_fill_price(order["id"]) or pos["current_price"]
            mgr = TrailingStopManager(config, config.stop_state_file,
                                      account=_account_broker.account_number)
            mgr.load(quiet=True)
            saved = mgr.get_position(symbol)
            _journal.record(
                symbol, pos["qty"],
                saved.entry_price if saved else pos["entry_price"], exit_price,
                "סגירה ידנית (הבוט כבוי)",
                saved.entry_time if saved else None,
                stop_price=saved.stop_loss if saved else None,
                initial_stop=(saved.stop_history[0][1]
                              if saved and saved.stop_history else None),
            )
            if saved:
                mgr.close_position(symbol)
                mgr.save()
    except Exception as e:
        log.error("Manual close failed for %s: %s", symbol, e)
        return jsonify({"ok": False, "error": str(e)}), 502

    log.info("Manual close: %s x %.4f", symbol, pos["qty"])
    return jsonify({"ok": True, "symbol": symbol, "qty": pos["qty"]})


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

# Set when the bot thread dies without anyone asking it to. The dashboard needs
# to tell "you switched it off" apart from "it fell over" — the first time this
# happened the user read "האוטומציה כבויה" and assumed they had forgotten to
# press the button, while the real cause was a dropped connection at 01:56.
crash_state: dict = {"crashed_at": None, "reason": "", "revives": 0}

# No heartbeat for this long means the loop is wedged, not merely busy. The loop
# stamps one every iteration (~3s); the scan and the reports run well inside a
# minute, so anything past this is a hang.
STALL_SECONDS = 180


def _watchdog():
    """Restart the bot if its thread dies while autostart is still on.

    The loop in main.py now survives transient faults on its own; this is the
    second line of defence for anything that kills the thread outright.
    """
    log = logging.getLogger("server")
    fails = 0
    while True:
        time.sleep(20)
        try:
            if not _autostart_on():
                fails = 0
                continue
            # A thread blocked on a dead socket is ALIVE, so is_alive() alone
            # declares everything fine while the bot does nothing. On 25/08/2026
            # the loop sat frozen for 12 minutes with five live positions and
            # the watchdog never noticed. The loop stamps a heartbeat every
            # iteration (~3s); if it goes quiet, treat it as dead.
            if bot_running():
                bot = state["bot"]
                beat = getattr(bot, "_heartbeat", None) if bot else None
                if beat is None or time.time() - beat < STALL_SECONDS:
                    fails = 0
                    continue
                stalled_for = int(time.time() - beat)
                crash_state.update(
                    crashed_at=now_local().strftime("%d/%m/%Y %H:%M:%S"),
                    reason=f"הלולאה נתקעה — ללא סימן חיים {stalled_for} שניות",
                )
                log.error("שומר: הלולאה תקועה %d שניות — מחליף את הבוט", stalled_for)
                try:
                    bot.stop()      # the old thread exits once its socket unblocks
                except Exception:
                    pass
            # Guard on "a bot has run in this process", NOT on state["thread"]:
            # a failed revive nulls that field, and the old guard then treated
            # the bot as "never started" and disarmed the watchdog for good.
            # That is exactly what happened on 20/08/2026 — DNS was down when
            # the revive fired, the single attempt failed, and the bot stayed
            # dead for 27 hours while the network came back.
            if not state.get("ever_started"):
                continue
            if not crash_state["crashed_at"]:
                crash_state.update(
                    crashed_at=now_local().strftime("%d/%m/%Y %H:%M:%S"),
                    reason="תהליכון הבוט נעצר מעצמו",
                )
                log.error("שומר: תהליכון הבוט מת — מנסה להרים מחדש")
            symbols = state["focus"] or DEFAULT_UNIVERSE
            state["bot"] = state["thread"] = None
            if _start_bot(symbols):
                crash_state["revives"] += 1
                fails = 0
                log.error("שומר: הבוט הופעל מחדש אוטומטית (פעם %d)",
                          crash_state["revives"])
        except Exception as e:                   # a watchdog must never die
            fails += 1
            # An outage can last hours. Keep trying forever, but back off so the
            # log does not fill with the same DNS error every 20 seconds.
            if fails <= 3 or fails % 15 == 0:
                log.warning("שומר: ניסיון החייאה %d נכשל, ממשיך לנסות — %s", fails, e)
            time.sleep(min(300, 20 * fails))


def _autostart_on() -> bool:
    try:
        return bool(json.loads(AUTOSTART_FILE.read_text()).get("on"))
    except Exception:
        return False


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
    state["ever_started"] = True      # the watchdog's arming flag; survives a
                                      # failed revive, unlike state["thread"]
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


_intel_cache: dict = {"ts": None, "data": {}, "quality": None}


@app.get("/api/intel")
def intel():
    """Opening intelligence for the focus list — display only, never a trade
    filter (backtest showed filters hurt on a fixed megacap universe)."""
    import opening_intel as oi

    now_et = datetime.now(ET)
    if now_et.weekday() >= 5 or (now_et.hour, now_et.minute) < (9, 36):
        return jsonify({"ready": False, "reason": "לפני פתיחה — המודיעין מוכן מ-16:36 שעון ישראל"})

    feed = "sip" if (now_et.hour, now_et.minute) >= (9, 52) else "iex"
    cache_age = (datetime.now() - _intel_cache["ts"]).total_seconds() if _intel_cache["ts"] else 1e9
    if cache_age > 300 or _intel_cache["quality"] != feed:
        symbols = state["focus"] or DEFAULT_UNIVERSE
        try:
            baseline = oi.compute_baseline(_account_broker.data, symbols, feed)
            data = oi.compute_today(_account_broker.data, symbols, baseline, feed)
            _intel_cache.update(ts=datetime.now(), data=data, quality=feed)
        except Exception as e:
            logging.getLogger("server").warning("Intel failed: %s", e)
            return jsonify({"ready": False, "reason": str(e)})

    rows = []
    for sym, d in _intel_cache["data"].items():
        hot = (d.get("rvol") or 0) >= 1.5 or abs(d.get("gap_pct") or 0) >= 2
        rows.append({**d, "symbol": sym, "hot": hot})
    rows.sort(key=lambda r: (r.get("rvol") or 0), reverse=True)
    return jsonify({"ready": True, "quality": _intel_cache["quality"], "rows": rows})


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

    # Reject unparseable dates here rather than letting pandas raise deep in the
    # backtest, which surfaced as a 500 with an English stack-trace message.
    for label, value in (("התחלה", start), ("סיום", end)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"תאריך {label} לא תקין: {value}"}), 400
    if start > end:
        return jsonify({"ok": False, "error": "תאריך ההתחלה מאוחר מתאריך הסיום"}), 400

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


def _lan_ip() -> str:
    """This machine's address on the local network, as the phone sees it."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no packet is sent; just picks the route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


if __name__ == "__main__":
    _maybe_autostart()
    threading.Thread(target=_watchdog, daemon=True, name="bot-watchdog").start()
    port = config.dashboard_port
    print(f"\n  מהמחשב:  http://localhost:{port}")

    if config.dashboard_host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"\n  מהטלפון (אותה רשת Wi-Fi) — פתח את הקישור המלא פעם אחת:")
            print(f"  http://{ip}:{port}/?token={ACCESS_TOKEN}")
            print("\n  אחרי הפעם הראשונה הטוקן נשמר בדפדפן ואפשר להיכנס ל-"
                  f"http://{ip}:{port}")
        else:
            print("\n  לא הצלחתי לזהות כתובת ברשת המקומית")
    print()

    app.run(host=config.dashboard_host, port=port, debug=False)
