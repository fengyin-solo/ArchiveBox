__package__ = "archivebox.api"

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.contrib.auth.models import User
from django.utils import timezone

from ninja import Router, Schema
from ninja.errors import HttpError

from archivebox.api.auth import (
    API_AUTH_METHODS,
    auth_using_token,
    auth_using_password,
    get_or_create_api_token,
    HeaderTokenAuth,
    BearerTokenAuth,
)
from archivebox.api.models import APIToken


router = Router(tags=["Authentication"], auth=None)


class PasswordAuthSchema(Schema):
    """Schema for a /get_api_token request"""

    username: str | None = None
    password: str | None = None


@router.post(
    "/get_api_token",
    auth=None,
    summary="Generate an API token for a given username & password (or currently logged-in user)",
)  # auth=None because they are not authed yet
def get_api_token(request: HttpRequest, auth_data: PasswordAuthSchema):
    user = auth_using_password(
        username=auth_data.username,
        password=auth_data.password,
        request=request,
    )

    if user and user.is_superuser:
        api_token = get_or_create_api_token(user)
        assert api_token is not None, "Failed to create API token"
        return {
            "success": True,
            "user_id": str(user.pk),
            "username": user.username,
            "token": api_token.token,
            "expires": api_token.expires.isoformat() if api_token.expires else None,
            "scope": api_token.effective_scope,
            "status": api_token.status,
        }

    return {"success": False, "errors": ["Invalid credentials"]}


class TokenAuthSchema(Schema):
    """Schema for a /check_api_token request"""

    token: str


@router.post(
    "/check_api_token",
    auth=None,
    summary="Validate an API token to make sure its valid, non-expired, and not revoked",
)  # auth=None because they are not authed yet
def check_api_token(request: HttpRequest, token_data: TokenAuthSchema):
    # Scope is intentionally not enforced here: this endpoint is a credential
    # validity probe, not a scoped resource action.
    user = auth_using_token(
        token=token_data.token,
        request=request,
        enforce_scope=False,
    )
    api_token = request.__dict__.get("_api_token")
    if user and isinstance(api_token, APIToken):
        return {
            "success": True,
            "user_id": str(user.pk),
            "scope": api_token.effective_scope,
            "status": api_token.status,
            "expires": api_token.expires.isoformat() if api_token.expires else None,
        }

    return {"success": False, "user_id": None}


### Fine-grained API token management #########################################


class APITokenSchema(Schema):
    """Public metadata for an API token (never includes the raw secret)."""

    id: UUID
    label: str
    scope: str | None
    effective_scope: str
    status: str
    resource_paths: list[dict[str, Any]]
    expires: datetime | None
    created_at: datetime
    modified_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    token_redacted: str
    created_by_id: str

    @staticmethod
    def resolve_created_by_id(obj) -> str:
        return str(obj.created_by_id)


class APITokenCreatedSchema(APITokenSchema):
    token: str


class APITokenCreateSchema(Schema):
    label: str = ""
    scope: str = "admin"
    resource_paths: list[dict[str, Any]] = []
    expires: datetime | None = None
    expires_in_days: int | None = 30


class APITokenUpdateSchema(Schema):
    label: str | None = None
    scope: str | None = None
    resource_paths: list[dict[str, Any]] | None = None
    expires: datetime | None = None
    expires_in_days: int | None = None
    status: str | None = None


class APITokenActionResponseSchema(Schema):
    success: bool
    token: APITokenSchema


def _require_full_admin(request: HttpRequest) -> APIToken:
    """Credential management requires an unrestricted admin token (and admin user)."""
    user = getattr(request, "auth", None)
    api_token = request.__dict__.get("_api_token")
    if not isinstance(user, User) or not user.is_active or not user.is_superuser:
        raise HttpError(403, "An active administrator API token is required")
    if not isinstance(api_token, APIToken) or not api_token.is_full_admin:
        raise HttpError(403, "Token management requires an admin-scoped API token without resource path restrictions")
    return api_token


def _get_owned_token(request: HttpRequest, token_id: str) -> APIToken:
    try:
        return APIToken.objects.get(id=token_id, created_by=request.user)
    except (APIToken.DoesNotExist, ValidationError, ValueError):
        raise HttpError(404, "API token not found") from None


def _resolve_expires(*, expires: datetime | None, expires_in_days: int | None, default_days: int | None) -> datetime | None:
    if expires is not None:
        if timezone.is_naive(expires):
            expires = timezone.make_aware(expires, timezone.get_current_timezone())
        if expires <= timezone.now():
            raise HttpError(400, "expires must be a future datetime")
        return expires
    if expires_in_days is None:
        return timezone.now() + timedelta(days=default_days) if default_days is not None else None
    if expires_in_days <= 0:
        raise HttpError(400, "expires_in_days must be positive, or null for a token that never expires")
    return timezone.now() + timedelta(days=expires_in_days)


def _validate_scope(scope: str | None) -> str:
    try:
        return APIToken.ScopeChoices(scope).value
    except ValueError:
        raise HttpError(400, f"scope must be one of: {', '.join(APIToken.ScopeChoices.values)}") from None


@router.post(
    "/tokens",
    auth=API_AUTH_METHODS,
    response=APITokenCreatedSchema,
    summary="Create a new scoped API token (admin only)",
)
def create_token(request: HttpRequest, data: APITokenCreateSchema):
    _require_full_admin(request)
    scope = _validate_scope(data.scope)
    try:
        resource_paths = APIToken.normalize_resource_paths(data.resource_paths)
    except ValueError as err:
        raise HttpError(400, str(err)) from err
    expires = _resolve_expires(expires=data.expires, expires_in_days=data.expires_in_days, default_days=30)

    api_token = APIToken.objects.create(
        created_by=request.user,
        scope=scope,
        resource_paths=resource_paths,
        label=data.label[:255],
        expires=expires,
    )
    return api_token


@router.get(
    "/tokens",
    auth=API_AUTH_METHODS,
    response=list[APITokenSchema],
    summary="List API tokens owned by the current user (admin only)",
)
def list_tokens(request: HttpRequest):
    _require_full_admin(request)
    return list(APIToken.objects.filter(created_by=request.user).order_by("-created_at"))


@router.get(
    "/tokens/{token_id}",
    auth=API_AUTH_METHODS,
    response=APITokenSchema,
    summary="Get one API token's metadata (admin only)",
)
def get_token(request: HttpRequest, token_id: str):
    _require_full_admin(request)
    return _get_owned_token(request, token_id)


@router.patch(
    "/tokens/{token_id}",
    auth=API_AUTH_METHODS,
    response=APITokenSchema,
    summary="Update an API token's label/scope/expiry (admin only)",
)
def update_token(request: HttpRequest, token_id: str, data: APITokenUpdateSchema):
    _require_full_admin(request)
    api_token = _get_owned_token(request, token_id)
    payload = data.dict(exclude_unset=True)

    if "label" in payload:
        api_token.label = (payload["label"] or "")[:255]
    if "scope" in payload:
        api_token.scope = _validate_scope(payload["scope"])
    if "resource_paths" in payload:
        try:
            api_token.resource_paths = APIToken.normalize_resource_paths(payload["resource_paths"])
        except ValueError as err:
            raise HttpError(400, str(err)) from err
    if "status" in payload:
        if payload["status"] not in APIToken.StatusChoices.values:
            raise HttpError(400, f"status must be one of: {', '.join(APIToken.StatusChoices.values)}")
        api_token.status = payload["status"]
    if "expires" in payload or "expires_in_days" in payload:
        api_token.expires = _resolve_expires(
            expires=payload.get("expires"),
            expires_in_days=payload.get("expires_in_days"),
            default_days=None,
        )

    api_token.save()
    api_token.refresh_from_db()
    return api_token


@router.post(
    "/tokens/{token_id}/revoke",
    auth=API_AUTH_METHODS,
    response=APITokenActionResponseSchema,
    summary="Revoke an API token so it can no longer authenticate (admin only)",
)
def revoke_token(request: HttpRequest, token_id: str):
    _require_full_admin(request)
    api_token = _get_owned_token(request, token_id)
    api_token.revoke()
    api_token.refresh_from_db()
    return {"success": True, "token": api_token}


@router.post(
    "/tokens/{token_id}/reenable",
    auth=API_AUTH_METHODS,
    response=APITokenActionResponseSchema,
    summary="Re-enable a previously revoked API token (admin only)",
)
def reenable_token(request: HttpRequest, token_id: str):
    _require_full_admin(request)
    api_token = _get_owned_token(request, token_id)
    api_token.reactivate()
    api_token.refresh_from_db()
    return {"success": True, "token": api_token}


@router.post(
    "/browser_session",
    auth=[HeaderTokenAuth(), BearerTokenAuth()],
    summary="Exchange an unrestricted administrator API key for a normal browser session",
)
def browser_session(request: HttpRequest):
    from django.conf import settings
    from django.contrib.auth import login
    from django.http import JsonResponse
    from archivebox.core.routes_util import get_admin_base_url

    user = request.auth
    if not user.is_active or not user.is_superuser:
        raise HttpError(403, "An active administrator is required")
    token = request.__dict__.get("_api_token")
    if not isinstance(token, APIToken) or not token.is_full_admin:
        raise HttpError(403, "Browser session exchange requires an admin-scoped API token without resource path restrictions")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # A browser session must not outlive the key used to establish it.
    lifetime = settings.SESSION_COOKIE_AGE
    if token.expires:
        lifetime = min(lifetime, max(0, int((token.expires - timezone.now()).total_seconds())))
    request.session.set_expiry(lifetime)
    request.session.save()
    response = JsonResponse(
        {
            "admin_url": get_admin_base_url(request=request).rstrip("/") + "/admin/",
            "cookie": {
                "name": settings.SESSION_COOKIE_NAME,
                "value": request.session.session_key,
                "expires": request.session.get_expiry_date().timestamp(),
                "secure": settings.SESSION_COOKIE_SECURE,
            },
        },
    )
    # The native client installs a host-only HttpOnly cookie on the admin origin;
    # API and admin hosts can differ, so a Set-Cookie on the API host is insufficient.
    response["Cache-Control"] = "no-store"
    return response
