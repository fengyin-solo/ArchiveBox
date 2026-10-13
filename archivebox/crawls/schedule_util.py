from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.conf import settings
from django.utils import timezone


SCHEDULE_ALIASES: dict[str, str] = {
    "minute": "* * * * *",
    "minutely": "* * * * *",
    "hour": "0 * * * *",
    "hourly": "0 * * * *",
    "day": "0 0 * * *",
    "daily": "0 0 * * *",
    "week": "0 0 * * 0",
    "weekly": "0 0 * * 0",
    "month": "0 0 1 * *",
    "monthly": "0 0 1 * *",
    "year": "0 0 1 1 *",
    "yearly": "0 0 1 1 *",
}

# Stable machine-readable ledger reason codes. Kept here so the model layer,
# the CLI and the admin share one vocabulary.
REASON_DST_GAP = "dst_gap"
REASON_DST_AMBIGUOUS_FIRST = "dst_ambiguous_first"
REASON_BACKFILL_LIMITED = "backfill_limited"
REASON_SCHEDULE_DISABLED = "schedule_disabled"
REASON_SCHEDULE_MODIFIED = "schedule_modified"
REASON_TEMPLATE_MODIFIED = "template_modified"
REASON_CRAWL_CANCELLED = "crawl_cancelled"
REASON_CRAWL_DELETED = "crawl_deleted"
REASON_STALE_CLAIM = "stale_claim"
REASON_INVALID_CRON = "invalid_cron"
REASON_INVALID_TIMEZONE = "invalid_timezone"

PLANNED_RUN_NORMAL = "normal"
PLANNED_RUN_DST_GAP = "dst_gap"
PLANNED_RUN_DST_AMBIGUOUS = "dst_ambiguous"

# Hard safety stop for cron fire enumeration across a long downtime window.
# A pathological "* * * * *" schedule left unattended for years must not make
# the scheduler enumerate (or insert) millions of rows.
MAX_ENUMERATE_RUNS = 10_000


def normalize_schedule(schedule: str) -> str:
    normalized = (schedule or "").strip()
    if not normalized:
        raise ValueError("Schedule cannot be empty.")

    return SCHEDULE_ALIASES.get(normalized.lower(), normalized)


def validate_schedule(schedule: str) -> str:
    normalized = normalize_schedule(schedule)
    if not croniter.is_valid(normalized):
        raise ValueError(
            "Invalid schedule. Use an alias like daily/weekly/monthly or a cron expression such as '0 */6 * * *'.",
        )
    return normalized


def validate_timezone(tzname: str | None) -> str:
    """Normalize a per-schedule IANA timezone name; '' means server default (UTC)."""
    normalized = (tzname or "").strip()
    if not normalized:
        return ""
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        raise ValueError(
            f"Invalid timezone {normalized!r}. Use an IANA timezone name such as 'UTC' or 'America/New_York'.",
        )
    return normalized


def get_schedule_timezone(tzname: str | None) -> ZoneInfo:
    normalized = validate_timezone(tzname)
    if not normalized:
        normalized = getattr(settings, "TIME_ZONE", "UTC") or "UTC"
    return ZoneInfo(normalized)


@dataclass(frozen=True)
class PlannedRun:
    """One cron fire resolved against the schedule's local timezone.

    planned_at is the deterministic UTC instant used as the ledger period key;
    wall_time is the naive local wall-clock time the cron expression matched.
    kind is one of the PLANNED_RUN_* constants: a dst_gap fire never exists in
    real time and is recorded as an auditable skipped period instead of running.
    """

    planned_at: datetime
    wall_time: datetime
    kind: str

    @property
    def is_fireable(self) -> bool:
        return self.kind != PLANNED_RUN_DST_GAP


def _resolve_wall_time(wall_time: datetime, tz: ZoneInfo) -> PlannedRun:
    first = wall_time.replace(tzinfo=tz, fold=0)
    offset_first = first.utcoffset()
    offset_second = wall_time.replace(tzinfo=tz, fold=1).utcoffset()
    if offset_first == offset_second:
        return PlannedRun(first.astimezone(UTC), wall_time, PLANNED_RUN_NORMAL)
    # PEP 495: offsets diverge for exactly one hour on DST transition days.
    # fold=0 offset larger than fold=1 (e.g. -4h vs -5h): ambiguous repeated
    # wall time -> deterministically take the first physical occurrence (fold=0).
    # fold=0 offset smaller (e.g. -5h vs -4h): nonexistent spring-forward gap.
    if offset_first > offset_second:
        return PlannedRun(first.astimezone(UTC), wall_time, PLANNED_RUN_DST_AMBIGUOUS)
    return PlannedRun(first.astimezone(UTC), wall_time, PLANNED_RUN_DST_GAP)


def iter_planned_runs(
    schedule: str,
    tzname: str | None,
    anchor: datetime,
    now: datetime,
    *,
    max_enumerate: int = MAX_ENUMERATE_RUNS,
) -> tuple[list[PlannedRun], bool]:
    """Enumerate cron fires in the half-open window (anchor, now].

    Cron is advanced in naive local wall-clock time so that DST handling stays
    explicit: croniter itself quietly rewrites spring-forward gaps and emits
    duplicate fires for repeated fall-back hours when fed aware datetimes.
    Resolving each wall time ourselves makes gaps auditable and ambiguous
    fires deterministic (first occurrence).

    Returns (fires, truncated). fires is ascending by planned_at. A dst_gap
    fire is included so the caller can persist a skipped ledger row and advance
    the cursor over it. truncated is True when the enumeration safety cap was
    reached before exhausting the window; the caller must treat every older,
    unenumerated fire as backfill-skipped.
    """
    normalized = validate_schedule(schedule)
    tz = get_schedule_timezone(tzname)
    if timezone.is_naive(anchor):
        anchor = timezone.make_aware(anchor, timezone.utc)
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.utc)

    local_anchor = anchor.astimezone(tz).replace(tzinfo=None)
    iterator = croniter(normalized, local_anchor)
    fires: list[PlannedRun] = []
    truncated = False
    for _ in range(max_enumerate):
        wall_time = iterator.get_next(datetime)
        planned = _resolve_wall_time(wall_time, tz)
        if planned.planned_at > now:
            break
        fires.append(planned)
    else:
        truncated = True
    return fires, truncated


def next_planned_run(schedule: str, tzname: str | None, after: datetime) -> PlannedRun:
    """Next fireable (non-gap) planned run after ``after``, keeping DST markers."""
    normalized = validate_schedule(schedule)
    tz = get_schedule_timezone(tzname)
    if timezone.is_naive(after):
        after = timezone.make_aware(after, UTC)
    local_after = after.astimezone(tz).replace(tzinfo=None)
    iterator = croniter(normalized, local_after)
    for _ in range(MAX_ENUMERATE_RUNS):
        planned = _resolve_wall_time(iterator.get_next(datetime), tz)
        if planned.is_fireable:
            return planned
    raise ValueError("Invalid schedule: unable to resolve a next run within the enumeration limit.")


def next_run_for_schedule(schedule: str, after: datetime, tzname: str | None = None) -> datetime:
    """Next fireable (non-gap) UTC instant after ``after``; for display/preview."""
    return next_planned_run(schedule, tzname, after).planned_at
