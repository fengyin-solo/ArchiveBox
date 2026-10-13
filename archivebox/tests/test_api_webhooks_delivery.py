"""Reliable outbound-webhook delivery tests.

All tests hit a real loopback HTTP server (no mocked HTTP): the server records
raw headers and bodies and returns scripted status codes so retries,
signatures, and replay are verified end to end.
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.urls import reverse

from archivebox.api.models import OutboundWebhook, WebhookDelivery, WebhookDeliveryAttempt
from archivebox.api.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    dispatch_outbound_webhooks,
    replay_webhook_delivery,
    verify_webhook_signature,
)
from archivebox.tests.conftest import ADMIN_TEST_HOST

pytestmark = pytest.mark.django_db


USER_REF = "django.contrib.auth.models.User"


@pytest.fixture
def fast_webhook_backoff():
    """Zero-delay backoff and a known attempt budget for fast deterministic tests."""
    os.environ["ARCHIVEBOX_WEBHOOK_MAX_ATTEMPTS"] = "5"
    os.environ["ARCHIVEBOX_WEBHOOK_BACKOFF_BASE"] = "0"
    os.environ["ARCHIVEBOX_WEBHOOK_BACKOFF_CAP"] = "0"
    yield


@pytest.fixture
def webhook_server():
    """Real loopback server whose responses are scripted through ``state``."""

    state = SimpleNamespace(
        lock=threading.Lock(),
        requests=[],
        statuses=[200],
        response_text="ok",
        next_index=0,
    )

    class RecordingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with state.lock:
                state.requests.append(
                    {
                        "path": self.path,
                        "headers": {key: value for key, value in self.headers.items()},
                        "body": body,
                    },
                )
                index = min(state.next_index, len(state.statuses) - 1)
                state.next_index += 1
                status_code = state.statuses[index]
                response_text = state.response_text
            payload = response_text.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_address[1]}/hook",
            state=state,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def closed_port():
    """A 127.0.0.1 port that is guaranteed closed (connect refused immediately)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_webhook(endpoint: str, *, enabled: bool = True, signal: str = "CREATE") -> OutboundWebhook:
    return OutboundWebhook.objects.create(
        name=f"test-webhook-{os.urandom(8).hex()}",
        signal=signal,
        ref=USER_REF,
        endpoint=endpoint,
        enabled=enabled,
    )


def make_user():
    return get_user_model().objects.create_user(username=f"user-{os.urandom(6).hex()}", password="pw")


def delivery_payload(user) -> dict:
    return {"model": USER_REF, "pk": str(user.pk), "fields": {"username": user.username}}


def attempt_rows(delivery: WebhookDelivery) -> list[WebhookDeliveryAttempt]:
    return list(delivery.attempts.order_by("attempt_number"))


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


def test_transient_failures_retry_until_success(fast_webhook_backoff, webhook_server):
    webhook_server.state.statuses = [503, 502, 200]
    hook = make_webhook(webhook_server.url)
    user = make_user()

    deliveries = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")

    assert len(deliveries) == 1
    delivery = deliveries[0]
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.StatusChoices.SUCCEEDED
    assert delivery.num_attempts == 3
    assert delivery.last_status_code == 200
    assert delivery.failure_reason == ""
    assert delivery.succeeded_at is not None and delivery.finished_at is not None

    attempts = attempt_rows(delivery)
    assert [(a.attempt_number, a.status, a.status_code) for a in attempts] == [
        (1, WebhookDeliveryAttempt.StatusChoices.RETRYABLE, 503),
        (2, WebhookDeliveryAttempt.StatusChoices.RETRYABLE, 502),
        (3, WebhookDeliveryAttempt.StatusChoices.SUCCEEDED, 200),
    ]
    assert "HTTP 503" in attempts[0].error
    assert all(a.duration_ms >= 0 for a in attempts)
    assert len(webhook_server.state.requests) == 3

    hook.refresh_from_db()
    assert hook.last_success is not None


def test_deterministic_4xx_failure_is_not_retried(fast_webhook_backoff, webhook_server):
    webhook_server.state.statuses = [404, 200]
    hook = make_webhook(webhook_server.url)
    user = make_user()

    deliveries = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")

    delivery = deliveries[0]
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.StatusChoices.FAILED
    assert delivery.num_attempts == 1
    assert "404" in delivery.failure_reason
    assert delivery.succeeded_at is None and delivery.finished_at is not None

    attempts = attempt_rows(delivery)
    assert len(attempts) == 1
    assert attempts[0].status == WebhookDeliveryAttempt.StatusChoices.PERMANENT
    assert attempts[0].status_code == 404
    assert "HTTP 404" in attempts[0].error
    assert len(webhook_server.state.requests) == 1

    hook.refresh_from_db()
    assert hook.last_failure is not None
    assert hook.last_success is None


def test_retries_stop_at_max_attempts_and_record_reason(webhook_server):
    os.environ["ARCHIVEBOX_WEBHOOK_MAX_ATTEMPTS"] = "3"
    os.environ["ARCHIVEBOX_WEBHOOK_BACKOFF_BASE"] = "0"
    webhook_server.state.statuses = [500]
    hook = make_webhook(webhook_server.url)
    user = make_user()

    deliveries = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")

    delivery = deliveries[0]
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.StatusChoices.FAILED
    assert delivery.num_attempts == 3
    assert delivery.last_status_code == 500
    assert "Exhausted 3 attempts" in delivery.failure_reason
    assert "HTTP 500" in delivery.failure_reason
    assert [a.status_code for a in attempt_rows(delivery)] == [500, 500, 500]
    assert all(a.status == WebhookDeliveryAttempt.StatusChoices.RETRYABLE for a in attempt_rows(delivery))
    assert len(webhook_server.state.requests) == 3
    hook.refresh_from_db()
    assert hook.last_failure is not None


def test_network_errors_are_transient_and_retry(fast_webhook_backoff, closed_port):
    make_webhook(f"http://127.0.0.1:{closed_port}/hook")
    user = make_user()

    deliveries = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")

    delivery = deliveries[0]
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.StatusChoices.FAILED
    assert delivery.num_attempts == 5
    attempts = attempt_rows(delivery)
    assert all(a.status == WebhookDeliveryAttempt.StatusChoices.RETRYABLE for a in attempts)
    assert all(a.status_code is None for a in attempts)
    assert all(a.error for a in attempts)
    assert "Exhausted 5 attempts" in delivery.failure_reason


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_requests_carry_verifiable_hmac_signature_and_timestamp(fast_webhook_backoff, webhook_server):
    hook = make_webhook(webhook_server.url)
    user = make_user()
    payload = delivery_payload(user)

    deliveries = dispatch_outbound_webhooks(instance=user, data=payload, method="CREATE")
    delivery = deliveries[0]

    recorded = webhook_server.state.requests[0]
    headers = recorded["headers"]
    raw_body = recorded["body"]
    timestamp = headers.get(TIMESTAMP_HEADER)
    signature = headers.get(SIGNATURE_HEADER)

    assert timestamp is not None and timestamp.isdigit()
    assert abs(int(timestamp) - int(time.time())) <= 30
    assert signature.startswith("sha256=")
    assert headers[DELIVERY_HEADER] == str(delivery.id)
    assert headers[EVENT_HEADER] == "CREATE"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(raw_body.decode("utf-8")) == payload

    # Receiver can verify provenance using the shared secret.
    assert verify_webhook_signature(hook.secret, timestamp, raw_body, signature)
    # Tampered bodies / wrong secrets must fail verification.
    assert not verify_webhook_signature(hook.secret, timestamp, raw_body + b"x", signature)
    assert not verify_webhook_signature("a" * 64, timestamp, raw_body, signature)
    assert not verify_webhook_signature(hook.secret, str(int(timestamp) + 1), raw_body, signature)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_appends_new_attempts_and_preserves_history(fast_webhook_backoff, webhook_server):
    webhook_server.state.statuses = [404]
    hook = make_webhook(webhook_server.url)
    user = make_user()

    delivery = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")[0]
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.StatusChoices.FAILED
    original_attempt = attempt_rows(delivery)[0]
    original_snapshot = {
        "id": original_attempt.id,
        "number": original_attempt.attempt_number,
        "status": original_attempt.status,
        "code": original_attempt.status_code,
        "error": original_attempt.error,
    }

    with pytest.raises(ValueError):
        # Replaying a still-pending/succeeded delivery is not allowed (failed
        # deliveries below are the only legal replay target).
        WebhookDelivery.objects.filter(pk=delivery.pk).update(status=WebhookDelivery.StatusChoices.SUCCEEDED)
        delivery.refresh_from_db()
        replay_webhook_delivery(delivery)

    WebhookDelivery.objects.filter(pk=delivery.pk).update(status=WebhookDelivery.StatusChoices.FAILED)
    delivery.refresh_from_db()
    webhook_server.state.statuses = [200]
    replayed = replay_webhook_delivery(delivery)

    assert replayed.status == WebhookDelivery.StatusChoices.SUCCEEDED
    assert replayed.num_attempts == 2
    attempts = attempt_rows(replayed)
    assert [(a.attempt_number, a.status, a.status_code) for a in attempts] == [
        (1, WebhookDeliveryAttempt.StatusChoices.PERMANENT, 404),
        (2, WebhookDeliveryAttempt.StatusChoices.SUCCEEDED, 200),
    ]
    historical = attempts[0]
    assert historical.id == original_snapshot["id"]
    assert historical.attempt_number == original_snapshot["number"]
    assert historical.status == original_snapshot["status"]
    assert historical.status_code == original_snapshot["code"]
    assert historical.error == original_snapshot["error"]
    hook.refresh_from_db()
    assert hook.last_success is not None


# ---------------------------------------------------------------------------
# Disabled webhooks
# ---------------------------------------------------------------------------


def test_disabled_webhook_is_not_dispatched(fast_webhook_backoff, webhook_server):
    make_webhook(webhook_server.url, enabled=False)
    user = make_user()

    deliveries = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")

    assert deliveries == []
    assert WebhookDelivery.objects.count() == 0
    assert WebhookDeliveryAttempt.objects.count() == 0
    assert webhook_server.state.requests == []


def test_signal_path_delivers_after_transaction_commit(fast_webhook_backoff, webhook_server):
    webhook_server.state.statuses = [200]
    make_webhook(webhook_server.url)

    with DjangoTestCase.captureOnCommitCallbacks(execute=True):
        make_user()

    delivery = WebhookDelivery.objects.get()
    assert delivery.status == WebhookDelivery.StatusChoices.SUCCEEDED
    assert delivery.event == "CREATE"
    assert delivery.ref == USER_REF
    assert len(webhook_server.state.requests) == 1


def test_signal_path_skips_disabled_webhook(fast_webhook_backoff, webhook_server):
    make_webhook(webhook_server.url, enabled=False)

    with DjangoTestCase.captureOnCommitCallbacks(execute=True):
        make_user()

    assert WebhookDelivery.objects.count() == 0
    assert webhook_server.state.requests == []


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def test_admin_failed_delivery_filter_change_page_and_replays(webhook_server, admin_client):
    os.environ["ARCHIVEBOX_WEBHOOK_MAX_ATTEMPTS"] = "1"
    os.environ["ARCHIVEBOX_WEBHOOK_BACKOFF_BASE"] = "0"
    webhook_server.state.statuses = [500]
    # Two endpoints (unique ref+endpoint pairs) so both replay entry points can
    # be exercised against their own failed delivery.
    hook_a = make_webhook(webhook_server.url + "/a")
    hook_b = make_webhook(webhook_server.url + "/b")
    user = make_user()
    dispatches = dispatch_outbound_webhooks(instance=user, data=delivery_payload(user), method="CREATE")
    assert len(dispatches) == 2
    delivery_a = WebhookDelivery.objects.get(webhook=hook_a)
    delivery_b = WebhookDelivery.objects.get(webhook=hook_b)
    for delivery in (delivery_a, delivery_b):
        assert delivery.status == WebhookDelivery.StatusChoices.FAILED
        assert delivery.num_attempts == 1

    changelist_url = reverse("admin:api_webhookdelivery_changelist")
    response = admin_client.get(
        changelist_url,
        {"status__exact": WebhookDelivery.StatusChoices.FAILED},
        HTTP_HOST=ADMIN_TEST_HOST,
    )
    assert response.status_code == 200
    assert str(delivery_a.id).encode() in response.content
    assert hook_a.name.encode() in response.content

    # Attempts admin and the delivery change page render (Replay button present).
    assert admin_client.get(reverse("admin:api_webhookdeliveryattempt_changelist"), HTTP_HOST=ADMIN_TEST_HOST).status_code == 200
    change_url = reverse("admin:api_webhookdelivery_change", args=[delivery_a.pk])
    change_page = admin_client.get(change_url, HTTP_HOST=ADMIN_TEST_HOST)
    assert change_page.status_code == 200
    assert b"Replay" in change_page.content

    webhook_server.state.statuses = [200]

    # 1) Per-object replay button (django-object-actions POST endpoint).
    action_url = reverse("admin:api_webhookdelivery_actions", args=[delivery_a.pk, "replay_delivery"])
    response = admin_client.post(action_url, HTTP_HOST=ADMIN_TEST_HOST)
    assert response.status_code == 302
    delivery_a.refresh_from_db()
    assert delivery_a.status == WebhookDelivery.StatusChoices.SUCCEEDED
    assert [(a.attempt_number, a.status_code) for a in attempt_rows(delivery_a)] == [(1, 500), (2, 200)]

    # 2) Bulk changelist replay only touches remaining failed deliveries.
    response = admin_client.post(
        changelist_url,
        {
            "action": "replay_selected_deliveries",
            "_selected_action": [str(delivery_a.pk), str(delivery_b.pk)],
            "post": "yes",
        },
        HTTP_HOST=ADMIN_TEST_HOST,
        follow=True,
    )
    assert response.status_code == 200
    delivery_b.refresh_from_db()
    assert delivery_b.status == WebhookDelivery.StatusChoices.SUCCEEDED
    assert [(a.attempt_number, a.status_code) for a in attempt_rows(delivery_b)] == [(1, 500), (2, 200)]
    delivery_a.refresh_from_db()
    assert delivery_a.num_attempts == 2  # succeeded delivery was skipped, history untouched
