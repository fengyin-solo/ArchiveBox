"""End-to-end Idempotency-Key replay tests for all guarded write routes."""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

from archivebox.api.models import IdempotencyKey
from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)


def idem_headers(api_headers: dict, key: str) -> dict:
    return {**api_headers, "HTTP_IDEMPOTENCY_KEY": key}


def assert_replayed(first_response, second_response):
    assert second_response.status_code == first_response.status_code
    assert second_response.content == first_response.content
    assert second_response["Idempotency-Status"] == "replayed"
    assert second_response["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in first_response


def test_create_snapshot_replays_first_response_without_second_side_effect(client, api_headers):
    payload = {"url": "https://example.com/idempotent-create-snapshot", "depth": 0, "tags": ["idem-create"]}

    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-create-key"),
    )
    second = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-create-key"),
    )

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    assert Snapshot.objects.filter(url=payload["url"]).count() == 1
    assert Crawl.objects.filter(urls=payload["url"]).count() == 1
    assert IdempotencyKey.objects.count() == 1
    assert Snapshot.objects.get(url=payload["url"]).tags.filter(name="idem-create").exists()


def test_patch_snapshot_replays_first_response_without_reapplying_update(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-patch-snapshot", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/idempotent-patch-snapshot", crawl=crawl)
    payload = {"tags": ["idem-patched"]}

    first = client.patch(
        f"/api/v1/core/snapshot/{snapshot.id}",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-patch-key"),
    )
    snapshot.refresh_from_db()
    modified_after_first = snapshot.modified_at

    second = client.patch(
        f"/api/v1/core/snapshot/{snapshot.id}",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-patch-key"),
    )

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    snapshot.refresh_from_db()
    assert list(snapshot.tags.values_list("name", flat=True)) == ["idem-patched"]
    # The replay returned the first response body (including its timestamps)
    # and did not run the update a second time.
    assert snapshot.modified_at == modified_after_first
    assert IdempotencyKey.objects.count() == 1


def test_delete_snapshot_replay_returns_first_deletion_response_not_404(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-delete-snapshot", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/idempotent-delete-snapshot", crawl=crawl)
    snapshot_id = str(snapshot.id)

    first = client.delete(
        f"/api/v1/core/snapshot/{snapshot_id}",
        **idem_headers(api_headers, "snapshot-delete-key"),
    )
    assert first.status_code == 200, first.content
    assert not Snapshot.objects.filter(id=snapshot_id).exists()

    second = client.delete(
        f"/api/v1/core/snapshot/{snapshot_id}",
        **idem_headers(api_headers, "snapshot-delete-key"),
    )

    assert_replayed(first, second)
    assert second.json()["success"] is True
    assert not Snapshot.objects.filter(id=snapshot_id).exists()
    assert IdempotencyKey.objects.count() == 1


def test_create_archiveresult_multipart_replay_writes_file_once(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-upload", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/idempotent-upload", crawl=crawl)
    contents = b"idempotent upload contents"

    def upload():
        return client.post(
            "/api/v1/core/archiveresults",
            data={
                "snapshot_id": str(snapshot.id),
                "plugin": "custom.upload",
                "hook_name": "on_Snapshot__archivebox_browser_extension_upload",
                "output_paths": "result.txt",
                "files": SimpleUploadedFile("result.txt", contents, content_type="text/plain"),
            },
            **idem_headers(api_headers, "archiveresult-create-key"),
        )

    first = upload()
    second = upload()

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    results = ArchiveResult.objects.filter(snapshot=snapshot, plugin="custom.upload")
    assert results.count() == 1
    assert (snapshot.output_dir / "custom.upload" / "result.txt").read_bytes() == contents


def test_patch_archiveresult_multipart_replay_appends_file_once(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-upload-patch", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/idempotent-upload-patch", crawl=crawl)
    result = ArchiveResult.objects.create(snapshot=snapshot, plugin="custom.patch")
    contents = b"idempotent patch contents"

    def patch_upload():
        return client.patch(
            f"/api/v1/core/archiveresult/{result.id}",
            data=encode_multipart(
                BOUNDARY,
                {
                    "output_paths": "patched.txt",
                    "files": SimpleUploadedFile("patched.txt", contents, content_type="text/plain"),
                },
            ),
            content_type=MULTIPART_CONTENT,
            **idem_headers(api_headers, "archiveresult-patch-key"),
        )

    first = patch_upload()
    second = patch_upload()

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    result.refresh_from_db()
    assert result.output_files["patched.txt"]["size"] == len(contents)
    assert (snapshot.output_dir / "custom.patch" / "patched.txt").read_bytes() == contents
    assert ArchiveResult.objects.filter(snapshot=snapshot).count() == 1


def test_create_crawl_replays_first_response_without_second_crawl(client, api_headers):
    payload = {"urls": ["https://example.com/idempotent-create-crawl"], "max_depth": 0}

    first = client.post(
        "/api/v1/crawls/crawls",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "crawl-create-key"),
    )
    second = client.post(
        "/api/v1/crawls/crawls",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "crawl-create-key"),
    )

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    assert Crawl.objects.filter(urls="https://example.com/idempotent-create-crawl").count() == 1


def test_patch_crawl_replays_first_response(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-patch-crawl", created_by=api_admin_user)
    payload = {"tags": ["idem-crawl-tag"]}

    first = client.patch(
        f"/api/v1/crawls/crawl/{crawl.id}",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "crawl-patch-key"),
    )
    crawl.refresh_from_db()
    modified_after_first = crawl.modified_at

    second = client.patch(
        f"/api/v1/crawls/crawl/{crawl.id}",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "crawl-patch-key"),
    )

    assert first.status_code == 200, first.content
    assert_replayed(first, second)
    crawl.refresh_from_db()
    assert crawl.tags_str == "idem-crawl-tag"
    assert crawl.modified_at == modified_after_first


def test_delete_crawl_replay_returns_first_deletion_response_not_404(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/idempotent-delete-crawl", created_by=api_admin_user)
    crawl_id = str(crawl.id)

    first = client.delete(
        f"/api/v1/crawls/crawl/{crawl_id}",
        **idem_headers(api_headers, "crawl-delete-key"),
    )
    assert first.status_code == 200, first.content
    assert not Crawl.objects.filter(id=crawl_id).exists()

    second = client.delete(
        f"/api/v1/crawls/crawl/{crawl_id}",
        **idem_headers(api_headers, "crawl-delete-key"),
    )

    assert_replayed(first, second)
    assert second.json()["success"] is True
    assert not Crawl.objects.filter(id=crawl_id).exists()
    assert IdempotencyKey.objects.count() == 1


def test_failed_first_request_response_is_replayed_identically(client, api_headers):
    payload = {"url": "", "depth": 0}

    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-invalid-key"),
    )
    second = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "snapshot-invalid-key"),
    )

    assert first.status_code == 400, first.content
    assert_replayed(first, second)
    assert Snapshot.objects.count() == 0
    assert IdempotencyKey.objects.count() == 1
