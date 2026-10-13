"""Idempotency-Key isolation, expiry, restart persistence, and no-key compatibility."""

import json
import secrets
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from archivebox.api.models import APIToken, IdempotencyKey
from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import api_auth_headers


pytestmark = pytest.mark.django_db(transaction=True)


def idem_headers(api_headers: dict, key: str) -> dict:
    return {**api_headers, "HTTP_IDEMPOTENCY_KEY": key}


def make_second_api_client():
    user = get_user_model().objects.create_superuser(
        username="idem-second-owner",
        email="idem-second-owner@example.com",
        password="testpass123",
    )
    token = APIToken.objects.create(created_by=user, token=secrets.token_hex(16))
    from django.test import Client

    return user, Client(), api_auth_headers(token.token, django_client=True)


def test_idempotency_keys_are_isolated_per_creator(client, api_admin_user, api_headers):
    payload = {"url": "https://example.com/creator-isolated-key", "depth": 0}

    first_owner = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "shared-key"),
    )
    assert first_owner.status_code == 200, first_owner.content

    second_user, second_client, second_headers = make_second_api_client()
    second_owner = second_client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(second_headers, "shared-key"),
    )
    assert second_owner.status_code == 200, second_owner.content

    # Same key string, different creator: independent executions and replays.
    assert Snapshot.objects.filter(url=payload["url"]).count() == 2
    first_id = first_owner.json()["id"]
    second_id = second_owner.json()["id"]
    assert first_id != second_id

    first_replay = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "shared-key"),
    )
    second_replay = second_client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(second_headers, "shared-key"),
    )
    assert first_replay.json()["id"] == first_id
    assert second_replay.json()["id"] == second_id
    assert Snapshot.objects.filter(url=payload["url"]).count() == 2

    rows = IdempotencyKey.objects.filter(key="shared-key")
    assert rows.count() == 2
    assert {row.created_by_id for row in rows} == {api_admin_user.pk, second_user.pk}


def test_same_key_on_different_paths_and_methods_does_not_conflict(client, api_headers):
    snapshot_payload = {"url": "https://example.com/path-scoped-key", "depth": 0}
    crawl_payload = {"urls": ["https://example.com/path-scoped-key-crawl"], "max_depth": 0}

    snapshot_response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(snapshot_payload),
        content_type="application/json",
        **idem_headers(api_headers, "same-key-different-path"),
    )
    crawl_response = client.post(
        "/api/v1/crawls/crawls",
        data=json.dumps(crawl_payload),
        content_type="application/json",
        **idem_headers(api_headers, "same-key-different-path"),
    )

    assert snapshot_response.status_code == 200, snapshot_response.content
    assert crawl_response.status_code == 200, crawl_response.content
    paths = set(IdempotencyKey.objects.values_list("request_path", flat=True))
    assert paths == {"/api/v1/core/snapshots", "/api/v1/crawls/crawls"}


def test_expired_key_is_reclaimed_and_request_executes_again(client, api_headers):
    payload = {"url": "https://example.com/expired-key-reuse", "depth": 0}

    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "expiring-key"),
    )
    assert first.status_code == 200, first.content
    assert Snapshot.objects.filter(url=payload["url"]).count() == 1

    original_row = IdempotencyKey.objects.get()
    # Default retention is 30 days.
    assert original_row.expires_at is not None
    assert timezone.now() + timedelta(days=29) < original_row.expires_at <= timezone.now() + timedelta(days=30)

    IdempotencyKey.objects.filter(pk=original_row.pk).update(expires_at=timezone.now() - timedelta(days=1))

    second = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "expiring-key"),
    )

    assert second.status_code == 200, second.content
    assert "Idempotency-Replayed" not in second
    assert second.json()["id"] != first.json()["id"]
    # The create-snapshot endpoint opens a fresh Crawl when none is passed, so
    # re-execution creates a second Snapshot/Crawl pair.
    assert Snapshot.objects.filter(url=payload["url"]).count() == 2
    assert Crawl.objects.filter(urls=payload["url"]).count() == 2
    assert IdempotencyKey.objects.count() == 1
    assert str(IdempotencyKey.objects.get().id) != str(original_row.id)


def test_zero_retention_days_makes_completed_keys_expire_immediately(client, api_headers, settings):
    settings.CONFIG = settings.CONFIG.model_copy(update={"IDEMPOTENCY_KEY_RETENTION_DAYS": 0})

    payload = {"url": "https://example.com/zero-ttl-key", "depth": 0}
    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "zero-ttl-key"),
    )
    assert first.status_code == 200, first.content
    assert IdempotencyKey.objects.get().expires_at <= timezone.now() + timedelta(seconds=1)

    second = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "zero-ttl-key"),
    )
    assert second.status_code == 200, second.content
    assert "Idempotency-Replayed" not in second
    assert Snapshot.objects.filter(url=payload["url"]).count() == 2


def test_completed_key_survives_database_connection_restart_and_replays(client, api_headers):
    payload = {"url": "https://example.com/persisted-across-restart", "depth": 0}

    first = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "restart-persistence-key"),
    )
    assert first.status_code == 200, first.content
    assert IdempotencyKey.objects.count() == 1

    # Simulate a process restart: drop all in-process DB connections. The
    # claim lives entirely in the database, so replay still works.
    connection.close()

    replay = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "restart-persistence-key"),
    )
    assert replay.status_code == 200, replay.content
    assert replay.content == first.content
    assert replay["Idempotency-Status"] == "replayed"
    assert Snapshot.objects.filter(url=payload["url"]).count() == 1
    assert Crawl.objects.filter(urls=payload["url"]).count() == 1


def test_request_without_idempotency_key_keeps_existing_behavior(client, api_headers):
    payload = {"url": "https://example.com/no-key-unchanged", "depth": 0}

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **api_headers,
    )

    assert response.status_code == 200, response.content
    assert "Idempotency-Status" not in response
    assert "Idempotency-Replayed" not in response
    assert IdempotencyKey.objects.count() == 0


def test_request_with_whitespace_key_is_treated_as_unkeyed(client, api_headers):
    payload = {"url": "https://example.com/whitespace-key-unchanged", "depth": 0}

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **api_headers,
        HTTP_IDEMPOTENCY_KEY="   ",
    )

    assert response.status_code == 200, response.content
    assert "Idempotency-Status" not in response
    assert IdempotencyKey.objects.count() == 0


def test_oversized_idempotency_key_is_rejected_before_business_runs(client, api_headers):
    payload = {"url": "https://example.com/oversized-key", "depth": 0}

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps(payload),
        content_type="application/json",
        **idem_headers(api_headers, "k" * 256),
    )

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "idempotency_key_invalid"
    assert Snapshot.objects.filter(url=payload["url"]).count() == 0
    assert IdempotencyKey.objects.count() == 0


def test_key_header_on_non_whitelisted_and_get_routes_is_ignored(client, api_headers):
    # A write route that is not in the idempotency whitelist.
    tag_response = client.post(
        "/api/v1/core/tags/create/",
        data=json.dumps({"name": "not-idempotized-tag"}),
        content_type="application/json",
        **idem_headers(api_headers, "ignored-on-tags-route"),
    )
    assert tag_response.status_code == 200, tag_response.content
    assert "Idempotency-Status" not in tag_response

    # GET requests never create claims.
    get_response = client.get("/api/v1/core/snapshots", **idem_headers(api_headers, "ignored-on-get"))
    assert get_response.status_code == 200, get_response.content
    assert "Idempotency-Status" not in get_response

    assert IdempotencyKey.objects.count() == 0


def test_cors_preflight_advertises_idempotency_key_header(client, api_headers):
    response = client.options(
        "/api/v1/core/snapshots",
        **api_headers,
        HTTP_ORIGIN="http://example.com",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="idempotency-key,x-archivebox-api-key,content-type",
    )
    assert response.status_code == 204
    assert "Idempotency-Key" in response["Access-Control-Allow-Headers"]
