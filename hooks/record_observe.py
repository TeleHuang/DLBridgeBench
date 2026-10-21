#!/usr/bin/env python3
"""Record interactive Claude Code behaviour for a manual observation trial.

The TBBCC plugin registers this script for session, prompt, tool and stop
events.  It is intentionally silent and never blocks the session: without an
active observation marker it exits 0 immediately, and every failure is
swallowed after a best-effort write.

Marker discovery order:
1. ``AGENTIC_OBSERVE_RUN_DIR`` environment variable.
2. ``.agentic-observe.json`` in the hook's cwd or one of its parents.

Outputs, all under ``<run_dir>/observe/``:
- ``hook_events.jsonl``   one sanitized JSON record per hook event
- ``active_session.json`` session id / transcript path seen so far
- ``transcripts/<id>.jsonl`` a copy of the Claude Code transcript
- ``session_summary.json`` models, token usage, turn and tool counts
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MARKER = ".agentic-observe.json"
MAX_STRING = 4000
MAX_ITEMS = 50
MAX_DEPTH = 8
MAX_EVENT_BYTES = 64 * 1024
SENSITIVE = re.compile(r"(token|secret|password|passwd|authorization|api[_-]?key|credential)",
                       re.IGNORECASE)
SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}"
                          r"|Bearer\s+[A-Za-z0-9._-]{12,})")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truncate(value: Any, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return "[depth limit]"
    if isinstance(value, str):
        value = SECRET_VALUE.sub("[REDACTED]", value)
        if len(value) > MAX_STRING:
            return value[:MAX_STRING] + f"...[truncated {len(value) - MAX_STRING} chars]"
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                out["..."] = f"[{len(value) - MAX_ITEMS} more keys]"
                break
            name = str(key)
            if SENSITIVE.search(name):
                out[name] = "[REDACTED]"
            else:
                out[name] = _truncate(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [_truncate(item, depth + 1) for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"...[{len(value) - MAX_ITEMS} more items]")
        return items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _truncate(str(value), depth + 1)


def _find_marker(payload: dict[str, Any]) -> Path | None:
    env = os.environ.get("AGENTIC_OBSERVE_RUN_DIR")
    if env:
        path = Path(env)
        if path.is_dir():
            return path
    starts = []
    if isinstance(payload.get("cwd"), str):
        starts.append(Path(payload["cwd"]))
    try:
        starts.append(Path.cwd())
    except OSError:
        pass
    seen: set[Path] = set()
    for start in starts:
        try:
            current = start.resolve()
        except OSError:
            continue
        for _ in range(6):
            if current in seen:
                break
            seen.add(current)
            marker = current / MARKER
            if marker.is_file():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
                run_dir = data.get("run_dir")
                if isinstance(run_dir, str) and Path(run_dir).is_dir():
                    return Path(run_dir)
            if current.parent == current:
                break
            current = current.parent
    return None


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    raw = line.encode("utf-8")
    if len(raw) > MAX_EVENT_BYTES:
        record = {"schema_version": 1, "ts_utc": _now(),
                  "event": record.get("event"), "session_id": record.get("session_id"),
                  "truncated": True, "original_bytes": len(raw),
                  "detail": {"note": "event exceeded the per-record cap"}}
        raw = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        with open(path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(raw.decode("utf-8"))
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        try:
            with open(path, "ab") as handle:
                handle.write(raw)
        except OSError:
            pass


def _summarize_transcript(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "session_ids": [], "models": {}, "usage": {"input_tokens": 0,
        "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0}, "assistant_turns": 0, "user_turns": 0,
        "tool_calls": {}, "first_timestamp": None, "last_timestamp": None,
        "lines": 0, "parse_errors": 0,
    }
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return summary
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            summary["lines"] += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                summary["parse_errors"] += 1
                continue
            if not isinstance(item, dict):
                continue
            session_id = item.get("sessionId") or item.get("session_id")
            if isinstance(session_id, str) and session_id not in summary["session_ids"]:
                summary["session_ids"].append(session_id)
            stamp = item.get("timestamp")
            if isinstance(stamp, str):
                summary["first_timestamp"] = summary["first_timestamp"] or stamp
                summary["last_timestamp"] = stamp
            kind = item.get("type")
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            if kind == "assistant" or message.get("role") == "assistant":
                summary["assistant_turns"] += 1
                model = message.get("model")
                if isinstance(model, str):
                    summary["models"][model] = summary["models"].get(model, 0) + 1
                usage = message.get("usage")
                if isinstance(usage, dict):
                    for key in summary["usage"]:
                        value = usage.get(key)
                        if isinstance(value, (int, float)):
                            summary["usage"][key] += int(value)
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name") or "unknown"
                            summary["tool_calls"][name] = summary["tool_calls"].get(name, 0) + 1
            elif kind == "user" or message.get("role") == "user":
                summary["user_turns"] += 1
    return summary


def _copy_transcript(path: Path, observe_dir: Path, session_id: str) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        target = observe_dir / "transcripts" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', session_id)}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        info = {"path": str(target), "source": str(path), "bytes": target.stat().st_size,
                "sha256": digest, "copied_utc": _now()}
        summary = _summarize_transcript(target)
        _atomic_json(observe_dir / "session_summary.json",
                     {"schema_version": 1, "session_id": session_id, **info,
                      "summary": summary, "updated_utc": _now()})
        _append_jsonl(observe_dir / "transcript_index.jsonl",
                      {"schema_version": 1, "ts_utc": _now(), "session_id": session_id,
                       **info})
        return info
    except OSError:
        return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    run_dir = _find_marker(payload)
    if run_dir is None:
        return 0
    observe_dir = run_dir / "observe"
    event = payload.get("hook_event_name") or payload.get("event") or "unknown"
    session_id = payload.get("session_id") or "unknown"
    transcript = payload.get("transcript_path")

    record = {
        "schema_version": 1,
        "ts_utc": _now(),
        "event": event,
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "permission_mode": payload.get("permission_mode"),
        "tool_name": payload.get("tool_name"),
        "tool_input": _truncate(payload.get("tool_input")),
        "tool_response": _truncate(payload.get("tool_response")),
        "prompt": _truncate(payload.get("prompt")),
        "source": payload.get("source"),
        "reason": payload.get("reason"),
        "stop_hook_active": payload.get("stop_hook_active"),
        "transcript_path": transcript,
    }
    _append_jsonl(observe_dir / "hook_events.jsonl", record)
    _atomic_json(observe_dir / "active_session.json", {
        "schema_version": 1, "session_id": session_id, "cwd": payload.get("cwd"),
        "transcript_path": transcript, "last_event": event, "updated_utc": _now(),
    })

    if event in ("Stop", "SessionEnd", "SubagentStop") and isinstance(transcript, str):
        _copy_transcript(Path(transcript), observe_dir, str(session_id))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A recorder failure must never interrupt the observed session.
        sys.exit(0)
