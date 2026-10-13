"""Idempotency-Key request-mismatch and in-progress conflict tests."""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from archivebox.api.models import IDEMPOTENCY_STATUS_PENDING, IdempotencyKey
from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)


def idem_headers(api_headers: dict, key: str) -> dict:
    return {**api_headers, "HTTP_IDEMPOTENCY_KEY": key}


def test_same_key_with_different_json_body_returns_conflict_then_original_replays(client, api_headers):
    first_payload = {"url": "https://example.com/idempotency-conflict-one", "depth": 0}
    second_payload = {"url": "https://example.com/idempotency-conflict-two", "depth": 0}

    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(first_payload),
        content_type="application/json",
        **idem_headers(api_headers, "conflict-key"),
    )
    conflict = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(second_payload),
        content_type="application/json",
        **idem_headers(api_headers, "conflict-key"),
    )
    replay = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(first_payload),
        content_type="application/json",
        **idem_headers(api_headers, "conflict-key"),
    )

    assert first.status_code == 200, first.content
    assert conflict.status_code == 409, conflict.content
    assert conflict["Idempotency-Status"] == "conflict"
    assert conflict.json()["code"] == "idempotency_key_request_mismatch"
    assert "different request body" in conflict.json()["detail"]
    assert Snapshot.objects.filter(url=first_payload["url"]).count() == 1
    assert Snapshot.objects.filter(url=second_payload["url"]).count() == 0

    assert replay.status_code == 200, replay.content
    assert replay.content == first.content
    assert replay["Idempotency-Status"] == "replayed"
    assert IdempotencyKey.objects.count() == 1


def test_same_key_with_different_multipart_body_returns_conflict_without_changing_files(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotency-upload-conflict", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/idempotency-upload-conflict", crawl=crawl)
    original_contents = b"original file contents"

    first = client.post(
        "/api/v1/core/archiveresults",
        data={
            "snapshot_id": str(snapshot.id),
            "plugin": "custom.conflict",
            "output_paths": "file.txt",
            "files": SimpleUploadedFile("file.txt", original_contents, content_type="text/plain"),
        },
        **idem_headers(api_headers, "upload-conflict-key"),
    )
    conflict = client.post(
        "/api/v1/core/archiveresults",
        data={
            "snapshot_id": str(snapshot.id),
            "plugin": "custom.conflict",
            "output_paths": "file.txt",
            "files": SimpleUploadedFile("file.txt", b"different file contents entirely", content_type="text/plain"),
        },
        **idem_headers(api_headers, "upload-conflict-key"),
    )

    assert first.status_code == 200, first.content
    assert conflict.status_code == 409, conflict.content
    assert conflict["Idempotency-Status"] == "conflict"
    assert ArchiveResult.objects.filter(snapshot=snapshot, plugin="custom.conflict").count() == 1
    assert (snapshot.output_dir / "custom.conflict" / "file.txt").read_bytes() == original_contents
    assert IdempotencyKey.objects.count() == 1


def test_pending_key_returns_in_progress_without_executing_side_effect(client, api_admin_user, api_headers):
    IdempotencyKey.objects.create(
        created_by=api_admin_user,
        key="pending-key",
        request_method="POST",
        request_path="/api/v1/core/snapshots",
        request_hash="will-not-match-without-a-row-insert",
        status=IDEMPOTENCY_STATUS_PENDING,
        locked_at=timezone.now(),
    )

    # Even a same-body request must not execute while the key is held pending.
    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": "https://example.com/while-pending", "depth": 0}),
        content_type="application/json",
        **idem_headers(api_headers, "pending-key"),
    )

    assert response.status_code == 409, response.content
    assert response["Idempotency-Status"] == "in-progress"
    assert response["Retry-After"] == "2"
    assert response.json()["code"] == "idempotency_key_in_progress"
    assert "still being processed" in response.json()["detail"]
    assert Snapshot.objects.filter(url="https://example.com/while-pending").count() == 0
    assert IdempotencyKey.objects.count() == 1
    # Sanity: token auth helper path matches what the middleware resolved.
    assert str(IdempotencyKey.objects.get().created_by_id) == str(api_admin_user.pk)


def test_in_progress_conflict_also_reported_for_different_body_against_pending_row(client, api_admin_user, api_headers):
    # A pending row claims the namespace regardless of body: callers must wait
    # for the first outcome rather than racing a second body in.
    IdempotencyKey.objects.create(
        created_by=api_admin_user,
        key="pending-body-key",
        request_method="POST",
        request_path="/api/v1/core/snapshots",
        request_hash="first-body-hash",
        status=IDEMPOTENCY_STATUS_PENDING,
        locked_at=timezone.now(),
    )

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": "https://example.com/pending-other-body", "depth": 0}),
        content_type="application/json",
        **idem_headers(api_headers, "pending-body-key"),
    )

    assert response.status_code == 409
    assert response["Idempotency-Status"] == "in-progress"
    assert Snapshot.objects.count() == 0


def test_fresh_pending_row_from_other_creator_does_not_block_this_creator(client, api_admin_user, api_headers):
    # Isolation proof at the conflict layer: another creator's pending row is
    # in a different namespace and must not interfere.
    from django.contrib.auth import get_user_model

    from archivebox.api.models import APIToken

    other_user = get_user_model().objects.create_superuser(
        username="other-owner",
        email="other-owner@example.com",
        password="testpass123",
    )
    APIToken.objects.create(created_by=other_user, token="0" * 32)
    IdempotencyKey.objects.create(
        created_by=other_user,
        key="shared-key-string",
        request_method="POST",
        request_path="/api/v1/core/snapshots",
        request_hash="other-hash",
        status=IDEMPOTENCY_STATUS_PENDING,
        locked_at=timezone.now(),
    )

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": "https://example.com/owner-b", "depth": 0}),
        content_type="application/json",
        **idem_headers(api_headers, "shared-key-string"),
    )

    assert response.status_code == 200, response.content
    assert Snapshot.objects.filter(url="https://example.com/owner-b").count() == 1
