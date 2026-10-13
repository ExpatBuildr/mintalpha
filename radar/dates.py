"""Launch-schedule parsing from free text.

Rules (deliberately conservative — reminders depend on this):
  * exact   = a calendar date (or today/tomorrow/weekday) + a clock time + a stated time zone,
              or a relative time ("in 2 hours") anchored to the post timestamp.
  * day     = a date without a time, or a date + time with NO stated zone. We never guess a zone.
  * unknown = nothing usable ("TBA", "soon", or no date at all).
Only 'exact' schedules get timed reminders.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Schedule

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
            "sunday": 6, "sun": 6}

# Abbreviation -> IANA zone. Wall-clock times are interpreted in the zone, so DST is handled
# by zoneinfo. "EST" in summer is treated as US Eastern local time (what posters almost always mean)
# and the assumption is recorded on the schedule.
TZ_ALIASES = {
    "UTC": "UTC", "GMT": "UTC", "Z": "UTC",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "BST": "Europe/London", "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "SGT": "Asia/Singapore", "PHT": "Asia/Manila", "PHST": "Asia/Manila", "HKT": "Asia/Hong_Kong",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}
_FIXED_EXPECTED = {"EST": False, "CST": False, "MST": False, "PST": False,   # standard time (no DST)
                   "EDT": True, "CDT": True, "MDT": True, "PDT": True, "CEST": True, "AEDT": True}
AMBIGUOUS = {"CST": "CST read as US Central (could be China Standard Time)",
             "IST": "IST read as India Standard Time (could be Irish/Israel time)",
             "BST": "BST read as British Summer Time"}

_MONTH_RE = r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_TZ_RE = r"(?P<tz>UTC\s*[+-]\s*\d{1,2}(?::?\d{2})?|GMT\s*[+-]\s*\d{1,2}(?::?\d{2})?|" + "|".join(
    sorted((k for k in TZ_ALIASES if k != "Z"), key=len, reverse=True)) + r")\b"

DATE_PATTERNS = [
    re.compile(_MONTH_RE + r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>20\d{2}))?", re.I),
    re.compile(r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\b(?:,?\s+(?P<year>20\d{2}))?", re.I),
    re.compile(r"\b(?P<year>20\d{2})-(?P<mnum>\d{1,2})-(?P<day>\d{1,2})\b"),
    re.compile(r"(?<![\d/])(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/(?P<year>(?:20)?\d{2}))?(?![\d/])"),
]
RELATIVE_DAY = re.compile(r"\b(?P<rel>today|tonight|tomorrow|tmrw|tmr)\b", re.I)
WEEKDAY_RE = re.compile(r"\b(?:this\s+|next\s+|on\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|fri|sat|sun)\b", re.I)
IN_RELATIVE = re.compile(r"\bin\s+(?P<n>\d{1,3})\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
TIME_PATTERNS = [
    re.compile(r"(?<![\d:/])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)(?![a-z])\s*\(?\s*(?:" + _TZ_RE + r")?", re.I),
    re.compile(r"(?<![\d:/])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?![\d:])\s*\(?\s*(?:" + _TZ_RE + r")?", re.I),
    re.compile(r"\b(?P<word>noon|midnight)\b\s*\(?\s*(?:" + _TZ_RE + r")?", re.I),
]
STANDALONE_TZ = re.compile(r"(?<![A-Za-z])" + _TZ_RE)  # case-sensitive: avoid matching words like "et"
LIVE_RE = re.compile(r"\b(mint(?:ing)?\s+(?:is\s+)?(?:now\s+)?live|live\s+now|now\s+live|minting\s+now|"
                     r"mint\s+now\s+open|mint\s+(?:is\s+)?open\b|we(?:'re| are)\s+live|is\s+live)\b", re.I)
ENDED_RE = re.compile(r"\b(minted\s+out|sold\s+out|mint\s+(?:has\s+|is\s+)?(?:ended|over|closed)|"
                      r"mint\s+(?:is\s+)?now\s+closed|fully\s+minted)\b", re.I)
TBA_RE = re.compile(r"\b(tba|tbd|tbc|date\s+soon|coming\s+soon|soon™?|stay\s+tuned)\b", re.I)


def resolve_tz(token: str | None):
    if not token:
        return None, None
    tok = re.sub(r"\s+", "", token.upper())
    m = re.match(r"^(UTC|GMT)([+-])(\d{1,2})(?::?(\d{2}))?$", tok)
    if m:
        sign = 1 if m.group(2) == "+" else -1
        delta = timedelta(hours=int(m.group(3)), minutes=int(m.group(4) or 0))
        return timezone(sign * delta), tok
    name = TZ_ALIASES.get(tok)
    if not name:
        return None, None
    return ZoneInfo(name), tok


def _infer_year(month: int, day: int, ref: datetime, explicit_year: int | None) -> date | None:
    try:
        if explicit_year:
            return date(explicit_year, month, day)
        candidate = date(ref.year, month, day)
        if candidate < (ref - timedelta(days=2)).date():
            candidate = date(ref.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def _find_date(text: str, ref: datetime, assumptions: list[str]) -> tuple[date | None, str | None, str | None]:
    """Returns (date, raw_match, relative_keyword). Relative dates are resolved later in the stated zone."""
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        gd = m.groupdict()
        year = gd.get("year")
        year_i = int(year) + (2000 if year and len(year) == 2 else 0) if year else None
        if i in (0, 1):
            d = _infer_year(MONTHS[gd["month"].lower()], int(gd["day"]), ref, year_i)
        elif i == 2:
            d = _infer_year(int(gd["mnum"]), int(gd["day"]), ref, year_i)
        else:
            a, b = int(gd["a"]), int(gd["b"])
            if a == b == 1:  # "1/1" = one-of-one artwork, not a date
                continue
            if a > 12 and b <= 12:
                month, day = b, a
            else:
                month, day = a, b
                if b <= 12 and a != b:
                    assumptions.append(f"'{m.group(0)}' read as month/day (US style)")
            d = _infer_year(month, day, ref, year_i)
        if d:
            return d, m.group(0), None
    m = RELATIVE_DAY.search(text)
    if m:
        return None, m.group(0), m.group("rel").lower()
    m = WEEKDAY_RE.search(text)
    if m and _weekday_in_schedule_context(text, m):
        return None, m.group(0), "wd:" + m.group("wd").lower()
    return None, None, None


def _weekday_in_schedule_context(text: str, m: re.Match) -> bool:
    # "Fri" etc. are common words; only accept weekdays near mint/launch/drop wording or a time.
    window = text[max(0, m.start() - 40): m.end() + 40].lower()
    return bool(re.search(r"mint|launch|drop|live|open|claim|\d\s*(?:am|pm)|\d:\d\d", window))


def _find_time(text: str):
    for pat in TIME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        gd = m.groupdict()
        if gd.get("word"):
            t = time(12, 0) if gd["word"].lower() == "noon" else time(0, 0)
        else:
            h, mi = int(gd["h"]), int(gd.get("m") or 0)
            ampm = (gd.get("ampm") or "").lower().replace(".", "")
            if ampm:
                if not 1 <= h <= 12:
                    continue
                h = (h % 12) + (12 if ampm == "pm" else 0)
            if h > 23 or mi > 59:
                continue
            t = time(h, mi)
        return t, gd.get("tz"), m.group(0)
    return None, None, None


def parse_schedule(text: str, ref: datetime) -> Schedule:
    """ref = the post's creation time (aware, UTC)."""
    sched = Schedule()
    sched.live_signal = bool(LIVE_RE.search(text))
    sched.ended_signal = bool(ENDED_RE.search(text))
    sched.tba = bool(TBA_RE.search(text))

    rel = IN_RELATIVE.search(text)
    if rel:
        n = int(rel.group("n"))
        unit = rel.group("unit").lower()
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        sched.launch_at = ref + delta
        sched.launch_date = sched.launch_at.date().isoformat()
        sched.precision = "exact"
        sched.tz_name = "relative"
        sched.raw = rel.group(0)
        sched.assumptions.append(f"'{rel.group(0)}' measured from the post time")
        return sched

    assumptions: list[str] = []
    d, raw_date, relative = _find_date(text, ref, assumptions)
    t, tz_token, raw_time = _find_time(text)
    if t is not None and not tz_token:
        m = STANDALONE_TZ.search(text)
        tz_token = m.group("tz") if m else None
    tzinfo, tz_label = resolve_tz(tz_token)

    if d is None and relative is None:
        if t is not None and tzinfo is not None and (sched.live_signal is False):
            # A bare time with zone and no date ("3pm UTC") — today if still ahead, else unknown.
            local_ref = ref.astimezone(tzinfo)
            cand = datetime.combine(local_ref.date(), t, tzinfo)
            if cand >= ref:
                sched.launch_at = cand.astimezone(timezone.utc)
                sched.launch_date = sched.launch_at.date().isoformat()
                sched.precision = "exact"
                sched.tz_name = tz_label
                sched.raw = raw_time
                sched.assumptions.append("time with no date read as today (post date)")
        return sched

    # Resolve relative day words in the stated zone (or UTC for day precision only).
    zone_for_relative = tzinfo or timezone.utc
    if relative:
        local_ref = ref.astimezone(zone_for_relative)
        if relative in ("today", "tonight"):
            d = local_ref.date()
        elif relative in ("tomorrow", "tmrw", "tmr"):
            d = local_ref.date() + timedelta(days=1)
        elif relative.startswith("wd:"):
            target = WEEKDAYS[relative[3:]]
            ahead = (target - local_ref.weekday()) % 7
            d = local_ref.date() + timedelta(days=ahead)
        if tzinfo is None:
            assumptions.append(f"'{raw_date}' resolved using UTC calendar day; zone not stated")

    sched.launch_date = d.isoformat()
    sched.raw = " ".join(x for x in (raw_date, raw_time) if x)
    if t is not None and tzinfo is not None:
        local = datetime.combine(d, t, tzinfo)
        sched.launch_at = local.astimezone(timezone.utc)
        sched.launch_date = sched.launch_at.date().isoformat()
        sched.precision = "exact"
        sched.tz_name = tz_label
        if tz_label in _FIXED_EXPECTED and isinstance(tzinfo, ZoneInfo):
            in_dst = bool(local.dst())
            if in_dst != _FIXED_EXPECTED[tz_label]:
                assumptions.append(f"'{tz_label}' read as local {tzinfo.key} time "
                                   f"({local.tzname()} in effect on that date)")
        if tz_label in AMBIGUOUS:
            assumptions.append(AMBIGUOUS[tz_label])
    else:
        sched.precision = "day"
        if t is not None:
            assumptions.append(f"time '{raw_time.strip()}' given without a time zone — not scheduling exact reminders")
    sched.assumptions.extend(assumptions)
    return sched
