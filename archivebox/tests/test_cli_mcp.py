#!/usr/bin/env python3
"""Tests for the ArchiveBox MCP server and CLI entry point."""

import json
import os
import uuid

import pytest

from archivebox.mcp.server import (
    DEFAULT_MCP_AUDIT_CAPACITY,
    DEFAULT_MCP_RESOURCES_PAGE_SIZE,
    MCPReadOnlyError,
    RESOURCE_NOT_FOUND_ERROR_CODE,
    RESOURCE_TYPES,
    READ_ONLY_ERROR_CODE,
    RESOURCE_FORBIDDEN_ERROR_CODE,
    MCPServer,
    build_resource_uri,
    decode_resource_cursor,
    encode_resource_cursor,
    invocation_is_read_only,
)
from archivebox.tests.conftest import run_archivebox_cmd


pytestmark = pytest.mark.django_db(transaction=True)


def create_resource_rows(*, crawls=0, snapshots=0, archiveresults=0):
    """Create real Crawl/Snapshot/ArchiveResult rows directly via the ORM."""

    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.crawls.models import Crawl

    user_pk = get_or_create_system_user_pk()
    created_crawls = [
        Crawl.objects.create(
            urls=f"https://mcp-resource-{index}.example.com/\n",
            created_by_id=user_pk,
            max_depth=0,
        )
        for index in range(crawls)
    ]
    crawl = (
        created_crawls[0]
        if created_crawls
        else Crawl.objects.create(
            urls="https://mcp-resource-parent.example.com/\n",
            created_by_id=user_pk,
        )
    )
    created_snapshots = [
        Snapshot.objects.create(
            url=f"https://mcp-snapshot-{index}.example.com/",
            crawl=crawl,
        )
        for index in range(snapshots)
    ]
    snapshot = (
        created_snapshots[0]
        if created_snapshots
        else Snapshot.objects.create(
            url="https://mcp-snapshot-parent.example.com/",
            crawl=crawl,
        )
    )
    created_results = [
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin=f"plugin_{index}",
            hook_name=f"on_Snapshot__plugin_{index}",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )
        for index in range(archiveresults)
    ]
    return created_crawls, created_snapshots, created_results


def read_all_resources(server):
    """Walk every resources/list page and return the collected uris."""

    uris = []
    cursor = None
    seen_cursors = set()
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = server.handle_resources_list(params)
        uris.extend(resource["uri"] for resource in page["resources"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
        assert cursor not in seen_cursors, "pagination cursor looped"
        seen_cursors.add(cursor)
    return uris


def test_mcp_help_runs_successfully(tmp_path):
    """The mcp command should be registered and expose help."""

    result = run_archivebox_cmd(["mcp", "--help"])

    assert result.returncode == 0
    assert "mcp" in result.stdout.lower()


def test_mcp_stdio_handles_handshake_notification_and_ping(initialized_archive):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "archivebox-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
    ]
    result = run_archivebox_cmd(
        ["mcp"],
        cwd=initialized_archive,
        input="".join(json.dumps(request) + "\n" for request in requests),
        default_cli_env=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]

    assert result.returncode == 0
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
    assert responses[1]["result"] == {}


def test_mcp_exposes_six_focused_tools():
    tools = MCPServer().handle_tools_list({})["tools"]
    tools_by_name = {tool["name"]: tool for tool in tools}

    assert set(tools_by_name) == {"add", "search", "crawl", "snapshot", "archiveresult", "shell"}
    assert len(tools) == 6
    assert all("outputSchema" in tool for tool in tools)

    crawl_schema = tools_by_name["crawl"]["inputSchema"]
    assert crawl_schema["properties"]["action"]["enum"] == ["create", "delete", "list", "update"]
    assert crawl_schema["properties"]["urls"]["type"] == "array"
    assert crawl_schema["properties"]["records"]["type"] == "array"
    assert tools_by_name["search"]["annotations"]["readOnlyHint"] is True
    assert tools_by_name["shell"]["inputSchema"]["required"] == ["code"]
    assert tools_by_name["shell"]["annotations"]["destructiveHint"] is True


def test_mcp_crawl_create_returns_structured_json(initialized_archive):
    os.chdir(initialized_archive)
    result = MCPServer().handle_tools_call(
        {
            "name": "crawl",
            "arguments": {
                "action": "create",
                "urls": ["https://mcp-test.example.com/"],
                "depth": 1,
                "tag": "mcp-test",
            },
        },
    )

    assert result["isError"] is False, result
    assert result["structuredContent"]["success"] is True
    assert result["structuredContent"]["error"] is None
    assert result["structuredContent"]["command"] == "archivebox crawl create"
    assert result["structuredContent"]["exitCode"] == 0
    assert result["structuredContent"]["records"][0]["urls"] == "https://mcp-test.example.com/"
    assert result["structuredContent"]["records"][0]["max_depth"] == 1
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_mcp_snapshot_update_accepts_records_without_a_jsonl_pipeline(initialized_archive):
    os.chdir(initialized_archive)
    server = MCPServer()
    created = server.handle_tools_call(
        {
            "name": "snapshot",
            "arguments": {
                "action": "create",
                "urls": ["https://mcp-update.example.com/"],
            },
        },
    )
    assert created["isError"] is False, created
    snapshot = created["structuredContent"]["records"][0]

    updated = server.handle_tools_call(
        {
            "name": "snapshot",
            "arguments": {
                "action": "update",
                "records": [{"id": snapshot["id"]}],
                "tag": "updated-through-mcp",
            },
        },
    )

    assert updated["isError"] is False
    assert updated["structuredContent"]["records"][0]["id"] == snapshot["id"]
    assert "updated-through-mcp" in updated["structuredContent"]["records"][0]["tags"]


def test_mcp_cli_errors_are_structured_for_agents(initialized_archive):
    os.chdir(initialized_archive)
    result = MCPServer().handle_tools_call(
        {
            "name": "crawl",
            "arguments": {"action": "create"},
        },
    )

    assert result["isError"] is True
    assert result["structuredContent"]["success"] is False
    assert result["structuredContent"]["exitCode"] == 1
    assert "No URLs provided" in result["structuredContent"]["error"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_mcp_invalid_action_is_a_protocol_error():
    response = MCPServer().handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "crawl", "arguments": {"action": "bogus"}},
        },
    )

    assert response["error"]["code"] == -32602
    assert "Choose one of: create, delete, list, update" in response["error"]["message"]


def test_mcp_shell_runs_python_through_archivebox_shell(initialized_archive):
    os.chdir(initialized_archive)
    result = MCPServer().handle_tools_call(
        {
            "name": "shell",
            "arguments": {
                "code": "from archivebox.core.models import Snapshot; print(f'shell_ok={Snapshot.objects.count() >= 0}')",
            },
        },
    )

    assert result["isError"] is False, result
    assert result["structuredContent"]["command"] == "archivebox shell"
    assert result["structuredContent"]["stdout"] == "shell_ok=True\n"


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


def test_read_only_classifier_matches_gate_and_hints():
    """The shared classifier is the single source of truth for gating/hints."""

    assert invocation_is_read_only("search") is True
    assert invocation_is_read_only("add") is False
    assert invocation_is_read_only("shell") is False
    assert invocation_is_read_only("crawl", "list") is True
    assert invocation_is_read_only("crawl", "create") is False
    assert invocation_is_read_only("snapshot", "update") is False
    assert invocation_is_read_only("snapshot", "delete") is False
    assert invocation_is_read_only("archiveresult", "remove") is False
    for action in ("help", "list", "search", "status", "version"):
        assert invocation_is_read_only(action) is True


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("add", {"urls": ["https://readonly-blocked.example.com/"]}),
        ("crawl", {"action": "create", "urls": ["https://readonly-blocked.example.com/"]}),
        ("snapshot", {"action": "create", "urls": ["https://readonly-blocked.example.com/"]}),
        ("snapshot", {"action": "update", "records": [{"id": str(uuid.uuid4())}]}),
        ("archiveresult", {"action": "delete"}),
        ("shell", {"code": "print('must not run')"}),
    ],
)
def test_read_only_mode_rejects_mutating_tools_with_jsonrpc_error(tool_name, arguments):
    from archivebox.crawls.models import Crawl

    server = MCPServer(read_only=True)
    before = Crawl.objects.count()
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
    )

    assert "result" not in response, response
    assert response["error"]["code"] == READ_ONLY_ERROR_CODE
    assert tool_name in response["error"]["message"]
    assert response["error"]["data"]["readOnly"] is True
    assert response["error"]["data"]["tool"] == tool_name
    assert Crawl.objects.count() == before
    with pytest.raises(MCPReadOnlyError):
        server.handle_tools_call({"name": tool_name, "arguments": arguments})


def test_read_only_mode_allows_read_only_actions(initialized_archive):
    os.chdir(initialized_archive)
    # Seed a real record through the default (writable) server first.
    seeded = MCPServer().handle_tools_call(
        {"name": "snapshot", "arguments": {"action": "create", "urls": ["https://readonly-read.example.com/"]}}
    )
    assert seeded["isError"] is False, seeded

    server = MCPServer(read_only=True)
    for tool_name, arguments in (
        ("crawl", {"action": "list"}),
        ("snapshot", {"action": "list"}),
        ("archiveresult", {"action": "list"}),
        ("search", {}),
    ):
        result = server.handle_tools_call({"name": tool_name, "arguments": arguments})
        assert result["isError"] is False, (tool_name, result)
        assert result["structuredContent"]["success"] is True
        assert isinstance(result["structuredContent"]["records"], list)

    # The read-only server really did not create anything.
    assert seeded["structuredContent"]["records"][0]["url"] == "https://readonly-read.example.com/"


def test_tools_list_read_only_hints_match_the_gate():
    default_tools = {tool["name"]: tool for tool in MCPServer().handle_tools_list({})["tools"]}
    assert default_tools["search"]["annotations"]["readOnlyHint"] is True
    assert default_tools["shell"]["annotations"]["readOnlyHint"] is False
    assert default_tools["add"]["annotations"]["readOnlyHint"] is False

    read_only_tools = {tool["name"]: tool for tool in MCPServer(read_only=True).handle_tools_list({})["tools"]}
    for name, tool in read_only_tools.items():
        assert tool["annotations"]["readOnlyHint"] is True, name


def test_initialize_declares_resources_and_read_only_state():
    default_init = MCPServer().handle_initialize({})
    assert default_init["capabilities"]["tools"] == {"listChanged": False}
    assert default_init["capabilities"]["resources"]["subscribe"] is False
    assert "readOnly" not in default_init

    read_only_init = MCPServer(read_only=True).handle_initialize({})
    assert read_only_init["readOnly"] is True
    assert read_only_init["capabilities"]["resources"]["listChanged"] is False
    assert "read-only" in read_only_init["instructions"]


def test_read_only_setting_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("ARCHIVEBOX_MCP_READ_ONLY", "1")
    monkeypatch.setenv("ARCHIVEBOX_MCP_AUDIT_CAPACITY", "7")
    monkeypatch.setenv("ARCHIVEBOX_MCP_RESOURCES_PAGE_SIZE", "3")
    server = MCPServer()
    assert server.settings.read_only is True
    assert server.settings.audit_capacity == 7
    assert server.settings.resources_page_size == 3


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_resources_list_paginates_with_stable_order_and_cursors():
    crawls, snapshots, results = create_resource_rows(crawls=3, snapshots=3, archiveresults=3)
    server = MCPServer(resources_page_size=2)

    pages = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = server.handle_resources_list(params)
        pages.append(page)
        assert len(page["resources"]) <= 2
        for item in page["resources"]:
            assert set(item) == {"uri", "name", "mimeType"}
            assert item["mimeType"] == "application/json"
        cursor = page.get("nextCursor")
        if not cursor:
            break
    assert "nextCursor" not in pages[-1]

    uris = [uri for page in pages for uri in (r["uri"] for r in page["resources"])]
    # A parent crawl/snapshot are also created when rows are requested; build
    # the expected order from the database by (type, id) to cover them exactly.
    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.crawls.models import Crawl

    expected = (
        [build_resource_uri("crawl", row_id) for row_id in Crawl.objects.order_by("id").values_list("id", flat=True)]
        + [build_resource_uri("snapshot", row_id) for row_id in Snapshot.objects.order_by("id").values_list("id", flat=True)]
        + [build_resource_uri("archiveresult", row_id) for row_id in ArchiveResult.objects.order_by("id").values_list("id", flat=True)]
    )
    assert uris == expected
    # Walking the pages again returns byte-identical ordering.
    assert read_all_resources(server) == uris


def test_resources_list_includes_cursor_metadata():
    create_resource_rows(crawls=2, snapshots=1)
    server = MCPServer(resources_page_size=1)

    first = server.handle_resources_list({})
    assert len(first["resources"]) == 1
    assert first.get("nextCursor")
    start_type, start_id = decode_resource_cursor(first["nextCursor"])
    assert (start_type, start_id) == decode_resource_cursor(encode_resource_cursor(start_type, start_id))

    second = server.handle_resources_list({"cursor": first["nextCursor"]})
    assert len(second["resources"]) == 1
    assert second["resources"][0]["uri"] != first["resources"][0]["uri"]


@pytest.mark.parametrize("bad_cursor", ["", "not-a-cursor", "!!!", "eyJ2IjogOX0="])
def test_resources_list_rejects_malformed_cursors(bad_cursor):
    server = MCPServer()
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {"cursor": bad_cursor}})
    assert response["error"]["code"] == -32602
    assert "cursor" in response["error"]["message"].lower()


def test_resources_read_returns_stable_json_and_text():
    crawls, snapshots, results = create_resource_rows(crawls=1, snapshots=1, archiveresults=1)
    server = MCPServer()

    for instance, type_name in (
        (crawls[0], "crawl"),
        (snapshots[0], "snapshot"),
        (results[0], "archiveresult"),
    ):
        uri = build_resource_uri(type_name, instance.id)
        result = server.handle_resources_read({"uri": uri})
        contents = result["contents"]
        assert [block["mimeType"] for block in contents] == ["application/json", "text/plain"]
        assert all(block["uri"] == uri for block in contents)

        payload = json.loads(contents[0]["text"])
        assert payload == instance.to_json()
        assert payload["type"].lower().replace(" ", "") == type_name
        text = contents[1]["text"]
        assert text.startswith(f"ArchiveBox {payload['type']}\n")
        assert str(instance.id) in text

        # Repeated reads are byte-identical.
        again = server.handle_resources_read({"uri": uri})
        assert again["contents"][0]["text"] == contents[0]["text"]
        assert again["contents"][1]["text"] == text


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "not a uri",
        "http://crawl/xyz",
        "archivebox://crawl",
        "archivebox://crawl/",
        "archivebox://crawl/not-a-uuid",
        "archivebox://crawl/12345?x=1",
    ],
)
def test_resources_read_rejects_illegal_uris(uri):
    response = MCPServer().handle_request({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}})
    assert response["error"]["code"] == -32602
    assert "contents" not in response


def test_resources_read_rejects_uris_outside_the_exposed_surface():
    other_id = uuid.uuid4()
    response = MCPServer().handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": f"archivebox://user/{other_id.hex}"},
        }
    )
    assert response["error"]["code"] == RESOURCE_FORBIDDEN_ERROR_CODE
    assert response["error"]["data"]["allowed_types"] == list(RESOURCE_TYPES)


def test_resources_read_missing_or_deleted_uri_is_a_clear_error_without_leaks():
    crawls, snapshots, _ = create_resource_rows(crawls=1, snapshots=2)
    server = MCPServer()
    other_url = snapshots[1].url

    missing = uuid.uuid4().hex
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": f"archivebox://snapshot/{missing}"},
        }
    )
    assert response["error"]["code"] == RESOURCE_NOT_FOUND_ERROR_CODE
    assert missing in response["error"]["message"]
    assert other_url not in response["error"]["message"]

    deleted_uri = build_resource_uri("snapshot", snapshots[0].id)
    snapshots[0].delete()
    deleted_response = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": deleted_uri}})
    assert deleted_response["error"]["code"] == RESOURCE_NOT_FOUND_ERROR_CODE
    assert deleted_uri in deleted_response["error"]["message"]
    # The surviving sibling record's content must not leak through the error.
    assert other_url not in deleted_response["error"]["message"]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_tool_and_resource_calls_write_bounded_audit_entries():
    crawls, snapshots, results = create_resource_rows(crawls=1, snapshots=1, archiveresults=1)
    server = MCPServer(audit_capacity=3)

    for instance, type_name in (
        (crawls[0], "crawl"),
        (snapshots[0], "snapshot"),
        (results[0], "archiveresult"),
    ):
        server.handle_resources_read({"uri": build_resource_uri(type_name, instance.id)})
    # Fourth read must evict the oldest entry while keeping sequence monotonic.
    server.handle_resources_read({"uri": build_resource_uri("crawl", crawls[0].id)})

    entries = server.handle_audit_list({"limit": 10})["entries"]
    assert len(entries) == 3
    assert [entry["seq"] for entry in entries] == [4, 3, 2]
    for entry in entries:
        assert {"seq", "ts", "kind", "name", "arguments", "success", "result", "duration_ms"} <= set(entry)
        assert entry["kind"] == "resource"
        assert entry["name"] == "resources/read"
        assert entry["success"] is True
        assert isinstance(entry["duration_ms"], (int, float))
        assert entry["duration_ms"] >= 0


def test_blocked_tool_and_failed_resource_reads_are_audited():
    create_resource_rows(snapshots=1)
    server = MCPServer(read_only=True, audit_capacity=10)

    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "shell", "arguments": {"code": "print(1)"}},
        }
    )
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "archivebox://snapshot/missing"},
        }
    )

    entries = server.handle_audit_list({})["entries"]
    assert entries[0]["name"] == "resources/read"
    assert entries[0]["success"] is False
    assert entries[0]["result"]["code"] == -32602

    tool_entry = entries[1]
    assert tool_entry["name"] == "shell"
    assert tool_entry["success"] is False
    assert tool_entry["result"]["blocked"] is True
    assert tool_entry["result"]["code"] == READ_ONLY_ERROR_CODE
    assert tool_entry["arguments"]["code"] == "print(1)"


def test_audit_list_reports_capacity_and_validates_limit():
    server = MCPServer(audit_capacity=2)
    server.handle_resources_list({})
    server.handle_resources_list({})
    server.handle_resources_list({})

    payload = server.handle_audit_list({})
    assert payload["capacity"] == 2
    assert payload["count"] == 2

    bad = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "audit/list", "params": {"limit": 0}})
    assert bad["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Default compatibility
# ---------------------------------------------------------------------------


def test_default_settings_preserve_existing_behavior():
    server = MCPServer()
    assert server.settings.read_only is False
    assert server.settings.audit_capacity == DEFAULT_MCP_AUDIT_CAPACITY
    assert server.settings.resources_page_size == DEFAULT_MCP_RESOURCES_PAGE_SIZE

    initialize = server.handle_initialize({})
    assert "readOnly" not in initialize
    assert {"tools", "resources"} <= set(initialize["capabilities"])

    names = {tool["name"] for tool in server.handle_tools_list({})["tools"]}
    assert names == {"add", "search", "crawl", "snapshot", "archiveresult", "shell"}


def test_mcp_stdio_read_only_mode_blocks_mutation_end_to_end(initialized_archive):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "crawl", "arguments": {"action": "create", "urls": ["https://stdio-blocked.example.com/"]}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
    ]
    result = run_archivebox_cmd(
        ["mcp"],
        cwd=initialized_archive,
        input="".join(json.dumps(request) + "\n" for request in requests),
        env={"ARCHIVEBOX_MCP_READ_ONLY": "true", "USE_COLOR": "False", "SHOW_PROGRESS": "False"},
        default_cli_env=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0, result.stderr
    assert responses[0]["result"].get("readOnly") is True
    assert responses[1]["error"]["code"] == READ_ONLY_ERROR_CODE
    assert "resources" in responses[2]["result"]


def test_mcp_read_only_and_limits_load_from_archivebox_conf(initialized_archive):
    config_file = initialized_archive / "ArchiveBox.conf"
    with config_file.open("a") as handle:
        handle.write("\n[MCP_CONFIG]\nMCP_READ_ONLY=True\nMCP_AUDIT_CAPACITY=12\nMCP_RESOURCES_PAGE_SIZE=5\n")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "audit/list", "params": {}},
    ]
    result = run_archivebox_cmd(
        ["mcp"],
        cwd=initialized_archive,
        input="".join(json.dumps(request) + "\n" for request in requests),
        default_cli_env=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0, result.stderr
    assert responses[0]["result"]["readOnly"] is True
    assert responses[1]["result"]["capacity"] == 12
