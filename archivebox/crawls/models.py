__package__ = "archivebox.crawls"

import logging
from typing import TYPE_CHECKING, Any
from collections.abc import Iterable, Mapping
import os
import uuid
import json
import re
from itertools import islice
from datetime import timedelta
from archivebox.uuid_compat import CompactUUIDField, uuid7
from pathlib import Path
from urllib.parse import urlparse

from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.db.models.fields.json import KT
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.conf import settings
from django.urls import reverse_lazy
from django.utils import timezone
from archivebox.config.common import rprint as print
from archivebox.core.permissions import PERMISSIONS_VALUES, normalize_permissions

from archivebox.base_models.models import (
    ModelWithUUID,
    ModelWithDeleteAfter,
    ModelWithOutputDir,
    ModelWithConfig,
    ModelWithNotes,
    ModelWithHealthStats,
    get_or_create_system_user_pk,
)
from archivebox.workers.models import ModelWithQueue
from archivebox.crawls.schedule_util import (
    PLANNED_RUN_DST_AMBIGUOUS,
    REASON_BACKFILL_LIMITED,
    REASON_CRAWL_CANCELLED,
    REASON_CRAWL_DELETED,
    REASON_DST_AMBIGUOUS_FIRST,
    REASON_DST_GAP,
    REASON_INVALID_CRON,
    REASON_INVALID_TIMEZONE,
    REASON_SCHEDULE_DISABLED,
    REASON_SCHEDULE_MODIFIED,
    REASON_STALE_CLAIM,
    REASON_TEMPLATE_MODIFIED,
    iter_planned_runs,
    next_run_for_schedule,
    validate_schedule,
    validate_timezone,
)
from archivebox.misc.util import parse_date, sanitize_html_text, validate_url, validate_url_length

if TYPE_CHECKING:
    from archivebox.core.models import Snapshot

logger = logging.getLogger(__name__)

# An update-kind claim runs maintenance synchronously in the claiming process.
# If that process dies between the claim INSERT and the finalizing UPDATE, the
# row is failed by reconciliation after this grace period.
STALE_UPDATE_CLAIM_SECONDS = 30 * 60

# Definition fields whose change invalidates any not-yet-started claimed run.
SCHEDULE_DEFINITION_FIELDS = frozenset({"schedule", "timezone_name", "config", "template_id", "max_backfill_runs"})

# Content fields of a template Crawl whose edit invalidates pending claims.
TEMPLATE_CONTENT_FIELDS = frozenset({"urls", "max_depth", "tags_str", "config", "persona_id", "label", "notes"})


def _is_database_lock_error(exc: Exception) -> bool:
    from django.db import DatabaseError

    if not isinstance(exc, DatabaseError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "could not obtain lock" in message or "deadlock detected" in message


def current_schedule_owner(source: str) -> str:
    """Stable auditable owner label: machine guid + pid + trigger source."""
    try:
        from archivebox.machine.models import Machine

        machine_guid = Machine.current().guid
    except Exception:
        logger.warning("Unable to resolve current Machine guid for schedule owner label", exc_info=True)
        machine_guid = "unknown-machine"
    return f"{machine_guid}:{os.getpid()}:{source}"[-128:]


class CrawlScheduleRun(ModelWithUUID):
    """One durable ledger row per (schedule, planned cron period).

    The database unique constraint on (schedule, planned_at) is the only
    cross-process claim gate: duplicate runners and admin run-all can race at
    SQLite/PostgreSQL, but exactly one INSERT can win.
    """

    class StatusChoices(models.TextChoices):
        CLAIMED = "claimed", "Claimed"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"

    schedule: "CrawlSchedule" = models.ForeignKey("CrawlSchedule", on_delete=models.CASCADE, related_name="runs", null=False)  # type: ignore
    planned_at = models.DateTimeField(null=False, blank=False, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, null=False)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=StatusChoices.choices, default=StatusChoices.CLAIMED, null=False, db_index=True)
    reason = models.CharField(max_length=32, blank=True, null=False, default="")
    message = models.TextField(blank=True, null=False, default="")
    owner = models.CharField(max_length=128, blank=True, null=False, default="")
    source = models.CharField(max_length=32, blank=True, null=False, default="")
    run_kind = models.CharField(max_length=16, null=False, blank=False)
    crawl: "Crawl | None" = models.ForeignKey(
        "Crawl",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedule_runs",
    )

    class Meta(ModelWithUUID.Meta):
        app_label = "crawls"
        verbose_name = "Scheduled Crawl Run"
        verbose_name_plural = "Scheduled Crawl Runs"
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "planned_at"],
                name="csrun_sched_planned_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["schedule", "status"], name="csrun_sched_status_idx"),
            models.Index(fields=["status", "planned_at"], name="csrun_status_planned_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.id}] schedule={self.schedule_id} planned={self.planned_at.isoformat()} {self.status}"

    def finalize(self, status: str, *, reason: str = "", message: str = "", now=None) -> bool:
        now = now or timezone.now()
        updated = (
            type(self)
            .objects.filter(pk=self.pk, status=self.StatusChoices.CLAIMED)
            .update(
                status=status,
                reason=reason or self.reason,
                message=message or self.message,
                finished_at=now,
                modified_at=now,
            )
        )
        if updated:
            self.status = status
            self.reason = reason or self.reason
            self.message = message or self.message
            self.finished_at = now
        return updated == 1

    def cancel(self, reason: str, *, now=None, message: str = "") -> bool:
        return self.finalize(self.StatusChoices.CANCELLED, reason=reason, message=message, now=now)


class CrawlSchedule(ModelWithUUID, ModelWithNotes):
    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    modified_at = models.DateTimeField(auto_now=True)

    template: "Crawl" = models.ForeignKey("Crawl", on_delete=models.CASCADE, null=False, blank=False)  # type: ignore
    schedule = models.CharField(max_length=64, blank=False, null=False)
    timezone_name = models.CharField(max_length=64, blank=True, null=False, default="", verbose_name="timezone")
    max_backfill_runs = models.PositiveSmallIntegerField(
        default=1,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Maximum number of missed periods to run when catching up after downtime. Older periods are recorded as skipped.",
    )
    is_enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, null=True, blank=True)
    label = models.CharField(max_length=64, blank=True, null=False, default="")
    notes = models.TextField(blank=True, null=False, default="")

    # Persistent scheduling cursor and auditable state.
    last_planned_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    missed_runs_skipped = models.PositiveBigIntegerField(default=0, null=False)
    last_skip_at = models.DateTimeField(null=True, blank=True)
    last_skip_count = models.PositiveBigIntegerField(default=0, null=False)
    last_skip_first_at = models.DateTimeField(null=True, blank=True)
    last_skip_last_at = models.DateTimeField(null=True, blank=True)
    last_skip_reason = models.CharField(max_length=32, blank=True, null=False, default="")
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, null=False, default="")
    last_error_message = models.TextField(blank=True, null=False, default="")

    crawl_set: models.Manager["Crawl"]
    runs: models.Manager[CrawlScheduleRun]

    class Meta(ModelWithUUID.Meta, ModelWithNotes.Meta):
        app_label = "crawls"
        verbose_name = "Scheduled Crawl"
        verbose_name_plural = "Scheduled Crawls"

    def __str__(self) -> str:
        urls_preview = self.template.urls[:64] if self.template and self.template.urls else ""
        return f"[{self.id}] {urls_preview} @ {self.schedule}"

    @property
    def api_url(self) -> str:
        return str(reverse_lazy("api-1:get_any", args=[self.id]))

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "label" in update_fields:
            self.label = sanitize_html_text(self.label).strip()
        if update_fields is None or "notes" in update_fields:
            self.notes = sanitize_html_text(self.notes)
        self.schedule = (self.schedule or "").strip()
        validate_schedule(self.schedule)
        self.timezone_name = validate_timezone(self.timezone_name)
        self.label = self.label or (sanitize_html_text(self.template.label).strip() if self.template else "")

        previous = None
        tracked_fields = {"is_enabled", *SCHEDULE_DEFINITION_FIELDS}
        tracked_update_fields = set(update_fields) & tracked_fields if update_fields is not None else tracked_fields
        # Always read the full tracked row: deciding the enable/disable
        # transition requires the current is_enabled value even when the
        # caller's update_fields omits it.
        if self.pk and tracked_update_fields:
            previous = type(self).objects.filter(pk=self.pk).values(*tracked_fields).first()

        super().save(*args, **kwargs)
        if self.template:
            self.template.safe_update(
                {
                    "schedule_id": self.pk,
                    "modified_at": timezone.now(),
                },
                refresh=False,
            )
            self.template.schedule_id = self.pk
            self.template.schedule = self

        if previous is None:
            return
        now = timezone.now()
        definition_changed = any(
            field != "is_enabled" and previous.get(field) != _schedule_field_value(self, field) for field in tracked_update_fields
        )
        was_enabled = bool(previous.get("is_enabled"))
        if was_enabled and not self.is_enabled:
            self.cancel_open_claims(REASON_SCHEDULE_DISABLED, now=now)
            self._reset_cursor(now)
        elif not was_enabled and self.is_enabled:
            # Re-enabling anchors cron at the enable moment so the disabled
            # window never produces a catch-up burst.
            self._reset_cursor(now)
        elif definition_changed:
            self.cancel_open_claims(REASON_SCHEDULE_MODIFIED, now=now)
            self._reset_cursor(now)

    @classmethod
    def disable_many(cls, queryset, *, now=None) -> int:
        """Bulk-disable path that still clears claims and resets each cursor."""
        now = now or timezone.now()
        disabled = 0
        for schedule in queryset.filter(is_enabled=True):
            schedule.is_enabled = False
            schedule.save(update_fields=["is_enabled", "modified_at"])
            disabled += 1
        return disabled

    @property
    def kind(self) -> str:
        return str((self.config or {}).get("SCHEDULE_KIND") or "crawl")

    @property
    def scheduling_anchor(self):
        """Exclusive lower bound for cron fire enumeration (UTC)."""
        return self.last_planned_run_at or self.created_at

    @property
    def last_run_at(self):
        return self.scheduling_anchor

    @property
    def next_run_at(self):
        return next_run_for_schedule(self.schedule, self.scheduling_anchor, self.timezone_name)

    def is_due(self, now=None) -> bool:
        now = now or timezone.now()
        return self.is_enabled and self.next_run_at <= now

    @property
    def has_open_claim(self) -> bool:
        return self.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).exists()

    # ------------------------------------------------------------------
    # Claim lifecycle
    # ------------------------------------------------------------------

    def _reset_cursor(self, now=None) -> None:
        """Anchor cron evaluation at ``now`` so disable/modify causes no catch-up."""
        now = now or timezone.now()
        type(self).objects.filter(pk=self.pk).update(last_planned_run_at=now, modified_at=now)
        self.last_planned_run_at = now

    def record_error(self, code: str, message: str, *, now=None) -> None:
        now = now or timezone.now()
        type(self).objects.filter(pk=self.pk).update(
            last_error_at=now,
            last_error_code=code[:64],
            last_error_message=message[:4096],
            modified_at=now,
        )
        self.last_error_at = now
        self.last_error_code = code[:64]
        self.last_error_message = message[:4096]
        logger.warning("CrawlSchedule %s claim blocked: %s: %s", self.pk, code, message)

    def _clear_error(self) -> None:
        if not self.last_error_code:
            return
        type(self).objects.filter(pk=self.pk).update(
            last_error_at=None,
            last_error_code="",
            last_error_message="",
        )
        self.last_error_at = None
        self.last_error_code = ""
        self.last_error_message = ""

    def reconcile_runs(self, *, now=None) -> int:
        """Resolve claimed rows whose crawl finished, vanished, or whose owner died."""
        now = now or timezone.now()
        reconciled = 0
        claimed_runs = list(
            self.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).select_related("crawl"),
        )
        for run in claimed_runs:
            if run.crawl_id is None:
                if run.run_kind == "update" and run.started_at <= now - timedelta(seconds=STALE_UPDATE_CLAIM_SECONDS):
                    if run.finalize(
                        CrawlScheduleRun.StatusChoices.FAILED,
                        reason=REASON_STALE_CLAIM,
                        message="Claiming process did not finish scheduled maintenance before the stale-claim deadline.",
                        now=now,
                    ):
                        reconciled += 1
                continue
            crawl = run.crawl
            if crawl is None:
                if run.finalize(
                    CrawlScheduleRun.StatusChoices.FAILED,
                    reason=REASON_CRAWL_DELETED,
                    message="The materialized Crawl was deleted before the run finished.",
                    now=now,
                ):
                    reconciled += 1
            elif crawl.status == Crawl.StatusChoices.SEALED:
                if run.finalize(CrawlScheduleRun.StatusChoices.SUCCEEDED, now=now):
                    reconciled += 1
        return reconciled

    def cancel_open_claims(self, reason: str, *, now=None, message: str = "") -> int:
        """Cancel claimed runs whose crawl has not STARTED; seal queued crawls."""
        now = now or timezone.now()
        cancelled = 0
        for run in self.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED):
            if run.crawl_id is not None:
                # Only a still-QUEUED materialized crawl may be sealed here. A
                # crawl that already STARTED keeps its claim; the conditional
                # UPDATE makes that decision at the database, not by timestamp.
                sealed = Crawl.objects.filter(pk=run.crawl_id, status=Crawl.StatusChoices.QUEUED).update(
                    status=Crawl.StatusChoices.SEALED,
                    retry_at=now,
                    modified_at=now,
                )
                if not sealed:
                    continue
            if run.cancel(reason, message=message, now=now):
                cancelled += 1
        return cancelled

    def invalidate_for_template_change(self, changed_fields: set[str], *, now=None) -> int:
        if not (changed_fields & TEMPLATE_CONTENT_FIELDS):
            return 0
        return self.cancel_open_claims(
            REASON_TEMPLATE_MODIFIED,
            now=now,
            message="The schedule template was modified before the claimed run started.",
        )

    def _record_skipped_period(self, planned, *, source: str, owner: str, now) -> CrawlScheduleRun | None:
        message = (
            f"Local wall time {planned.wall_time.isoformat()} does not exist in "
            f"{self.timezone_name or settings.TIME_ZONE} on this date (spring-forward DST gap); the period was not run."
        )
        try:
            with transaction.atomic():
                run = CrawlScheduleRun(
                    schedule=self,
                    planned_at=planned.planned_at,
                    started_at=now,
                    finished_at=now,
                    status=CrawlScheduleRun.StatusChoices.SKIPPED,
                    reason=REASON_DST_GAP,
                    message=message,
                    owner=owner,
                    source=source,
                    run_kind=self.kind,
                )
                run.save()
                self._advance_cursor(planned.planned_at)
        except IntegrityError:
            return None
        return run

    def _advance_cursor(self, planned_at) -> None:
        type(self).objects.filter(pk=self.pk, last_planned_run_at=self.last_planned_run_at).update(
            last_planned_run_at=planned_at,
        )
        self.last_planned_run_at = planned_at

    def _record_backfill_skip(self, dropped, *, truncated: bool, now) -> None:
        count = len(dropped)
        message = f"{count} missed period(s) exceeded max_backfill_runs={self.max_backfill_runs} and were not run."
        if truncated:
            message += " The enumeration safety cap was also reached; older periods are uncounted."
        type(self).objects.filter(pk=self.pk).update(
            missed_runs_skipped=models.F("missed_runs_skipped") + count,
            last_skip_at=now,
            last_skip_count=count,
            last_skip_first_at=dropped[0].planned_at,
            last_skip_last_at=dropped[-1].planned_at,
            last_skip_reason=REASON_BACKFILL_LIMITED,
            modified_at=now,
        )
        self.missed_runs_skipped = (self.missed_runs_skipped or 0) + count
        self.last_skip_at = now
        self.last_skip_count = count
        self.last_skip_first_at = dropped[0].planned_at
        self.last_skip_last_at = dropped[-1].planned_at
        self.last_skip_reason = REASON_BACKFILL_LIMITED

    def claim_run(self, planned, *, source: str, owner: str, now) -> CrawlScheduleRun | None:
        """Atomically claim one cron period; the (schedule, planned_at) unique
        constraint is the cross-process gate. Crawl materialization happens in
        the same short transaction so a losing race rolls the Crawl back too."""
        reason = REASON_DST_AMBIGUOUS_FIRST if planned.kind == PLANNED_RUN_DST_AMBIGUOUS else ""
        message = ""
        if reason:
            message = (
                f"Local wall time {planned.wall_time.isoformat()} is ambiguous in "
                f"{self.timezone_name or settings.TIME_ZONE} (fall-back DST repeat); the first physical occurrence was used."
            )
        try:
            with transaction.atomic():
                run = CrawlScheduleRun(
                    schedule=self,
                    planned_at=planned.planned_at,
                    started_at=now,
                    status=CrawlScheduleRun.StatusChoices.CLAIMED,
                    reason=reason,
                    message=message,
                    owner=owner,
                    source=source,
                    run_kind=self.kind,
                )
                run.save()
                if self.kind != "update":
                    run.crawl = self.enqueue(queued_at=now)
                    run.save(update_fields=["crawl", "modified_at"])
                self._advance_cursor(planned.planned_at)
        except IntegrityError:
            return None
        return run

    def _execute_claimed_maintenance(self, run: CrawlScheduleRun, *, now) -> None:
        from archivebox.cli.archivebox_update import run_scheduled_maintenance

        try:
            run_scheduled_maintenance()
        except Exception as exc:
            run.finalize(
                CrawlScheduleRun.StatusChoices.FAILED,
                reason="maintenance_error",
                message=f"{type(exc).__name__}: {exc}"[:4096],
                now=now,
            )
            raise
        run.finalize(CrawlScheduleRun.StatusChoices.SUCCEEDED, now=now)

    def evaluate(self, *, now=None, source: str = "runner", force: bool = False) -> dict:
        """Reconcile, then claim due periods for this one schedule.

        ``force`` (admin run-all / legacy dispatch) claims the next planned
        period early instead of waiting for its wall time. Every claim passes
        through the same ledger gate as the daemon runner.
        """
        now = now or timezone.now()
        result = {"state": "idle", "crawl": None, "claimed": 0, "skipped_periods": 0, "maintained": 0, "failed": 0}
        self.reconcile_runs(now=now)
        if self.runs.filter(status=CrawlScheduleRun.StatusChoices.CLAIMED).exists():
            result["state"] = "busy"
            return result

        anchor = self.scheduling_anchor
        try:
            if force:
                from archivebox.crawls.schedule_util import next_planned_run

                target = next_planned_run(self.schedule, self.timezone_name, anchor)
                window_end = target.planned_at
            else:
                target = None
                window_end = now
            fires, truncated = iter_planned_runs(self.schedule, self.timezone_name, anchor, window_end)
        except ValueError as exc:
            message = str(exc)
            code = REASON_INVALID_TIMEZONE if "timezone" in message.lower() else REASON_INVALID_CRON
            self.record_error(code, message, now=now)
            result["state"] = "error"
            return result
        self._clear_error()

        owner = current_schedule_owner(source)
        for planned in fires:
            if not planned.is_fireable:
                if self._record_skipped_period(planned, source=source, owner=owner, now=now) is not None:
                    result["skipped_periods"] += 1
        due_runs = [planned for planned in fires if planned.is_fireable]
        if not due_runs:
            result["state"] = "skipped_dst" if result["skipped_periods"] else "idle"
            return result

        if len(due_runs) > self.max_backfill_runs:
            dropped = due_runs[: -self.max_backfill_runs]
            due_runs = due_runs[-self.max_backfill_runs :]
            self._record_backfill_skip(dropped, truncated=truncated, now=now)
            result["skipped_periods"] += len(dropped)

        for planned in due_runs:
            run = self.claim_run(planned, source=source, owner=owner, now=now)
            if run is None:
                # Another runner/admin already holds this exact period.
                result["state"] = "busy"
                break
            result["claimed"] += 1
            if run.run_kind == "update":
                self._execute_claimed_maintenance(run, now=now)
                if run.status == CrawlScheduleRun.StatusChoices.SUCCEEDED:
                    result["maintained"] += 1
                    result["state"] = "succeeded"
                else:
                    result["failed"] += 1
                    result["state"] = "failed"
                    break
            else:
                result["crawl"] = run.crawl
                result["state"] = "enqueued"
        if result["state"] == "idle" and result["skipped_periods"]:
            result["state"] = "skipped_dst"
        return result

    @classmethod
    def dispatch_due(
        cls,
        *,
        now=None,
        source: str = "runner",
        force: bool = False,
        schedules=None,
    ) -> dict:
        """Evaluate all enabled schedules through the shared ledger gate."""
        now = now or timezone.now()
        queryset = schedules if schedules is not None else cls.objects.filter(is_enabled=True)
        summary = {
            "schedules": 0,
            "enqueued": 0,
            "maintained": 0,
            "skipped": 0,
            "claimed": 0,
            "skipped_periods": 0,
            "failed": 0,
            "errors": 0,
            "runs": [],
        }
        for schedule in queryset.select_related("template", "template__created_by"):
            summary["schedules"] += 1
            try:
                outcome = schedule.evaluate(now=now, source=source, force=force)
            except Exception as exc:
                if _is_database_lock_error(exc):
                    # Another runner/admin process holds the write lock for
                    # this exact schedule tick. Skip it; the unique ledger
                    # constraint plus next tick's evaluation remain the gate.
                    logger.info("CrawlSchedule %s claim skipped: database locked by another process", schedule.pk)
                    summary["skipped"] += 1
                    summary["runs"].append({"schedule_id": str(schedule.pk), "state": "locked"})
                    continue
                logger.warning("CrawlSchedule %s evaluation failed: %s", schedule.pk, exc, exc_info=True)
                summary["errors"] += 1
                summary["runs"].append({"schedule_id": str(schedule.pk), "state": "error", "error": str(exc)[:512]})
                continue
            state = outcome["state"]
            summary["runs"].append({"schedule_id": str(schedule.pk), "state": state})
            summary["claimed"] += outcome["claimed"]
            summary["skipped_periods"] += outcome["skipped_periods"]
            summary["failed"] += outcome["failed"]
            if state == "enqueued":
                summary["enqueued"] += 1
            elif state == "busy":
                summary["skipped"] += 1
            summary["maintained"] += outcome["maintained"]
        return summary

    def dispatch(self, queued_at=None) -> "Crawl | None":
        """Legacy entry point: claim the next planned period and run it now."""
        outcome = self.evaluate(now=queued_at or timezone.now(), source="legacy_dispatch", force=True)
        if outcome["state"] == "error":
            raise ValueError(self.last_error_message or "Schedule evaluation failed")
        return outcome.get("crawl")

    def enqueue(self, queued_at=None) -> "Crawl":
        from archivebox.config.common import build_crawl_config_snapshot

        queued_at = queued_at or timezone.now()
        template = self.template
        label = template.label or self.label
        persona = template.persona if template.persona_id else None
        crawl_config = {key: value for key, value in (self.config or {}).items() if key != "SCHEDULE_KIND"}

        return Crawl.objects.create(
            urls=template.urls,
            config=build_crawl_config_snapshot(persona=persona, overrides=crawl_config),
            max_depth=template.max_depth,
            tags_str=template.tags_str,
            persona_id=template.persona_id,
            label=label,
            notes=template.notes,
            schedule=self,
            status=Crawl.StatusChoices.QUEUED,
            retry_at=queued_at,
            created_by=template.created_by,
        )


def _schedule_field_value(schedule: CrawlSchedule, field: str):
    if field == "template_id":
        return schedule.template_id
    return getattr(schedule, field)


class Crawl(ModelWithDeleteAfter, ModelWithOutputDir, ModelWithConfig, ModelWithHealthStats, ModelWithQueue):
    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    modified_at = models.DateTimeField(auto_now=True)

    urls = models.TextField(blank=False, null=False, help_text="Newline-separated list of URLs to crawl")
    config = models.JSONField(default=dict, null=True, blank=True)
    permissions = models.GeneratedField(
        expression=KT("config__PERMISSIONS"),
        output_field=models.CharField(max_length=16, null=True),
        db_persist=True,
        db_index=True,
        editable=False,
    )
    max_depth = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(4)])
    tags_str = models.CharField(max_length=1024, blank=True, null=False, default="")
    persona = models.ForeignKey(
        "personas.Persona",
        db_column="persona_id",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crawls",
    )
    label = models.CharField(max_length=64, blank=True, null=False, default="")
    notes = models.TextField(blank=True, null=False, default="")
    schedule = models.ForeignKey(CrawlSchedule, on_delete=models.SET_NULL, null=True, blank=True, editable=True)

    status = ModelWithQueue.StatusField(
        choices=ModelWithQueue.StatusChoices,
        default=ModelWithQueue.StatusChoices.QUEUED,
    )
    retry_at = ModelWithQueue.RetryAtField(default=timezone.now)

    retry_at_field_name = "retry_at"
    state_field_name = "status"
    StatusChoices = ModelWithQueue.StatusChoices
    INITIAL_STATE = StatusChoices.QUEUED
    ACTIVE_STATE = StatusChoices.STARTED
    FINAL_STATES = (StatusChoices.SEALED,)
    FINAL_OR_ACTIVE_STATES = (*FINAL_STATES, ACTIVE_STATE)
    active_state = StatusChoices.STARTED
    delete_after_final_statuses = (StatusChoices.SEALED,)
    RUNNABLE_STATES = (StatusChoices.QUEUED, StatusChoices.STARTED)
    INACTIVE_STATES = (StatusChoices.PAUSED, StatusChoices.SEALED)

    schedule_id: uuid.UUID | None

    snapshot_set: models.Manager["Snapshot"]

    class Meta(
        ModelWithDeleteAfter.Meta,
        ModelWithOutputDir.Meta,
        ModelWithConfig.Meta,
        ModelWithHealthStats.Meta,
        ModelWithQueue.Meta,
    ):
        app_label = "crawls"
        verbose_name = "Crawl"
        verbose_name_plural = "Crawls"
        indexes = [
            models.Index(fields=["-created_at", "-retry_at", "-id"], name="crawl_admin_order_idx"),
            models.Index(fields=["status", "-modified_at"], name="crawl_progress_status_idx"),
        ]

    def __str__(self):
        first_url = next((line.strip() for line in (self.urls or "").splitlines() if line.strip() and not line.strip().startswith("#")), "")
        # Show last 8 digits of UUID and more of the URL
        short_id = str(self.id)[-8:]
        return f"[...{short_id}] {first_url[:120]}"

    def get_delete_after_config_value(self):
        from archivebox.config.common import resolve_delete_after_config_value

        return resolve_delete_after_config_value(self.config)

    def pause(self, *, save: bool = True) -> bool:
        paused = super().pause(save=save)
        if paused and save and self.pk:
            from archivebox.core.models import Snapshot

            for snapshot in self.snapshot_set.exclude(status__in=Snapshot.FINAL_STATES).iterator():
                snapshot.pause()
        return paused

    def resume(self, *, when=None, save: bool = True) -> bool:
        resumed = super().resume(when=when, save=save)
        if resumed and self.pk:
            from archivebox.core.models import Snapshot

            resume_at = when or timezone.now()
            active_snapshots = self.snapshot_set.filter(
                status=Snapshot.StatusChoices.PAUSED,
            )
            active_snapshots.update(
                status=Snapshot.StatusChoices.QUEUED,
                retry_at=resume_at,
                modified_at=timezone.now(),
            )
        return resumed

    def cancel(self) -> None:
        now = timezone.now()
        self.schedule_child_snapshots_for_sealing()
        # User-initiated cancellation may come from an admin/API request while
        # the runner owns the crawl lease. This is intentionally a plain
        # conditional UPDATE instead of CAS: cancellation is an idempotent user
        # command, not a stale iterator write. Keep it to a tight scheduler row
        # update and let the runner claim the SEALED+due row for cleanup hooks.
        type(self).objects.filter(pk=self.pk).exclude(status=self.StatusChoices.SEALED).update(
            status=self.StatusChoices.SEALED,
            retry_at=now,
            modified_at=now,
        )
        self.status = self.StatusChoices.SEALED
        self.retry_at = now
        # Release the schedule ledger claim so the next period can be claimed;
        # a tight conditional UPDATE keeps this on the same scheduler-row path.
        CrawlScheduleRun.objects.filter(crawl=self, status=CrawlScheduleRun.StatusChoices.CLAIMED).update(
            status=CrawlScheduleRun.StatusChoices.CANCELLED,
            reason=REASON_CRAWL_CANCELLED,
            finished_at=now,
            modified_at=now,
        )

    def schedule_child_snapshots_for_sealing(self) -> int:
        from archivebox.core.models import Snapshot

        now = timezone.now()
        # Cancellation seals the Crawl first, then lets the runner seal each
        # child Snapshot through its own lifecycle. Active children that
        # are already due need no write; the runner will claim them as-is.
        active_children = self.snapshot_set.filter(
            status__in=Snapshot.OPEN_STATES,
        )
        return active_children.filter(
            Q(retry_at__isnull=True) | Q(retry_at__gt=now),
        ).update(
            retry_at=now,
            modified_at=now,
        )

    def schedule_child_snapshots_for_pause(self) -> int:
        from archivebox.core.models import Snapshot

        now = timezone.now()
        # Parent pause is a scheduler command. Wake child rows only; each
        # Snapshot runner claim performs the real pause transition, keeping
        # request/admin transactions tiny.
        active_children = self.snapshot_set.filter(
            status__in=Snapshot.RUNNABLE_STATES,
        )
        return active_children.filter(
            Q(retry_at__isnull=True) | Q(retry_at__gt=now),
        ).update(
            retry_at=now,
            modified_at=now,
        )

    @classmethod
    def missing_delete_at_candidates(cls):
        return cls.objects.filter(delete_at__isnull=True, config__has_key="DELETE_AFTER")

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "label" in update_fields:
            self.label = sanitize_html_text(self.label).strip()
        if update_fields is None or "notes" in update_fields:
            self.notes = sanitize_html_text(self.notes)
        if update_fields is None or "tags_str" in update_fields:
            self.tags_str = ",".join(self.parse_tag_names(self.tags_str or ""))
        sync_tags = update_fields is None or "tags_str" in update_fields
        old_crawl = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        previous_tag_names = set()
        if sync_tags and old_crawl is not None:
            previous_tag_names = set(self.parse_tag_names(old_crawl.tags_str or ""))

        config = dict(self.config or {})
        is_new = self._state.adding or old_crawl is None
        persona = self.persona if self.persona_id else None
        if is_new:
            from archivebox.config.common import build_crawl_config_snapshot

            config = build_crawl_config_snapshot(persona=persona, overrides=config)
        if str(config.get("PERMISSIONS") or "").strip().lower() not in PERMISSIONS_VALUES:
            from archivebox.config.common import get_config

            config["PERMISSIONS"] = normalize_permissions(get_config(persona=persona, include_machine=True).PERMISSIONS)
        if "CRAWL_MAX_CONCURRENT_SNAPSHOTS" in config:
            raw_concurrency = config["CRAWL_MAX_CONCURRENT_SNAPSHOTS"]
            if raw_concurrency in (None, ""):
                config.pop("CRAWL_MAX_CONCURRENT_SNAPSHOTS", None)
            else:
                config["CRAWL_MAX_CONCURRENT_SNAPSHOTS"] = max(1, int(raw_concurrency))

        if config != (self.config or {}):
            self.config = config
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = tuple(dict.fromkeys([*update_fields, "config"]))

        super().save(*args, **kwargs)
        old_permissions = getattr(old_crawl, "permissions", None)
        if old_crawl is not None and old_permissions != self.permissions:
            transaction.on_commit(lambda: self.update_child_snapshot_permissions(old_permissions, self.permissions))
        if sync_tags:
            next_tag_names = set(self.parse_tag_names(self.tags_str or ""))
            added_tag_names = next_tag_names - previous_tag_names
            removed_tag_names = previous_tag_names - next_tag_names
            if added_tag_names or removed_tag_names:
                # Keep the SQLite write phase short: the Crawl row is already
                # saved, and the potentially large snapshot tag fanout runs in
                # chunked ORM writes after any caller atomic() exits.
                transaction.on_commit(
                    lambda: self.apply_snapshot_tag_diff(
                        added_tag_names=added_tag_names,
                        removed_tag_names=removed_tag_names,
                    ),
                )

    def update_child_snapshot_permissions(self, old_permissions: str | None, new_permissions: str | None) -> int:
        from archivebox.core.models import Snapshot

        normalized_new_permissions = normalize_permissions(new_permissions)
        now = timezone.now()
        batch = []
        updated = 0
        queryset = self.snapshot_set.filter(Q(permissions=old_permissions) | Q(permissions__isnull=True)).only("id", "config")
        for snapshot in queryset.iterator(chunk_size=500):
            config = dict(snapshot.config or {})
            config["PERMISSIONS"] = normalized_new_permissions
            snapshot.config = config
            snapshot.modified_at = now
            batch.append(snapshot)
            if len(batch) >= 500:
                Snapshot.objects.bulk_update(batch, ["config", "modified_at"], batch_size=500)
                updated += len(batch)
                batch.clear()
        if batch:
            Snapshot.objects.bulk_update(batch, ["config", "modified_at"], batch_size=500)
            updated += len(batch)
        return updated

    @property
    def api_url(self) -> str:
        return str(reverse_lazy("api-1:get_crawl", args=[self.id]))

    @staticmethod
    def parse_tag_names(tags: Iterable[str] | str, *, pattern: str = r",") -> list[str]:
        raw_tags = re.split(pattern, tags) if isinstance(tags, str) else tags
        tag_names: list[str] = []
        seen: set[str] = set()
        for raw_tag in raw_tags:
            tag_name = sanitize_html_text(raw_tag).strip()
            if not tag_name:
                continue
            lowered = tag_name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tag_names.append(tag_name)
        return tag_names

    def current_tag_names(self) -> list[str]:
        current_tags_str = type(self).objects.filter(pk=self.pk).values_list("tags_str", flat=True).first() if self.pk else self.tags_str
        if current_tags_str is not None:
            self.tags_str = current_tags_str
        return self.parse_tag_names(self.tags_str or "")

    def apply_snapshot_tag_diff(self, *, added_tag_names: Iterable[str], removed_tag_names: Iterable[str]) -> None:
        from archivebox.core.models import Snapshot, SnapshotTag, Tag

        added_names = self.parse_tag_names(added_tag_names)
        removed_names = self.parse_tag_names(removed_tag_names)
        if not added_names and not removed_names:
            return

        if added_names:
            tags_by_name = {tag.name: tag for tag in Tag.objects.filter(name__in=added_names)}
            missing_tags = [Tag(name=name) for name in added_names if name not in tags_by_name]
            if missing_tags:
                # One small write for missing tag rows, followed by chunked
                # M2M fanout below; avoid per-snapshot get_or_create loops.
                Tag.objects.bulk_create(missing_tags, ignore_conflicts=True)
                tags_by_name = {tag.name: tag for tag in Tag.objects.filter(name__in=added_names)}

            tag_ids = [tag.pk for tag_name in added_names if (tag := tags_by_name.get(tag_name))]
            snapshot_ids = Snapshot.objects.filter(crawl=self).values_list("id", flat=True).iterator(chunk_size=5000)
            while True:
                batch_snapshot_ids = list(islice(snapshot_ids, 5000))
                if not batch_snapshot_ids:
                    break
                for tag_id in tag_ids:
                    # Chunked bulk_create keeps memory bounded and uses the
                    # SnapshotTag uniqueness constraint instead of row-by-row
                    # existence checks.
                    SnapshotTag.objects.bulk_create(
                        [SnapshotTag(snapshot_id=snapshot_id, tag_id=tag_id) for snapshot_id in batch_snapshot_ids],
                        ignore_conflicts=True,
                        batch_size=5000,
                    )

        if removed_names:
            removed_tag_ids = list(Tag.objects.filter(name__in=removed_names).values_list("pk", flat=True))
            if removed_tag_ids:
                # One DELETE with a subquery keeps the tag removal transaction
                # bounded to the M2M rows touched by this crawl only.
                SnapshotTag.objects.filter(snapshot__crawl=self, tag_id__in=removed_tag_ids).delete()

    def to_json(self) -> dict:
        """
        Convert Crawl model instance to a JSON-serializable dict.
        """
        from archivebox.config import VERSION
        from archivebox.config.common import redact_sensitive_config

        return {
            "type": "Crawl",
            "schema_version": VERSION,
            "id": str(self.id),
            "urls": self.urls,
            "status": self.status,
            "max_depth": self.max_depth,
            "config": redact_sensitive_config(self.config),
            "tags_str": self.tags_str,
            "label": self.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "created_by": self.created_by.username,
        }

    @staticmethod
    def from_json(record: dict, overrides: dict | None = None):
        """
        Create or get a Crawl from a JSON dict.

        Args:
            record: Dict with 'urls' (required), optional 'max_depth', 'tags_str', 'label'
            overrides: Dict of field overrides (e.g., created_by_id)

        Returns:
            Crawl instance or None if invalid
        """
        from django.utils import timezone

        overrides = overrides or {}

        # Check if crawl already exists by ID
        crawl_id = record.get("id")
        if crawl_id:
            try:
                return Crawl.objects.get(id=crawl_id)
            except Crawl.DoesNotExist:
                pass

        # Get URLs - can be string (newline-separated) or from 'url' field
        urls = record.get("urls", "")
        if not urls and record.get("url"):
            urls = record["url"]

        if not urls:
            return None

        # Create new crawl (status stays QUEUED, not started)
        crawl = Crawl.objects.create(
            urls=urls,
            max_depth=record.get("max_depth", record.get("depth", 0)),
            config=record.get("config") or {},
            tags_str=record.get("tags_str", record.get("tags", "")),
            label=record.get("label", ""),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=timezone.now(),
            **overrides,
        )
        return crawl

    @property
    def output_dir(self) -> Path:
        from archivebox.config import CONSTANTS
        from archivebox.core.models import Snapshot

        date_str = self.created_at.strftime("%Y%m%d")
        first_url = next((url for url in self.get_urls_list() if url), "")
        domain = Snapshot.extract_domain_from_url(first_url) if first_url else "unknown"

        output_dir = CONSTANTS.USERS_DIR / self.created_by.username / CONSTANTS.CRAWLS_DIR_NAME / date_str / domain / str(self.id)
        hyphen_dir = output_dir.with_name(str(uuid.UUID(hex=self.id.hex)))
        return output_dir if output_dir.exists() or not hyphen_dir.exists() else hyphen_dir

    def get_urls_list(self) -> list[str]:
        """Get list of URLs from urls field, filtering out comments and empty lines."""
        if not self.urls:
            return []
        return [url for _raw_line, url in self._iter_url_lines() if url]

    @staticmethod
    def normalize_domain(value: str) -> str:
        candidate = (value or "").strip().lower()
        if not candidate:
            return ""
        if "://" not in candidate and "/" not in candidate:
            candidate = f"https://{candidate.lstrip('.')}"
        try:
            parsed = urlparse(candidate)
            hostname = parsed.hostname or ""
            if not hostname:
                return ""
            if parsed.port:
                return f"{hostname}_{parsed.port}"
            return hostname
        except Exception:
            return ""

    @staticmethod
    def split_filter_patterns(value) -> list[str]:
        patterns = []
        seen = set()
        if isinstance(value, list):
            raw_values = value
        elif isinstance(value, str):
            raw_values = value.splitlines()
        else:
            raw_values = []

        for raw_value in raw_values:
            pattern = str(raw_value or "").strip()
            if not pattern or pattern in seen:
                continue
            seen.add(pattern)
            patterns.append(pattern)
        return patterns

    @classmethod
    def _pattern_matches_url(cls, url: str, pattern: str) -> bool:
        normalized_pattern = str(pattern or "").strip()
        if not normalized_pattern:
            return False

        if re.fullmatch(r"[\w.*:-]+", normalized_pattern):
            wildcard_only_subdomains = normalized_pattern.startswith("*.")
            normalized_domain = cls.normalize_domain(
                normalized_pattern[2:] if wildcard_only_subdomains else normalized_pattern,
            )
            normalized_url_domain = cls.normalize_domain(url)
            if not normalized_domain or not normalized_url_domain:
                return False

            pattern_host = normalized_domain.split("_", 1)[0]
            url_host = normalized_url_domain.split("_", 1)[0]

            if wildcard_only_subdomains:
                return url_host.endswith(f".{pattern_host}")

            if normalized_url_domain == normalized_domain:
                return True
            return url_host == pattern_host or url_host.endswith(f".{pattern_host}")

        try:
            return bool(re.search(normalized_pattern, url))
        except re.error:
            return False

    def get_current_config(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh and self.pk:
            config = type(self).objects.filter(pk=self.pk).values_list("config", flat=True).first()
            if config is not None:
                self.config = config
        return dict(self.config or {})

    def get_url_allowlist(self, *, use_effective_config: bool = False, snapshot=None) -> list[str]:
        if use_effective_config:
            config = self.get_current_config(refresh=True)
        else:
            config = self.get_current_config()
        if snapshot is not None and snapshot.config:
            config.update(snapshot.config)
        return self.split_filter_patterns(config.get("URL_ALLOWLIST", ""))

    def get_url_denylist(self, *, use_effective_config: bool = False, snapshot=None) -> list[str]:
        if use_effective_config:
            config = self.get_current_config(refresh=True)
        else:
            config = self.get_current_config()
        if snapshot is not None and snapshot.config:
            config.update(snapshot.config)
        return self.split_filter_patterns(config.get("URL_DENYLIST", ""))

    def url_passes_filters(self, url: str, *, snapshot=None, use_effective_config: bool = True) -> bool:
        denylist = self.get_url_denylist(use_effective_config=use_effective_config, snapshot=snapshot)
        allowlist = self.get_url_allowlist(use_effective_config=use_effective_config, snapshot=snapshot)
        return self.url_passes_compiled_filters(url, allowlist=allowlist, denylist=denylist)

    def url_passes_compiled_filters(self, url: str, *, allowlist: list[str], denylist: list[str]) -> bool:
        for pattern in denylist:
            if self._pattern_matches_url(url, pattern):
                return False

        if allowlist:
            return any(self._pattern_matches_url(url, pattern) for pattern in allowlist)

        return True

    def set_url_filters(self, allowlist, denylist) -> None:
        config = dict(self.config or {})
        allow_patterns = self.split_filter_patterns(allowlist)
        deny_patterns = self.split_filter_patterns(denylist)

        if allow_patterns:
            config["URL_ALLOWLIST"] = "\n".join(allow_patterns)
        else:
            config.pop("URL_ALLOWLIST", None)

        if deny_patterns:
            config["URL_DENYLIST"] = "\n".join(deny_patterns)
        else:
            config.pop("URL_DENYLIST", None)

        self.config = config

    def apply_crawl_config_filters(self) -> dict[str, int]:
        from archivebox.core.models import Snapshot

        removed_urls = self.prune_urls(
            lambda url: not self.url_passes_filters(url, use_effective_config=False),
        )

        filtered_snapshots = [
            snapshot
            for snapshot in self.snapshot_set.filter(
                status__in=[
                    Snapshot.StatusChoices.QUEUED,
                    Snapshot.StatusChoices.STARTED,
                    Snapshot.StatusChoices.PAUSED,
                ],
            ).only("pk", "url", "status")
            if not self.url_passes_filters(snapshot.url, snapshot=snapshot, use_effective_config=False)
        ]

        deleted_snapshots = 0
        if filtered_snapshots:
            started_snapshots = [snapshot for snapshot in filtered_snapshots if snapshot.status == Snapshot.StatusChoices.STARTED]
            for snapshot in started_snapshots:
                snapshot.cancel_running_hooks()

            filtered_snapshot_ids = [snapshot.pk for snapshot in filtered_snapshots]
            deleted_snapshots, _ = self.snapshot_set.filter(pk__in=filtered_snapshot_ids).delete()

        return {
            "removed_urls": len(removed_urls),
            "deleted_snapshots": deleted_snapshots,
        }

    def _iter_url_lines(self) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for raw_line in (self.urls or "").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                entries.append((raw_line.rstrip(), ""))
                continue
            try:
                entry = json.loads(stripped)
                # Crawl.urls accepts plain lines and JSONL URL records. Other
                # valid JSON values, e.g. a quoted string from hostile input,
                # are not records and must stay inert text instead of raising
                # during later Snapshot.save() bookkeeping.
                if isinstance(entry, dict):
                    entries.append((raw_line.rstrip(), str(entry.get("url", "") or "").strip()))
                else:
                    entries.append((raw_line.rstrip(), stripped))
            except json.JSONDecodeError:
                entries.append((raw_line.rstrip(), stripped))
        return entries

    def count_urls_for_limit(self) -> int:
        """
        Count unique URLs already queued or snapshotted for this crawl.

        max_urls is a crawl-wide cap on snapshots, so direct URL entries and
        recursively discovered snapshots both have to consume the same budget.
        """
        from archivebox.misc.util import fix_url_from_markdown, sanitize_extracted_url

        urls = set(self.snapshot_set.values_list("url", flat=True))
        for _raw_line, raw_url in self._iter_url_lines():
            url = sanitize_extracted_url(fix_url_from_markdown(str(raw_url or "").strip()))
            if url:
                urls.add(url)
        return len(urls)

    def remaining_url_capacity(self) -> int | None:
        max_urls = int(self._config_value(self.get_current_config(refresh=True), "CRAWL_MAX_URLS", 0) or 0)
        if max_urls <= 0:
            return None
        return max(max_urls - self.count_urls_for_limit(), 0)

    def has_remaining_url_capacity(self) -> bool:
        remaining = self.remaining_url_capacity()
        return remaining is None or remaining > 0

    def remaining_snapshot_capacity(self) -> int | None:
        max_urls = int(self._config_value(self.get_current_config(refresh=True), "CRAWL_MAX_URLS", 0) or 0)
        if max_urls <= 0:
            return None
        return max(max_urls - self.snapshot_set.count(), 0)

    def has_remaining_snapshot_capacity(self) -> bool:
        remaining = self.remaining_snapshot_capacity()
        return remaining is None or remaining > 0

    def prune_urls(self, predicate) -> list[str]:
        kept_lines: list[str] = []
        removed_urls: list[str] = []

        for raw_line, url in self._iter_url_lines():
            if not url:
                kept_lines.append(raw_line)
                continue
            if predicate(url):
                removed_urls.append(url)
                continue
            kept_lines.append(raw_line)

        next_urls = "\n".join(kept_lines)
        if next_urls != (self.urls or ""):
            self.urls = next_urls
            self.save(update_fields=["urls", "modified_at"])
        return removed_urls

    def prune_url(self, url: str) -> int:
        target = (url or "").strip()
        removed = self.prune_urls(lambda candidate: candidate == target)
        return len(removed)

    def exclude_domain(self, domain: str) -> dict[str, int | str | bool]:
        normalized_domain = self.normalize_domain(domain)
        if not normalized_domain:
            return {
                "domain": "",
                "created": False,
                "removed_urls": 0,
                "deleted_snapshots": 0,
            }

        domains = self.get_url_denylist(use_effective_config=False)
        created = normalized_domain not in domains
        if created:
            domains.append(normalized_domain)
            self.set_url_filters(
                self.get_url_allowlist(use_effective_config=False),
                domains,
            )
            self.save(update_fields=["config", "modified_at"])

        filter_result = self.apply_crawl_config_filters()

        return {
            "domain": normalized_domain,
            "created": created,
            "removed_urls": filter_result["removed_urls"],
            "deleted_snapshots": filter_result["deleted_snapshots"],
        }

    def resolve_persona(self):
        from archivebox.personas.models import Persona

        if self.persona_id:
            return Persona.objects.filter(id=self.persona_id).first()

        return None

    @staticmethod
    def _config_value(config: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
        if isinstance(config, Mapping):
            return config.get(key, default)
        return config[key] if key in config else default

    @classmethod
    def create_scheduler_row(cls, **kwargs) -> "Crawl":
        from archivebox.base_models.models import normalize_config_json_values
        from archivebox.config.common import build_crawl_config_snapshot

        now = timezone.now()
        kwargs.setdefault("created_at", now)
        kwargs.setdefault("modified_at", now)
        config = normalize_config_json_values(kwargs.get("config") or {})
        persona = kwargs.get("persona")
        if persona is None and kwargs.get("persona_id"):
            from archivebox.personas.models import Persona

            persona = Persona.objects.filter(pk=kwargs["persona_id"]).first()
        kwargs["config"] = build_crawl_config_snapshot(persona=persona, overrides=config)
        crawl = cls(**kwargs)
        if crawl.delete_at is None:
            crawl.set_delete_at_from_config()
        cls.objects.bulk_create([crawl])
        return crawl

    def limit_stop_reason(
        self,
        *,
        config: Mapping[str, Any] | Any | None = None,
        output_dir: Path | None = None,
        num_snapshots: int | None = None,
    ) -> str:
        from abx_dl.limits import CrawlLimitState

        if output_dir is None:
            output_dir = self.output_dir
        if config is None:
            from archivebox.config.common import get_config

            config = get_config(crawl=self, include_machine=False).for_crawl_runtime(
                crawl=self,
                persona=self.resolve_persona(),
                crawl_output_dir=output_dir,
            )

        limits_path = output_dir / ".abx-dl" / "limits.json"
        if limits_path.exists():
            stop_reason = CrawlLimitState.from_config(config).get_stop_reason()
            if stop_reason:
                return stop_reason

        max_urls = int(self._config_value(config, "CRAWL_MAX_URLS", 0) or 0)
        if num_snapshots is None:
            num_snapshots = self.snapshot_set.count()
        if max_urls > 0 and num_snapshots >= max_urls and self.count_urls_for_limit() >= max_urls:
            return "crawl_max_urls"

        return ""

    def lifecycle_stop_reason(self, *, num_snapshots: int | None = None, num_sealed_snapshots: int | None = None) -> str:
        if self.is_paused:
            return "paused"

        if self.status != self.StatusChoices.SEALED:
            return ""

        if num_snapshots is None:
            num_snapshots = self.snapshot_set.count()
        if num_snapshots == 0:
            return "no_viable_urls"

        if num_sealed_snapshots is None:
            from archivebox.core.models import Snapshot

            num_sealed_snapshots = self.snapshot_set.filter(status=Snapshot.StatusChoices.SEALED).count()
        if num_sealed_snapshots >= num_snapshots:
            return "done"

        return ""

    def stop_reason(
        self,
        *,
        config: Mapping[str, Any] | Any | None = None,
        output_dir: Path | None = None,
        num_snapshots: int | None = None,
        num_sealed_snapshots: int | None = None,
    ) -> str:
        return self.limit_stop_reason(config=config, output_dir=output_dir, num_snapshots=num_snapshots) or self.lifecycle_stop_reason(
            num_snapshots=num_snapshots,
            num_sealed_snapshots=num_sealed_snapshots,
        )

    def add_url(self, entry: dict) -> bool:
        """
        Add a URL to the crawl queue if not already present.

        Args:
            entry: dict with 'url', optional 'depth', 'title', 'timestamp', 'tags', 'via_snapshot', 'plugin'

        Returns:
            True if URL was added, False if skipped (duplicate or depth exceeded)
        """
        from archivebox.misc.util import fix_url_from_markdown, sanitize_extracted_url

        url = sanitize_extracted_url(fix_url_from_markdown(str(entry.get("url", "") or "").strip()))
        if not url:
            return False
        try:
            validate_url_length(url)
        except ValueError:
            return False
        if not self.url_passes_filters(url):
            return False

        depth = entry.get("depth", 1)

        # Skip if depth exceeds max_depth
        if depth > self.max_depth:
            return False

        # Skip if already a Snapshot for this crawl
        if self.snapshot_set.filter(url=url).exists():
            return False

        # Check if already in urls (parse existing JSONL entries)
        existing_urls = {url for _raw_line, url in self._iter_url_lines() if url}

        if url in existing_urls:
            return False

        if not self.has_remaining_url_capacity():
            return False

        # Append as JSONL
        entry = {**entry, "url": url}
        jsonl_entry = json.dumps(entry)
        self.urls = (self.urls.rstrip() + "\n" + jsonl_entry).lstrip("\n")
        self.save(update_fields=["urls", "modified_at"])
        return True

    def create_snapshots_from_urls(self) -> list["Snapshot"]:
        """
        Create Snapshot objects for each URL in self.urls that doesn't already exist.

        Returns:
            List of newly created Snapshot objects
        """
        from archivebox.core.models import Snapshot, Tag
        from archivebox.misc.util import fix_url_from_markdown, sanitize_extracted_url

        if self.status == self.StatusChoices.SEALED:
            return []
        created_snapshots = []
        crawl_tag_names = self.current_tag_names()
        tags_by_name: dict[str, Tag] = {}

        for line in self.urls.splitlines():
            if not line.strip():
                continue
            config = self.get_current_config(refresh=True)
            only_new_urls = bool(self._config_value(config, "ONLY_NEW", True))

            # Parse JSONL or plain URL
            try:
                entry = json.loads(line)
                snapshot_id = entry.get("id") or entry.get("snapshot_id")
                url = sanitize_extracted_url(fix_url_from_markdown(str(entry.get("url", "") or "").strip()))
                depth = entry.get("depth", 0)
                title = entry.get("title")
                timestamp = entry.get("timestamp")
                tag_names = [*crawl_tag_names, *self.parse_tag_names(entry.get("tags", ""))]
            except json.JSONDecodeError:
                snapshot_id = None
                url = sanitize_extracted_url(fix_url_from_markdown(line.strip()))
                depth = 0
                title = None
                timestamp = None
                tag_names = crawl_tag_names

            if not url:
                continue
            try:
                validate_url(url)
            except ValueError as err:
                print(f"[yellow][!] Skipping invalid snapshot URL: {url[:120]}... ({err})[/yellow]")
                continue
            if Snapshot.is_archivebox_internal_url(url, config=config):
                print(f"[yellow][!] Skipping internal ArchiveBox snapshot URL: {url}[/yellow]")
                continue
            if not self.url_passes_filters(url, use_effective_config=False):
                continue
            if only_new_urls and Snapshot.objects.filter(url=url).exists():
                continue

            # Skip if depth exceeds max_depth
            if depth > self.max_depth:
                continue

            # Stop creating new snapshots once the crawl-wide URL cap is reached.
            if not self.has_remaining_snapshot_capacity():
                break

            defaults = {
                "depth": depth,
                "title": title,
                "timestamp": timestamp or str(timezone.now().timestamp()),
                "status": Snapshot.INITIAL_STATE,
                "retry_at": timezone.now(),
                # Note: created_by removed in 0.9.0 - Snapshot inherits from Crawl
            }
            try:
                # Intentionally avoid get_or_create/update_or_create here:
                # Django wraps those helpers in atomic(), and Snapshot.save() schedules
                # filesystem/crawl maintenance callbacks. Keeping this as explicit
                # read-then-save lets SQLite commit each write immediately unless the
                # caller deliberately wrapped us in transaction.atomic().
                if snapshot_id:
                    snapshot = Snapshot.objects.filter(id=snapshot_id).first()
                    if snapshot:
                        created = False
                        for field, value in {
                            **defaults,
                            "url": url,
                            "crawl": self,
                        }.items():
                            setattr(snapshot, field, value)
                        snapshot.save(update_fields=["depth", "title", "timestamp", "status", "retry_at", "url", "crawl", "modified_at"])
                    else:
                        snapshot = Snapshot(id=snapshot_id, url=url, crawl=self, **defaults)
                        snapshot.save()
                        created = True
                else:
                    snapshot = Snapshot.objects.filter(url=url, crawl=self).first()
                    if snapshot:
                        created = False
                    else:
                        try:
                            snapshot = Snapshot(url=url, crawl=self, **defaults)
                            snapshot.save()
                            created = True
                        except IntegrityError:
                            snapshot = Snapshot.objects.get(url=url, crawl=self)
                            created = False
            except ValidationError as err:
                print(f"[yellow][!] Skipping blocked snapshot URL: {url} ({err})[/yellow]")
                continue

            if created:
                created_snapshots.append(snapshot)
            if tag_names:
                missing_names = [tag_name for tag_name in tag_names if tag_name not in tags_by_name]
                if missing_names:
                    tags_by_name.update({tag.name: tag for tag in Tag.objects.filter(name__in=missing_names)})
                    missing_tags = [Tag(name=tag_name) for tag_name in missing_names if tag_name not in tags_by_name]
                    if missing_tags:
                        # Create tag rows in bulk, then attach through the M2M
                        # relation without clearing any non-crawl snapshot tags.
                        Tag.objects.bulk_create(missing_tags, ignore_conflicts=True)
                        tags_by_name.update({tag.name: tag for tag in Tag.objects.filter(name__in=missing_names)})
                snapshot.add_tag_ids([tag.pk for tag_name in tag_names if (tag := tags_by_name.get(tag_name))])

            # Symlink creation touches the filesystem and can be slow on remote disks.
            # Defer it until after any active DB transaction commits so SQLite does
            # not hold a write lock while mkdir/symlink work runs.
            transaction.on_commit(lambda snapshot=snapshot: snapshot.ensure_crawl_symlink())

        return created_snapshots

    def create_discovered_snapshot(
        self,
        parent_snapshot,
        *,
        url: str,
        depth: int,
        title: str = "",
        tags: str = "",
        created_by_id: int | None = None,
    ):
        """Create one child snapshot if it passes crawl filters and limits."""
        snapshots = self.create_discovered_snapshots(
            parent_snapshot,
            [{"url": url, "title": title, "tags": tags}],
            depth=depth,
            created_by_id=created_by_id,
        )
        return snapshots[0] if snapshots else None

    def create_discovered_snapshots(
        self,
        parent_snapshot,
        records: Iterable[Mapping[str, Any]],
        *,
        depth: int,
        created_by_id: int | None = None,
    ) -> list["Snapshot"]:
        """Create child snapshots from discovered URL records after filtering and deduping once."""
        from archivebox.core.models import Snapshot, SnapshotTag, Tag
        from archivebox.misc.util import fix_url_from_markdown, sanitize_extracted_url

        if self.status == self.StatusChoices.SEALED:
            return []

        if depth > self.max_depth:
            return []

        crawl_tag_names = self.current_tag_names()
        config = self.get_current_config(refresh=True)
        if parent_snapshot is not None and parent_snapshot.config:
            config.update(parent_snapshot.config)
        allowlist = self.split_filter_patterns(config.get("URL_ALLOWLIST", ""))
        denylist = self.split_filter_patterns(config.get("URL_DENYLIST", ""))

        def metadata_score(record: Mapping[str, Any]) -> int:
            # Multiple parsers can discover the same URL from one import root.
            # Keep the record with the richest user-facing metadata so generic
            # text/HTML extraction does not erase RSS/Netscape/JSON fields.
            return sum(bool(record.get(field)) for field in ("title", "bookmarked_at", "timestamp", "tags"))

        deduped_records: dict[str, Mapping[str, Any]] = {}
        for record in records:
            url = sanitize_extracted_url(fix_url_from_markdown(str(record.get("url") or "").strip()))
            if not url:
                continue
            try:
                validate_url(url)
            except ValueError as err:
                print(f"[yellow][!] Skipping invalid discovered snapshot URL: {url[:120]}... ({err})[/yellow]")
                continue
            if Snapshot.is_archivebox_internal_url(url, config=config):
                print(f"[yellow][!] Skipping internal ArchiveBox discovered snapshot URL: {url}[/yellow]")
                continue
            if self.url_passes_compiled_filters(url, allowlist=allowlist, denylist=denylist):
                existing_record = deduped_records.get(url)
                if existing_record is None or metadata_score(record) > metadata_score(existing_record):
                    deduped_records[url] = record

        if not deduped_records:
            return []

        existing_in_crawl = {
            snapshot.url: snapshot for snapshot in self.snapshot_set.prefetch_related("tags").filter(url__in=deduped_records.keys())
        }
        for url, snapshot in existing_in_crawl.items():
            record = deduped_records[url]
            update_fields = []
            title = Snapshot._normalize_title_candidate(str(record.get("title") or "").strip()[:512], snapshot_url=url)
            if title and (not snapshot.title or len(title) > len(snapshot.title or "")):
                snapshot.title = title
                update_fields.append("title")
            bookmarked_at = None
            try:
                bookmarked_at = parse_date(record.get("bookmarked_at") or record.get("timestamp"))
            except (TypeError, ValueError, OSError):
                pass
            if bookmarked_at and snapshot.bookmarked_at != bookmarked_at:
                snapshot.bookmarked_at = bookmarked_at
                update_fields.append("bookmarked_at")
            if update_fields:
                snapshot.save(update_fields=[*update_fields, "modified_at"])
            tag_names = {
                *crawl_tag_names,
                *self.parse_tag_names(
                    str(record.get("tags") or ""),
                    pattern=self._config_value(config, "TAG_SEPARATOR_PATTERN", r"[,]"),
                ),
            }
            if tag_names:
                tag_ids = [Tag.get_or_create_by_name(tag_name)[0].pk for tag_name in tag_names]
                snapshot.add_tag_ids(tag_ids)

        existing_scope = Snapshot.objects if bool(self._config_value(config, "ONLY_NEW", True)) else self.snapshot_set
        existing_urls = set(existing_scope.filter(url__in=deduped_records.keys()).values_list("url", flat=True))
        urls = [url for url in deduped_records.keys() if url not in existing_urls]
        remaining = self.remaining_snapshot_capacity()
        if remaining is not None:
            urls = urls[:remaining]
        if not urls:
            return []

        now = timezone.now()
        snapshots = []
        for index, url in enumerate(urls):
            record = deduped_records[url]
            bookmarked_at = now
            try:
                bookmarked_at = parse_date(record.get("bookmarked_at") or record.get("timestamp")) or now
            except (TypeError, ValueError, OSError):
                pass
            snapshots.append(
                Snapshot(
                    url=url,
                    timestamp=str((now + timedelta(microseconds=index)).timestamp()),
                    title=Snapshot._normalize_title_candidate(
                        str(record.get("title") or "").strip()[:512],
                        snapshot_url=url,
                    )
                    or None,
                    crawl=self,
                    parent_snapshot=parent_snapshot,
                    depth=depth,
                    status=Snapshot.StatusChoices.QUEUED,
                    retry_at=now,
                    bookmarked_at=bookmarked_at,
                    created_at=now,
                ),
            )
        for snapshot in snapshots:
            snapshot.set_delete_at_from_config(self._config_value(config, "DELETE_AFTER", "0"))

        created_snapshots = []
        for snapshot in snapshots:
            try:
                # Snapshot.save() owns URL validation and filesystem/index side
                # effects. Do not use bulk_create() here; it bypasses save().
                snapshot.save()
            except IntegrityError:
                continue
            except ValidationError as err:
                print(f"[yellow][!] Skipping blocked discovered snapshot URL: {snapshot.url} ({err})[/yellow]")
                continue
            created_snapshots.append(snapshot)
        if not created_snapshots:
            return []

        tag_names_by_url: dict[str, set[str]] = {}
        for snapshot in created_snapshots:
            tag_names = {
                *crawl_tag_names,
                *self.parse_tag_names(
                    str(deduped_records[snapshot.url].get("tags") or ""),
                    pattern=self._config_value(config, "TAG_SEPARATOR_PATTERN", r"[,]"),
                ),
            }
            if tag_names:
                tag_names_by_url[snapshot.url] = tag_names
            # Snapshot.save() handles model-level validation. The crawl symlink
            # can still wait until after commit so SQLite does not hold a write
            # lock while touching the filesystem.
            transaction.on_commit(lambda snapshot=snapshot: snapshot.ensure_crawl_symlink())

        tag_names = {tag for tags in tag_names_by_url.values() for tag in tags}
        if tag_names:
            tags_by_name = {tag.name: tag for tag in Tag.objects.filter(name__in=tag_names)}
            missing_tags = [Tag(name=name) for name in sorted(tag_names - tags_by_name.keys())]
            if missing_tags:
                Tag.objects.bulk_create(missing_tags, ignore_conflicts=True)
                tags_by_name = {tag.name: tag for tag in Tag.objects.filter(name__in=tag_names)}
            SnapshotTag.objects.bulk_create(
                [
                    SnapshotTag(snapshot=snapshot, tag=tags_by_name[tag_name])
                    for snapshot in created_snapshots
                    for tag_name in tag_names_by_url.get(snapshot.url, set())
                    if tag_name in tags_by_name
                ],
                ignore_conflicts=True,
            )

        return created_snapshots

    def is_finished(self) -> bool:
        """Check if crawl is finished (all snapshots sealed or no snapshots exist)."""
        from archivebox.core.models import Snapshot

        # Check if any snapshots exist for this crawl
        snapshots = Snapshot.objects.filter(crawl=self)

        # If no snapshots exist, allow finishing (e.g., system crawls that only run setup hooks)
        if not snapshots.exists():
            return True

        # If snapshots exist, check if all are sealed
        if snapshots.filter(
            status__in=[
                Snapshot.StatusChoices.QUEUED,
                Snapshot.StatusChoices.STARTED,
                Snapshot.StatusChoices.PAUSED,
            ],
        ).exists():
            return False

        return True

    def can_start(self) -> bool:
        return bool(self.urls and self.get_urls_list())

    def has_finished_snapshots(self) -> bool:
        from archivebox.core.models import Snapshot

        snapshots = self.snapshot_set.all()
        return snapshots.exists() and not snapshots.exclude(status=Snapshot.StatusChoices.SEALED).exists()

    def mark_started(self) -> bool:
        now = timezone.now()
        updated = self.safe_update(
            {
                "status": self.StatusChoices.STARTED,
                "retry_at": now + timedelta(seconds=2),
            },
            extra_filter={"status": self.StatusChoices.QUEUED},
        )
        return updated

    def seal(self) -> bool:
        """Finalize a runner-owned Crawl without dispatching hooks directly."""
        now = timezone.now()
        updated = self.safe_update(
            {
                "status": self.StatusChoices.SEALED,
                "retry_at": None,
                "modified_at": now,
            },
            refresh=False,
            extra_filter={"status__in": (*self.RUNNABLE_STATES, self.StatusChoices.SEALED)},
        )
        if not updated:
            self.refresh_from_db()
            return False
        self.status = self.StatusChoices.SEALED
        self.retry_at = None
        self.modified_at = now
        self.schedule_child_snapshots_for_sealing()
        self.cleanup_runtime()
        return True

    def advance_lifecycle(self) -> bool:
        """Advance one explicit lifecycle step after the runner claims this row."""
        if self.status == self.StatusChoices.PAUSED:
            return False
        if self.status == self.StatusChoices.QUEUED:
            if self.has_finished_snapshots():
                return self.seal()
            if not self.can_start():
                return False
            return self.mark_started()
        if self.status == self.StatusChoices.STARTED and self.is_finished():
            return self.seal()
        return False

    def cleanup_runtime(self) -> None:
        """Remove runner-owned runtime artifacts after abx-dl cleanup hooks finish."""
        if self.output_dir.exists():
            for pid_file in self.output_dir.glob("**/*.pid"):
                pid_file.unlink(missing_ok=True)

        persona = self.resolve_persona()
        if persona:
            persona.cleanup_runtime_for_crawl(self)
