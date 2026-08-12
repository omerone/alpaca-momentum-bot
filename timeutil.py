"""Single source of truth for time.

Two clocks exist in this project and mixing them up is the classic bug:

  ET  — where the market lives. All *logic* runs on it: session hours, the
        entry window, the end-of-day flatten, "which trading day is this".
  IL  — where the user lives. Everything *shown* to a human uses it.

Nothing outside this module should call ``datetime.now()`` for display or
format a timestamp by hand.
"""

import logging
from datetime import datetime

import pytz

ET = pytz.timezone("America/New_York")      # market clock — logic only
IL = pytz.timezone("Asia/Jerusalem")        # user clock — display only
DISPLAY_TZ = IL


def now_local() -> datetime:
    return datetime.now(DISPLAY_TZ)


def now_et() -> datetime:
    return datetime.now(ET)


def to_local(dt: datetime | None) -> datetime | None:
    """Any datetime -> display timezone. Naive values are assumed machine-local."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(DISPLAY_TZ)


def fmt_local(dt: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    local = to_local(dt)
    return local.strftime(fmt) if local else "—"


def et_to_local(hhmm: str) -> str:
    """'09:35' on the market clock -> the same moment on the user clock.
    Uses today's date so DST shifts are handled automatically."""
    h, m = (int(x) for x in hhmm.split(":"))
    at_et = datetime.now(ET).replace(hour=h, minute=m, second=0, microsecond=0)
    return at_et.astimezone(DISPLAY_TZ).strftime("%H:%M")


def et_range_to_local(start_hhmm: str, end_hhmm: str) -> str:
    return f"{et_to_local(start_hhmm)}-{et_to_local(end_hhmm)}"


def install_log_timezone() -> None:
    """Force every log line onto the user clock, whatever the machine is set to."""
    def _converter(secs):
        return datetime.fromtimestamp(secs, DISPLAY_TZ).timetuple()

    # staticmethod matters: a plain function assigned to the class would bind as
    # a method and be handed `self`, which breaks every log record.
    logging.Formatter.converter = staticmethod(_converter)
