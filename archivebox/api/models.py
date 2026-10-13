__package__ = "archivebox.api"

import secrets
from archivebox.uuid_compat import CompactUUIDField, uuid7

from django.conf import settings
from django.db import models
from django.utils import timezone
from django_stubs_ext.db.models import TypedModelMeta
from signal_webhooks.models import WebhookBase

from archivebox.base_models.models import get_or_create_system_user_pk


IDEMPOTENCY_STATUS_PENDING = "pending"
IDEMPOTENCY_STATUS_COMPLETED = "completed"
IDEMPOTENCY_STATUS_CHOICES = (
    (IDEMPOTENCY_STATUS_PENDING, "Pending"),
    (IDEMPOTENCY_STATUS_COMPLETED, "Completed"),
)


def generate_secret_token() -> str:
    return secrets.token_hex(16)


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


class IdempotencyKey(models.Model):
    """Server-side record backing the Idempotency-Key write API header.

    One row per (created_by, request_method, request_path, key) namespace.
    The first request inserts a ``pending`` row, executes the side effects,
    and stores the exact status/body for replays until ``expires_at``.
    """

    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        default=get_or_create_system_user_pk,
        null=False,
        db_index=True,
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)

    key = models.CharField(max_length=255)
    request_method = models.CharField(max_length=10)
    request_path = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)

    status = models.CharField(max_length=20, choices=IDEMPOTENCY_STATUS_CHOICES, default=IDEMPOTENCY_STATUS_PENDING)
    status_code = models.IntegerField(null=True, blank=True)
    response_content_type = models.CharField(max_length=255, default="", blank=True)
    response_body = models.TextField(default="", blank=True)

    locked_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta(TypedModelMeta):
        app_label = "api"
        verbose_name = "Idempotency Key"
        verbose_name_plural = "Idempotency Keys"
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "request_method", "request_path", "key"],
                name="unique_idempotency_key_per_creator_path",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.request_method} {self.request_path} [{self.key[:8]}] {self.status}"


class OutboundWebhook(WebhookBase):
    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta(WebhookBase.Meta):
        app_label = "api"
        verbose_name = "API Outbound Webhook"

    def __str__(self) -> str:
        return f"[{self.id}] {self.ref} -> {self.endpoint}"
