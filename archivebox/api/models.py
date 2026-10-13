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


# Fine-grained API token scopes. Tiers are cumulative: write includes read,
# admin includes write.
TOKEN_SCOPE_READ = "read"
TOKEN_SCOPE_WRITE = "write"
TOKEN_SCOPE_ADMIN = "admin"

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"})
ALL_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})

SCOPE_METHODS: dict[str, frozenset[str]] = {
    TOKEN_SCOPE_READ: READ_METHODS,
    TOKEN_SCOPE_WRITE: WRITE_METHODS,
    TOKEN_SCOPE_ADMIN: ALL_HTTP_METHODS,
}

# Error returned by every protected API route when a valid token's scope does
# not cover the request method/path. Kept in one place so the denial is
# identical across routers.
INSUFFICIENT_TOKEN_SCOPE_MESSAGE = (
    "API token is not allowed to perform this request (insufficient token scope or resource path restrictions)"
)


def normalize_api_path(path: str | None) -> str:
    """Normalize an API resource path to the /v1-relative form used for matching."""
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    for prefix in ("/api/v1", "/api"):
        if normalized == prefix:
            return "/"
        if normalized.startswith(prefix + "/"):
            normalized = normalized[len(prefix) :]
            break
    if len(normalized) > 1:
        normalized = normalized.rstrip("/") or "/"
    return normalized


def api_path_matches(request_path: str, allowed_pattern: str) -> bool:
    """Whether a concrete request path is covered by an allowed resource path.

    Matching is segment-aware so that ``/core/snapshot`` does not grant access
    to ``/core/snapshots``; same-segment suffixes such as ``/core/snapshots.rss``
    and sub-paths are covered. A trailing ``*`` allows a plain prefix.
    """
    request_path = normalize_api_path(request_path)
    allowed_pattern = normalize_api_path(allowed_pattern)
    if allowed_pattern == "/":
        return True
    if allowed_pattern.endswith("*"):
        return request_path.startswith(allowed_pattern[:-1])
    if request_path == allowed_pattern:
        return True
    if request_path.startswith(allowed_pattern.rstrip("/") + "/"):
        return True
    return request_path.startswith(allowed_pattern) and request_path[len(allowed_pattern) :].startswith(".")


class APIToken(models.Model):
    class ScopeChoices(models.TextChoices):
        READ = TOKEN_SCOPE_READ, "Read-only (safe requests: GET/HEAD/OPTIONS)"
        WRITE = TOKEN_SCOPE_WRITE, "Read + Write (adds POST/PUT/PATCH)"
        ADMIN = TOKEN_SCOPE_ADMIN, "Admin (all methods, incl. DELETE and credential management)"

    class StatusChoices(models.TextChoices):
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"

    id = CompactUUIDField(primary_key=True, default=uuid7, editable=False, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, default=get_or_create_system_user_pk, null=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    modified_at = models.DateTimeField(auto_now=True)
    token = models.CharField(max_length=32, default=generate_secret_token, unique=True)
    expires = models.DateTimeField(null=True, blank=True)

    # NULL means a historical token created before scopes existed. It keeps
    # full admin rights at auth time and is only persisted as ADMIN the first
    # time the row is updated (see save()).
    scope = models.CharField(
        max_length=16,
        choices=ScopeChoices.choices,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="Access tier. Leave blank for legacy tokens (treated as Admin until the token is updated).",
    )
    # Optional per-resource allowlist, e.g. [{"path": "/core/snapshots", "methods": ["GET"]}].
    # An empty list means the scope tier alone decides; non-empty restricts the
    # token to exactly the listed path prefixes and methods.
    resource_paths = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional list of {'path': '/v1-relative/resource', 'methods': ['GET']} allowlist entries.",
    )
    label = models.CharField(max_length=255, blank=True, default="", help_text="Human-readable note for this key.")
    status = models.CharField(max_length=16, choices=StatusChoices.choices, default=StatusChoices.ACTIVE, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta(TypedModelMeta):
        app_label = "api"
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self) -> str:
        return self.token_redacted

    @property
    def token_redacted(self):
        return f"************{self.token[-4:]}"

    @property
    def effective_scope(self) -> str:
        """Scope tier actually enforced: legacy unscoped tokens remain full-admin."""
        return self.scope or TOKEN_SCOPE_ADMIN

    @property
    def is_full_admin(self) -> bool:
        """True only for unrestricted admin tokens (browser-session exchange requires this)."""
        return self.effective_scope == TOKEN_SCOPE_ADMIN and not self.resource_paths

    def is_valid(self, for_date=None) -> bool:
        """True when the token has not passed its expiry date."""
        return not self.expires or self.expires >= (for_date or timezone.now())

    def is_usable(self, for_date=None) -> bool:
        """True when the token is active (not revoked) and unexpired."""
        return self.status == self.StatusChoices.ACTIVE and self.is_valid(for_date=for_date)

    def allows_request(self, method: str, path: str) -> bool:
        """Whether this token's scope/resource allowlist covers the given request."""
        method = str(method or "").upper()
        if method not in SCOPE_METHODS.get(self.effective_scope, frozenset()):
            return False

        entries = self.resource_paths or []
        if not entries:
            return True

        return any(method in entry["methods"] and api_path_matches(path, entry["path"]) for entry in entries)

    @staticmethod
    def normalize_resource_paths(raw_paths) -> list[dict[str, list[str] | str]]:
        """Validate/normalize a resource-path allowlist, raising ValueError on bad input."""
        if raw_paths in (None, ""):
            return []
        if not isinstance(raw_paths, list):
            raise ValueError("resource_paths must be a list of {'path', 'methods'} entries")

        normalized: list[dict[str, list[str] | str]] = []
        for entry in raw_paths:
            if not isinstance(entry, dict):
                raise ValueError("each resource_paths entry must be an object with 'path' and 'methods'")
            raw_path = str(entry.get("path") or "").strip()
            raw_methods = entry.get("methods")
            if not raw_path:
                raise ValueError("each resource_paths entry requires a non-empty 'path'")
            if not isinstance(raw_methods, list) or not raw_methods:
                raise ValueError("each resource_paths entry requires a non-empty 'methods' list")
            methods = sorted({str(method).strip().upper() for method in raw_methods if str(method).strip()})
            if not methods or any(method not in ALL_HTTP_METHODS for method in methods):
                raise ValueError(f"resource_paths methods must be one of: {', '.join(sorted(ALL_HTTP_METHODS))}")
            normalized.append({"path": normalize_api_path(raw_path), "methods": methods})
        return normalized

    def save(self, *args, **kwargs):
        # Lazy migration of historical unscoped tokens: they keep admin rights
        # until the row is first updated, at which point the scope is persisted.
        # _state.adding (not self.pk) distinguishes inserts from updates because
        # the UUID primary key is pre-populated from its callable default.
        if not self._state.adding and not self.scope:
            self.scope = TOKEN_SCOPE_ADMIN
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "scope" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "scope"]
        super().save(*args, **kwargs)

    def revoke(self) -> None:
        self.status = self.StatusChoices.REVOKED
        self.save(update_fields=["status"])

    def reactivate(self) -> None:
        self.status = self.StatusChoices.ACTIVE
        self.save(update_fields=["status"])

    def touch_last_used(self, ip: str | None = None) -> None:
        """Record a successful authentication using a short autocommit update.

        Goes through QuerySet.update() (not save()) so usage tracking does not
        itself migrate legacy token scopes or bump unrelated fields.
        """
        now = timezone.now()
        APIToken.objects.filter(pk=self.pk).update(last_used_at=now, last_used_ip=ip or None)
        self.last_used_at = now
        self.last_used_ip = ip or None


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
