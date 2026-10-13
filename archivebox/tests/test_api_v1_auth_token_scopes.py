"""Tests for fine-grained APIToken scopes, resource paths, lifecycle and usage tracking."""

from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.utils import timezone

from archivebox.api.models import APIToken, INSUFFICIENT_TOKEN_SCOPE_MESSAGE
from archivebox.core.models import Tag
from archivebox.tests.conftest import API_TEST_HOST, api_client_request

pytestmark = pytest.mark.django_db(transaction=True)


TAGS_PATH = "/api/v1/core/tags"
TAG_CREATE_PATH = "/api/v1/core/tags/create/"
SNAPSHOTS_PATH = "/api/v1/core/snapshots"
MACHINES_PATH = "/api/v1/machine/machines"
CRAWLS_PATH = "/api/v1/crawls/crawls"
BROWSER_SESSION_PATH = "/api/v1/auth/browser_session"
TOKENS_PATH = "/api/v1/auth/tokens"


def make_token(api_admin_user, **kwargs):
    kwargs.setdefault("created_by", api_admin_user)
    kwargs.setdefault("expires", timezone.now() + timedelta(days=1))
    return APIToken.objects.create(**kwargs)


def token_headers(token):
    return {"HTTP_HOST": API_TEST_HOST, "HTTP_X_ARCHIVEBOX_API_KEY": token.token}


def call(client, method, path, token, payload=None):
    return api_client_request(
        client,
        method,
        path,
        payload=payload,
        headers=token_headers(token) if token is not None else {"HTTP_HOST": API_TEST_HOST},
    )


def make_tag(api_admin_user, name):
    return Tag.objects.create(name=name, created_by=api_admin_user)


### Scope tiers ###############################################################


def test_read_scope_allows_safe_requests_only(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.READ)

    assert call(client, "get", TAGS_PATH, token).status_code == 200
    assert call(client, "get", SNAPSHOTS_PATH, token).status_code == 200

    response = call(client, "post", TAG_CREATE_PATH, token, payload={"name": "read-scope-tag"})
    assert response.status_code == 403
    assert INSUFFICIENT_TOKEN_SCOPE_MESSAGE.encode() in response.content

    tag = make_tag(api_admin_user, "read-scope-delete")
    response = call(client, "delete", f"/api/v1/core/tag/{tag.pk}", token)
    assert response.status_code == 403
    assert Tag.objects.filter(pk=tag.pk).exists()


def test_write_scope_allows_creates_but_not_deletes(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.WRITE)

    response = call(client, "post", TAG_CREATE_PATH, token, payload={"name": "write-scope-tag"})
    assert response.status_code == 200
    assert response.json()["created"] is True

    tag = make_tag(api_admin_user, "write-scope-delete")
    response = call(client, "delete", f"/api/v1/core/tag/{tag.pk}", token)
    assert response.status_code == 403
    assert INSUFFICIENT_TOKEN_SCOPE_MESSAGE.encode() in response.content
    assert Tag.objects.filter(pk=tag.pk).exists()


def test_admin_scope_allows_all_methods(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.ADMIN)

    tag = make_tag(api_admin_user, "admin-scope-delete")
    response = call(client, "delete", f"/api/v1/core/tag/{tag.pk}", token)
    assert response.status_code == 200, response.content
    assert not Tag.objects.filter(pk=tag.pk).exists()


def test_scope_denial_is_consistent_across_routers(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.READ)

    core_response = call(client, "post", TAG_CREATE_PATH, token, payload={"name": "deny-core"})
    crawls_response = call(client, "post", CRAWLS_PATH, token, payload={"urls": ["https://example.com"]})

    assert core_response.status_code == 403
    assert crawls_response.status_code == 403
    assert core_response.json()["detail"] == crawls_response.json()["detail"] == INSUFFICIENT_TOKEN_SCOPE_MESSAGE


### Resource path allowlists #################################################


def test_resource_paths_restrict_by_path_and_method(client, api_admin_user):
    token = make_token(
        api_admin_user,
        scope=APIToken.ScopeChoices.ADMIN,
        resource_paths=[{"path": "/core/tags", "methods": ["GET"]}],
    )

    assert call(client, "get", TAGS_PATH, token).status_code == 200
    assert call(client, "get", SNAPSHOTS_PATH, token).status_code == 403
    assert call(client, "get", MACHINES_PATH, token).status_code == 403

    response = call(client, "post", TAG_CREATE_PATH, token, payload={"name": "path-restricted"})
    assert response.status_code == 403


def test_resource_paths_match_on_segment_boundaries(client, api_admin_user):
    tag = make_tag(api_admin_user, "segment-boundary-tag")
    token = make_token(
        api_admin_user,
        scope=APIToken.ScopeChoices.ADMIN,
        resource_paths=[{"path": "/core/tag", "methods": ["GET"]}],
    )

    # singular /core/tag/{id} and subpaths are covered ...
    assert call(client, "get", f"/api/v1/core/tag/{tag.pk}", token).status_code == 200
    # ... but the distinct plural collection route is not.
    assert call(client, "get", TAGS_PATH, token).status_code == 403


def test_resource_paths_cover_dotted_subroutes_and_api_prefix(client, api_admin_user):
    token = make_token(
        api_admin_user,
        scope=APIToken.ScopeChoices.READ,
        resource_paths=[{"path": "/api/v1/core/snapshots", "methods": ["GET"]}],
    )

    # Stored /api/v1 prefix normalizes away; dotted same-segment routes stay covered.
    assert call(client, "get", SNAPSHOTS_PATH, token).status_code == 200
    assert call(client, "get", "/api/v1/core/snapshots.rss", token).status_code == 200
    assert call(client, "get", TAGS_PATH, token).status_code == 403


### Expiry / revocation ######################################################


def test_expired_token_does_not_authenticate(client, api_admin_user):
    token = make_token(api_admin_user, expires=timezone.now() - timedelta(seconds=1))

    response = call(client, "get", TAGS_PATH, token)
    assert response.status_code == 401

    check = api_client_request(
        client,
        "post",
        "/api/v1/auth/check_api_token",
        payload={"token": token.token},
        headers={"HTTP_HOST": API_TEST_HOST},
    )
    assert check.status_code == 200
    assert check.json()["success"] is False


def test_revoked_token_is_rejected_until_reenabled(client, api_admin_user):
    admin = make_token(api_admin_user, scope=APIToken.ScopeChoices.ADMIN)
    target = make_token(api_admin_user, scope=APIToken.ScopeChoices.READ, label="to-revoke")

    revoked = api_client_request(
        client,
        "post",
        f"{TOKENS_PATH}/{target.id}/revoke",
        payload={},
        headers=token_headers(admin),
    )
    assert revoked.status_code == 200, revoked.content
    assert revoked.json()["token"]["status"] == APIToken.StatusChoices.REVOKED

    target.refresh_from_db()
    assert target.status == APIToken.StatusChoices.REVOKED
    response = call(client, "get", TAGS_PATH, target)
    assert response.status_code == 401

    reenabled = api_client_request(
        client,
        "post",
        f"{TOKENS_PATH}/{target.id}/reenable",
        payload={},
        headers=token_headers(admin),
    )
    assert reenabled.status_code == 200, reenabled.content
    assert reenabled.json()["token"]["status"] == APIToken.StatusChoices.ACTIVE
    assert call(client, "get", TAGS_PATH, target).status_code == 200


### last_used tracking #######################################################


def test_last_used_metadata_is_updated_on_authentication(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.READ)
    assert token.last_used_at is None
    assert token.last_used_ip is None

    response = call(client, "get", TAGS_PATH, token)
    assert response.status_code == 200

    token.refresh_from_db()
    assert token.last_used_at is not None
    assert token.last_used_at >= token.created_at - timedelta(seconds=5)
    assert token.last_used_ip == "127.0.0.1"


### Browser session exchange boundary #######################################


@pytest.mark.parametrize("scope", [APIToken.ScopeChoices.READ, APIToken.ScopeChoices.WRITE])
def test_browser_session_rejects_non_admin_scopes(client, api_admin_user, scope):
    token = make_token(api_admin_user, scope=scope)
    before = Session.objects.count()
    response = call(client, "post", BROWSER_SESSION_PATH, token, payload={})
    assert response.status_code == 403, response.content
    assert Session.objects.count() == before


def test_browser_session_rejects_admin_scope_with_resource_paths(client, api_admin_user):
    token = make_token(
        api_admin_user,
        scope=APIToken.ScopeChoices.ADMIN,
        resource_paths=[{"path": "/core/snapshots", "methods": ["GET"]}],
    )
    before = Session.objects.count()
    response = call(client, "post", BROWSER_SESSION_PATH, token, payload={})
    assert response.status_code == 403
    assert Session.objects.count() == before


def test_browser_session_accepts_explicit_admin_and_legacy_tokens(client, api_admin_user):
    admin_token = make_token(api_admin_user, scope=APIToken.ScopeChoices.ADMIN)
    legacy_token = make_token(api_admin_user, scope=None)  # historical, pre-scope token

    for token in (admin_token, legacy_token):
        response = call(client, "post", BROWSER_SESSION_PATH, token, payload={})
        assert response.status_code == 200, response.content
        data = response.json()
        assert Session.objects.filter(session_key=data["cookie"]["value"]).exists()


### Legacy token migration ###################################################


def test_legacy_unscoped_token_has_admin_rights_and_migrates_on_update(client, api_admin_user):
    token = make_token(api_admin_user, scope=None)
    assert token.effective_scope == APIToken.ScopeChoices.ADMIN
    assert token.is_full_admin is True

    tag = make_tag(api_admin_user, "legacy-delete")
    response = call(client, "delete", f"/api/v1/core/tag/{tag.pk}", token)
    assert response.status_code == 200, response.content

    # Usage stamping must not itself migrate the legacy scope.
    token.refresh_from_db()
    assert token.scope is None
    assert token.last_used_at is not None

    response = api_client_request(
        client,
        "patch",
        f"{TOKENS_PATH}/{token.id}",
        payload={"label": "migrated now"},
        headers=token_headers(token),
    )
    assert response.status_code == 200, response.content
    assert response.json()["scope"] == APIToken.ScopeChoices.ADMIN

    token.refresh_from_db()
    assert token.scope == APIToken.ScopeChoices.ADMIN
    assert token.label == "migrated now"


### Token management endpoints ##############################################


def test_token_management_requires_full_admin(client, api_admin_user):
    read_token = make_token(api_admin_user, scope=APIToken.ScopeChoices.READ)
    write_token = make_token(api_admin_user, scope=APIToken.ScopeChoices.WRITE)

    assert call(client, "get", TOKENS_PATH, read_token).status_code == 403
    assert call(client, "get", TOKENS_PATH, write_token).status_code == 403
    assert call(client, "post", TOKENS_PATH, write_token, payload={"scope": "read"}).status_code == 403

    path_limited_admin = make_token(
        api_admin_user,
        scope=APIToken.ScopeChoices.ADMIN,
        resource_paths=[{"path": "/auth/tokens", "methods": ["GET", "POST"]}],
    )
    assert call(client, "get", TOKENS_PATH, path_limited_admin).status_code == 403


def test_admin_can_create_scoped_token_and_metadata_endpoints_work(client, api_admin_user):
    admin = make_token(api_admin_user, scope=APIToken.ScopeChoices.ADMIN, label="boss key")

    created = api_client_request(
        client,
        "post",
        TOKENS_PATH,
        payload={
            "label": "ci read key",
            "scope": "read",
            "resource_paths": [{"path": "/core/tags", "methods": ["get"]}],
            "expires_in_days": 7,
        },
        headers=token_headers(admin),
    )
    assert created.status_code == 200, created.content
    created_data = created.json()
    assert len(created_data["token"]) == 32
    assert created_data["scope"] == "read"
    assert created_data["label"] == "ci read key"
    assert created_data["resource_paths"] == [{"path": "/core/tags", "methods": ["GET"]}]
    assert created_data["expires"] is not None

    new_token = APIToken.objects.get(id=created_data["id"])
    assert call(client, "get", TAGS_PATH, new_token).status_code == 200
    assert call(client, "post", TAG_CREATE_PATH, new_token, payload={"name": "nope"}).status_code == 403

    listing = call(client, "get", TOKENS_PATH, admin)
    assert listing.status_code == 200
    listed_ids = {row["id"] for row in listing.json()}
    assert str(new_token.id) in listed_ids and str(admin.id) in listed_ids

    detail = call(client, "get", f"{TOKENS_PATH}/{new_token.id}", admin)
    assert detail.status_code == 200
    assert detail.json()["token_redacted"].startswith("************")
    assert "token" not in detail.json()


def test_token_creation_rejects_invalid_scope_and_paths(client, api_admin_user):
    admin = make_token(api_admin_user, scope=APIToken.ScopeChoices.ADMIN)

    bad_scope = api_client_request(
        client,
        "post",
        TOKENS_PATH,
        payload={"scope": "superuser"},
        headers=token_headers(admin),
    )
    assert bad_scope.status_code == 400

    bad_paths = api_client_request(
        client,
        "post",
        TOKENS_PATH,
        payload={"scope": "read", "resource_paths": [{"path": "", "methods": ["GET"]}]},
        headers=token_headers(admin),
    )
    assert bad_paths.status_code == 400


### check_api_token ##########################################################


def test_check_api_token_reports_scope_and_status(client, api_admin_user):
    token = make_token(api_admin_user, scope=APIToken.ScopeChoices.WRITE)

    response = api_client_request(
        client,
        "post",
        "/api/v1/auth/check_api_token",
        payload={"token": token.token},
        headers={"HTTP_HOST": API_TEST_HOST},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["scope"] == "write"
    assert data["status"] == "active"
