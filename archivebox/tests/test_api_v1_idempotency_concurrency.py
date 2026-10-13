"""Concurrency Idempotency-Key tests using real threads and the real lifecycle lock."""

import json
import threading
import time
from datetime import timedelta

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone

from archivebox.api.idempotency import IDEMPOTENCY_IN_FLIGHT_MAX_AGE, canonical_request_fingerprint
from archivebox.api.models import IDEMPOTENCY_STATUS_COMPLETED, IDEMPOTENCY_STATUS_PENDING, IdempotencyKey
from archivebox.core.models import Snapshot
from archivebox.crawls.locks import crawl_lifecycle_lock
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)


def idem_headers(api_headers: dict, key: str) -> dict:
    return {**api_headers, "HTTP_IDEMPOTENCY_KEY": key}


def test_in_flight_key_blocks_concurrent_duplicate_until_first_delete_finishes(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/concurrent-idempotent-delete", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/concurrent-idempotent-delete", crawl=crawl)
    snapshot_id = str(snapshot.id)
    crawl_id = str(crawl.id)

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_crawl_lock():
        with crawl_lifecycle_lock(crawl_id):
            lock_acquired.set()
            release_lock.wait(timeout=30)

    holder = threading.Thread(target=hold_crawl_lock, name="hold-lifecycle-lock", daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=5)

    first_result: dict = {}

    def send_first_delete():
        worker_client = Client()
        first_result["response"] = worker_client.delete(
            f"/api/v1/core/snapshot/{snapshot_id}",
            **idem_headers(api_headers, "concurrent-delete-key"),
        )

    first_thread = threading.Thread(target=send_first_delete, name="first-delete", daemon=True)
    first_thread.start()

    # The pending claim is committed before the view blocks on the lifecycle lock.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = IdempotencyKey.objects.filter(status=IDEMPOTENCY_STATUS_PENDING).first()
        if pending is not None and pending.key == "concurrent-delete-key":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("first request never committed its pending idempotency claim")

    try:
        concurrent = client.delete(
            f"/api/v1/core/snapshot/{snapshot_id}",
            **idem_headers(api_headers, "concurrent-delete-key"),
        )

        assert concurrent.status_code == 409, concurrent.content
        assert concurrent["Idempotency-Status"] == "in-progress"
        assert concurrent["Retry-After"] == "2"
        # The duplicate must not have deleted anything.
        assert Snapshot.objects.filter(id=snapshot_id).exists()
        assert IdempotencyKey.objects.count() == 1
    finally:
        release_lock.set()
        first_thread.join(timeout=60)
        holder.join(timeout=10)

    first_response = first_result["response"]
    assert first_response.status_code == 200, first_response.content
    assert not Snapshot.objects.filter(id=snapshot_id).exists()

    # After completion the same key replays the first response instead of
    # attempting a second delete (which would 404).
    replay = client.delete(
        f"/api/v1/core/snapshot/{snapshot_id}",
        **idem_headers(api_headers, "concurrent-delete-key"),
    )
    assert replay.status_code == 200, replay.content
    assert replay.content == first_response.content
    assert replay["Idempotency-Status"] == "replayed"
    assert IdempotencyKey.objects.count() == 1
    assert IdempotencyKey.objects.get().status == IDEMPOTENCY_STATUS_COMPLETED


def test_stale_pending_claim_is_reclaimed_by_single_new_request(client, api_admin_user, api_headers):
    url = "https://example.com/stale-pending-reclaim"
    body = json.dumps({"url": url, "depth": 0})
    factory_request = RequestFactory().post("/api/v1/core/snapshots", data=body, content_type="application/json")
    request_hash = canonical_request_fingerprint(factory_request)

    stale_row = IdempotencyKey.objects.create(
        created_by=api_admin_user,
        key="stale-claim-key",
        request_method="POST",
        request_path="/api/v1/core/snapshots",
        request_hash=request_hash,
        status=IDEMPOTENCY_STATUS_PENDING,
        locked_at=timezone.now() - (IDEMPOTENCY_IN_FLIGHT_MAX_AGE + timedelta(minutes=1)),
    )

    response = client.post(
        "/api/v1/core/snapshots",
        data=body,
        content_type="application/json",
        **idem_headers(api_headers, "stale-claim-key"),
    )

    assert response.status_code == 200, response.content
    assert Snapshot.objects.filter(url=url).count() == 1
    stale_row.refresh_from_db()
    assert stale_row.status == IDEMPOTENCY_STATUS_COMPLETED
    assert stale_row.locked_at >= timezone.now() - timedelta(seconds=30)
    assert IdempotencyKey.objects.count() == 1


def test_concurrent_stale_pending_claims_result_in_exactly_one_side_effect(api_admin_user, api_headers):
    url = "https://example.com/concurrent-stale-reclaim"
    body = json.dumps({"url": url, "depth": 0})
    factory_request = RequestFactory().post("/api/v1/core/snapshots", data=body, content_type="application/json")
    request_hash = canonical_request_fingerprint(factory_request)

    IdempotencyKey.objects.create(
        created_by=api_admin_user,
        key="concurrent-stale-key",
        request_method="POST",
        request_path="/api/v1/core/snapshots",
        request_hash=request_hash,
        status=IDEMPOTENCY_STATUS_PENDING,
        locked_at=timezone.now() - (IDEMPOTENCY_IN_FLIGHT_MAX_AGE + timedelta(minutes=1)),
    )

    barrier = threading.Barrier(2)
    responses: list = []

    def send_request():
        try:
            barrier.wait(timeout=10)
            worker_client = Client()
            responses.append(
                worker_client.post(
                    "/api/v1/core/snapshots",
                    data=body,
                    content_type="application/json",
                    **idem_headers(api_headers, "concurrent-stale-key"),
                ),
            )
        except Exception as err:  # pragma: no cover - surfaced by assertions below
            responses.append(err)

    threads = [threading.Thread(target=send_request, name=f"stale-reclaim-{index}", daemon=True) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(responses) == 2
    assert all(not isinstance(item, Exception) for item in responses), responses
    status_codes = sorted(response.status_code for response in responses)

    # Exactly one side effect no matter the interleaving: the loser of the CAS
    # either saw the fresh pending claim (409 in-progress) or arrived after the
    # winner finished (200 replay); it never executed a second time.
    assert status_codes.count(200) >= 1
    assert all(code in (200, 409) for code in status_codes), status_codes
    assert Snapshot.objects.filter(url=url).count() == 1
    assert IdempotencyKey.objects.count() == 1
    if status_codes == [200, 200]:
        replayed = [response for response in responses if response["Idempotency-Status"] == "replayed"]
        assert len(replayed) == 1
    else:
        assert status_codes == [200, 409]
        in_progress = next(response for response in responses if response.status_code == 409)
        assert in_progress["Idempotency-Status"] == "in-progress"

    assert IdempotencyKey.objects.get().status == IDEMPOTENCY_STATUS_COMPLETED
