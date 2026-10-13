"""Model Context Protocol server for ArchiveBox.

The server exposes six agent-friendly workflow tools backed by ArchiveBox's
existing Click CLI and newline-delimited JSON-RPC, plus read-only resource
access to Crawl/Snapshot/ArchiveResult records and a bounded in-memory audit
log of every tool call and resource read.
"""

import base64
import binascii
import json
import os
import sys
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import click
from click.testing import CliRunner

from archivebox.config.version import VERSION

PROTOCOL_VERSION = "2025-11-25"
PUBLIC_TOOLS = ("add", "search", "crawl", "snapshot", "archiveresult", "shell")
ACTION_TOOLS = {"crawl", "snapshot", "archiveresult"}
READ_ONLY_ACTIONS = {"help", "list", "search", "status", "version"}
DESTRUCTIVE_ACTIONS = {"delete", "remove"}

RESOURCE_SCHEME = "archivebox"
RESOURCE_TYPES = ("crawl", "snapshot", "archiveresult")
DEFAULT_MCP_READ_ONLY = False
DEFAULT_MCP_AUDIT_CAPACITY = 1000
DEFAULT_MCP_RESOURCES_PAGE_SIZE = 100
AUDIT_DEFAULT_LIMIT = 50

# JSON-RPC server error codes (reserved application range: -32000..-32099).
READ_ONLY_ERROR_CODE = -32000
RESOURCE_FORBIDDEN_ERROR_CODE = -32003
RESOURCE_NOT_FOUND_ERROR_CODE = -32004

_AUDIT_STRING_LIMIT = 500
_AUDIT_MAX_LIST_ITEMS = 10
_AUDIT_MAX_DICT_KEYS = 25
_AUDIT_SENSITIVE_KEYS = ("secret", "token", "password", "api_key", "apikey")


class MCPError(Exception):
    """Base class for JSON-RPC errors carrying their own response code."""

    code: int = -32603

    def __init__(self, message: str, data: Any = None):
        super().__init__(message)
        self.error_data = data


class MCPReadOnlyError(MCPError):
    """A mutating tool was invoked while the server runs read-only."""

    code = READ_ONLY_ERROR_CODE


class MCPResourceForbiddenError(MCPError):
    """A well-formed resource uri addresses a type outside the MCP surface."""

    code = RESOURCE_FORBIDDEN_ERROR_CODE


class MCPResourceNotFoundError(MCPError):
    """A well-formed, allowed resource uri does not match a live record."""

    code = RESOURCE_NOT_FOUND_ERROR_CODE


@dataclass(frozen=True)
class MCPSettings:
    """Resolved operational limits for one MCP server instance."""

    read_only: bool = DEFAULT_MCP_READ_ONLY
    audit_capacity: int = DEFAULT_MCP_AUDIT_CAPACITY
    resources_page_size: int = DEFAULT_MCP_RESOURCES_PAGE_SIZE


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_mcp_settings(
    *,
    read_only: bool | None = None,
    audit_capacity: int | None = None,
    resources_page_size: int | None = None,
) -> MCPSettings:
    """Resolve MCP limits from kwargs, environment, collection config, defaults.

    Precedence (highest first): explicit constructor arguments, the
    ``ARCHIVEBOX_MCP_*`` environment variables, the collection's
    ``MCP_*`` config items (ArchiveBox.conf), then built-in defaults.
    """

    values: dict[str, Any] = {
        "read_only": DEFAULT_MCP_READ_ONLY,
        "audit_capacity": DEFAULT_MCP_AUDIT_CAPACITY,
        "resources_page_size": DEFAULT_MCP_RESOURCES_PAGE_SIZE,
    }

    try:
        from django.conf import settings

        config = getattr(settings, "CONFIG", None)
        if config is not None:
            values["read_only"] = bool(getattr(config, "MCP_READ_ONLY", values["read_only"]))
            values["audit_capacity"] = int(getattr(config, "MCP_AUDIT_CAPACITY", values["audit_capacity"]))
            values["resources_page_size"] = int(
                getattr(config, "MCP_RESOURCES_PAGE_SIZE", values["resources_page_size"]),
            )
    except (ImportError, RuntimeError, ValueError, TypeError):
        # Django/config is not available yet (imports, isolated unit tests);
        # environment variables and defaults still apply.
        pass

    env_read_only = _env_bool("ARCHIVEBOX_MCP_READ_ONLY")
    if env_read_only is not None:
        values["read_only"] = env_read_only
    env_capacity = _env_int("ARCHIVEBOX_MCP_AUDIT_CAPACITY")
    if env_capacity is not None:
        values["audit_capacity"] = env_capacity
    env_page_size = _env_int("ARCHIVEBOX_MCP_RESOURCES_PAGE_SIZE")
    if env_page_size is not None:
        values["resources_page_size"] = env_page_size

    if read_only is not None:
        values["read_only"] = bool(read_only)
    if audit_capacity is not None:
        values["audit_capacity"] = int(audit_capacity)
    if resources_page_size is not None:
        values["resources_page_size"] = int(resources_page_size)

    values["audit_capacity"] = max(1, values["audit_capacity"])
    values["resources_page_size"] = max(1, values["resources_page_size"])
    return MCPSettings(**values)


def _redact_key(key: Any) -> bool:
    text = str(key).lower()
    return any(needle in text for needle in _AUDIT_SENSITIVE_KEYS)


def summarize_value(value: Any, *, _depth: int = 0) -> Any:
    """Return a bounded, JSON-serializable summary of tool/resource arguments."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _AUDIT_STRING_LIMIT:
            return value[:_AUDIT_STRING_LIMIT] + f"...<truncated {len(value) - _AUDIT_STRING_LIMIT} chars>"
        return value
    if _depth >= 2:
        return str(value)[:_AUDIT_STRING_LIMIT]
    if isinstance(value, (list, tuple)):
        items = [summarize_value(item, _depth=_depth + 1) for item in list(value)[:_AUDIT_MAX_LIST_ITEMS]]
        if len(value) > _AUDIT_MAX_LIST_ITEMS:
            items.append(f"...<{len(value) - _AUDIT_MAX_LIST_ITEMS} more items>")
        return items
    if isinstance(value, dict):
        summary: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _AUDIT_MAX_DICT_KEYS:
                summary["..."] = f"<{len(value) - _AUDIT_MAX_DICT_KEYS} more keys>"
                break
            if _redact_key(key):
                summary[str(key)] = "********"
            else:
                summary[str(key)] = summarize_value(item, _depth=_depth + 1)
        return summary
    return str(value)[:_AUDIT_STRING_LIMIT]


def summarize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Summarize a tools/call response without storing full stdout verbatim."""

    structured = result.get("structuredContent") or {}
    return {
        "isError": bool(result.get("isError")),
        "success": structured.get("success"),
        "exitCode": structured.get("exitCode"),
        "record_count": len(structured.get("records") or []),
        "error": summarize_value(structured.get("error")),
    }


@dataclass
class AuditLog:
    """Bounded in-memory audit trail; oldest entries are evicted first."""

    capacity: int
    _entries: deque[dict[str, Any]] = field(default_factory=deque, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)
    _sequence: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._entries = deque(maxlen=max(1, int(self.capacity)))

    def record(
        self,
        *,
        kind: str,
        name: str,
        arguments: Any,
        success: bool,
        result: Any,
        duration_ms: float,
    ) -> dict[str, Any] | None:
        """Append one entry; never raise into the JSON-RPC call path."""

        try:
            from django.utils import timezone

            timestamp = timezone.now().isoformat()
        except Exception:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        entry = {
            "seq": 0,
            "ts": timestamp,
            "kind": kind,
            "name": name,
            "arguments": summarize_value(arguments),
            "success": bool(success),
            "result": summarize_value(result),
            "duration_ms": round(max(0.0, float(duration_ms)), 3),
        }
        try:
            with self._lock:
                self._sequence += 1
                entry["seq"] = self._sequence
                self._entries.append(entry)
        except Exception:
            return None
        return entry

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recent entries newest-first, optionally capped at ``limit``."""

        with self._lock:
            entries = list(self._entries)
        if limit is not None:
            limit = max(0, min(int(limit), self.capacity))
            entries = entries[-limit:]
        return list(reversed(entries))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class MCPJSONEncoder(json.JSONEncoder):
    """JSON encoder for UUIDs, paths, Click sentinels, and other CLI values."""

    def default(self, value):
        sentinel_type = getattr(click.core, "_SentinelClass", None)
        if isinstance(sentinel_type, type) and isinstance(value, sentinel_type):
            return None
        if isinstance(value, tuple):
            return list(value)
        try:
            return super().default(value)
        except TypeError:
            return str(value)


@dataclass(frozen=True)
class MCPTool:
    """A discovered leaf Click command and its ArchiveBox command path."""

    name: str
    command_path: tuple[str, ...]
    command: click.Command


def click_type_to_json_schema_type(click_type: click.ParamType) -> dict[str, Any]:
    """Convert a Click parameter type to JSON Schema."""

    if isinstance(click_type, click.types.StringParamType):
        return {"type": "string"}
    if isinstance(click_type, click.types.IntParamType):
        return {"type": "integer"}
    if isinstance(click_type, click.types.FloatParamType):
        return {"type": "number"}
    if isinstance(click_type, click.types.BoolParamType):
        return {"type": "boolean"}
    if isinstance(click_type, click.types.Choice):
        schema: dict[str, Any] = {"enum": list(click_type.choices)}
        if all(isinstance(choice, str) for choice in click_type.choices):
            schema["type"] = "string"
        return schema
    if isinstance(click_type, (click.types.Path, click.types.File)):
        return {"type": "string", "description": "File or directory path"}
    if isinstance(click_type, click.types.Tuple):
        return {
            "type": "array",
            "prefixItems": [click_type_to_json_schema_type(item) for item in click_type.types],
            "minItems": len(click_type.types),
            "maxItems": len(click_type.types),
        }
    return {"type": "string"}


def command_accepts_stdin(click_command: click.Command) -> bool:
    """Return whether a command documents a stdin/JSONL input path."""

    command_help = " ".join(
        value
        for value in (
            click_command.help,
            click_command.short_help,
            click_command.callback.__doc__ if click_command.callback else None,
        )
        if value
    ).lower()
    return "stdin" in command_help


def invocation_is_read_only(tool_name: str, action: str | None = None) -> bool:
    """Return whether a resolved MCP tool invocation is free of writes.

    This is the single source of truth shared by the read-only gate and tool
    annotations, so ``readOnlyHint`` in tools/list can never drift from what
    handle_tools_call actually enforces. ``shell`` and ``add`` always mutate;
    crawl/snapshot/archiveresult only allow read-only leaf actions (list,
    help, status, version, search).
    """

    if tool_name == "shell":
        return False
    if tool_name in ACTION_TOOLS:
        return action in READ_ONLY_ACTIONS
    return tool_name in READ_ONLY_ACTIONS


def tool_annotations(command_path: tuple[str, ...]) -> dict[str, Any]:
    """Describe a command's side effects to MCP clients."""

    action = command_path[-1]
    root = command_path[0]
    read_only = invocation_is_read_only(root, action)
    destructive = action in DESTRUCTIVE_ACTIONS
    return {
        "title": "ArchiveBox " + " ".join(command_path),
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": read_only or destructive,
        "openWorldHint": root in {"add", "extract", "oneshot", "run", "update"} or (root in {"crawl", "snapshot"} and action == "create"),
    }


def click_command_to_mcp_tool(tool: MCPTool) -> dict[str, Any]:
    """Convert a leaf Click command to an MCP tool definition."""

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for param in tool.command.params:
        if param.name is None or param.name in {"help", "version"}:
            continue

        param_schema = click_type_to_json_schema_type(param.type)
        help_text = getattr(param, "help", None)
        if help_text:
            param_schema["description"] = help_text

        default = param.default
        sentinel_type = getattr(click.core, "_SentinelClass", None)
        is_sentinel = isinstance(sentinel_type, type) and isinstance(default, sentinel_type)
        if default is not None and default != () and not is_sentinel:
            param_schema["default"] = default

        if param.multiple or param.nargs != 1:
            item_schema = param_schema
            if isinstance(param.type, click.types.Tuple):
                properties[param.name] = param_schema
            else:
                properties[param.name] = {
                    "type": "array",
                    "items": item_schema,
                    "description": help_text or f"One or more {param.name.replace('_', ' ')} values",
                }
        else:
            properties[param.name] = param_schema

        if param.required:
            required.append(param.name)

    if command_accepts_stdin(tool.command):
        properties["records"] = {
            "type": "array",
            "items": {"type": "object"},
            "description": "Records to pass to the command as JSONL stdin. Use this instead of building a CLI pipeline.",
        }
        properties["stdin"] = {
            "type": "string",
            "description": "Raw stdin text. Prefer records for JSONL commands.",
        }

    command_name = "archivebox " + " ".join(tool.command_path)
    description = tool.command.help or tool.command.short_help or f"Run {command_name}"
    return {
        "name": tool.name,
        "title": " ".join(part.title() for part in tool.command_path),
        "description": f"{description}\n\nEquivalent CLI command: `{command_name}`.",
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": tool_output_schema(),
        "annotations": tool_annotations(tool.command_path),
    }


def tool_output_schema() -> dict[str, Any]:
    """Return the shared envelope schema for all CLI-backed tools."""

    return {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "success": {"type": "boolean"},
            "error": {"type": ["string", "null"]},
            "exitCode": {"type": "integer"},
            "records": {"type": "array", "items": {}},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
        },
        "required": ["command", "success", "error", "exitCode", "records", "stdout", "stderr"],
    }


def click_group_to_mcp_tool(group_name: str, actions: list[MCPTool]) -> dict[str, Any]:
    """Expose a Click command group as one MCP tool with an action selector."""

    action_names = [tool.command_path[-1] for tool in actions]
    properties: dict[str, dict[str, Any]] = {
        "action": {
            "type": "string",
            "enum": action_names,
            "description": f"{group_name.title()} action to run.",
        },
    }
    action_help = []
    for action_tool in actions:
        action_def = click_command_to_mcp_tool(action_tool)
        action_name = action_tool.command_path[-1]
        action_help.append(f"- {action_name}: {action_tool.command.help or action_tool.command.short_help}")
        for name, schema in action_def["inputSchema"]["properties"].items():
            if name not in properties:
                properties[name] = dict(schema)
            elif properties[name].get("default") != schema.get("default"):
                properties[name].pop("default", None)

    return {
        "name": group_name,
        "title": f"ArchiveBox {group_name.title()}",
        "description": (f"Manage ArchiveBox {group_name} records through the existing CLI.\n\nActions:\n{chr(10).join(action_help)}"),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["action"],
            "additionalProperties": False,
        },
        "outputSchema": tool_output_schema(),
        "annotations": {"title": f"ArchiveBox {group_name.title()}"},
    }


def shell_to_mcp_tool() -> dict[str, Any]:
    """Expose ``archivebox shell -c`` as one explicit Python escape hatch."""

    return {
        "name": "shell",
        "title": "ArchiveBox Python Shell",
        "description": (
            "Run arbitrary Python with ArchiveBox and Django initialized. "
            "Equivalent CLI command: `archivebox shell --plain --quiet-load -c CODE`. "
            "This has full access to the collection database and filesystem."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to execute in the initialized ArchiveBox Django shell.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "outputSchema": tool_output_schema(),
        "annotations": {
            "title": "ArchiveBox Python Shell",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }


def _option_args(param: click.Option, value: Any) -> list[str]:
    """Serialize one MCP argument using the option's real Click spellings."""

    primary = next((opt for opt in param.opts if opt.startswith("--")), param.opts[0])
    if param.is_bool_flag:
        if value:
            return [primary]
        if param.secondary_opts:
            secondary = next((opt for opt in param.secondary_opts if opt.startswith("--")), param.secondary_opts[0])
            return [secondary]
        return []

    values = value if isinstance(value, list) and param.multiple else [value]
    args: list[str] = []
    for item in values:
        args.extend((primary, str(item)))
    return args


def arguments_to_cli(
    click_command: click.Command,
    arguments: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Convert MCP JSON arguments into Click argv and optional stdin."""

    supplied = dict(arguments)
    records = supplied.pop("records", None)
    stdin_text = supplied.pop("stdin", None)
    if records is not None and stdin_text is not None:
        raise ValueError("Pass either records or stdin, not both")
    if records is not None:
        stdin_text = "".join(json.dumps(record, cls=MCPJSONEncoder) + "\n" for record in records)

    param_map = {param.name: param for param in click_command.params}
    unknown = sorted(set(supplied) - set(param_map))
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(unknown)}")

    option_args: list[str] = []
    positional_args: list[str] = []
    for key, value in supplied.items():
        if value is None:
            continue
        param = param_map[key]
        if isinstance(param, click.Argument):
            values = value if isinstance(value, list) else [value]
            positional_args.extend(str(item) for item in values)
        else:
            option_args.extend(_option_args(param, value))

    return [*option_args, *positional_args], stdin_text


def parse_structured_records(stdout: str) -> list[Any]:
    """Parse a CLI JSON array or JSONL stream without guessing at human output."""

    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        records = []
        for line in stripped.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                return []
        return records
    return parsed if isinstance(parsed, list) else [parsed]


def execute_click_command(tool: MCPTool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one discovered CLI command and return MCP structured content."""

    from archivebox.cli import cli

    try:
        cli_args, stdin_text = arguments_to_cli(tool.command, arguments)
        result = CliRunner().invoke(
            cli,
            [*tool.command_path, *cli_args],
            input=stdin_text,
            prog_name="archivebox",
            catch_exceptions=False,
        )
        return _tool_result(
            tool,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            is_error=result.exit_code != 0,
        )
    except (click.ClickException, click.UsageError, OSError, SystemExit, ValueError) as err:
        print(traceback.format_exc(), file=sys.stderr)
        return _tool_result(
            tool,
            exit_code=1,
            stderr=f"Could not invoke archivebox {' '.join(tool.command_path)}: {err}",
            is_error=True,
        )


def _tool_result(
    tool: MCPTool,
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    is_error: bool,
) -> dict[str, Any]:
    error = None
    if is_error:
        error = stderr.strip() or stdout.strip() or f"Command failed with exit code {exit_code}"
    structured = {
        "command": "archivebox " + " ".join(tool.command_path),
        "success": not is_error,
        "error": error,
        "exitCode": exit_code,
        "records": parse_structured_records(stdout),
        "stdout": stdout,
        "stderr": stderr,
    }
    return {
        "content": [{"type": "text", "text": json.dumps(structured, cls=MCPJSONEncoder)}],
        "structuredContent": structured,
        "isError": is_error,
    }


def get_resource_models() -> dict[str, type]:
    """Map exposed resource type names to their Django models."""

    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.crawls.models import Crawl

    return {
        "crawl": Crawl,
        "snapshot": Snapshot,
        "archiveresult": ArchiveResult,
    }


def build_resource_uri(type_name: str, record_id: Any) -> str:
    return f"{RESOURCE_SCHEME}://{type_name}/{record_id}"


def parse_resource_uri(uri: Any) -> tuple[str, uuid.UUID]:
    """Validate a resource uri and return ``(type_name, id)``.

    Malformed uris raise ValueError (-32602); well-formed uris addressing a
    type outside the MCP surface raise MCPResourceForbiddenError. Error
    messages only ever echo the requested uri, never other record contents.
    """

    if not isinstance(uri, str) or not uri:
        raise ValueError("resources/read requires a non-empty 'uri' string")

    parsed = urlparse(uri)
    if parsed.scheme.lower() != RESOURCE_SCHEME or not parsed.netloc:
        raise ValueError(f"Invalid resource uri: {uri!r}. Expected {RESOURCE_SCHEME}://<type>/<id>")
    if parsed.params or parsed.query or parsed.fragment or parsed.username is not None or parsed.port is not None:
        raise ValueError(f"Invalid resource uri: {uri!r}. Expected {RESOURCE_SCHEME}://<type>/<id>")

    type_name = parsed.netloc.lower()
    id_text = parsed.path.lstrip("/")
    if not id_text or "/" in id_text:
        raise ValueError(f"Invalid resource uri: {uri!r}. Expected {RESOURCE_SCHEME}://<type>/<id>")

    try:
        record_id = uuid.UUID(id_text)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Invalid resource uri: {uri!r}. Record id must be a UUID") from None

    if type_name not in RESOURCE_TYPES:
        raise MCPResourceForbiddenError(
            f"Resource type {type_name!r} is not exposed over MCP (requested {uri!r})",
            data={"uri": uri, "allowed_types": list(RESOURCE_TYPES)},
        )
    return type_name, record_id


def encode_resource_cursor(type_name: str, record_id: Any) -> str:
    """Encode an opaque keyset cursor (last type + id served)."""

    payload = json.dumps(
        {"v": 1, "t": type_name, "i": str(record_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_resource_cursor(cursor: Any) -> tuple[str, uuid.UUID | None]:
    """Decode an opaque cursor; missing cursor starts at the first type."""

    if cursor is None:
        return RESOURCE_TYPES[0], None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Invalid pagination cursor: expected a non-empty string")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("Invalid pagination cursor: undecodable cursor") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("Invalid pagination cursor: unsupported cursor version")
    type_name = payload.get("t")
    if type_name not in RESOURCE_TYPES:
        raise ValueError("Invalid pagination cursor: unknown resource type")
    try:
        record_id = uuid.UUID(str(payload.get("i")))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Invalid pagination cursor: invalid record id") from None
    return type_name, record_id


def resource_list_item(type_name: str, record_id: Any) -> dict[str, Any]:
    return {
        "uri": build_resource_uri(type_name, record_id),
        "name": f"{type_name} {record_id}",
        "mimeType": "application/json",
    }


def render_resource_text(payload: dict[str, Any]) -> str:
    """Render the same stable JSON payload as deterministic key:value text."""

    lines = [f"ArchiveBox {payload.get('type', 'Resource')}"]
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, cls=MCPJSONEncoder, indent=2, ensure_ascii=False, sort_keys=False)
            indented = "\n".join(f"  {line}" for line in rendered.splitlines())
            lines.append(f"{key}:\n{indented}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


class MCPServer:
    """ArchiveBox MCP server using JSON-RPC 2.0 over stdio."""

    def __init__(
        self,
        *,
        read_only: bool | None = None,
        audit_capacity: int | None = None,
        resources_page_size: int | None = None,
    ):
        from archivebox.cli import ArchiveBoxGroup

        self.cli_group = ArchiveBoxGroup()
        self.protocol_version = PROTOCOL_VERSION
        self.settings = resolve_mcp_settings(
            read_only=read_only,
            audit_capacity=audit_capacity,
            resources_page_size=resources_page_size,
        )
        self.audit_log = AuditLog(self.settings.audit_capacity)
        self._tools: dict[str, MCPTool] | None = None

    def _discover_command(
        self,
        command: click.Command,
        command_path: tuple[str, ...],
        tools: dict[str, MCPTool],
    ) -> None:
        if isinstance(command, click.Group):
            context = click.Context(command)
            for child_name in command.list_commands(context):
                child = command.get_command(context, child_name)
                if child is not None:
                    self._discover_command(child, (*command_path, child_name), tools)
            return

        tool_name = "_".join(command_path).replace("-", "_")
        tools[tool_name] = MCPTool(tool_name, command_path, command)

    def get_tools(self) -> dict[str, MCPTool]:
        """Discover and cache the CLI commands backing the five public tools."""

        if self._tools is None:
            tools: dict[str, MCPTool] = {}
            context = click.Context(self.cli_group)
            for command_name in PUBLIC_TOOLS:
                command = self.cli_group.get_command(context, command_name)
                if command is not None:
                    self._discover_command(command, (command_name,), tools)
            self._tools = tools
        return self._tools

    def get_public_tool_definitions(self) -> list[dict[str, Any]]:
        """Build the small agent-facing surface from the existing Click tree."""

        commands = self.get_tools()
        definitions = [
            shell_to_mcp_tool() if name == "shell" else click_command_to_mcp_tool(commands[name])
            for name in PUBLIC_TOOLS
            if name not in ACTION_TOOLS
        ]
        for group_name in PUBLIC_TOOLS:
            if group_name not in ACTION_TOOLS:
                continue
            actions = sorted(
                (tool for tool in commands.values() if tool.command_path[0] == group_name),
                key=lambda tool: tool.command_path,
            )
            definitions.append(click_group_to_mcp_tool(group_name, actions))
        return definitions

    def handle_initialize(self, params: dict) -> dict:
        instructions = (
            "Use add to archive URLs and search for deep search. Use crawl, snapshot, or archiveresult "
            "with action=create/list/update/delete to manage records. Pass returned records directly "
            "to update/delete actions through the records argument. Use shell only when the curated "
            "tools cannot express the operation; it runs arbitrary Python with full collection access. "
            "Crawl, Snapshot and ArchiveResult records are exposed as resources under "
            f"{RESOURCE_SCHEME}://<type>/<id>; page resources/list with the returned cursor."
        )
        result = {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {
                "name": "archivebox-mcp",
                "title": "ArchiveBox",
                "version": VERSION,
            },
            "instructions": instructions,
        }
        if self.settings.read_only:
            result["readOnly"] = True
            result["instructions"] = (
                instructions + " This server is running in read-only mode: add, shell, and create/update/delete "
                "actions are refused; search, list, status, version, help and resource reads still work."
            )
        return result

    def handle_tools_list(self, params: dict) -> dict:
        definitions = self.get_public_tool_definitions()
        if self.settings.read_only:
            # The gate makes every exposed invocation side-effect free, so the
            # advertised hints must match that real enforcement.
            for definition in definitions:
                definition.setdefault("annotations", {})["readOnlyHint"] = True
        return {"tools": definitions}

    def handle_tools_call(self, params: dict) -> dict:
        tool_name = params.get("name")
        if not tool_name:
            raise ValueError("Missing required parameter: name")
        if tool_name not in PUBLIC_TOOLS:
            raise ValueError(f"Unknown tool: {tool_name}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be an object")
        arguments = dict(arguments)
        command_name = tool_name
        action: str | None = None
        if tool_name == "shell":
            unknown = sorted(set(arguments) - {"code"})
            if unknown:
                raise ValueError(f"Unknown shell argument(s): {', '.join(unknown)}")
            code = arguments.get("code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("shell requires a non-empty code string")
        elif tool_name in ACTION_TOOLS:
            action = arguments.pop("action", None)
            available_actions = sorted(tool.command_path[-1] for tool in self.get_tools().values() if tool.command_path[0] == tool_name)
            if action not in available_actions:
                raise ValueError(
                    f"Unknown {tool_name} action: {action!r}. Choose one of: {', '.join(available_actions)}",
                )
            command_name = f"{tool_name}_{action}"

        started = time.perf_counter()
        try:
            if self.settings.read_only and not invocation_is_read_only(tool_name, action):
                label = f"{tool_name} {action}".strip() if action else tool_name
                raise MCPReadOnlyError(
                    f"MCP server is in read-only mode; mutating tool {label!r} is blocked",
                    data={
                        "readOnly": True,
                        "tool": tool_name,
                        "action": action,
                        "allowed_read_only_actions": sorted(READ_ONLY_ACTIONS),
                    },
                )

            if tool_name == "shell":
                code = arguments.get("code")
                arguments = {"args": ["--plain", "--quiet-load", "-c", code]}

            tool = self.get_tools().get(command_name)
            if tool is None:
                raise ValueError(f"ArchiveBox CLI command is unavailable: {command_name}")
            result = execute_click_command(tool, arguments)
        except Exception as err:
            self.audit_log.record(
                kind="tool",
                name=tool_name,
                arguments=params.get("arguments", {}),
                success=False,
                result=self._audit_error_result(err),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        self.audit_log.record(
            kind="tool",
            name=tool_name,
            arguments=params.get("arguments", {}),
            success=not result.get("isError"),
            result=summarize_tool_result(result),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    @staticmethod
    def _audit_error_result(err: Exception) -> dict[str, Any]:
        """Build a bounded audit result summary for a failed invocation."""

        if isinstance(err, MCPError):
            return {"blocked": True, "code": err.code, "error": str(err)[:_AUDIT_STRING_LIMIT]}
        return {
            "code": MCPServer._jsonrpc_error_code(err),
            "error": traceback.format_exc(limit=2).strip()[-_AUDIT_STRING_LIMIT:],
        }

    @staticmethod
    def _jsonrpc_error_code(err: Exception) -> int:
        """Mirror the JSON-RPC code handle_request will return for an error."""

        if isinstance(err, MCPError):
            return err.code
        if isinstance(err, (TypeError, ValueError)):
            return -32602
        return -32603

    def handle_resources_list(self, params: dict) -> dict:
        """List Crawl/Snapshot/ArchiveResult resources with stable keyset paging.

        Global order is fixed: resource type (crawl, snapshot, archiveresult),
        then primary key ascending (UUIDv7, so insertion order). New rows get
        larger keys and only ever extend later pages.
        """

        params = params or {}
        if not isinstance(params, dict):
            raise TypeError("resources/list params must be an object")
        start_type, start_id = decode_resource_cursor(params.get("cursor"))
        page_size = self.settings.resources_page_size
        models = get_resource_models()

        started = time.perf_counter()
        try:
            resources: list[dict[str, Any]] = []
            type_index = RESOURCE_TYPES.index(start_type)
            current_id = start_id
            remaining = page_size
            while remaining > 0 and type_index < len(RESOURCE_TYPES):
                type_name = RESOURCE_TYPES[type_index]
                queryset = models[type_name].objects.order_by("id")
                if current_id is not None:
                    queryset = queryset.filter(id__gt=current_id)
                rows = list(queryset.values_list("id", flat=True)[:remaining])
                for record_id in rows:
                    resources.append(resource_list_item(type_name, record_id))
                remaining -= len(rows)
                if rows and remaining == 0:
                    # Page is full; resume after this row on the next call.
                    current_id = rows[-1]
                    break
                # This type is exhausted at the cursor; continue with the next.
                type_index += 1
                current_id = None

            next_cursor = None
            if resources and len(resources) == page_size:
                last_type_name, last_id = parse_resource_uri(resources[-1]["uri"])
                if self._has_more_resources(RESOURCE_TYPES.index(last_type_name), last_id):
                    next_cursor = encode_resource_cursor(last_type_name, last_id)

            result = {"resources": resources, **({"nextCursor": next_cursor} if next_cursor else {})}
        except Exception:
            self.audit_log.record(
                kind="resource",
                name="resources/list",
                arguments={"cursor": params.get("cursor")},
                success=False,
                result={"error": traceback.format_exc(limit=2).strip()[-_AUDIT_STRING_LIMIT:]},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        self.audit_log.record(
            kind="resource",
            name="resources/list",
            arguments={"cursor": params.get("cursor")},
            success=True,
            result={"count": len(resources), "has_more": bool(next_cursor)},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    def _has_more_resources(self, type_index: int, record_id: uuid.UUID) -> bool:
        models = get_resource_models()
        for index in range(type_index, len(RESOURCE_TYPES)):
            queryset = models[RESOURCE_TYPES[index]].objects.order_by("id")
            if index == type_index:
                queryset = queryset.filter(id__gt=record_id)
            if queryset.exists():
                return True
        return False

    def handle_resources_read(self, params: dict) -> dict:
        params = params or {}
        if not isinstance(params, dict):
            raise TypeError("resources/read params must be an object")
        uri = params.get("uri")
        started = time.perf_counter()
        try:
            type_name, record_id = parse_resource_uri(uri)
            model = get_resource_models()[type_name]
            record = model.objects.filter(pk=record_id).first()
            if record is None:
                raise MCPResourceNotFoundError(
                    f"Resource not found (deleted or never existed): {uri}",
                    data={"uri": uri},
                )
            payload = record.to_json()
            json_text = json.dumps(payload, cls=MCPJSONEncoder, ensure_ascii=False)
            text = render_resource_text(payload)
            result = {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": json_text},
                    {"uri": uri, "mimeType": "text/plain", "text": text},
                ]
            }
        except Exception as err:
            self.audit_log.record(
                kind="resource",
                name="resources/read",
                arguments={"uri": uri if isinstance(uri, str) else None},
                success=False,
                result={
                    "error": str(err)[:_AUDIT_STRING_LIMIT],
                    "code": self._jsonrpc_error_code(err),
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        self.audit_log.record(
            kind="resource",
            name="resources/read",
            arguments={"uri": uri},
            success=True,
            result={"uri": uri, "type": type_name},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    def handle_audit_list(self, params: dict) -> dict:
        """Return recent bounded audit entries (newest first) for operators."""

        params = params or {}
        if not isinstance(params, dict):
            raise TypeError("audit/list params must be an object")
        limit = params.get("limit", AUDIT_DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, self.settings.audit_capacity)
        entries = self.audit_log.recent(limit)
        return {
            "entries": entries,
            "capacity": self.settings.audit_capacity,
            "count": len(entries),
        }

    def handle_request(self, request: dict) -> dict | None:
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        is_notification = "id" not in request

        if is_notification:
            return None

        try:
            if method == "initialize":
                result = self.handle_initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self.handle_tools_list(params)
            elif method == "tools/call":
                result = self.handle_tools_call(params)
            elif method == "resources/list":
                result = self.handle_resources_list(params)
            elif method == "resources/read":
                result = self.handle_resources_read(params)
            elif method == "audit/list":
                result = self.handle_audit_list(params)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPError as err:
            error = {"code": err.code, "message": str(err)}
            if err.error_data is not None:
                error["data"] = err.error_data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except (TypeError, ValueError) as err:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(err)},
            }
        except (click.ClickException, click.UsageError, OSError, RuntimeError) as err:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": str(err),
                    "data": traceback.format_exc(),
                },
            }

    def run_stdio_server(self) -> None:
        """Read and write one UTF-8 JSON-RPC message per line."""

        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                if response is not None:
                    print(json.dumps(response, cls=MCPJSONEncoder), flush=True)
            except json.JSONDecodeError as err:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error", "data": str(err)},
                }
                print(json.dumps(response, cls=MCPJSONEncoder), flush=True)


def run_mcp_server() -> None:
    """Start the ArchiveBox MCP stdio server."""

    MCPServer().run_stdio_server()
