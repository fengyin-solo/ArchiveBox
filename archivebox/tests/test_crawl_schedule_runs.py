#!/usr/bin/env python3
"""Tests for the persistent CrawlSchedule run ledger.

Covers timezone/DST cron resolution, transactional period claiming, backfill
limits, concurrent runners, manual run-all gating, disable/modify/cancel
cleanup, and stale-claim reconciliation.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connections, IntegrityError, transaction
from django.utils import timezone

from archivebox.crawls.schedule_util import (
    MAX_ENUMERATE_RUNS,
    PLANNED_RUN_DST_AMBIGUOUS,
    PLANNED_RUN_DST_GAP,
    PLANNED_RUN_NORMAL,
    get_schedule_timezone,
    iter_planned_runs,
    next_run_for_schedule,
    validate_schedule,
    validate_timezone,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def archivebox_db(initialized_archive):
    from archivebox.tests.test_orm_helpers import use_archivebox_db

    with use_archivebox_db(initialized_archive):
        yield initialized_archive


def _make_schedule(cron="hourly", *, kind="crawl", urls="https://example.com/", **kwargs):
    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.crawls.models import Crawl, CrawlSchedule

    user_id = get_or_create_system_user_pk()
    template = Crawl.objects.create(
        urls=urls,
        created_by_id=user_id,
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
    )
    config = {"SCHEDULE_KIND": kind} if kind == "update" else {}
    schedule = CrawlSchedule.objects.create(
        template=template,
        schedule=cron,
        config=config,
        created_by_id=user_id,
        **kwargs,
    )
    return schedule, template


def _set_cursor(schedule, when):
    from archivebox.crawls.models import CrawlSchedule

    CrawlSchedule.objects.filter(pk=schedule.pk).update(last_planned_run_at=when)
    schedule.refresh_from_db()


def _materialized_crawls(schedule):
    from archivebox.crawls.models import Crawl

    return Crawl.objects.filter(schedule=schedule).exclude(pk=schedule.template_id)



# ---------------------------------------------------------------------------
# Task 1: timezone / cron / DST resolution (pure functions, no DB needed)
# ---------------------------------------------------------------------------


def test_validate_timezone_accepts_empty_and_iana_names():
    assert validate_timezone("") == ""
    assert validate_timezone(None) == ""
    assert validate_timezone(" America/New_York ") == "America/New_York"
    assert get_schedule_timezone("") is not None


def test_validate_timezone_rejects_unknown_zone():
    with pytest.raises(ValueError, match="Invalid timezone"):
        validate_timezone("Mars/Olympus")


def test_validate_schedule_rejects_invalid_cron():
    with pytest.raises(ValueError, match="Invalid schedule"):
        validate_schedule("nonsense")
    assert validate_schedule("daily") == "0 0 * * *"


def test_cron_fires_resolve_in_schedule_timezone_winter_and_summer():
    ny = "America/New_York"
    # 2026-01-14 09:00 local is EST (UTC-5) -> 14:00 UTC
    fires, truncated = iter_planned_runs(
        "0 9 * * *",
        ny,
        anchor=datetime(2026, 1, 14, tzinfo=UTC),
        now=datetime(2026, 1, 15, tzinfo=UTC),
    )
    assert truncated is False
    assert len(fires) == 1
    assert fires[0].kind == PLANNED_RUN_NORMAL
    assert fires[0].planned_at == datetime(2026, 1, 14, 14, 0, tzinfo=UTC)

    # 2026-07-14 09:00 local is EDT (UTC-4) -> 13:00 UTC
    fires, _ = iter_planned_runs(
        "0 9 * * *",
        ny,
        anchor=datetime(2026, 7, 14, tzinfo=UTC),
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )
    assert len(fires) == 1
    assert fires[0].planned_at == datetime(2026, 7, 14, 13, 0, tzinfo=UTC)


def test_spring_forward_gap_is_flagged_not_silently_skipped():
    # America/New_York springs forward 02:00 -> 03:00 on 2026-03-08; 02:30 does
    # not exist. croniter alone would silently move the fire to 03:00.
    fires, truncated = iter_planned_runs(
        "30 2 * * *",
        "America/New_York",
        anchor=datetime(2026, 3, 8, tzinfo=UTC),
        now=datetime(2026, 3, 9, 12, tzinfo=UTC),
    )
    assert truncated is False
    gap_fires = [fire for fire in fires if fire.kind == PLANNED_RUN_DST_GAP]
    normal_fires = [fire for fire in fires if fire.kind == PLANNED_RUN_NORMAL]
    assert len(gap_fires) == 1
    assert gap_fires[0].wall_time == datetime(2026, 3, 8, 2, 30)
    # Deterministic UTC period key for the nonexistent wall time (fold=0, EST).
    assert gap_fires[0].planned_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert len(normal_fires) == 1
    assert normal_fires[0].wall_time == datetime(2026, 3, 9, 2, 30)
    assert normal_fires[0].planned_at == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_fall_back_ambiguous_fire_runs_once_at_first_occurrence():
    # America/New_York falls back 02:00 -> 01:00 on 2026-11-01; 01:30 happens
    # twice. Policy: exactly one fire, first physical occurrence (EDT, 05:30 UTC).
    fires, truncated = iter_planned_runs(
        "30 1 * * *",
        "America/New_York",
        anchor=datetime(2026, 11, 1, tzinfo=UTC),
        now=datetime(2026, 11, 2, tzinfo=UTC),
    )
    assert truncated is False
    assert len(fires) == 1
    fire = fires[0]
    assert fire.kind == PLANNED_RUN_DST_AMBIGUOUS
    assert fire.wall_time == datetime(2026, 11, 1, 1, 30)
    assert fire.planned_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_iter_planned_runs_reports_truncation_for_pathological_window():
    fires, truncated = iter_planned_runs(
        "* * * * *",
        "UTC",
        anchor=datetime(2020, 1, 1, tzinfo=UTC),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        max_enumerate=100,
    )
    assert truncated is True
    assert len(fires) == 100


def test_next_run_for_schedule_skips_gap_and_uses_timezone():
    # Asked just before the 2026-03-08 gap: next *fireable* run is 2026-03-09.
    nxt = next_run_for_schedule(
        "30 2 * * *",
        datetime(2026, 3, 8, 5, tzinfo=UTC),  # 00:00 local
        "America/New_York",
    )
    assert nxt == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)

    nxt = next_run_for_schedule("hourly", datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    assert nxt == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Task 2: model constraints / validation / cursor-derived properties
# ---------------------------------------------------------------------------


def test_schedule_run_planned_at_is_unique_per_schedule(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("hourly")
    planned = datetime(2026, 6, 1, 1, 0, tzinfo=UTC)
    CrawlScheduleRun.objects.create(schedule=schedule, planned_at=planned, run_kind="crawl")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CrawlScheduleRun.objects.create(schedule=schedule, planned_at=planned, run_kind="crawl")


def test_schedule_save_rejects_invalid_cron_and_timezone(archivebox_db):
    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.crawls.models import Crawl, CrawlSchedule

    user_id = get_or_create_system_user_pk()
    template = Crawl.objects.create(
        urls="https://example.com/",
        created_by_id=user_id,
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
    )
    with pytest.raises(ValueError, match="Invalid schedule"):
        CrawlSchedule.objects.create(template=template, schedule="nonsense cron", created_by_id=user_id)
    with pytest.raises(ValueError, match="Invalid timezone"):
        CrawlSchedule.objects.create(
            template=template,
            schedule="hourly",
            timezone_name="Mars/Olympus",
            created_by_id=user_id,
        )
    assert CrawlSchedule.objects.count() == 0


def test_next_run_at_uses_schedule_timezone_and_cursor_fallback(archivebox_db):
    schedule, _template = _make_schedule("0 9 * * *", timezone_name="America/New_York")
    # No cursor yet: anchor is created_at (UTC). Compare against independent util.
    expected = next_run_for_schedule("0 9 * * *", schedule.created_at, "America/New_York")
    assert schedule.next_run_at == expected
    assert schedule.last_run_at == schedule.created_at


# ---------------------------------------------------------------------------
# Task 4: claiming, backfill, concurrency, DST ledger, errors, reconciliation
# ---------------------------------------------------------------------------


def test_downtime_across_five_periods_backfills_only_latest_one(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *", max_backfill_runs=1)
    anchor = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    _set_cursor(schedule, anchor)

    outcome = schedule.evaluate(now=now, source="runner")

    assert outcome["state"] == "enqueued"
    crawls = _materialized_crawls(schedule)
    assert crawls.count() == 1
    runs = schedule.runs.exclude(status=CrawlScheduleRun.StatusChoices.SKIPPED)
    assert runs.count() == 1
    run = runs.get()
    assert run.planned_at == datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    assert run.status == CrawlScheduleRun.StatusChoices.CLAIMED
    assert run.crawl_id == crawls.get().id
    assert run.owner and f":runner" in run.owner
    schedule.refresh_from_db()
    assert schedule.last_planned_run_at == datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    assert schedule.missed_runs_skipped == 4
    assert schedule.last_skip_count == 4
    assert schedule.last_skip_first_at == datetime(2026, 6, 1, 1, 0, tzinfo=UTC)
    assert schedule.last_skip_last_at == datetime(2026, 6, 1, 4, 0, tzinfo=UTC)
    assert schedule.last_skip_reason == "backfill_limited"


def test_backfill_limit_three_claims_most_recent_periods(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *", max_backfill_runs=3)
    anchor = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    _set_cursor(schedule, anchor)

    outcome = schedule.evaluate(now=now, source="runner")

    assert outcome["state"] == "enqueued"
    crawls = _materialized_crawls(schedule)
    assert crawls.count() == 3
    planned = set(
        schedule.runs.exclude(status=CrawlScheduleRun.StatusChoices.SKIPPED).values_list("planned_at", flat=True),
    )
    assert planned == {
        datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
    }
    assert all(crawl.status == "queued" for crawl in crawls)
    schedule.refresh_from_db()
    assert schedule.missed_runs_skipped == 7
    assert schedule.last_skip_count == 7
    assert schedule.last_skip_first_at == datetime(2026, 6, 1, 1, 0, tzinfo=UTC)
    assert schedule.last_skip_last_at == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    assert CrawlScheduleRun.objects.filter(schedule=schedule).count() == 3


def test_concurrent_runners_claim_one_period_once(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule, CrawlScheduleRun

    schedule, _template = _make_schedule("30 * * * *")
    anchor = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, 0, 30, tzinfo=UTC)
    _set_cursor(schedule, anchor)
    schedule_id = schedule.pk

    barrier = threading.Barrier(2)
    summaries = []

    def worker():
        try:
            barrier.wait()
            summaries.append(
                CrawlSchedule.dispatch_due(
                    now=now,
                    source="runner",
                    schedules=CrawlSchedule.objects.filter(pk=schedule_id),
                ),
            )
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One process claims; the loser is either rejected by the unique constraint
    # (busy) or skips the tick on a SQLite write lock (locked) — never creates
    # a second Crawl.
    states = sorted(run["state"] for summary in summaries for run in summary["runs"])
    assert states.count("enqueued") == 1
    assert set(states) - {"enqueued"} <= {"busy", "locked"}
    assert sum(summary["enqueued"] for summary in summaries) == 1
    assert sum(summary["errors"] for summary in summaries) == 0
    schedule.refresh_from_db()
    assert schedule.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).count() == 1
    assert _materialized_crawls(schedule).count() == 1


def test_open_claim_blocks_later_periods_until_crawl_seals(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    anchor = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    _set_cursor(schedule, anchor)

    first = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC), source="runner")
    assert first["state"] == "enqueued"
    first_crawl = first["crawl"]

    blocked = schedule.evaluate(now=datetime(2026, 6, 1, 3, 0, tzinfo=UTC), source="runner")
    assert blocked["state"] == "busy"
    assert _materialized_crawls(schedule).count() == 1
    assert schedule.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).count() == 1

    # Crawl finishes: reconciliation closes the first claim, then backfill
    # policy applies to the periods crossed while it was running.
    Crawl.objects.filter(pk=first_crawl.pk).update(status=Crawl.StatusChoices.SEALED)
    resumed = schedule.evaluate(now=datetime(2026, 6, 1, 3, 0, tzinfo=UTC), source="runner")
    assert resumed["state"] == "enqueued"
    assert _materialized_crawls(schedule).count() == 2
    assert schedule.runs.filter(status=CrawlScheduleRun.StatusChoices.SUCCEEDED).count() == 1
    schedule.refresh_from_db()
    assert schedule.missed_runs_skipped == 1  # 02:00 period abandoned


def test_dst_gap_period_is_recorded_skipped_and_cursor_advances(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("30 2 * * *", timezone_name="America/New_York")
    _set_cursor(schedule, datetime(2026, 3, 8, 5, 0, tzinfo=UTC))  # local Mar 8 00:00

    outcome = schedule.evaluate(now=datetime(2026, 3, 9, 12, tzinfo=UTC), source="runner")

    assert outcome["state"] == "enqueued"
    gap = schedule.runs.get(status=CrawlScheduleRun.StatusChoices.SKIPPED)
    assert gap.reason == "dst_gap"
    assert gap.planned_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert "02:30:00" in gap.message and "America/New_York" in gap.message
    claimed = schedule.runs.exclude(status=CrawlScheduleRun.StatusChoices.SKIPPED).get()
    assert claimed.planned_at == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    assert _materialized_crawls(schedule).count() == 1
    schedule.refresh_from_db()
    assert schedule.last_planned_run_at == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_dst_ambiguous_period_runs_once_at_first_occurrence(archivebox_db):
    schedule, _template = _make_schedule("30 1 * * *", timezone_name="America/New_York")
    _set_cursor(schedule, datetime(2026, 11, 1, 4, 0, tzinfo=UTC))  # local Nov 1 00:00 EDT

    outcome = schedule.evaluate(now=datetime(2026, 11, 2, 0, 0, tzinfo=UTC), source="runner")

    assert outcome["state"] == "enqueued"
    run = schedule.runs.get()
    assert run.planned_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert run.reason == "dst_ambiguous_first"
    assert "ambiguous" in run.message
    assert _materialized_crawls(schedule).count() == 1


def test_invalid_cron_and_timezone_in_db_record_auditable_error(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule

    schedule, _template = _make_schedule("hourly")
    CrawlSchedule.objects.filter(pk=schedule.pk).update(schedule="not a cron")
    schedule.refresh_from_db()
    outcome = schedule.evaluate(now=timezone.now())
    assert outcome["state"] == "error"
    schedule.refresh_from_db()
    assert schedule.last_error_code == "invalid_cron"
    assert schedule.last_error_at is not None
    assert "Invalid schedule" in schedule.last_error_message
    assert _materialized_crawls(schedule).count() == 0

    CrawlSchedule.objects.filter(pk=schedule.pk).update(schedule="hourly", timezone_name="Mars/Olympus")
    schedule.refresh_from_db()
    outcome = schedule.evaluate(now=timezone.now())
    assert outcome["state"] == "error"
    schedule.refresh_from_db()
    assert schedule.last_error_code == "invalid_timezone"
    assert _materialized_crawls(schedule).count() == 0

    CrawlSchedule.objects.filter(pk=schedule.pk).update(timezone_name="")
    schedule.refresh_from_db()
    schedule.evaluate(now=timezone.now())
    schedule.refresh_from_db()
    assert schedule.last_error_code == ""  # recovers on the next tick


def test_reconcile_marks_sealed_crawl_succeeded_and_stale_update_failed(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    outcome = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))
    Crawl.objects.filter(pk=outcome["crawl"].pk).update(status=Crawl.StatusChoices.SEALED)
    assert schedule.reconcile_runs() == 1
    run = schedule.runs.get()
    assert run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED
    assert run.finished_at is not None

    update_schedule, _update_template = _make_schedule("daily", kind="update")
    stale = CrawlScheduleRun.objects.create(
        schedule=update_schedule,
        planned_at=timezone.now() - timedelta(hours=2),
        started_at=timezone.now() - timedelta(hours=2),
        run_kind="update",
        status=CrawlScheduleRun.StatusChoices.CLAIMED,
    )
    assert update_schedule.reconcile_runs() == 1
    stale.refresh_from_db()
    assert stale.status == CrawlScheduleRun.StatusChoices.FAILED
    assert stale.reason == "stale_claim"


def test_update_kind_schedule_runs_maintenance_through_runner_and_finalizes(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule

    schedule, _template = _make_schedule("daily", kind="update")
    _set_cursor(schedule, timezone.now() - timedelta(days=2))

    summary = CrawlSchedule.dispatch_due(now=timezone.now(), source="runner")

    assert summary["maintained"] == 1
    assert summary["enqueued"] == 0
    run = schedule.runs.get()
    assert run.run_kind == "update"
    assert run.status == "succeeded"
    assert run.crawl_id is None
    assert run.finished_at is not None
    assert run.owner.endswith(":runner")


def test_force_claim_used_by_run_all_respects_gate_for_crawl_kind(archivebox_db):
    schedule, _template = _make_schedule("daily")
    first = schedule.evaluate(now=timezone.now(), source="manual_run_all", force=True)
    assert first["state"] == "enqueued"
    first_planned = schedule.runs.get().planned_at

    second = schedule.evaluate(now=timezone.now(), source="manual_run_all", force=True)
    assert second["state"] == "busy"
    assert _materialized_crawls(schedule).count() == 1
    assert schedule.runs.count() == 1
    assert schedule.runs.get().planned_at == first_planned


def test_dispatch_due_summarizes_busy_and_enqueued_schedules(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule

    ready_schedule, _ = _make_schedule("30 * * * *")
    _set_cursor(ready_schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    busy_schedule, _ = _make_schedule("30 * * * *")
    _set_cursor(busy_schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))

    first = CrawlSchedule.dispatch_due(now=datetime(2026, 6, 1, 0, 30, tzinfo=UTC), source="runner")
    assert first["enqueued"] == 2
    assert first["skipped"] == 0

    second = CrawlSchedule.dispatch_due(now=datetime(2026, 6, 1, 0, 31, tzinfo=UTC), source="manual_run_all", force=True)
    assert second["enqueued"] == 0
    assert second["skipped"] == 2


# ---------------------------------------------------------------------------
# Task 5: disable / modify / cancel cleanup
# ---------------------------------------------------------------------------


def test_disabling_schedule_cancels_claim_seals_crawl_and_prevents_catchup(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    outcome = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))
    crawl = outcome["crawl"]

    disable_at = timezone.now()
    schedule.is_enabled = False
    schedule.save(update_fields=["is_enabled", "modified_at"])

    run = schedule.runs.get()
    assert run.status == CrawlScheduleRun.StatusChoices.CANCELLED
    assert run.reason == "schedule_disabled"
    assert run.finished_at is not None
    crawl.refresh_from_db()
    assert crawl.status == "sealed"
    schedule.refresh_from_db()
    assert schedule.last_planned_run_at >= disable_at

    # Re-enable much later: no catch-up crawl for the disabled window.
    schedule.is_enabled = True
    schedule.save(update_fields=["is_enabled", "modified_at"])
    schedule.evaluate(now=timezone.now())
    assert _materialized_crawls(schedule).count() == 1


def test_changing_schedule_definition_cancels_unstarted_claim(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    outcome = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))
    crawl = outcome["crawl"]

    schedule.schedule = "daily"
    schedule.save(update_fields=["schedule", "modified_at"])

    run = schedule.runs.get()
    assert run.status == CrawlScheduleRun.StatusChoices.CANCELLED
    assert run.reason == "schedule_modified"
    crawl.refresh_from_db()
    assert crawl.status == "sealed"
    schedule.refresh_from_db()
    # New cadence starts at the modification moment: hourly fires are gone.
    assert schedule.next_run_at > timezone.now()


def test_started_claim_is_not_cancelled_by_definition_change(archivebox_db):
    from archivebox.crawls.models import Crawl, CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    outcome = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))
    Crawl.objects.filter(pk=outcome["crawl"].pk).update(status=Crawl.StatusChoices.STARTED)

    schedule.schedule = "daily"
    schedule.save(update_fields=["schedule", "modified_at"])

    run = schedule.runs.get()
    assert run.status == CrawlScheduleRun.StatusChoices.CLAIMED
    outcome["crawl"].refresh_from_db()
    assert outcome["crawl"].status == "started"


def test_template_modification_invalidates_claim_and_next_run_uses_new_template(archivebox_db):
    schedule, template = _make_schedule("daily", urls="https://example.com/old")
    outcome = schedule.evaluate(now=timezone.now(), source="manual_run_all", force=True)
    old_crawl = outcome["crawl"]

    template.urls = "https://example.com/new"
    template.save(update_fields=["urls", "modified_at"])
    invalidated = schedule.invalidate_for_template_change({"urls"})

    assert invalidated == 1
    old_crawl.refresh_from_db()
    assert old_crawl.status == "sealed"

    next_outcome = schedule.evaluate(now=timezone.now(), source="manual_run_all", force=True)
    assert next_outcome["state"] == "enqueued"
    assert next_outcome["crawl"].urls == "https://example.com/new"
    assert _materialized_crawls(schedule).count() == 2


def test_crawl_cancel_releases_ledger_claim_for_next_period(archivebox_db):
    from archivebox.crawls.models import CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    outcome = schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))

    outcome["crawl"].cancel()

    run = schedule.runs.get(status=CrawlScheduleRun.StatusChoices.CANCELLED)
    assert run.reason == "crawl_cancelled"
    assert run.finished_at is not None
    # Next period can be claimed immediately after cancellation.
    follow_up = schedule.evaluate(now=datetime(2026, 6, 1, 2, 0, tzinfo=UTC))
    assert follow_up["state"] == "enqueued"
    assert _materialized_crawls(schedule).filter(status="queued").count() == 1
    assert schedule.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).count() == 1


def test_disable_many_bulk_helper_clears_claims_and_resets_cursors(archivebox_db):
    from archivebox.crawls.models import CrawlSchedule, CrawlScheduleRun

    schedule, _template = _make_schedule("0 * * * *")
    _set_cursor(schedule, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    schedule.evaluate(now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC))

    disabled = CrawlSchedule.disable_many(CrawlSchedule.objects.all())
    assert disabled == 1
    schedule.refresh_from_db()
    assert schedule.is_enabled is False
    assert schedule.runs.filter(status=CrawlScheduleRun.StatusChoices.CANCELLED, reason="schedule_disabled").count() == 1

