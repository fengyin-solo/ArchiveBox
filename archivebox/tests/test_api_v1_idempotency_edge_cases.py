"""Idempotency-Key hardening tests: fingerprint canonicity, auth ordering, purge, boundaries."""

import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test.client import encode_multipart
from django.utils import timezone

from archivebox.api.idempotency import canonical_request_fingerprint
from archivebox.api.models import IDEMPOTENCY_STATUS_COMPLETED, IDEMPOTENCY_STATUS_PENDING, IdempotencyKey
from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)


API_HOST_HEADERS = {"HTTP_HOST": "api.archivebox.localhost:5797"}


def idem_headers(api_headers: dict, key: str) -> dict:
    return {**api_headers, "HTTP_IDEMPOTENCY_KEY": key}


# ---------------------------------------------------------------------------
# Request fingerprint canonicity (TR-2.1)
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_multipart_transport_boundary():
    factory = RequestFactory()

    def build_request(boundary: str):
        data = {
            "plugin": "custom.fingerprint",
            "files": SimpleUploadedFile("f.txt", b"fingerprint contents", content_type="text/plain"),
        }
        return factory.post(
            "/api/v1/core/archiveresults",
            data=encode_multipart(boundary, data),
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    first = canonical_request_fingerprint(build_request("FirstBoundaryValue"))
    second = canonical_request_fingerprint(build_request("CompletelyDifferentBoundary"))
    assert first == second

    changed = build_request("FirstBoundaryValue")
    changed_data = {
        "plugin": "custom.fingerprint",
        "files": SimpleUploadedFile("f.txt", b"CHANGED file contents", content_type="text/plain"),
    }
    changed_request = RequestFactory().post(
        "/api/v1/core/archiveresults",
        data=encode_multipart("FirstBoundaryValue", changed_data),
        content_type="multipart/form-data; boundary=FirstBoundaryValue",
    )
    assert canonical_request_fingerprint(changed_request) != first
    assert changed is not None


def test_fingerprint_json_is_independent_of_key_order_and_unicode_escaping():
    factory = RequestFactory()
    first = factory.post(
        "/api/v1/core/snapshots",
        data=rb'{"url":"https://example.com/\u00e9","depth":0,"tags":["a"]}',
        content_type="application/json",
    )
    second = factory.post(
        "/api/v1/core/snapshots",
        data='{"tags": ["a"], "depth": 0, "url": "https://example.com/é"}'.encode(),
        content_type="application/json",
    )
    assert canonical_request_fingerprint(first) == canonical_request_fingerprint(second)

    changed = factory.post(
        "/api/v1/core/snapshots",
        data=b'{"url":"https://example.com/different","depth":0}',
        content_type="application/json",
    )
    assert canonical_request_fingerprint(changed) != canonical_request_fingerprint(first)


def test_fingerprint_is_deterministic_for_malformed_json():
    factory = RequestFactory()
    malformed = b'{"url": '
    first = factory.post("/api/v1/core/snapshots", data=malformed, content_type="application/json")
    second = factory.post("/api/v1/core/snapshots", data=malformed, content_type="application/json")
    assert canonical_request_fingerprint(first) == canonical_request_fingerprint(second)


# ---------------------------------------------------------------------------
# HTTP-level boundary-independence and malformed-body replay
# ---------------------------------------------------------------------------


def test_multipart_replay_works_across_different_transport_boundaries(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/boundary-proof", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/boundary-proof", crawl=crawl)
    contents = b"boundary-independent contents"

    def upload(boundary: str):
        return client.post(
            "/api/v1/core/archiveresults",
            data=encode_multipart(
                boundary,
                {
                    "snapshot_id": str(snapshot.id),
                    "plugin": "custom.boundary",
                    "output_paths": "out.txt",
                    "files": SimpleUploadedFile("out.txt", contents, content_type="text/plain"),
                },
            ),
            content_type=f"multipart/form-data; boundary={boundary}",
            **idem_headers(api_headers, "boundary-proof-key"),
        )

    first = upload("TransportBoundaryOne")
    second = upload("ADifferentTransportBoundaryTwo")

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    assert second.content == first.content
    assert second["Idempotency-Status"] == "replayed"
    assert ArchiveResult.objects.filter(snapshot=snapshot).count() == 1
    assert (snapshot.output_dir / "custom.boundary" / "out.txt").read_bytes() == contents


def test_malformed_json_first_response_is_replayed_deterministically(client, api_headers):
    malformed_body = b'{"url": '

    first = client.post(
        "/api/v1/core/snapshots",
        data=malformed_body,
        content_type="application/json",
        **idem_headers(api_headers, "malformed-json-key"),
    )
    second = client.post(
        "/api/v1/core/snapshots",
        data=malformed_body,
        content_type="application/json",
        **idem_headers(api_headers, "malformed-json-key"),
    )

    assert first.status_code in (400, 422), first.content
    assert second.status_code == first.status_code
    assert second.content == first.content
    assert second["Idempotency-Status"] == "replayed"
    assert Snapshot.objects.count() == 0


# ---------------------------------------------------------------------------
# Auth ordering and namespace boundaries
# ---------------------------------------------------------------------------


def test_anonymous_request_with_key_bypasses_idempotency_and_hits_route_auth(client):
    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": "https://example.com/anonymous-key", "depth": 0}),
        content_type="application/json",
        **API_HOST_HEADERS,
        HTTP_IDEMPOTENCY_KEY="anonymous-should-not-claim",
    )

    assert response.status_code == 401, response.content
    assert IdempotencyKey.objects.count() == 0


def test_anonymous_oversized_key_still_gets_route_auth_not_idempotency_400(client):
    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": "https://example.com/anonymous-oversized", "depth": 0}),
        content_type="application/json",
        **API_HOST_HEADERS,
        HTTP_IDEMPOTENCY_KEY="k" * 256,
    )

    assert response.status_code == 401, response.content
    assert IdempotencyKey.objects.count() == 0


def test_same_key_same_path_different_methods_are_independent(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/method-scoped", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/method-scoped", crawl=crawl)

    patch = client.patch(
        f"/api/v1/core/snapshot/{snapshot.id}",
        data=json.dumps({"tags": ["method-scoped-tag"]}),
        content_type="application/json",
        **idem_headers(api_headers, "same-path-different-method"),
    )
    assert patch.status_code == 200, patch.content

    delete = client.delete(
        f"/api/v1/core/snapshot/{snapshot.id}",
        **idem_headers(api_headers, "same-path-different-method"),
    )
    assert delete.status_code == 200, delete.content

    rows = IdempotencyKey.objects.order_by("request_method")
    assert [row.request_method for row in rows] == ["DELETE", "PATCH"]
    assert all(row.status == IDEMPOTENCY_STATUS_COMPLETED for row in rows)


def test_max_length_key_is_accepted(client, api_headers):
    payload = {"url": "https://example.com/max-length-key", "depth": 0}
    key = "k" * 255

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, key),
    )

    assert response.status_code == 200, response.content
    assert IdempotencyKey.objects.get(key=key)


def test_overlong_object_path_bypasses_idempotency_and_returns_route_404(client, api_headers):
    response = client.delete(
        f"/api/v1/core/snapshot/{'u' * 256}",
        **idem_headers(api_headers, "overlong-path-key"),
    )

    assert response.status_code == 404, response.content
    assert "Idempotency-Status" not in response
    assert IdempotencyKey.objects.count() == 0


# ---------------------------------------------------------------------------
# Expired-row sweep
# ---------------------------------------------------------------------------


def test_new_claim_sweeps_expired_rows_but_keeps_valid_and_pending_rows(client, api_admin_user, api_headers):
    def make_row(key: str, *, status: str, expires_at) -> IdempotencyKey:
        return IdempotencyKey.objects.create(
            created_by_id=api_admin_user.pk,
            key=key,
            request_method="POST",
            request_path="/api/v1/core/snapshots",
            request_hash=f"hash-{key}",
            status=status,
            locked_at=timezone.now(),
            expires_at=expires_at,
        )

    past = timezone.now() - timedelta(days=1)
    fresh = timezone.now() + timedelta(days=10)
    make_row("expired-1", status=IDEMPOTENCY_STATUS_COMPLETED, expires_at=past)
    make_row("expired-2", status=IDEMPOTENCY_STATUS_COMPLETED, expires_at=past)
    make_row("expired-3", status=IDEMPOTENCY_STATUS_COMPLETED, expires_at=past)
    make_row("still-valid", status=IDEMPOTENCY_STATUS_COMPLETED, expires_at=fresh)
    make_row("stuck-pending", status=IDEMPOTENCY_STATUS_PENDING, expires_at=None)

    payload = {"url": "https://example.com/purge-trigger", "depth": 0}
    trigger = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "purge-trigger"),
    )
    assert trigger.status_code == 200, trigger.content

    surviving_keys = set(IdempotencyKey.objects.values_list("key", flat=True))
    assert surviving_keys == {"still-valid", "stuck-pending", "purge-trigger"}
