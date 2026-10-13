__package__ = "archivebox.api"

from django import forms
from django.contrib import admin, messages
from django.http import HttpRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.text import capfirst
from django_object_actions import action
from signal_webhooks.admin import WebhookAdmin, WebhookModelForm
from signal_webhooks.settings import webhook_settings
from signal_webhooks.utils import get_webhook_model, model_from_reference

from archivebox.base_models.admin import BaseModelAdmin

from archivebox.api.models import APIToken, WebhookDelivery, WebhookDeliveryAttempt
from archivebox.api.webhooks import replay_webhook_delivery


def _webhook_fields(*names: str) -> tuple[str, ...]:
    model_fields = {field.name for field in get_webhook_model()._meta.fields}
    return tuple(name for name in names if name in model_fields)


class APITokenAdmin(BaseModelAdmin):
    list_display = ("created_at", "id", "created_by", "token_redacted", "expires")
    sort_fields = ("id", "created_at", "created_by", "expires")
    readonly_fields = ("created_at", "modified_at")
    search_fields = ("id", "created_by__username", "token")

    fieldsets = (
        (
            "Token",
            {
                "fields": ("token", "expires"),
                "classes": ("card",),
            },
        ),
        (
            "Owner",
            {
                "fields": ("created_by",),
                "classes": ("card",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "modified_at"),
                "classes": ("card",),
            },
        ),
    )

    list_filter = ("created_by",)
    ordering = ["-created_at"]
    list_per_page = 100


class OutboundWebhookAdminForm(WebhookModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ref"] = forms.ChoiceField(
            label=self.fields["ref"].label,
            help_text=self.fields["ref"].help_text,
            choices=[
                (ref, f"{capfirst(model_from_reference(ref, check_hooks=False)._meta.verbose_name_plural)} ({ref})")
                for ref in sorted(webhook_settings.HOOKS)
            ],
        )


class CustomWebhookAdmin(WebhookAdmin, BaseModelAdmin):
    form = OutboundWebhookAdminForm
    list_display = ("created_at", "created_by", "id", *WebhookAdmin.list_display)
    sort_fields = _webhook_fields("created_at", "created_by", "id", "ref", "endpoint", "last_success", "last_failure")
    readonly_fields = _webhook_fields("created_at", "modified_at", *WebhookAdmin.readonly_fields)

    fieldsets = (
        (
            "Webhook",
            {
                "fields": _webhook_fields("name", "signal", "ref", "endpoint", "headers", "keep_last_response"),
                "classes": ("card", "wide"),
            },
        ),
        (
            "Authentication",
            {
                "fields": _webhook_fields("auth_token", "secret"),
                "classes": ("card",),
            },
        ),
        (
            "Status",
            {
                "fields": _webhook_fields("enabled", "last_success", "last_failure", "last_response"),
                "classes": ("card",),
            },
        ),
        (
            "Owner",
            {
                "fields": _webhook_fields("created_by"),
                "classes": ("card",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": _webhook_fields("created_at", "modified_at"),
                "classes": ("card",),
            },
        ),
    )

    def lookup_allowed(self, lookup: str, value: str, request: HttpRequest | None = None) -> bool:
        """Preserve WebhookAdmin's auth token filter with Django's current admin signature."""
        return not lookup.startswith("auth_token") and admin.ModelAdmin.lookup_allowed(self, lookup, value, request)


class WebhookDeliveryAttemptInline(admin.TabularInline):
    model = WebhookDeliveryAttempt
    extra = 0
    can_delete = False
    max_num = 0
    fields = ("attempt_number", "status", "status_code", "duration_ms", "error", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


class WebhookDeliveryAdmin(BaseModelAdmin):
    list_display = (
        "created_at",
        "id",
        "webhook_name",
        "event",
        "ref",
        "status",
        "num_attempts",
        "last_status_code",
        "finished_at",
    )
    list_filter = ("status", "event", "ref")
    search_fields = ("id", "webhook_name", "endpoint", "ref", "object_id", "last_error", "failure_reason")
    ordering = ["-created_at"]
    list_per_page = 100
    inlines = [WebhookDeliveryAttemptInline]
    actions = ["replay_selected_deliveries"]
    change_actions = ("replay_delivery",)
    # Delivery records are an append-only audit log; the only mutation path is
    # the replay action, which appends new attempt rows.
    readonly_fields = (
        "id",
        "created_at",
        "modified_at",
        "webhook",
        "webhook_name",
        "endpoint",
        "event",
        "ref",
        "object_id",
        "payload",
        "status",
        "num_attempts",
        "last_status_code",
        "last_error",
        "failure_reason",
        "succeeded_at",
        "finished_at",
    )

    fieldsets = (
        (
            "Delivery",
            {
                "fields": ("id", "status", "event", "ref", "object_id", "num_attempts"),
                "classes": ("card",),
            },
        ),
        (
            "Endpoint",
            {
                "fields": ("webhook", "webhook_name", "endpoint"),
                "classes": ("card",),
            },
        ),
        (
            "Result",
            {
                "fields": ("last_status_code", "last_error", "failure_reason", "succeeded_at", "finished_at"),
                "classes": ("card",),
            },
        ),
        (
            "Payload",
            {
                "fields": ("payload", "created_at", "modified_at"),
                "classes": ("card",),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def _replay_one(self, request: HttpRequest, delivery: WebhookDelivery) -> None:
        if delivery.status != WebhookDelivery.StatusChoices.FAILED:
            messages.warning(request, f"Delivery {delivery.id} is {delivery.status}; only failed deliveries can be replayed.")
            return
        try:
            replayed = replay_webhook_delivery(delivery)
        except Exception as exc:
            messages.error(request, f"Replay of delivery {delivery.id} failed to run: {type(exc).__name__}: {exc}")
            return
        if replayed.status == WebhookDelivery.StatusChoices.SUCCEEDED:
            messages.success(request, f"Delivery {delivery.id} replay succeeded on attempt {replayed.num_attempts}.")
        else:
            messages.error(request, f"Delivery {delivery.id} replay failed: {replayed.failure_reason or replayed.last_error}")

    @admin.action(description="Replay selected failed webhook deliveries")
    def replay_selected_deliveries(self, request: HttpRequest, queryset) -> None:
        failed = list(queryset.filter(status=WebhookDelivery.StatusChoices.FAILED))
        skipped = queryset.count() - len(failed)
        for delivery in failed:
            self._replay_one(request, delivery)
        if skipped:
            messages.warning(request, f"Skipped {skipped} non-failed delivery/ies.")

    @action(
        label="Replay",
        description="Re-deliver this failed webhook event (appends new attempt records)",
        methods=("POST",),
    )
    def replay_delivery(self, request: HttpRequest, obj: WebhookDelivery):
        self._replay_one(request, obj)
        return redirect("admin:api_webhookdelivery_change", obj.pk)


class WebhookDeliveryAttemptAdmin(BaseModelAdmin):
    list_display = ("created_at", "delivery_link", "attempt_number", "status", "status_code", "duration_ms", "error_excerpt")
    list_filter = ("status",)
    search_fields = ("id", "delivery__id", "delivery__webhook_name", "delivery__endpoint", "error")
    ordering = ["-created_at"]
    list_per_page = 100
    readonly_fields = (
        "id",
        "created_at",
        "delivery",
        "attempt_number",
        "status",
        "status_code",
        "duration_ms",
        "error",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    @admin.display(description="Delivery", ordering="delivery_id")
    def delivery_link(self, obj: WebhookDeliveryAttempt) -> str:
        url = reverse("admin:api_webhookdelivery_change", args=[obj.delivery_id])
        return format_html('<a href="{}"><code>{}</code></a>', url, str(obj.delivery_id)[:8])

    @admin.display(description="Error")
    def error_excerpt(self, obj: WebhookDeliveryAttempt) -> str:
        return obj.error[:200]


def register_admin(admin_site: admin.AdminSite) -> None:
    admin_site.register(APIToken, APITokenAdmin)
    admin_site.register(get_webhook_model(), CustomWebhookAdmin)
    admin_site.register(WebhookDelivery, WebhookDeliveryAdmin)
    admin_site.register(WebhookDeliveryAttempt, WebhookDeliveryAttemptAdmin)
