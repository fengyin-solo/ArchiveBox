__package__ = "archivebox.api"

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from django.db import transaction
from django.utils import timezone
from signal_webhooks.settings import webhook_settings
from signal_webhooks.utils import reference_for_model, truncate

from archivebox.api.models import OutboundWebhook, WebhookDelivery, WebhookDeliveryAttempt


logger = logging.getLogger(__name__)


# Headers attached to every signed delivery request.
SIGNATURE_HEADER = "X-ArchiveBox-Webhook-Signature"
TIMESTAMP_HEADER = "X-ArchiveBox-Webhook-Timestamp"
DELIVERY_HEADER = "X-ArchiveBox-Webhook-Id"
EVENT_HEADER = "X-ArchiveBox-Webhook-Event"

SIGNATURE_VERSION = "sha256"
MAX_ERROR_LEN = 8000


# ---------------------------------------------------------------------------
# Tunables (env-overridable so operators and tests can configure delivery)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def get_max_attempts() -> int:
    """Total attempts per delivery, including the first one (>= 1)."""
    return max(1, _env_int("ARCHIVEBOX_WEBHOOK_MAX_ATTEMPTS", 5))


def get_backoff_base() -> float:
    """Seconds to wait before the 2nd attempt; doubles after every retry."""
    return max(0.0, _env_float("ARCHIVEBOX_WEBHOOK_BACKOFF_BASE", 1.0))


def get_backoff_cap() -> float:
    """Upper bound (seconds) for a single exponential-backoff sleep."""
    return max(0.0, _env_float("ARCHIVEBOX_WEBHOOK_BACKOFF_CAP", 300.0))


def backoff_delay(attempt_number: int) -> float:
    """Delay before ``attempt_number`` (1-based) is tried; 0 for the first try."""
    if attempt_number <= 1:
        return 0.0
    return min(get_backoff_base() * (2 ** (attempt_number - 2)), get_backoff_cap())


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 of ``<timestamp>.<raw request body>`` using the shared secret."""
    signed_payload = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_webhook_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Constant-time verification helper for receiving endpoints/tests."""
    if not signature:
        return False
    if signature.startswith(f"{SIGNATURE_VERSION}="):
        signature = signature.split("=", 1)[1]
    expected = compute_signature(secret, timestamp, body).split("=", 1)[1]
    return hmac.compare_digest(expected, signature.strip())


# ---------------------------------------------------------------------------
# signal_webhooks integration
# ---------------------------------------------------------------------------


def warning_error_handler(hook: Any, error: Exception | None) -> None:
    if error is not None:
        logger.warning("Outbound webhook %r failed: %s", hook.name, error)
        return

    logger.warning("Outbound webhook %r returned a non-success response.", hook.name)


def transaction_on_commit_task_handler(hook: Callable[..., None], **kwargs: Any) -> None:
    """Run the reliable delivery fan-out after the triggering transaction commits.

    signal_webhooks passes its default in-process hook callable as ``hook``;
    reliable delivery (persisted rows, retries, signatures) replaces that path
    entirely, so the callable is intentionally unused here.
    """

    def run_webhook() -> None:
        try:
            dispatch_outbound_webhooks(
                instance=kwargs["instance"],
                data=kwargs["data"],
                method=kwargs["method"],
            )
        except Exception:
            logger.warning("Outbound webhook delivery failed after transaction commit.", exc_info=True)

    try:
        transaction.on_commit(run_webhook)
    except Exception:
        logger.warning("Could not schedule outbound webhook after transaction commit.", exc_info=True)


# ---------------------------------------------------------------------------
# Delivery engine
# ---------------------------------------------------------------------------


def dispatch_outbound_webhooks(*, instance: Any, data: Any, method: str) -> list[WebhookDelivery]:
    """Create and execute one pending delivery per enabled endpoint for the event."""
    # get_for_model filters on ref + signal matching + enabled=True, so a
    # disabled webhook produces neither a delivery row nor an HTTP request.
    hooks = list(OutboundWebhook.objects.get_for_model(instance, method=method))

    deliveries: list[WebhookDelivery] = []
    ref = reference_for_model(type(instance))
    object_id = "" if instance.pk is None else str(instance.pk)
    for hook in hooks:
        delivery = WebhookDelivery.objects.create(
            webhook=hook,
            webhook_name=hook.name,
            endpoint=hook.endpoint,
            event=method,
            ref=ref,
            object_id=object_id,
            payload=data,
            status=WebhookDelivery.StatusChoices.PENDING,
        )
        try:
            deliver_webhook_delivery(delivery)
        except Exception:
            logger.exception("Unexpected error while delivering webhook %s", delivery.id)
        deliveries.append(delivery)
    return deliveries


def deliver_webhook_delivery(
    delivery: WebhookDelivery,
    *,
    max_attempts: int | None = None,
    timeout: float | None = None,
) -> WebhookDelivery:
    """Attempt one pending/in-progress delivery until success, a permanent
    failure, or the attempt budget is exhausted.

    Each HTTP try is persisted as its own append-only ``WebhookDeliveryAttempt``
    *after* the network call returns (never inside a transaction held across
    the request). Re-transient sleeps use exponential backoff.
    """
    delivery.refresh_from_db()
    hook = delivery.webhook

    if hook is None:
        return _finish_permanent_failure(delivery, status_code=None, reason="Webhook endpoint configuration was deleted.")
    if not hook.enabled:
        return _finish_permanent_failure(delivery, status_code=None, reason="Webhook endpoint is disabled.")

    if max_attempts is None:
        max_attempts = get_max_attempts()
    if timeout is None:
        timeout = webhook_settings.TIMEOUT

    body = json.dumps(delivery.payload).encode("utf-8")

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        while delivery.num_attempts < max_attempts:
            attempt_number = delivery.num_attempts + 1
            timestamp = str(int(time.time()))
            headers = _build_headers(hook, delivery, body, timestamp)

            started = time.monotonic()
            try:
                response = client.post(delivery.endpoint, content=body, headers=headers)
            except httpx.HTTPError as exc:
                duration_ms = _elapsed_ms(started)
                error = _summarize_error(exc)
                _persist_attempt(
                    delivery,
                    attempt_number=attempt_number,
                    status=WebhookDeliveryAttempt.StatusChoices.RETRYABLE,
                    status_code=None,
                    duration_ms=duration_ms,
                    error=error,
                )
            else:
                duration_ms = _elapsed_ms(started)
                status_code = response.status_code

                if 200 <= status_code < 300:
                    _persist_attempt(
                        delivery,
                        attempt_number=attempt_number,
                        status=WebhookDeliveryAttempt.StatusChoices.SUCCEEDED,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        error="",
                    )
                    return _finish_success(delivery, hook, response, status_code)

                if 400 <= status_code < 500:
                    reason = f"Endpoint returned a deterministic HTTP {status_code} error; not retrying."
                    _persist_attempt(
                        delivery,
                        attempt_number=attempt_number,
                        status=WebhookDeliveryAttempt.StatusChoices.PERMANENT,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        error=truncate(f"HTTP {status_code}: {response.text}"),
                    )
                    _mark_webhook_failure(hook, response)
                    return _finish_permanent_failure(delivery, status_code=status_code, reason=reason)

                # 5xx (or any other non-2xx) is treated as a transient failure.
                error = truncate(f"HTTP {status_code}: {response.text}")
                _persist_attempt(
                    delivery,
                    attempt_number=attempt_number,
                    status=WebhookDeliveryAttempt.StatusChoices.RETRYABLE,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    error=error,
                )

            if delivery.num_attempts >= max_attempts:
                break

            delay = backoff_delay(attempt_number + 1)
            if delay:
                time.sleep(delay)

    reason = f"Exhausted {max_attempts} attempts (max retries reached); last error: {delivery.last_error or 'unknown error'}"
    return _finish_exhausted_failure(delivery, reason=reason)


def replay_webhook_delivery(delivery: WebhookDelivery) -> WebhookDelivery:
    """Re-deliver a previously failed delivery.

    Grants a fresh attempt budget; new attempts keep incrementing the attempt
    sequence and are appended as new rows. Historical attempt rows are never
    modified or deleted.
    """
    delivery.refresh_from_db()
    if delivery.status != WebhookDelivery.StatusChoices.FAILED:
        raise ValueError(f"Only failed deliveries can be replayed (current status: {delivery.status})")

    now = timezone.now()
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.StatusChoices.PENDING,
        last_error="",
        failure_reason="",
        succeeded_at=None,
        finished_at=None,
        modified_at=now,
    )
    delivery.refresh_from_db()

    # Continue numbering past the historical attempts while granting a full
    # new budget of retries on top of them.
    max_attempts = delivery.num_attempts + get_max_attempts()
    return deliver_webhook_delivery(delivery, max_attempts=max_attempts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_headers(hook: OutboundWebhook, delivery: WebhookDelivery, body: bytes, timestamp: str) -> dict[str, str]:
    headers = dict(hook.default_headers())
    headers[DELIVERY_HEADER] = str(delivery.id)
    headers[EVENT_HEADER] = delivery.event
    headers[TIMESTAMP_HEADER] = timestamp
    headers[SIGNATURE_HEADER] = compute_signature(hook.secret, timestamp, body)
    return headers


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _summarize_error(error: Exception) -> str:
    return truncate(f"{type(error).__name__}: {error}")


def _persist_attempt(
    delivery: WebhookDelivery,
    *,
    attempt_number: int,
    status: str,
    status_code: int | None,
    duration_ms: int,
    error: str,
) -> None:
    # One short autocommit write per attempt; the slow network call already
    # happened outside this block, so no DB lock is held across I/O.
    with transaction.atomic():
        WebhookDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=attempt_number,
            status=status,
            status_code=status_code,
            duration_ms=duration_ms,
            error=truncate(error),
        )
        delivery.num_attempts = attempt_number
        delivery.last_status_code = status_code
        delivery.last_error = truncate(error)
        delivery.save(update_fields=["num_attempts", "last_status_code", "last_error", "modified_at"])


def _finish_success(delivery: WebhookDelivery, hook: OutboundWebhook, response: httpx.Response, status_code: int) -> WebhookDelivery:
    now = timezone.now()
    if hook.keep_last_response:
        OutboundWebhook.objects.filter(pk=hook.pk).update(last_success=now, last_response=truncate(response.text))
    else:
        OutboundWebhook.objects.filter(pk=hook.pk).update(last_success=now)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.StatusChoices.SUCCEEDED,
        last_status_code=status_code,
        last_error="",
        failure_reason="",
        succeeded_at=now,
        finished_at=now,
        modified_at=now,
    )
    delivery.refresh_from_db()
    return delivery


def _mark_webhook_failure(hook: OutboundWebhook, response: httpx.Response | None) -> None:
    now = timezone.now()
    updates: dict[str, Any] = {"last_failure": now}
    if hook.keep_last_response and response is not None:
        updates["last_response"] = truncate(response.text)
    OutboundWebhook.objects.filter(pk=hook.pk).update(**updates)


def _finish_permanent_failure(delivery: WebhookDelivery, *, status_code: int | None, reason: str) -> WebhookDelivery:
    now = timezone.now()
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.StatusChoices.FAILED,
        last_status_code=status_code,
        failure_reason=truncate(reason),
        finished_at=now,
        modified_at=now,
    )
    delivery.refresh_from_db()
    return delivery


def _finish_exhausted_failure(delivery: WebhookDelivery, *, reason: str) -> WebhookDelivery:
    now = timezone.now()
    # Surface the failure on the endpoint too (parity with the old fire path).
    if delivery.webhook_id is not None:
        OutboundWebhook.objects.filter(pk=delivery.webhook_id).update(last_failure=now)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.StatusChoices.FAILED,
        failure_reason=truncate(reason, MAX_ERROR_LEN),
        finished_at=now,
        modified_at=now,
    )
    delivery.refresh_from_db()
    logger.warning("Webhook delivery %s permanently failed: %s", delivery.id, reason)
    return delivery
