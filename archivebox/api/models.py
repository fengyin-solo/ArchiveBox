__package__ = "archivebox.api"

import secrets
from archivebox.uuid_compat import CompactUUIDField, uuid7

from django.conf import settings
from django.db import models
from django.utils import timezone
from django_stubs_ext.db.models import TypedModelMeta
from signal_webhooks.models import WebhookBase

from archivebox.base_models.models import get_or_create_system_user_pk


def generate_secret_token() -> str:
    return secrets.token_hex(16)


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


class APIToken(models.Model):
    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)
    token = models.CharField(max_length=32, default=generate_secret_token, unique=True)
    expires = models.DateTimeField(null=True, blank=True)

    class Meta(TypedModelMeta):
        app_label = "api"
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self) -> str:
        return self.token

    @property
    def token_redacted(self):
        return f"************{self.token[-4:]}"

    def is_valid(self, for_date=None):
        return not self.expires or self.expires >= (for_date or timezone.now())


class OutboundWebhook(WebhookBase):
    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)

    secret = models.CharField(
        max_length=64,
        default=generate_webhook_secret,
        verbose_name="signing secret",
        help_text=(
            "Shared HMAC-SHA256 secret. Every delivery is signed with an "
            "X-ArchiveBox-Webhook-Signature header over '<timestamp>.<raw body>'; "
            "give this value to the receiving endpoint so it can verify the request came from this instance."
        ),
    )

    class Meta(WebhookBase.Meta):
        app_label = "api"
        verbose_name = "API Outbound Webhook"

    def __str__(self) -> str:
        return f"[{self.id}] {self.ref} -> {self.endpoint}"


class WebhookDelivery(models.Model):
    """One logical delivery of one model event to one webhook endpoint.

    Attempt rows (``WebhookDeliveryAttempt``) are append-only: retries and
    admin-triggered replays add new attempts but never modify past attempts.
    """

    class StatusChoices(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)

    webhook = models.ForeignKey(
        "api.OutboundWebhook",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
        verbose_name="webhook",
    )
    # Snapshotted so the audit trail survives deletion of the endpoint config.
    webhook_name = models.CharField(max_length=255, blank=True, default="", verbose_name="webhook name")
    endpoint = models.URLField(max_length=2048, verbose_name="endpoint")

    event = models.CharField(max_length=32, db_index=True, verbose_name="event")
    ref = models.CharField(max_length=1024, blank=True, default="", verbose_name="referenced model")
    object_id = models.CharField(max_length=255, blank=True, default="", verbose_name="object id")
    payload = models.JSONField(default=dict, verbose_name="payload")

    status = models.CharField(
        max_length=16,
        choices=StatusChoices.choices,
        default=StatusChoices.PENDING,
        db_index=True,
        verbose_name="status",
    )
    num_attempts = models.PositiveIntegerField(default=0, verbose_name="attempts")
    last_status_code = models.PositiveIntegerField(null=True, blank=True, verbose_name="last status code")
    last_error = models.CharField(max_length=8000, blank=True, default="", verbose_name="last error")
    failure_reason = models.CharField(max_length=8000, blank=True, default="", verbose_name="failure reason")

    succeeded_at = models.DateTimeField(null=True, blank=True, verbose_name="succeeded at")
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="finished at")

    class Meta(TypedModelMeta):
        app_label = "api"
        verbose_name = "Webhook Delivery"
        verbose_name_plural = "Webhook Deliveries"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="api_wd_status_created_idx"),
            models.Index(fields=["webhook", "created_at"], name="api_wd_webhook_created_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.id}] {self.event} {self.ref} -> {self.endpoint} ({self.status})"

    @property
    def is_final_failure(self) -> bool:
        return self.status == self.StatusChoices.FAILED


class WebhookDeliveryAttempt(models.Model):
    """A single HTTP attempt made for a ``WebhookDelivery`` (append-only)."""

    class StatusChoices(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        RETRYABLE = "retryable", "Retryable failure"
        PERMANENT = "permanent", "Permanent failure"

    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    delivery = models.ForeignKey(
        "api.WebhookDelivery",
        on_delete=models.CASCADE,
        related_name="attempts",
        verbose_name="delivery",
    )
    attempt_number = models.PositiveIntegerField(verbose_name="attempt number")
    status = models.CharField(
        max_length=16,
        choices=StatusChoices.choices,
        db_index=True,
        verbose_name="status",
    )
    status_code = models.PositiveIntegerField(null=True, blank=True, verbose_name="status code")
    duration_ms = models.PositiveIntegerField(default=0, verbose_name="duration (ms)")
    error = models.CharField(max_length=8000, blank=True, default="", verbose_name="error summary")

    class Meta(TypedModelMeta):
        app_label = "api"
        verbose_name = "Webhook Delivery Attempt"
        verbose_name_plural = "Webhook Delivery Attempts"
        ordering = ["delivery_id", "attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery", "attempt_number"],
                name="api_wda_delivery_attempt_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.delivery_id}] attempt {self.attempt_number}: {self.status}"
