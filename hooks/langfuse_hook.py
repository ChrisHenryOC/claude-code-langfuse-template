#!/usr/bin/env python3.12
"""
Sends Claude Code traces to Langfuse after each response.

Hook type: Stop (runs after each assistant response)
Opt-in: Only runs when TRACE_TO_LANGFUSE=true is set in project settings.

Resilience: If Langfuse is unavailable, traces are queued locally and
automatically drained on the next successful connection.
"""

import getpass
import hashlib
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Prevent local directories named "langfuse" (e.g., Docker Compose project dirs)
# from shadowing the real langfuse SDK via namespace package resolution.
# Remove CWD and '' from sys.path temporarily during import.
_original_path = sys.path[:]
sys.path = [p for p in sys.path if p not in ("", ".") and Path(p).resolve() != Path.cwd().resolve()]

# Check if Langfuse is available
try:
    from langfuse import Langfuse
except ImportError:
    print("Error: langfuse package not installed. Run: pip install langfuse", file=sys.stderr)
    sys.exit(0)
finally:
    sys.path = _original_path

# Configuration
LOG_FILE = Path.home() / ".claude" / "state" / "langfuse_hook.log"
STATE_FILE = Path.home() / ".claude" / "state" / "langfuse_state.json"

# How long a subagent transcript must sit unmodified before we treat it as
# finished. Async subagents are appended to for as long as they run; emitting
# early is unrecoverable (see subagent_is_complete).
SUBAGENT_QUIESCE_SECONDS = 60
QUEUE_FILE = Path.home() / ".claude" / "state" / "pending_traces.jsonl"
DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
HEALTH_CHECK_TIMEOUT = 2  # seconds
PERMISSION_EVENTS_FILE = Path.home() / ".claude" / "logs" / "permission-events.jsonl"

# OTel resource attribute (replaces the default "unknown_service")
SERVICE_NAME = "claude-code-hook"

# Conservative key-name patterns for redacting tool inputs/outputs. Only redacts
# values whose KEY matches; doesn't scan free-text bodies.
SECRET_KEY_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|credential)"),
    re.compile(r"(?i)(_KEY|_SECRET|_TOKEN)$"),
]

# Truncate large tool inputs/outputs (chars). None disables.
TOOL_IO_MAX_CHARS = 20_000


def log(level: str, message: str) -> None:
    """Log a message to the log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp} [{level}] {message}\n")


def debug(message: str) -> None:
    """Log a debug message (only if DEBUG is enabled)."""
    if DEBUG:
        log("DEBUG", message)


def check_langfuse_health(host: str) -> bool:
    """Quick health check to see if Langfuse is reachable.

    Uses socket connection to avoid slow HTTP timeouts.
    """
    try:
        # Parse host to get hostname and port
        if host.startswith("http://"):
            host_part = host[7:]
            default_port = 80
        elif host.startswith("https://"):
            host_part = host[8:]
            default_port = 443
        else:
            host_part = host
            default_port = 443

        if ":" in host_part:
            hostname, port_str = host_part.split(":", 1)
            port = int(port_str.rstrip("/"))
        else:
            hostname = host_part.rstrip("/")
            port = default_port

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(HEALTH_CHECK_TIMEOUT)
        result = sock.connect_ex((hostname, port))
        sock.close()

        is_healthy = result == 0
        debug(f"Health check for {hostname}:{port} - {'OK' if is_healthy else 'FAILED'}")
        return is_healthy
    except Exception as e:
        debug(f"Health check error: {e}")
        return False


def queue_trace(trace_data: dict) -> None:
    """Append a trace to the local queue file."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trace_data["queued_at"] = datetime.now(timezone.utc).isoformat()
    with open(QUEUE_FILE, "a") as f:
        f.write(json.dumps(trace_data) + "\n")
    log("INFO", f"Queued trace for session {trace_data.get('session_id', 'unknown')}, turn {trace_data.get('turn_num', '?')}")


def load_queued_traces() -> list[dict]:
    """Load all pending traces from the queue file."""
    if not QUEUE_FILE.exists():
        return []

    traces = []
    try:
        with open(QUEUE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    traces.append(json.loads(line))
    except (json.JSONDecodeError, IOError) as e:
        log("ERROR", f"Failed to load queue: {e}")
        return []

    return traces


def clear_queue() -> None:
    """Clear the queue file after successful drain."""
    if QUEUE_FILE.exists():
        QUEUE_FILE.unlink()
        debug("Queue cleared")


def drain_queue(langfuse: Langfuse) -> int:
    """Drain all queued traces to Langfuse. Returns count of drained traces."""
    traces = load_queued_traces()
    if not traces:
        return 0

    log("INFO", f"Draining {len(traces)} queued traces to Langfuse")

    drained = 0
    for trace_data in traces:
        try:
            create_trace(
                langfuse=langfuse,
                session_id=trace_data["session_id"],
                turn_num=trace_data["turn_num"],
                user_msg=trace_data["user_msg"],
                assistant_msgs=trace_data["assistant_msgs"],
                tool_results=trace_data["tool_results"],
                project_name=trace_data.get("project_name", ""),
                transcript_path=trace_data.get("transcript_path"),
            )
            drained += 1
        except Exception as e:
            log("ERROR", f"Failed to drain trace: {e}")
            # If we fail mid-drain, rewrite remaining traces and exit
            remaining = traces[drained:]
            clear_queue()
            for remaining_trace in remaining:
                queue_trace(remaining_trace)
            return drained

    clear_queue()
    log("INFO", f"Successfully drained {drained} traces")
    return drained


def load_state() -> dict:
    """Load the state file containing session tracking info."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def save_state(state: dict) -> None:
    """Save the state file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_content(msg: dict) -> Any:
    """Extract content from a message."""
    if isinstance(msg, dict):
        if "message" in msg:
            return msg["message"].get("content")
        return msg.get("content")
    return None


def is_tool_result(msg: dict) -> bool:
    """Check if a message contains tool results."""
    content = get_content(msg)
    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result"
            for item in content
        )
    return False


def get_tool_calls(msg: dict) -> list:
    """Extract tool use blocks from a message."""
    content = get_content(msg)
    if isinstance(content, list):
        return [
            item for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
    return []


def get_text_content(msg: dict) -> str:
    """Extract text content from a message."""
    content = get_content(msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(text_parts)
    return ""


def merge_assistant_parts(parts: list) -> dict:
    """Merge multiple assistant message parts into one.

    For streaming responses split across multiple JSONL records, Anthropic's
    final usage/stop_reason lands on the LAST chunk. We merge content from all
    parts but adopt the last non-empty `usage` and `stop_reason` so cost/token
    metrics reflect reality.
    """
    if not parts:
        return {}

    merged_content = []
    for part in parts:
        content = get_content(part)
        if isinstance(content, list):
            merged_content.extend(content)
        elif content:
            merged_content.append({"type": "text", "text": str(content)})

    # Use the structure from the first part as the envelope
    result = parts[0].copy()
    if "message" in result:
        result["message"] = result["message"].copy()
        result["message"]["content"] = merged_content
        # Pull the final usage / stop_reason from the latest part that has them.
        for part in reversed(parts):
            if not isinstance(part, dict):
                continue
            pmsg = part.get("message") or {}
            if isinstance(pmsg, dict):
                if pmsg.get("usage"):
                    result["message"]["usage"] = pmsg["usage"]
                    break
        for part in reversed(parts):
            if not isinstance(part, dict):
                continue
            pmsg = part.get("message") or {}
            if isinstance(pmsg, dict) and pmsg.get("stop_reason"):
                result["message"]["stop_reason"] = pmsg["stop_reason"]
                break
    else:
        result["content"] = merged_content

    return result


def merge_by_message_id(records: list) -> list:
    """Collapse assistant records that belong to the same API call.

    Claude Code writes one record per content block (thinking, text, each
    tool_use), and every one of them repeats the SAME `message.usage`. Treating
    them as separate calls inflates token counts ~2.2-2.8x. Group by
    `message.id` and merge; records without an id stand alone.
    """
    groups: list[list] = []
    by_id: dict[str, list] = {}
    for r in records:
        mid = (r.get("message") or {}).get("id")
        if not mid:
            groups.append([r])
            continue
        if mid in by_id:
            by_id[mid].append(r)
        else:
            group = [r]
            by_id[mid] = group
            groups.append(group)
    return [merge_assistant_parts(g) for g in groups]


def has_task_notification(parent_transcript: Path | str | None, agent_id: str) -> bool:
    """True when the parent transcript carries a <task-notification> for agent_id.

    This is Claude Code's definitive "subagent finished" signal, but it is not
    always emitted (observed 3/8 on one session, 8/8 on another), so it is only
    ever used as a fast path — never as the sole completion test.
    """
    if not parent_transcript or not agent_id:
        return False
    try:
        return f"<task-id>{agent_id}</task-id>" in Path(parent_transcript).read_text(errors="ignore")
    except OSError:
        return False


def subagent_is_complete(jsonl_path: Path, parent_transcript=None, agent_id: str = "") -> bool:
    """True when a subagent transcript has stopped growing and is safe to emit.

    Since Claude Code ~v2.1.196 the Agent tool is ASYNC: the parent's tool_result
    is only a spawn acknowledgement ("Async agent launched successfully"), so the
    file is still being appended to when the spawning turn is finalized. Emitting
    then captures a fraction of the subagent's calls, and because observation ids
    cannot be pinned in the Langfuse v3 SDK, an early emit cannot be corrected
    later without duplicating. So require quiescence, accepting a notification as
    proof of completion when one exists.
    """
    try:
        idle = time.time() - jsonl_path.stat().st_mtime
    except OSError:
        return False
    if idle >= SUBAGENT_QUIESCE_SECONDS:
        return True
    return has_task_notification(parent_transcript, agent_id)


def subagent_by_id(parent_transcript: Path | str | None, agent_id: str) -> dict | None:
    """Build subagent info straight from an agentId, bypassing description matching."""
    if not parent_transcript or not agent_id:
        return None
    p = Path(parent_transcript)
    subagent_dir = p.parent / p.stem / "subagents"
    jsonl_path = subagent_dir / f"agent-{agent_id}.jsonl"
    if not jsonl_path.exists():
        return None
    meta = {}
    try:
        meta = json.loads((subagent_dir / f"agent-{agent_id}.meta.json").read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {
        "path": jsonl_path,
        "agent_type": meta.get("agentType", "unknown"),
        "agent_id": agent_id,
        "description": meta.get("description", ""),
    }


def resolve_subagent(subagents: dict, desc: str, agent_id: str, parent_transcript) -> dict | None:
    """Resolve an Agent tool_use to its subagent, preferring the agentId link."""
    return subagent_by_id(parent_transcript, agent_id) or subagents.get(desc) or next(
        (v for k, v in subagents.items() if k.startswith(f"{desc}#")), None
    )


def agent_id_from_result(raw_result: Any) -> str:
    """Pull `agentId: <id>` out of an async Agent tool_result (the spawn ack).

    This links a tool_use to its subagent by id rather than by free-text
    description, which is what makes attribution robust.
    """
    try:
        text = json.dumps(raw_result)
    except (TypeError, ValueError):
        return ""
    m = re.search(r"agentId:\s*([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else ""


# ─── v2 logging helpers ────────────────────────────────────────────────────
# All helpers below contribute to per-API-call generations, real cost
# attribution (incl. cache tokens), subagent expansion, and trace metadata.

_RELEASE_CACHE: dict[str, str] = {}


def derive_release(transcript_path: str | Path | None = None) -> str:
    """Resolve Claude Code release tag, e.g. 'cc2.1.133'.

    Priority:
      1. `version` field embedded in the transcript records (most accurate
         per-session — Stop hooks don't inherit CLAUDE_CODE_EXECPATH).
      2. CLAUDE_CODE_EXECPATH env var, when the hook was invoked with it.
      3. Resolve the `claude` binary's symlink target (gives the latest
         installed version).
      4. Final fallback: 'cc-unknown'.
    """
    cache_key = str(transcript_path) if transcript_path else "<global>"
    if (cached := _RELEASE_CACHE.get(cache_key)):
        return cached

    # 1. Transcript record-level version
    if transcript_path:
        try:
            with open(transcript_path, "r") as fh:
                for i, ln in enumerate(fh):
                    if i > 30:
                        break
                    try:
                        rec = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    v = rec.get("version")
                    if v:
                        result = f"cc{v}"
                        _RELEASE_CACHE[cache_key] = result
                        return result
        except (OSError, ValueError):
            pass

    # 2. CLAUDE_CODE_EXECPATH (set in interactive Claude env, often missing in hooks)
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if (m := re.search(r"versions/(\d+\.\d+\.\d+)", execpath)):
        result = f"cc{m.group(1)}"
        _RELEASE_CACHE[cache_key] = result
        return result

    # 3. Resolve `claude` binary symlink → latest installed version
    try:
        import shutil
        bin_path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        real_path = os.path.realpath(bin_path)
        if (m := re.search(r"versions/(\d+\.\d+\.\d+)", real_path)):
            result = f"cc{m.group(1)}"
            _RELEASE_CACHE[cache_key] = result
            return result
    except OSError:
        pass

    _RELEASE_CACHE[cache_key] = "cc-unknown"
    return "cc-unknown"


def derive_trace_name(user_msg: dict, turn_num: int, max_chars: int = 80) -> str:
    """Trace names should carry semantic content, not just `Turn N`."""
    text = get_text_content(user_msg).strip()
    if not text:
        return f"Turn {turn_num}"
    first_line = text.splitlines()[0].strip()
    snippet = first_line[:max_chars]
    if len(first_line) > max_chars:
        snippet = snippet.rstrip() + "…"
    return f"T{turn_num}: {snippet}"


def extract_usage_details(usage: dict) -> dict[str, int]:
    """Map Anthropic API usage to Langfuse usage_details using the canonical
    key names that Langfuse's model price table is keyed on.

    Cache reads dominate Claude Code's token economy; surfacing each tier
    separately (5m vs 1h) is what makes Langfuse's price table compute the
    fully accurate billed cost. Anthropic charges different rates for the
    two cache TTLs.

    Output keys (chosen to literally match Langfuse's model.prices map):
      input                          → uncached input
      output                         → completion
      cache_read_input_tokens        → cached reads (10% of base input price)
      input_cache_creation_5m        → 5-minute cache writes (1.25× base)
      input_cache_creation_1h        → 1-hour cache writes (2× base)
    """
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    if (v := usage.get("input_tokens")) is not None:
        out["input"] = int(v)
    if (v := usage.get("output_tokens")) is not None:
        out["output"] = int(v)
    if (v := usage.get("cache_read_input_tokens")) is not None:
        out["cache_read_input_tokens"] = int(v)

    # Cache creation: prefer the per-TTL breakdown so 5m and 1h price separately
    cache_creation = usage.get("cache_creation") or {}
    five_min = cache_creation.get("ephemeral_5m_input_tokens")
    one_hour = cache_creation.get("ephemeral_1h_input_tokens")
    if five_min is not None or one_hour is not None:
        if five_min is not None:
            out["input_cache_creation_5m"] = int(five_min)
        if one_hour is not None:
            out["input_cache_creation_1h"] = int(one_hour)
    elif (v := usage.get("cache_creation_input_tokens")) is not None:
        # Fall back to aggregate when the breakdown isn't provided
        out["cache_creation_input_tokens"] = int(v)

    if out:
        out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def extract_model_parameters(message: dict) -> dict[str, Any]:
    """Capture useful model parameters from the assistant message envelope."""
    if not isinstance(message, dict):
        return {}
    params: dict[str, Any] = {}
    for k in ("temperature", "max_tokens", "top_p", "top_k", "stop_sequences",
              "service_tier", "thinking"):
        if k in message and message[k] is not None:
            v = message[k]
            if isinstance(v, (str, int, bool, float)):
                params[k] = v
            elif isinstance(v, list):
                params[k] = [str(x) for x in v[:10]]
            else:
                params[k] = json.dumps(v)[:200]
    usage = message.get("usage") or {}
    if (st := usage.get("service_tier")):
        params.setdefault("service_tier", st)
    return params


def redact(value: Any) -> Any:
    """Lightweight key-name-based redaction for tool inputs/outputs."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(p.search(k) for p in SECRET_KEY_PATTERNS):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def truncate(value: Any, limit: int | None = TOOL_IO_MAX_CHARS) -> Any:
    if limit is None or value is None:
        return value
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(s) <= limit:
        return value
    return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"


def discover_subagents(parent_transcript: Path | str | None) -> dict[str, dict]:
    """Map subagent description → {path, agent_type, agent_id}.

    Subagents live at <project>/<session-uuid>/subagents/agent-<id>.jsonl with
    a sibling agent-<id>.meta.json containing {agentType, description}.
    Returns {} when there are no subagents (the common case).
    """
    if not parent_transcript:
        return {}
    p = Path(parent_transcript)
    subagent_dir = p.parent / p.stem / "subagents"
    if not subagent_dir.exists():
        return {}
    out: dict[str, dict] = {}
    for meta_path in subagent_dir.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Bare id (no "agent-" prefix), so this matches the id parsed from the
        # spawn-ack in subagent_by_id. A mismatch here double-keys the same
        # subagent across the two resolution paths — breaking exactly-once,
        # the subagent_id label, and <task-id> notification matching.
        agent_id = meta_path.stem.removesuffix(".meta").removeprefix("agent-")
        jsonl_path = subagent_dir / f"agent-{agent_id}.jsonl"
        if not jsonl_path.exists():
            continue
        desc = meta.get("description", "")
        if not desc:
            continue
        key = desc if desc not in out else f"{desc}#{agent_id}"
        out[key] = {
            "path": jsonl_path,
            "agent_type": meta.get("agentType", "unknown"),
            "agent_id": agent_id,
            "description": desc,
        }
    return out


def emit_subagent_span(
    langfuse: Langfuse,
    *,
    subagent_info: dict,
    parent_tool_use: dict,
    parent_tool_result: Any,
    trace_id: str | None = None,
) -> None:
    """Replay a subagent's transcript as a nested span under the parent's
    Agent tool_use, with one GENERATION per subagent API call plus child
    spans for the subagent's own tool calls."""
    transcript_path: Path = subagent_info["path"]
    agent_type = subagent_info["agent_type"]
    agent_id = subagent_info["agent_id"]
    description = subagent_info["description"]

    records = []
    try:
        for ln in transcript_path.read_text().split("\n"):
            if not ln:
                continue
            try:
                records.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        records = []

    asst_msgs = merge_by_message_id(
        [r for r in records if (r.get("type") or (r.get("message") or {}).get("role")) == "assistant"]
    )
    tool_results = [r for r in records if (r.get("type") or (r.get("message") or {}).get("role")) == "user" and is_tool_result(r)]

    total_in = total_cache_r = total_cache_c = total_out = 0
    for r in asst_msgs:
        u = (r.get("message") or {}).get("usage") or {}
        total_in += u.get("input_tokens", 0) or 0
        total_cache_r += u.get("cache_read_input_tokens", 0) or 0
        total_cache_c += u.get("cache_creation_input_tokens", 0) or 0
        total_out += u.get("output_tokens", 0) or 0

    is_error = isinstance(parent_tool_result, dict) and parent_tool_result.get("_error") is True
    parent_output = parent_tool_result.get("content") if is_error else parent_tool_result

    span_meta = {
        "subagent_type": agent_type,
        "subagent_id": agent_id,
        "subagent_description": description,
        "subagent_api_calls": len(asst_msgs),
        "subagent_tool_calls": sum(len(get_tool_calls(m)) for m in asst_msgs),
        "subagent_total_input_tokens": total_in,
        "subagent_total_cache_read_tokens": total_cache_r,
        "subagent_total_cache_creation_tokens": total_cache_c,
        "subagent_total_output_tokens": total_out,
        "subagent_record_count": len(records),
        "subagent_transcript": str(transcript_path),
    }

    with langfuse.start_as_current_span(
        trace_context={"trace_id": trace_id} if trace_id else None,
        name=f"Tool: Agent ({agent_type})",
        input=redact(truncate(parent_tool_use.get("input"))),
        output=redact(truncate(parent_output)),
        metadata=span_meta,
        level="ERROR" if is_error else None,
        status_message="subagent reported error" if is_error else None,
    ):
        for i, asst_rec in enumerate(asst_msgs, start=1):
            msg = asst_rec.get("message") or {}
            usage_details = extract_usage_details(msg.get("usage") or {})
            model_params = extract_model_parameters(msg)
            tool_uses = get_tool_calls(asst_rec)
            tool_use_summary = [{"name": t.get("name"), "id": t.get("id")} for t in tool_uses]

            gen = langfuse.start_observation(
                as_type="generation",
                name=f"[{agent_type}] API call {i}/{len(asst_msgs)}",
                model=msg.get("model") or "claude",
                input={
                    "anthropic_message_id": msg.get("id"),
                    "tool_uses_planned": tool_use_summary,
                },
                output={
                    "text": get_text_content(asst_rec),
                    "stop_reason": msg.get("stop_reason"),
                    "tool_uses": tool_use_summary,
                },
                model_parameters=model_params,
                usage_details=usage_details,
                metadata={
                    "subagent_type": agent_type,
                    "subagent_id": agent_id,
                    "api_call_index": i,
                    "stop_reason": msg.get("stop_reason"),
                    "anthropic_message_id": msg.get("id"),
                },
            )
            gen.end()

        for asst_rec in asst_msgs:
            for tu in get_tool_calls(asst_rec):
                tool_name = tu.get("name", "unknown")
                tool_id = tu.get("id", "")
                raw_result = _find_tool_result(tool_id, tool_results)
                tool_is_error = isinstance(raw_result, dict) and raw_result.get("_error") is True
                with langfuse.start_as_current_span(
                    name=f"[{agent_type}] Tool: {tool_name}",
                    input=redact(truncate(tu.get("input"))),
                    output=redact(truncate(raw_result)),
                    metadata={
                        "tool_name": tool_name,
                        "tool_id": tool_id,
                        "is_error": tool_is_error,
                        "subagent_type": agent_type,
                        "subagent_id": agent_id,
                    },
                    level="ERROR" if tool_is_error else None,
                    status_message="tool reported error" if tool_is_error else None,
                ):
                    pass


def _find_tool_result(tool_id: str, results: list) -> Any:
    """Return the content of a tool_result for a given tool_use_id, or
    {'_error': True, 'content': ...} when the result is marked is_error."""
    for r in results:
        c = get_content(r)
        if not isinstance(c, list):
            continue
        for item in c:
            if isinstance(item, dict) and item.get("tool_use_id") == tool_id:
                payload = item.get("content")
                if item.get("is_error"):
                    return {"_error": True, "content": payload}
                return payload
    return None



def extract_project_name(project_dir: Path) -> str:
    """Extract a human-readable project name from the Claude projects directory name.

    Directory names look like: -Users-doneyli-djg-family-office
    We extract the last segment as the project name.
    """
    dir_name = project_dir.name
    # Split on the path-encoded dashes and take the last non-empty segment
    parts = dir_name.split("-")
    # Rebuild: find the last meaningful project name
    # Pattern: -Users-<user>-<project-name> or -Users-<user>-<path>-<project-name>
    # Take everything after the username (3rd segment onward)
    if len(parts) > 3:
        # parts[0] is empty (leading dash), parts[1] is "Users", parts[2] is username
        project_parts = parts[3:]
        return "-".join(project_parts)
    return dir_name


def find_latest_transcript() -> tuple[str, Path, str] | None:
    """Find the most recently modified transcript file.

    Claude Code stores transcripts as *.jsonl files directly in the project directory.
    Main conversation files have UUID names, agent files have agent-*.jsonl names.
    The session ID is stored inside each JSON line.

    Returns: (session_id, transcript_path, project_name) or None
    """
    projects_dir = Path.home() / ".claude" / "projects"

    if not projects_dir.exists():
        debug(f"Projects directory not found: {projects_dir}")
        return None

    latest_file = None
    latest_mtime = 0
    latest_project_dir = None

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        # Look for all .jsonl files directly in the project directory
        for transcript_file in project_dir.glob("*.jsonl"):
            mtime = transcript_file.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_file = transcript_file
                latest_project_dir = project_dir

    if latest_file and latest_project_dir:
        # Extract session ID from the first line of the file
        try:
            first_line = latest_file.read_text().split("\n")[0]
            first_msg = json.loads(first_line)
            session_id = first_msg.get("sessionId", latest_file.stem)
            project_name = extract_project_name(latest_project_dir)
            debug(f"Found transcript: {latest_file}, session: {session_id}, project: {project_name}")
            return (session_id, latest_file, project_name)
        except (json.JSONDecodeError, IOError, IndexError) as e:
            debug(f"Error reading transcript {latest_file}: {e}")
            return None

    debug("No transcript files found")
    return None


def find_modified_transcripts(state: dict, max_sessions: int = 10) -> list[tuple[str, Path, str]]:
    """Find all transcripts that have been modified since their last state update.

    Returns up to max_sessions transcripts, sorted by modification time (most recent first).
    This ensures we don't miss sessions when multiple are active concurrently.

    Returns: list of (session_id, transcript_path, project_name) tuples
    """
    projects_dir = Path.home() / ".claude" / "projects"

    if not projects_dir.exists():
        debug(f"Projects directory not found: {projects_dir}")
        return []

    modified_transcripts = []

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = extract_project_name(project_dir)

        # Look for all .jsonl files directly in the project directory
        for transcript_file in project_dir.glob("*.jsonl"):
            # Skip subagent transcripts (they're in subdirectories and caught by glob **)
            if "subagents" in str(transcript_file):
                continue

            try:
                # Get file modification time
                mtime = transcript_file.stat().st_mtime

                # Extract session ID from the first line
                first_line = transcript_file.read_text().split("\n")[0]
                first_msg = json.loads(first_line)
                session_id = first_msg.get("sessionId", transcript_file.stem)

                # Check if this session has been modified since last update
                session_state = state.get(session_id, {})
                last_update = session_state.get("updated", "1970-01-01T00:00:00+00:00")
                last_update_timestamp = datetime.fromisoformat(last_update).timestamp()

                # If file modified after last state update, it needs processing
                if mtime > last_update_timestamp:
                    modified_transcripts.append({
                        "session_id": session_id,
                        "transcript_file": transcript_file,
                        "project_name": project_name,
                        "mtime": mtime,
                    })
                    debug(f"Found modified session: {session_id} (project: {project_name})")
            except (json.JSONDecodeError, IOError, IndexError) as e:
                debug(f"Error reading transcript {transcript_file}: {e}")
                continue

    # Sort by modification time (most recent first) and limit
    modified_transcripts.sort(key=lambda x: x["mtime"], reverse=True)
    result = [
        (t["session_id"], t["transcript_file"], t["project_name"])
        for t in modified_transcripts[:max_sessions]
    ]

    debug(f"Found {len(result)} modified transcripts (out of {len(modified_transcripts)} total)")
    return result


def queue_turns_from_messages(
    messages: list,
    session_id: str,
    turn_count: int,
    project_name: str,
    transcript_path: str | None = None,
) -> int:
    """Parse messages into turns and queue them locally. Returns number of turns queued."""
    turns = 0
    current_user = None
    current_assistants = []
    current_assistant_parts = []
    current_msg_id = None
    current_tool_results = []

    for msg in messages:
        role = msg.get("type") or (msg.get("message", {}).get("role"))

        if role == "user":
            if is_tool_result(msg):
                current_tool_results.append(msg)
                continue

            # New user message - finalize previous turn
            if current_msg_id and current_assistant_parts:
                merged = merge_assistant_parts(current_assistant_parts)
                current_assistants.append(merged)
                current_assistant_parts = []
                current_msg_id = None

            if current_user and current_assistants:
                turns += 1
                turn_num = turn_count + turns
                queue_trace({
                    "session_id": session_id,
                    "turn_num": turn_num,
                    "user_msg": current_user,
                    "assistant_msgs": current_assistants,
                    "tool_results": current_tool_results,
                    "project_name": project_name,
                    "transcript_path": transcript_path,
                })

            current_user = msg
            current_assistants = []
            current_assistant_parts = []
            current_msg_id = None
            current_tool_results = []

        elif role == "assistant":
            msg_id = None
            if isinstance(msg, dict) and "message" in msg:
                msg_id = msg["message"].get("id")

            if not msg_id:
                current_assistant_parts.append(msg)
            elif msg_id == current_msg_id:
                current_assistant_parts.append(msg)
            else:
                if current_msg_id and current_assistant_parts:
                    merged = merge_assistant_parts(current_assistant_parts)
                    current_assistants.append(merged)
                current_msg_id = msg_id
                current_assistant_parts = [msg]

    # Process final turn
    if current_msg_id and current_assistant_parts:
        merged = merge_assistant_parts(current_assistant_parts)
        current_assistants.append(merged)

    if current_user and current_assistants:
        turns += 1
        turn_num = turn_count + turns
        queue_trace({
            "session_id": session_id,
            "turn_num": turn_num,
            "user_msg": current_user,
            "assistant_msgs": current_assistants,
            "tool_results": current_tool_results,
            "project_name": project_name,
        })

    return turns


def get_permission_flags(session_id: str) -> list[dict]:
    """Read flagged permission events for the current session."""
    if not PERMISSION_EVENTS_FILE.exists():
        return []
    events = []
    try:
        for line in PERMISSION_EVENTS_FILE.read_text().strip().split("\n"):
            if not line:
                continue
            event = json.loads(line)
            if event.get("session_id") == session_id:
                events.append(event)
    except (json.JSONDecodeError, OSError):
        pass
    return events


def create_trace(
    langfuse: Langfuse,
    session_id: str,
    turn_num: int,
    user_msg: dict,
    assistant_msgs: list,
    tool_results: list,
    project_name: str = "",
    transcript_path: str | Path | None = None,
    emitted_ids: set | None = None,
) -> list:
    """Create a Langfuse trace for a single turn (v2 schema).

    Emits one GENERATION per assistant API call with real Anthropic-reported
    usage (incl. cache tokens), expands Agent tool_use blocks into nested
    subagent observations, marks errored tools at level=ERROR, and sets
    user_id / release / semantic trace name.

    Returns one outcome dict per Agent tool_use — either {"status": "emitted"}
    or {"status": "pending", ...} for a subagent that was still running. The
    caller records these so the sweep can finish the pending ones exactly once.
    """
    emitted_ids = emitted_ids or set()
    subagent_outcomes: list = []
    user_text = get_text_content(user_msg)
    final_text = get_text_content(assistant_msgs[-1]) if assistant_msgs else ""

    trace_name = derive_trace_name(user_msg, turn_num)
    release = derive_release(transcript_path)
    user_id = getpass.getuser()
    subagents = discover_subagents(transcript_path)

    # Pre-compute trace-level totals/error counts for metadata
    error_count = 0
    for r in tool_results:
        c = get_content(r)
        if isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("is_error"):
                    error_count += 1

    trace_meta: dict[str, Any] = {
        "source": "claude-code",
        "turn_number": turn_num,
        "session_id": session_id,
        "project": project_name,
        "release": release,
        "assistant_api_calls": len(assistant_msgs),
        "tool_call_count": sum(len(get_tool_calls(m)) for m in assistant_msgs),
        "tool_result_count": len(tool_results),
        "subagent_count": len(subagents),
    }
    if error_count:
        trace_meta["tool_error_count"] = error_count
    if transcript_path:
        trace_meta["transcript_path"] = str(transcript_path)

    tags = ["claude-code"]
    if project_name:
        tags.append(project_name)

    with langfuse.start_as_current_span(
        name=trace_name,
        input={"role": "user", "content": user_text},
        metadata=trace_meta,
        level="ERROR" if error_count else None,
        status_message=f"{error_count} tool error(s)" if error_count else None,
    ) as trace_span:
        langfuse.update_current_trace(
            name=trace_name,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            version=release,
            metadata=trace_meta,
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": final_text},
        )

        # ─── ONE GENERATION PER ASSISTANT API CALL ────────────────────────
        for i, asst_rec in enumerate(assistant_msgs, start=1):
            msg = asst_rec.get("message") if isinstance(asst_rec, dict) else None
            msg = msg or {}
            usage_details = extract_usage_details(msg.get("usage") or {})
            model_params = extract_model_parameters(msg)
            tool_uses = get_tool_calls(asst_rec)
            tool_use_summary = [{"name": t.get("name"), "id": t.get("id")} for t in tool_uses]

            level = None
            status_message = None
            if msg.get("stop_reason") in ("max_tokens", "refusal"):
                level = "WARNING"
                status_message = f"stop_reason={msg.get('stop_reason')}"

            gen = langfuse.start_observation(
                as_type="generation",
                name=f"API call {i}/{len(assistant_msgs)}",
                model=msg.get("model") or "claude",
                input={
                    "anthropic_message_id": msg.get("id"),
                    "tool_uses_planned": tool_use_summary,
                },
                output={
                    "text": get_text_content(asst_rec),
                    "stop_reason": msg.get("stop_reason"),
                    "tool_uses": tool_use_summary,
                },
                model_parameters=model_params,
                usage_details=usage_details,
                metadata={
                    "api_call_index": i,
                    "api_call_count": len(assistant_msgs),
                    "tool_use_count": len(tool_uses),
                    "tool_uses": tool_use_summary,
                    "stop_reason": msg.get("stop_reason"),
                    "anthropic_message_id": msg.get("id"),
                },
                level=level,
                status_message=status_message,
            )
            gen.end()

        # ─── TOOL CALLS as child spans (with subagent expansion) ─────────
        for asst_rec in assistant_msgs:
            for tu in get_tool_calls(asst_rec):
                tool_name = tu.get("name", "unknown")
                tool_id = tu.get("id", "")
                raw_result = _find_tool_result(tool_id, tool_results)

                # Subagent expansion: when tool is Agent, resolve the subagent —
                # by agentId from the tool_result when present, else by
                # description — and emit its transcript as nested observations.
                # Async subagents are usually still running here, so anything
                # unfinished is deferred to the sweep rather than emitted partial.
                if tool_name == "Agent":
                    desc = (tu.get("input") or {}).get("description", "")
                    sub = resolve_subagent(
                        subagents, desc, agent_id_from_result(raw_result), transcript_path
                    )
                    if sub:
                        agent_id = sub["agent_id"]
                        if agent_id in emitted_ids:
                            continue
                        if subagent_is_complete(sub["path"], transcript_path, agent_id):
                            try:
                                emit_subagent_span(
                                    langfuse,
                                    subagent_info=sub,
                                    parent_tool_use=tu,
                                    parent_tool_result=raw_result,
                                )
                                subagent_outcomes.append({"status": "emitted", "agent_id": agent_id})
                                continue
                            except Exception as e:
                                debug(f"subagent expansion failed for {desc!r}: {e}")
                                # Fall through to leaf span
                        else:
                            subagent_outcomes.append({
                                "status": "pending",
                                "agent_id": agent_id,
                                "trace_id": langfuse.get_current_trace_id(),
                                "path": str(sub["path"]),
                                "agent_type": sub["agent_type"],
                                "description": sub["description"],
                                "parent_transcript": str(transcript_path or ""),
                                "tool_use": tu,
                            })
                            debug(f"subagent {agent_id} still running; deferred to sweep")

                tool_is_error = isinstance(raw_result, dict) and raw_result.get("_error") is True
                with langfuse.start_as_current_span(
                    name=f"Tool: {tool_name}",
                    input=redact(truncate(tu.get("input"))),
                    output=redact(truncate(raw_result)),
                    metadata={
                        "tool_name": tool_name,
                        "tool_id": tool_id,
                        "is_error": tool_is_error,
                    },
                    level="ERROR" if tool_is_error else None,
                    status_message="tool reported error" if tool_is_error else None,
                ):
                    pass
                debug(f"Created span for tool: {tool_name}")

        # Add permission governance data
        perm_events = get_permission_flags(session_id)
        if perm_events:
            tags.append("has-permission-flags")
            langfuse.update_current_trace(tags=tags)
            flag_summary: dict[str, int] = {}
            for evt in perm_events:
                for flag in evt.get("flags", []):
                    flag_summary[flag] = flag_summary.get(flag, 0) + 1
            with langfuse.start_as_current_span(
                name="Permission Events",
                input={"flagged_event_count": len(perm_events), "flag_summary": flag_summary},
                metadata={"source": "claude-governance"},
            ) as perm_span:
                perm_span.update(output={"events": perm_events})

        trace_span.update(output={"role": "assistant", "content": final_text})

    debug(f"Created trace for turn {turn_num} ({len(assistant_msgs)} API calls, {len(subagents)} subagents)")
    return subagent_outcomes


def sweep_pending_subagents(langfuse: Langfuse, state: dict) -> int:
    """Emit subagents that were still running when their spawning turn was traced.

    Runs every fire over every session with pending work — deliberately NOT
    limited to sessions whose parent transcript changed, because an async
    subagent finishing does not necessarily touch the parent at all.

    Exactly-once is enforced here, via `emitted_subagents` in the state file. The
    Langfuse v3 SDK generates observation ids from OpenTelemetry and gives no way
    to pin them, so a re-emit is a duplicate rather than an upsert; this
    bookkeeping is the only thing preventing that.
    """
    emitted = 0
    for session_id, sess in list(state.items()):
        if not isinstance(sess, dict):
            continue
        pending = sess.get("pending_subagents") or {}
        if not pending:
            continue
        done = set(sess.get("emitted_subagents") or [])
        for agent_id, info in list(pending.items()):
            if agent_id in done:
                pending.pop(agent_id, None)
                continue
            path = Path(info.get("path", ""))
            if not subagent_is_complete(path, info.get("parent_transcript"), agent_id):
                continue
            try:
                emit_subagent_span(
                    langfuse,
                    subagent_info={
                        "path": path,
                        "agent_type": info.get("agent_type", "unknown"),
                        "agent_id": agent_id,
                        "description": info.get("description", ""),
                    },
                    parent_tool_use=info.get("tool_use") or {},
                    parent_tool_result=None,
                    trace_id=info.get("trace_id"),
                )
            except Exception as e:
                log("WARN", f"sweep: could not emit subagent {agent_id}: {e}")
                continue
            done.add(agent_id)
            pending.pop(agent_id, None)
            emitted += 1
        sess["emitted_subagents"] = sorted(done)
        sess["pending_subagents"] = pending
    if emitted:
        save_state(state)
        log("INFO", f"sweep: emitted {emitted} completed subagent(s)")
    return emitted


def record_subagent_outcomes(outcomes: list, emitted_ids: set, pending: dict) -> None:
    """Fold create_trace's per-Agent outcomes into this session's bookkeeping."""
    for o in outcomes or []:
        agent_id = o.get("agent_id")
        if not agent_id:
            continue
        if o.get("status") == "emitted":
            emitted_ids.add(agent_id)
            pending.pop(agent_id, None)
        elif agent_id not in emitted_ids:
            pending[agent_id] = {k: v for k, v in o.items() if k != "status"}


def process_transcript(langfuse: Langfuse, session_id: str, transcript_file: Path, state: dict, project_name: str = "") -> int:
    """Process a transcript file and create traces for new turns."""
    # Get previous state for this session
    session_state = state.get(session_id, {})
    last_line = session_state.get("last_line", 0)
    turn_count = session_state.get("turn_count", 0)
    emitted_ids = set(session_state.get("emitted_subagents") or [])
    pending_subagents = dict(session_state.get("pending_subagents") or {})
    # The turn currently open at the end of the previous fire. A turn is
    # user -> assistant(s), but fires process only [last_line -> EOF], so a long
    # async turn's opening user message and its later assistant replies land in
    # different fires. Without carrying the user forward, a fire that sees only
    # the assistant side has current_user=None, the finalize guard skips it, and
    # last_line advances past the whole turn — dropping its parent generation AND
    # any subagents it spawned. Restoring it lets that fire finalize the turn.
    open_user = session_state.get("open_user")

    # Read transcript
    lines = transcript_file.read_text().strip().split("\n")
    total_lines = len(lines)

    if last_line >= total_lines:
        debug(f"No new lines to process (last: {last_line}, total: {total_lines})")
        return 0

    # Parse new messages
    new_messages = []
    for i in range(last_line, total_lines):
        try:
            msg = json.loads(lines[i])
            new_messages.append(msg)
        except json.JSONDecodeError:
            continue

    if not new_messages:
        return 0

    debug(f"Processing {len(new_messages)} new messages")

    # Group messages into turns (user -> assistant(s) -> tool_results).
    # current_user starts from the turn left open by the previous fire, so
    # assistant replies arriving in this fire are attributed to it rather than
    # dropped (see open_user above).
    turns = 0
    current_user = open_user
    current_assistants = []
    current_assistant_parts = []
    current_msg_id = None
    current_tool_results = []

    for msg in new_messages:
        role = msg.get("type") or (msg.get("message", {}).get("role"))

        if role == "user":
            # Check if this is a tool result
            if is_tool_result(msg):
                current_tool_results.append(msg)
                continue

            # New user message - finalize previous turn
            if current_msg_id and current_assistant_parts:
                merged = merge_assistant_parts(current_assistant_parts)
                current_assistants.append(merged)
                current_assistant_parts = []
                current_msg_id = None

            if current_user and current_assistants:
                turns += 1
                turn_num = turn_count + turns
                record_subagent_outcomes(
                    create_trace(langfuse, session_id, turn_num, current_user, current_assistants, current_tool_results, project_name, transcript_path=str(transcript_file), emitted_ids=emitted_ids),
                    emitted_ids, pending_subagents,
                )

            # Start new turn
            current_user = msg
            current_assistants = []
            current_assistant_parts = []
            current_msg_id = None
            current_tool_results = []

        elif role == "assistant":
            msg_id = None
            if isinstance(msg, dict) and "message" in msg:
                msg_id = msg["message"].get("id")

            if not msg_id:
                # No message ID, treat as continuation
                current_assistant_parts.append(msg)
            elif msg_id == current_msg_id:
                # Same message ID, add to current parts
                current_assistant_parts.append(msg)
            else:
                # New message ID - finalize previous message
                if current_msg_id and current_assistant_parts:
                    merged = merge_assistant_parts(current_assistant_parts)
                    current_assistants.append(merged)

                # Start new assistant message
                current_msg_id = msg_id
                current_assistant_parts = [msg]

    # Process final turn
    if current_msg_id and current_assistant_parts:
        merged = merge_assistant_parts(current_assistant_parts)
        current_assistants.append(merged)

    if current_user and current_assistants:
        turns += 1
        turn_num = turn_count + turns
        record_subagent_outcomes(
            create_trace(langfuse, session_id, turn_num, current_user, current_assistants, current_tool_results, project_name, transcript_path=str(transcript_file), emitted_ids=emitted_ids),
            emitted_ids, pending_subagents,
        )

    # Update state. emitted/pending subagents must survive this write — losing
    # them either strands a subagent forever or re-emits it as a duplicate.
    state[session_id] = {
        "last_line": total_lines,
        "turn_count": turn_count + turns,
        "updated": datetime.now(timezone.utc).isoformat(),
        "emitted_subagents": sorted(emitted_ids),
        "pending_subagents": pending_subagents,
        # Carry the still-open turn's user message to the next fire so its later
        # assistant replies are not dropped. None once the turn closes.
        "open_user": current_user,
    }
    save_state(state)

    return turns


def main():
    script_start = datetime.now()
    debug("Hook started")

    # Check if tracing is enabled
    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        debug("Tracing disabled (TRACE_TO_LANGFUSE != true)")
        sys.exit(0)

    # Check for required environment variables
    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("CC_LANGFUSE_HOST") or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    if not public_key or not secret_key:
        log("ERROR", "Langfuse API keys not set (CC_LANGFUSE_PUBLIC_KEY / CC_LANGFUSE_SECRET_KEY)")
        sys.exit(0)

    # Load state
    state = load_state()

    # Find all modified transcripts (up to 10 most recent)
    modified_transcripts = find_modified_transcripts(state, max_sessions=10)

    if not modified_transcripts:
        debug("No modified transcripts found")
        sys.exit(0)

    debug(f"Found {len(modified_transcripts)} modified session(s) to process")

    # Check if Langfuse is reachable
    langfuse_available = check_langfuse_health(host)

    if not langfuse_available:
        # Queue all modified sessions
        log("WARN", f"Langfuse unavailable at {host}, queuing traces locally")

        total_turns_queued = 0
        for session_id, transcript_file, project_name in modified_transcripts:
            # Get previous state for this session
            session_state = state.get(session_id, {})
            last_line = session_state.get("last_line", 0)
            turn_count = session_state.get("turn_count", 0)

            # Read transcript
            try:
                lines = transcript_file.read_text().strip().split("\n")
                total_lines = len(lines)

                if last_line >= total_lines:
                    continue

                # Parse new messages and queue turns
                new_messages = []
                for i in range(last_line, total_lines):
                    try:
                        msg = json.loads(lines[i])
                        new_messages.append(msg)
                    except json.JSONDecodeError:
                        continue

                if new_messages:
                    turns_queued = queue_turns_from_messages(
                        new_messages, session_id, turn_count, project_name,
                        transcript_path=str(transcript_file),
                    )
                    total_turns_queued += turns_queued

                    # Update state even when queuing. Preserve the subagent
                    # bookkeeping and open turn unchanged — this degraded path
                    # doesn't expand subagents, but it must not clobber what the
                    # normal path tracked, or already-emitted subagents re-emit
                    # (no upsert on SDK v3) and in-flight ones are stranded.
                    state[session_id] = {
                        "last_line": total_lines,
                        "turn_count": turn_count + turns_queued,
                        "updated": datetime.now(timezone.utc).isoformat(),
                        "emitted_subagents": session_state.get("emitted_subagents", []),
                        "pending_subagents": session_state.get("pending_subagents", {}),
                        "open_user": session_state.get("open_user"),
                    }
            except Exception as e:
                debug(f"Error queuing session {session_id}: {e}")
                continue

        save_state(state)
        duration = (datetime.now() - script_start).total_seconds()
        log("INFO", f"Queued {total_turns_queued} turns from {len(modified_transcripts)} sessions in {duration:.1f}s")
        sys.exit(0)

    # Langfuse is available - initialize client
    # Set OTel resource attribute so spans don't show as "unknown_service"
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)
    try:
        langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            release=derive_release(),
        )
    except Exception as e:
        log("ERROR", f"Failed to initialize Langfuse client: {e}")
        sys.exit(0)

    try:
        # First, drain any queued traces
        drained = drain_queue(langfuse)
        if drained > 0:
            langfuse.flush()

        # Process all modified transcripts
        total_turns = 0
        for session_id, transcript_file, project_name in modified_transcripts:
            try:
                turns = process_transcript(langfuse, session_id, transcript_file, state, project_name)
                total_turns += turns
                debug(f"Processed {turns} turns from session {session_id}")
            except Exception as e:
                log("ERROR", f"Failed to process session {session_id}: {e}")
                import traceback
                debug(traceback.format_exc())
                continue

        # Emit any subagents that have finished since a previous fire. Runs over
        # every session with pending work, not just the modified ones.
        swept = sweep_pending_subagents(langfuse, state)

        # Flush to ensure all data is sent
        langfuse.flush()

        # Log execution time
        duration = (datetime.now() - script_start).total_seconds()
        log("INFO", f"Processed {total_turns} turns from {len(modified_transcripts)} sessions (drained {drained} from queue, swept {swept} subagents) in {duration:.1f}s")

        if duration > 180:
            log("WARN", f"Hook took {duration:.1f}s (>3min), consider optimizing")

    except Exception as e:
        log("ERROR", f"Failed to process transcripts: {e}")
        import traceback
        debug(traceback.format_exc())
    finally:
        langfuse.shutdown()

    sys.exit(0)


if __name__ == "__main__":
    main()
