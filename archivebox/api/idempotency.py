"""Idempotency-Key support for the ArchiveBox write API.

A single Django middleware (:class:`IdempotencyMiddleware`) guards an explicit
whitelist of write routes. The protocol:

* ``Idempotency-Key: <key>`` identifies a request within the
  ``(creator user, HTTP method, request path, key)`` namespace.
* The first request inserts a short-lived ``pending`` row (autocommit INSERT
  backed by a UNIQUE constraint), performs the side effects once, then stores
  the exact response status/body with ``completed`` state and an expiry.
* Retries with the same canonical request body replay the stored response.
* Retries with a different body get ``409 Idempotency-Status: conflict``.
* Concurrent retries while the first request is still running get
  ``409 Idempotency-Status: in-progress`` (no second side effect).
* Stale ``pending`` rows (crashed/killed workers) are atomically claimed once
  they age past :data:`IDEMPOTENCY_IN_FLIGHT_MAX_AGE`.
* Expired ``completed`` rows are lazily deleted and the key becomes reusable.

All database work is made of short autocommit statements; no transaction or
lock is ever held across the wrapped view's side effects.
"""

__package__ = "archivebox.api"

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Any

from django.conf import settings
from django.core.files.uploadhandler import load_handler
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, QueryDict
from django.http.multipartparser import MultiPartParser, MultiPartParserError
from django.utils import timezone

from archivebox.api.auth import auth_using_token, token_from_request
from archivebox.api.models import (
    IDEMPOTENCY_STATUS_COMPLETED,
    IDEMPOTENCY_STATUS_PENDING,
    IdempotencyKey,
)


IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_STATUS_HEADER = "Idempotency-Status"
IDEMPOTENCY_REPLAYED_HEADER = "Idempotency-Replayed"

IDEMPOTENCY_KEY_MAX_LENGTH = 255

# A pending claim older than this is presumed orphaned by a crashed/killed
# worker and may be atomically reclaimed. These endpoints finish in seconds
# (a slow inline runner call included), so one hour is a generous bound.
IDEMPOTENCY_IN_FLIGHT_MAX_AGE = timedelta(hours=1)

# Bound on the opportunistic expired-row sweep done per new claim.
IDEMPOTENCY_PURGE_BATCH_SIZE = 1000

STATUS_REPLAYED = "replayed"
STATUS_CONFLICT = "conflict"
STATUS_IN_PROGRESS = "in-progress"

# (path regex, HTTP methods) — the only routes that interpret the header.
_IDEMPOTENT_ROUTES: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = tuple(
    (
        re.compile(pattern),
        methods,
    )
    for pattern, methods in (
        (r"^/api/v1/core/snapshots/?$", frozenset({"POST"})),
        (r"^/api/v1/core/snapshot/[^/]+/?$", frozenset({"PATCH", "DELETE"})),
        (r"^/api/v1/core/archiveresults/?$", frozenset({"POST"})),
        (r"^/api/v1/core/archiveresult/[^/]+/?$", frozenset({"PATCH"})),
        (r"^/api/v1/crawls/crawls/?$", frozenset({"POST"})),
        (r"^/api/v1/crawls/crawl/[^/]+/?$", frozenset({"PATCH", "DELETE"})),
    )
)


ACTION_EXECUTE = "execute"
ACTION_REPLAY = "replay"
ACTION_CONFLICT = "conflict"
ACTION_IN_PROGRESS = "in_progress"
ACTION_INVALID = "invalid"


def route_is_idempotency_eligible(method: str, path: str) -> bool:
    """True only for the explicit write-route whitelist in FR-1."""
    method = method.upper()
    for pattern, methods in _IDEMPOTENT_ROUTES:
        if method in methods and pattern.match(path):
            return True
    return False


# ---------------------------------------------------------------------------
# Request body fingerprinting
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _fresh_upload_handlers() -> list:
    """Independent handler instances so fingerprint parsing never mutates the
    request's own handlers before the view parses the upload."""
    return [load_handler(handler_path) for handler_path in settings.FILE_UPLOAD_HANDLERS]


def _canonical_multipart_payload(request: HttpRequest, body: bytes) -> str:
    """Canonical form of a multipart body that ignores the transport boundary.

    Parsed from a private BytesIO copy; Django resets ``request._stream`` to a
    fresh ``BytesIO(request.body)`` when ``.body`` is accessed, so the view's
    later Form resolution / manual MultiPartParser parse is unaffected.
    """
    parser = MultiPartParser(
        request.META,
        BytesIO(body),
        _fresh_upload_handlers(),
        request.encoding or settings.DEFAULT_CHARSET,
    )
    post_data, files = parser.parse()

    canonical_parts: list[tuple[str, ...]] = []
    for field_name in sorted(post_data.keys()):
        for value in post_data.getlist(field_name):
            canonical_parts.append(("field", field_name, str(value)))
    for field_name in sorted(files.keys()):
        for uploaded_file in files.getlist(field_name):
            digest = hashlib.sha256()
            for chunk in uploaded_file.chunks():
                digest.update(chunk)
            canonical_parts.append(
                (
                    "file",
                    field_name,
                    str(uploaded_file.name or ""),
                    str(getattr(uploaded_file, "content_type", "") or ""),
                    digest.hexdigest(),
                ),
            )
            uploaded_file.close()
    return _canonical_json(canonical_parts)


def canonical_request_fingerprint(request: HttpRequest) -> str:
    """Hash the request payload independently of its transport encoding."""
    content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
    try:
        body = request.body or b""
    except Exception:
        body = b""

    def raw_digest() -> str:
        digest = hashlib.sha256()
        digest.update(content_type.encode("ascii", errors="replace"))
        digest.update(b"\x00")
        digest.update(body)
        return digest.hexdigest()

    try:
        canonical_payload: str | None = None
        if content_type == "application/json":
            if body:
                canonical_payload = _canonical_json(json.loads(body.decode(request.encoding or "utf-8")))
        elif content_type == "multipart/form-data":
            canonical_payload = _canonical_multipart_payload(request, body)
        elif content_type == "application/x-www-form-urlencoded":
            parsed = QueryDict(body, encoding=request.encoding or "utf-8")
            canonical_payload = _canonical_json(
                [(name, value) for name in sorted(parsed.keys()) for value in parsed.getlist(name)],
            )
        else:
            return raw_digest()
    except (UnicodeDecodeError, json.JSONDecodeError, MultiPartParserError, ValueError):
        return raw_digest()

    digest = hashlib.sha256()
    digest.update(content_type.encode("ascii", errors="replace"))
    digest.update(b"\x00")
    digest.update((canonical_payload or "").encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def build_invalid_key_response() -> HttpResponse:
    response = HttpResponse(
        json.dumps(
            {
                "detail": f"{IDEMPOTENCY_HEADER} header value must be between 1 and {IDEMPOTENCY_KEY_MAX_LENGTH} characters.",
                "code": "idempotency_key_invalid",
            },
        ),
        content_type="application/json",
        status=400,
    )
    return response


def build_conflict_response() -> HttpResponse:
    response = HttpResponse(
        json.dumps(
            {
                "detail": "This Idempotency-Key is already in use on this endpoint with a different request body; resend the original body or use a new key.",
                "code": "idempotency_key_request_mismatch",
            },
        ),
        content_type="application/json",
        status=409,
    )
    response[IDEMPOTENCY_STATUS_HEADER] = STATUS_CONFLICT
    return response


def build_in_progress_response() -> HttpResponse:
    response = HttpResponse(
        json.dumps(
            {
                "detail": "A request with this Idempotency-Key is still being processed; retry shortly to receive its response.",
                "code": "idempotency_key_in_progress",
            },
        ),
        content_type="application/json",
        status=409,
    )
    response[IDEMPOTENCY_STATUS_HEADER] = STATUS_IN_PROGRESS
    response["Retry-After"] = "2"
    return response


def build_replay_response(record: IdempotencyKey) -> HttpResponse:
    response = HttpResponse(
        record.response_body.encode("utf-8"),
        content_type=record.response_content_type or "application/json",
        status=record.status_code or 200,
    )
    response[IDEMPOTENCY_STATUS_HEADER] = STATUS_REPLAYED
    response[IDEMPOTENCY_REPLAYED_HEADER] = "true"
    return response


# ---------------------------------------------------------------------------
# Claim state machine
# ---------------------------------------------------------------------------


@dataclass
class IdempotencyDecision:
    action: str
    record: IdempotencyKey | None = None
    response: HttpResponse | None = None
    retention_days: int = 30


def _retention_days(request: HttpRequest) -> int:
    from archivebox.config.common import get_request_config

    config = get_request_config(request, resolve_plugins=False)
    try:
        return max(int(config.IDEMPOTENCY_KEY_RETENTION_DAYS), 0)
    except (AttributeError, TypeError, ValueError):
        return 30


def _purge_expired_keys(now) -> None:
    """Best-effort bounded sweep; must never affect the main request flow."""
    try:
        expired_ids = list(
            IdempotencyKey.objects.filter(expires_at__isnull=False, expires_at__lte=now)
            .order_by("expires_at")
            .values_list("pk", flat=True)[:IDEMPOTENCY_PURGE_BATCH_SIZE],
        )
        if expired_ids:
            IdempotencyKey.objects.filter(pk__in=expired_ids).delete()
    except Exception:
        pass


def _claim_idempotency_key(
    *,
    user,
    method: str,
    path: str,
    key: str,
    request_hash: str,
    now,
    retention_days: int,
) -> IdempotencyDecision:
    """Resolve one request to execute / replay / conflict / in-progress.

    Every branch is reached through short autocommit reads and the UNIQUE
    constraint resolves insert races; no transaction spans the view work.
    """
    stale_cutoff = now - IDEMPOTENCY_IN_FLIGHT_MAX_AGE

    for _attempt in range(5):
        existing = IdempotencyKey.objects.filter(
            created_by=user,
            request_method=method,
            request_path=path,
            key=key,
        ).first()

        if existing is None:
            record = IdempotencyKey(
                created_by=user,
                key=key,
                request_method=method,
                request_path=path,
                request_hash=request_hash,
                status=IDEMPOTENCY_STATUS_PENDING,
                locked_at=now,
            )
            try:
                record.save()
            except IntegrityError:
                # Concurrent first-request: re-read and follow their claim.
                continue
            _purge_expired_keys(now)
            return IdempotencyDecision(ACTION_EXECUTE, record=record, retention_days=retention_days)

        if existing.status == IDEMPOTENCY_STATUS_COMPLETED:
            if existing.expires_at is not None and existing.expires_at <= now:
                deleted_count, _ = IdempotencyKey.objects.filter(
                    pk=existing.pk,
                    status=IDEMPOTENCY_STATUS_COMPLETED,
                    expires_at__lte=now,
                ).delete()
                if deleted_count:
                    # The expired claim is gone; loop back to claim it.
                    continue
                # Concurrent expiry reclaim won; re-read the new row.
                continue

            if existing.request_hash == request_hash:
                return IdempotencyDecision(
                    ACTION_REPLAY,
                    record=existing,
                    response=build_replay_response(existing),
                    retention_days=retention_days,
                )
            return IdempotencyDecision(
                ACTION_CONFLICT,
                record=existing,
                response=build_conflict_response(),
                retention_days=retention_days,
            )

        # existing.status == pending
        if existing.locked_at is not None and existing.locked_at <= stale_cutoff:
            # Orphaned by a crashed worker: one CAS winner takes the claim.
            claimed_count = IdempotencyKey.objects.filter(
                pk=existing.pk,
                status=IDEMPOTENCY_STATUS_PENDING,
                locked_at__lte=stale_cutoff,
            ).update(
                locked_at=now,
                request_hash=request_hash,
                modified_at=now,
            )
            if claimed_count:
                existing.locked_at = now
                existing.request_hash = request_hash
                _purge_expired_keys(now)
                return IdempotencyDecision(ACTION_EXECUTE, record=existing, retention_days=retention_days)
            continue

        return IdempotencyDecision(
            ACTION_IN_PROGRESS,
            record=existing,
            response=build_in_progress_response(),
            retention_days=retention_days,
        )

    # Extreme contention: never guess, tell the client to retry shortly.
    return IdempotencyDecision(ACTION_IN_PROGRESS, response=build_in_progress_response(), retention_days=retention_days)


def begin_idempotent_request(request: HttpRequest) -> IdempotencyDecision | None:
    """Inspect an incoming request and decide how the middleware handles it.

    Returns ``None`` when the request must proceed with existing behavior
    (non-whitelisted route, no usable header, or unauthenticated caller).
    """
    method = request.method.upper()
    path = request.path

    if not route_is_idempotency_eligible(method, path):
        return None

    raw_key = request.headers.get(IDEMPOTENCY_HEADER, "")
    key = raw_key.strip()
    if not key:
        return None

    # Resolve the creator through the exact same token auth the guarded routes
    # use (header/bearer/query). Without a valid API token the route's own auth
    # rejects the request, so never create or consult claims for it.
    user = auth_using_token(token=token_from_request(request), request=request)
    if user is None or not user.is_active or not user.pk:
        return None

    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        return IdempotencyDecision(ACTION_INVALID, response=build_invalid_key_response())
    if len(path) > 255:
        # Cannot be stored in the fixed-width namespace column; fall back to
        # normal handling so the route returns its own 4xx instead of a 500.
        return None

    request_hash = canonical_request_fingerprint(request)
    now = timezone.now()
    return _claim_idempotency_key(
        user=user,
        method=method,
        path=path,
        key=key,
        request_hash=request_hash,
        now=now,
        retention_days=_retention_days(request),
    )


def finish_idempotent_request(decision: IdempotencyDecision, response: HttpResponse) -> None:
    """Persist the first execution's response and release the pending claim."""
    record = decision.record
    if record is None or not isinstance(response, HttpResponse):
        return

    now = timezone.now()

    if getattr(response, "streaming", False):
        # Streaming responses cannot be faithfully replayed; release the key
        # so clients are never served a fabricated replay. None of the
        # whitelisted routes stream today.
        IdempotencyKey.objects.filter(pk=record.pk, status=IDEMPOTENCY_STATUS_PENDING).delete()
        return

    body_bytes = response.content or b""
    charset = response.charset or "utf-8"
    try:
        response_body = body_bytes.decode(charset)
    except (UnicodeDecodeError, LookupError):
        response_body = body_bytes.decode("utf-8", errors="replace")

    expires_at = now + timedelta(days=decision.retention_days) if decision.retention_days > 0 else now
    IdempotencyKey.objects.filter(pk=record.pk, status=IDEMPOTENCY_STATUS_PENDING).update(
        status=IDEMPOTENCY_STATUS_COMPLETED,
        status_code=response.status_code,
        response_content_type=response.headers.get("Content-Type", ""),
        response_body=response_body,
        completed_at=now,
        expires_at=expires_at,
        modified_at=now,
    )


class IdempotencyMiddleware:
    """Apply Idempotency-Key semantics to the whitelisted API write routes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        decision = begin_idempotent_request(request)
        if decision is None or decision.action == ACTION_EXECUTE:
            if decision is None:
                return self.get_response(request)

            response = self.get_response(request)
            finish_idempotent_request(decision, response)
            return response

        # Short-circuit: invalid key / replay / body conflict / in-progress.
        return decision.response
