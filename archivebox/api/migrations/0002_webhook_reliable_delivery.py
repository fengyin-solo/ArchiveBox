# Generated for reliable outbound webhook delivery (delivery + attempt records, signing secret)

import secrets

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

import archivebox.api.models
from archivebox.uuid_compat import uuid7


def backfill_webhook_secrets(apps, schema_editor):
    """Give every pre-existing webhook a unique random signing secret."""
    OutboundWebhook = apps.get_model("api", "OutboundWebhook")
    for hook in OutboundWebhook.objects.all().iterator():
        if not hook.secret:
            OutboundWebhook.objects.filter(pk=hook.pk).update(secret=secrets.token_hex(32))


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="outboundwebhook",
            name="secret",
            field=models.CharField(
                default=archivebox.api.models.generate_webhook_secret,
                help_text=(
                    "Shared HMAC-SHA256 secret. Every delivery is signed with an "
                    "X-ArchiveBox-Webhook-Signature header over '<timestamp>.<raw body>'; "
                    "give this value to the receiving endpoint so it can verify the request came from this instance."
                ),
                max_length=64,
                verbose_name="signing secret",
            ),
        ),
        migrations.RunPython(backfill_webhook_secrets, reverse_code=noop_reverse),
        migrations.CreateModel(
            name="WebhookDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid7, editable=False, primary_key=True, serialize=False, unique=True)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("modified_at", models.DateTimeField(auto_now=True)),
                (
                    "webhook_name",
                    models.CharField(blank=True, default="", max_length=255, verbose_name="webhook name"),
                ),
                ("endpoint", models.URLField(max_length=2048, verbose_name="endpoint")),
                ("event", models.CharField(db_index=True, max_length=32, verbose_name="event")),
                (
                    "ref",
                    models.CharField(blank=True, default="", max_length=1024, verbose_name="referenced model"),
                ),
                ("object_id", models.CharField(blank=True, default="", max_length=255, verbose_name="object id")),
                ("payload", models.JSONField(default=dict, verbose_name="payload")),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")],
                        db_index=True,
                        default="pending",
                        max_length=16,
                        verbose_name="status",
                    ),
                ),
                ("num_attempts", models.PositiveIntegerField(default=0, verbose_name="attempts")),
                (
                    "last_status_code",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="last status code"),
                ),
                (
                    "last_error",
                    models.CharField(blank=True, default="", max_length=8000, verbose_name="last error"),
                ),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=8000, verbose_name="failure reason"),
                ),
                (
                    "succeeded_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="succeeded at"),
                ),
                (
                    "finished_at",
                    models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="finished at"),
                ),
                (
                    "webhook",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="deliveries",
                        to="api.outboundwebhook",
                        verbose_name="webhook",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook Delivery",
                "verbose_name_plural": "Webhook Deliveries",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["status", "created_at"], name="api_wd_status_created_idx"),
                    models.Index(fields=["webhook", "created_at"], name="api_wd_webhook_created_idx"),
                ],
                "app_label": "api",
            },
        ),
        migrations.CreateModel(
            name="WebhookDeliveryAttempt",
            fields=[
                ("id", models.UUIDField(default=uuid7, editable=False, primary_key=True, serialize=False, unique=True)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("attempt_number", models.PositiveIntegerField(verbose_name="attempt number")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("succeeded", "Succeeded"),
                            ("retryable", "Retryable failure"),
                            ("permanent", "Permanent failure"),
                        ],
                        db_index=True,
                        max_length=16,
                        verbose_name="status",
                    ),
                ),
                (
                    "status_code",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="status code"),
                ),
                ("duration_ms", models.PositiveIntegerField(default=0, verbose_name="duration (ms)")),
                (
                    "error",
                    models.CharField(blank=True, default="", max_length=8000, verbose_name="error summary"),
                ),
                (
                    "delivery",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attempts",
                        to="api.webhookdelivery",
                        verbose_name="delivery",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook Delivery Attempt",
                "verbose_name_plural": "Webhook Delivery Attempts",
                "ordering": ["delivery_id", "attempt_number"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("delivery", "attempt_number"),
                        name="api_wda_delivery_attempt_uniq",
                    ),
                ],
                "app_label": "api",
            },
        ),
    ]
