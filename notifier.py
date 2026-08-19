from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from typing import Any
from datetime import datetime, timezone

from desktop_cdp_transport import DesktopCdpClient
from websocket_transport import SharedAppServerProcess, WebSocketConnection


STATE_VERSION = 2
NOTIFIER_VERSION = "1.2.1"
QUOTE_FOOTER = "↩ 引用此条信息进行回复"
QUEUE_HINT = "如任务正在处理，则默认排队，直接提交请加前缀“/y”"
WECHAT_BLANK_LINE = "\u200b"
_state_write_snapshots: dict[str, str] = {}
_state_write_snapshots_lock = threading.Lock()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    required = ("codex_db", "cc_connect", "cc_project", "state_file", "log_file")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(missing)}")

    config.setdefault("allowed_sources", ["vscode"])
    config.setdefault("poll_seconds", 3)
    config.setdefault("send_timeout_seconds", 60)
    config.setdefault("max_message_chars", 3400)
    config.setdefault("sent_history_limit", 2000)
    config.setdefault("quote_route_history_limit", 2000)
    config.setdefault("handled_message_history_limit", 2000)
    config.setdefault("router_host", "127.0.0.1")
    config.setdefault("router_port", 18765)
    config.setdefault("router_token", "")
    config.setdefault("codex_turn_timeout_seconds", 7200)
    config.setdefault("codex_app_server_request_timeout_seconds", 30)
    config.setdefault("codex_app_server_transport", "stdio")
    config.setdefault("codex_app_server_ws_url", "ws://127.0.0.1:18766")
    config.setdefault("codex_app_server_start_timeout_seconds", 30)
    config.setdefault("codex_submit_transport", "app-server")
    config.setdefault("codex_desktop_cdp_url", "http://127.0.0.1:9335")
    config.setdefault("codex_desktop_cdp_timeout_seconds", 30)
    config.setdefault("reply_retry_limit", 5)
    config.setdefault("cc_connect_config", str(Path.home() / ".cc-connect" / "config.toml"))
    config.setdefault(
        "codex_global_state",
        str(Path(str(config["codex_db"])).with_name(".codex-global-state.json")),
    )
    config.setdefault(
        "codex_catalog_db",
        str(Path(str(config["codex_db"])).with_name("sqlite") / "codex-dev.db"),
    )
    config.setdefault(
        "codex_automations_dir",
        str(Path(str(config["codex_db"])).with_name("automations")),
    )
    if config.get("router_enabled", True) and not config.get("codex_cli"):
        raise ValueError("Missing config key: codex_cli")
    if config["codex_app_server_transport"] not in {
        "stdio",
        "desktop-shared-websocket",
    }:
        raise ValueError("Invalid codex_app_server_transport")
    if config["codex_submit_transport"] not in {"app-server", "desktop-cdp"}:
        raise ValueError("Invalid codex_submit_transport")
    return config


def setup_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("codex_pinned_wechat_notifier")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # pythonw.exe has no console streams; file logging remains authoritative.
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "offsets": {},
        "pending": [],
        "sent_turns": {},
        "quote_routes": [],
        "queue_ack_routes": [],
        "reply_queue": [],
        "handled_message_ids": {},
        "push_enabled": True,
        "pinned_project_push_enabled": False,
        "automation_runs_initialized": False,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        path.replace(backup)
        return empty_state()

    version = state.get("version")
    if version == 1:
        state["version"] = STATE_VERSION
    elif version != STATE_VERSION:
        raise RuntimeError(
            f"Unsupported state version: {state.get('version')!r}; expected {STATE_VERSION}"
        )
    state.setdefault("offsets", {})
    state.setdefault("pending", [])
    state.setdefault("sent_turns", {})
    state.setdefault("quote_routes", [])
    state.setdefault("queue_ack_routes", [])
    state.setdefault("reply_queue", [])
    state.setdefault("handled_message_ids", {})
    state.setdefault("push_enabled", True)
    state.setdefault("pinned_project_push_enabled", False)
    state.setdefault("automation_runs_initialized", False)
    for item in state["pending"]:
        if item.get("delivery_status") == "sending":
            item["delivery_status"] = "queued"
        else:
            item.setdefault("delivery_status", "queued")
    for item in state["reply_queue"]:
        item.setdefault("mode", "queue")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    snapshot = json.dumps(state, ensure_ascii=False, indent=2)
    key = str(path.resolve())
    with _state_write_snapshots_lock:
        if path.exists() and _state_write_snapshots.get(key) == snapshot:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _state_write_snapshots[key] = snapshot


def normalized_path(raw_path: str) -> Path:
    if raw_path.startswith("\\\\?\\"):
        raw_path = raw_path[4:]
    return Path(raw_path)


def reverse_file_lines(path: Path, block_size: int = 64 * 1024):
    """Yield non-empty lines from a potentially large JSONL file, newest first."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + remainder
            lines = data.split(b"\n")
            if position > 0:
                remainder = lines.pop(0)
            else:
                remainder = b""
            for line in reversed(lines):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def parse_event_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_thread_runtime(rollout_path: str | Path) -> dict[str, Any]:
    """Return the latest task lifecycle state without loading the whole rollout."""
    path = normalized_path(str(rollout_path))
    try:
        lines = reverse_file_lines(path)
        for raw in lines:
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            event_type = str(payload.get("type") or "")
            if event_type in {"task_started", "turn_started"}:
                return {
                    "active": True,
                    "turn_id": str(payload.get("turn_id") or payload.get("turnId") or ""),
                    "started_at": parse_event_time(item.get("timestamp")) or path.stat().st_mtime,
                }
            if event_type in {
                "task_complete",
                "turn_aborted",
                "turn_cancelled",
                "task_cancelled",
            }:
                return {
                    "active": False,
                    "turn_id": str(payload.get("turn_id") or payload.get("turnId") or ""),
                    "started_at": None,
                }
    except OSError:
        pass
    return {"active": False, "turn_id": "", "started_at": None}


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{secs:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分"


def read_automation_definitions(config: dict[str, Any]) -> list[dict[str, str]]:
    """Read Desktop automation metadata without depending on task titles."""
    root = Path(str(config.get("codex_automations_dir") or ""))
    if not root.is_dir():
        return []
    definitions: list[dict[str, str]] = []
    try:
        paths = sorted(root.glob("*/automation.toml"))
    except OSError:
        return []
    for path in paths:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        automation_id = str(data.get("id") or path.parent.name).strip()
        target_thread_id = str(data.get("target_thread_id") or "").strip()
        if not automation_id or not target_thread_id:
            continue
        definitions.append(
            {
                "id": automation_id,
                "name": str(data.get("name") or automation_id).strip() or automation_id,
                "status": str(data.get("status") or "").strip(),
                "target_thread_id": target_thread_id,
            }
        )
    return definitions


def automation_id_from_title(title: str) -> str:
    match = re.search(r"(?im)^Automation ID:\s*([^\r\n]+)", title)
    return match.group(1).strip() if match else ""


def automation_name_from_title(title: str) -> str:
    match = re.search(r"(?im)^Automation:\s*([^\r\n]+)", title)
    return match.group(1).strip() if match else ""


def apply_automation_metadata(
    rows: list[dict[str, Any]], config: dict[str, Any]
) -> None:
    by_target = {
        item["target_thread_id"]: item for item in read_automation_definitions(config)
    }
    for row in rows:
        definition = by_target.get(str(row.get("id") or ""))
        row["is_automation_target"] = definition is not None
        if definition is None:
            continue
        row["automation_id"] = definition["id"]
        row["automation_status"] = definition["status"]
        row["title"] = definition["name"]


def read_desktop_threads(config: dict[str, Any]) -> list[dict[str, Any]]:
    db_path = Path(config["codex_db"]).resolve()
    uri = db_path.as_uri() + "?mode=ro"
    sources = list(config["allowed_sources"])
    if not sources:
        return []
    placeholders = ",".join("?" for _ in sources)
    sql = f"""
        SELECT id, title, rollout_path, is_pinned, model, reasoning_effort, cwd
        FROM threads
        WHERE archived = 0
          AND rollout_path IS NOT NULL
          AND thread_source IN ('user', 'automation')
          AND source IN ({placeholders})
    """
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        rows = [dict(row) for row in db.execute(sql, sources).fetchall()]
    finally:
        db.close()
    pinned_ids, pinned_project_thread_ids = read_sidebar_pin_state(config)
    catalog_titles = read_catalog_titles(config, [str(row["id"]) for row in rows])
    for row in rows:
        display_title = catalog_titles.get(str(row["id"]))
        if display_title:
            row["title"] = display_title
    apply_automation_metadata(rows, config)
    if pinned_ids is None:
        for row in rows:
            row["is_project_pinned"] = (
                str(row["id"]) in pinned_project_thread_ids
            )
        return rows
    pinned_index = {thread_id: index for index, thread_id in enumerate(pinned_ids)}
    for row in rows:
        thread_id = str(row["id"])
        row["is_pinned"] = thread_id in pinned_index
        row["pinned_index"] = pinned_index.get(thread_id)
        row["is_project_pinned"] = thread_id in pinned_project_thread_ids
    rows.sort(
        key=lambda row: (
            0 if bool(row.get("is_pinned")) else 1,
            int(row.get("pinned_index") or 0),
        )
    )
    return rows


def read_automation_runs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return execution threads mapped to their current automation targets."""
    definitions = read_automation_definitions(config)
    if not definitions:
        return []
    pinned_ids, _ = read_sidebar_pin_state(config)
    pinned = set(pinned_ids or [])
    by_id = {item["id"]: item for item in definitions}

    db_path = Path(config["codex_db"]).resolve()
    uri = db_path.as_uri() + "?mode=ro"
    sources = list(config["allowed_sources"])
    if not sources:
        return []
    placeholders = ",".join("?" for _ in sources)
    sql = f"""
        SELECT id, title, rollout_path, archived, source, thread_source,
               model, reasoning_effort, cwd
        FROM threads
        WHERE rollout_path IS NOT NULL
          AND thread_source = 'automation'
          AND source IN ({placeholders})
    """
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        rows = [dict(row) for row in db.execute(sql, sources).fetchall()]
    finally:
        db.close()

    runs: list[dict[str, Any]] = []
    for row in rows:
        raw_title = str(row.get("title") or "")
        automation_id = automation_id_from_title(raw_title)
        definition = by_id.get(automation_id)
        automation_name = automation_name_from_title(raw_title)
        if (
            definition is None
            or automation_name != definition["name"]
            or str(row["id"]) == definition["target_thread_id"]
        ):
            continue
        row.update(
            {
                "automation_id": automation_id,
                "is_automation_run": True,
                "is_pinned": definition["target_thread_id"] in pinned,
                "is_project_pinned": False,
                "route_thread_id": definition["target_thread_id"],
                "title": definition["name"],
            }
        )
        runs.append(row)
    return runs


def read_pinned_automation_runs(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in read_automation_runs(config) if bool(row.get("is_pinned"))]


def read_monitored_threads(
    config: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    threads = {str(row["id"]): row for row in read_desktop_threads(config)}
    for row in read_automation_runs(config):
        threads[str(row["id"])] = row
    return list(threads.values())


def read_desktop_thread(config: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    db_path = Path(config["codex_db"]).resolve()
    uri = db_path.as_uri() + "?mode=ro"
    sources = list(config["allowed_sources"])
    if not sources:
        return None
    placeholders = ",".join("?" for _ in sources)
    sql = f"""
        SELECT id, title, rollout_path, is_pinned, archived, source, thread_source,
               model, reasoning_effort, cwd
        FROM threads
        WHERE id = ?
          AND source IN ({placeholders})
    """
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        row = db.execute(sql, [thread_id, *sources]).fetchone()
        result = dict(row) if row is not None else None
    finally:
        db.close()
    if result is None:
        return None
    display_title = read_catalog_titles(config, [thread_id]).get(thread_id)
    if display_title:
        result["title"] = display_title
    apply_automation_metadata([result], config)
    pinned_ids, pinned_project_thread_ids = read_sidebar_pin_state(config)
    if pinned_ids is not None:
        result["is_pinned"] = thread_id in set(pinned_ids)
    result["is_project_pinned"] = thread_id in pinned_project_thread_ids
    return result


def find_submitted_reply_turn(
    rollout_path: str | Path, reply: str, queued_at: int
) -> str:
    """Find a matching Desktop user message created after a queued reply."""
    path = normalized_path(str(rollout_path))
    cutoff = max(0, queued_at - 2)
    try:
        for raw in reverse_file_lines(path):
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            timestamp = parse_event_time(item.get("timestamp"))
            if timestamp is not None and timestamp < cutoff:
                break
            if item.get("type") != "response_item":
                continue
            payload = item.get("payload") or {}
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            content = payload.get("content") or []
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"input_text", "text"}
            )
            if text.strip() != reply.strip():
                continue
            metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
            return str(metadata.get("turn_id") or metadata.get("turnId") or "submitted")
    except OSError:
        return ""
    return ""


def reply_failure_is_transient(config: dict[str, Any], detail: str) -> bool:
    """Keep replies queued while the local Codex submission channel is unhealthy."""
    if config.get("codex_submit_transport") == "desktop-cdp":
        return True
    transient_markers = (
        "Codex app-server request timed out",
        "Codex app-server timed out",
        "Codex app-server WebSocket connection closed",
        "Codex app-server exited with code",
        "Codex app-server stdin is unavailable",
    )
    return any(marker in detail for marker in transient_markers)


def recover_interrupted_reply_requests(
    config: dict[str, Any], state: dict[str, Any], logger: logging.Logger
) -> tuple[int, int]:
    recovered = 0
    requeued = 0
    for item in list(state.get("reply_queue", [])):
        if item.get("status") not in {"running", "promoting", "dispatching"}:
            continue
        if item.get("status") == "promoting" and item.get("native_message_id"):
            item["status"] = "native_queuing"
            requeued += 1
            continue
        request_id = str(item.get("request_id") or "")
        thread_id = str(item.get("thread_id") or "")
        thread = read_desktop_thread(config, thread_id)
        turn_id = ""
        if thread is not None:
            turn_id = find_submitted_reply_turn(
                str(thread.get("rollout_path") or ""),
                str(item.get("reply") or ""),
                int(item.get("queued_at") or 0),
            )
        if turn_id:
            state["reply_queue"].remove(item)
            state["handled_message_ids"][request_id] = int(time.time())
            logger.info(
                "Recovered submitted WeChat reply thread=%s request=%s turn=%s",
                thread_id,
                request_id,
                turn_id,
            )
            recovered += 1
        else:
            item["status"] = "queued"
            logger.info(
                "Requeued interrupted WeChat reply thread=%s request=%s",
                thread_id,
                request_id,
            )
            requeued += 1
    state["handled_message_ids"] = trim_timestamp_dict(
        state["handled_message_ids"],
        int(config.get("handled_message_history_limit", 2000)),
    )
    return recovered, requeued


def read_desktop_global_state(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read Codex Desktop's global sidebar state, falling back to its backup."""
    configured = config.get("codex_global_state")
    if configured:
        state_path = Path(str(configured))
    else:
        state_path = Path(str(config["codex_db"])).with_name(".codex-global-state.json")
    candidates = (state_path, state_path.with_suffix(state_path.suffix + ".bak"))
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def read_sidebar_pin_state(
    config: dict[str, Any],
) -> tuple[list[str] | None, set[str]]:
    """Return exact pinned order and threads belonging to pinned projects."""
    data = read_desktop_global_state(config)
    if data is None:
        return None, set()

    raw_ids = data.get("pinned-thread-ids")
    pinned_ids: list[str] | None = None
    if isinstance(raw_ids, list):
        seen: set[str] = set()
        pinned_ids = []
        for raw_id in raw_ids:
            thread_id = str(raw_id).strip()
            if thread_id and thread_id not in seen:
                seen.add(thread_id)
                pinned_ids.append(thread_id)

    raw_project_ids = data.get("pinned-project-ids")
    pinned_project_ids = (
        {
            str(project_id).strip()
            for project_id in raw_project_ids
            if str(project_id).strip()
        }
        if isinstance(raw_project_ids, list)
        else set()
    )
    assignments = data.get("thread-project-assignments")
    project_thread_ids: set[str] = set()
    if pinned_project_ids and isinstance(assignments, dict):
        for raw_thread_id, assignment in assignments.items():
            if isinstance(assignment, dict):
                project_id = str(assignment.get("projectId") or "").strip()
            else:
                project_id = str(assignment or "").strip()
            thread_id = str(raw_thread_id).strip()
            if thread_id and project_id in pinned_project_ids:
                project_thread_ids.add(thread_id)
    return pinned_ids, project_thread_ids


def read_pinned_thread_ids(config: dict[str, Any]) -> list[str] | None:
    """Read the Codex Desktop sidebar's authoritative pinned task order."""
    pinned_ids, _ = read_sidebar_pin_state(config)
    return pinned_ids


def thread_push_is_enabled(thread: dict[str, Any], state: dict[str, Any]) -> bool:
    if bool(thread.get("is_pinned")):
        return True
    return bool(state.get("pinned_project_push_enabled", False)) and bool(
        thread.get("is_project_pinned")
    )


def read_catalog_titles(config: dict[str, Any], thread_ids: list[str]) -> dict[str, str]:
    """Read the titles shown by the current Codex Desktop sidebar catalog."""
    if not thread_ids:
        return {}
    configured = config.get("codex_catalog_db")
    if configured:
        catalog_path = Path(str(configured))
    else:
        catalog_path = Path(str(config["codex_db"])).with_name("sqlite") / "codex-dev.db"
    placeholders = ",".join("?" for _ in thread_ids)
    sql = f"""
        SELECT thread_id, display_title
        FROM local_thread_catalog
        WHERE host_id = 'local'
          AND thread_id IN ({placeholders})
    """
    try:
        db = sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = db.execute(sql, thread_ids).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    return {
        str(thread_id): str(display_title).strip()
        for thread_id, display_title in rows
        if str(display_title or "").strip()
    }


def format_pinned_task_status(
    config: dict[str, Any],
    state: dict[str, Any],
    active_sessions: dict[str, dict[str, Any]] | None = None,
) -> str:
    threads = read_desktop_threads(config)
    pinned = [thread for thread in threads if bool(thread.get("is_pinned"))]
    project_count = sum(bool(thread.get("is_project_pinned")) for thread in threads)
    pinned_push = bool(state.get("push_enabled", True))
    project_push = bool(state.get("pinned_project_push_enabled", False))
    pinned_line = f"置顶任务回复推送：{'已开启' if pinned_push else '已关闭'}"
    project_line = (
        f"置顶文件夹任务回复推送：{'已开启' if project_push else '已关闭'}"
        + (f"（当前 {project_count} 个对话）" if project_push else "")
    )
    if not pinned:
        return f"{pinned_line}\n{project_line}\n\n当前没有单独置顶任务。"
    active_sessions = active_sessions or {}
    now = time.time()
    automation_runtimes: dict[str, dict[str, Any]] = {}
    for run in read_pinned_automation_runs(config):
        runtime = latest_thread_runtime(str(run.get("rollout_path") or ""))
        if not runtime.get("active"):
            continue
        target_id = str(run.get("route_thread_id") or "")
        current = automation_runtimes.get(target_id)
        if current is None or float(runtime.get("started_at") or now) < float(
            current.get("started_at") or now
        ):
            automation_runtimes[target_id] = runtime
    lines = [pinned_line, project_line, "", f"置顶任务（{len(pinned)}）"]
    for index, thread in enumerate(pinned, 1):
        thread_id = str(thread["id"])
        session = active_sessions.get(thread_id)
        runtime = automation_runtimes.get(thread_id) or latest_thread_runtime(
            str(thread["rollout_path"])
        )
        if session is not None:
            runtime = {
                "active": True,
                "started_at": float(session.get("started_at") or now),
            }
        title = clean_chat_title(str(thread.get("title") or "未命名任务"))
        if runtime.get("active"):
            elapsed = format_duration(now - float(runtime.get("started_at") or now))
            status_text = f"运行中｜已处理 {elapsed}"
        else:
            status_text = "空闲"
        queued = sum(
            1
            for item in state.get("reply_queue", [])
            if str(item.get("thread_id")) == thread_id
            and item.get("status") in {"queued", "desktop_queued", "native_queuing"}
        )
        if queued:
            status_text += f"｜排队 {queued}"
        lines.append(f"{index}. 【{title}】{status_text}")
    return "\n".join(lines)


def baseline_state(state: dict[str, Any], threads: list[dict[str, Any]]) -> None:
    offsets: dict[str, Any] = {}
    for thread in threads:
        rollout = normalized_path(thread["rollout_path"])
        try:
            size = rollout.stat().st_size
        except OSError:
            continue
        offsets[thread["id"]] = {"path": str(rollout), "offset": size}
    state["offsets"] = offsets
    state["initialized"] = True
    state["initialized_at"] = int(time.time())


def scan_rollout(path: Path, offset: int) -> tuple[int, list[dict[str, str]]]:
    events: list[dict[str, str]] = []
    try:
        size = path.stat().st_size
    except OSError:
        return offset, events
    if size < offset:
        offset = 0

    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                return handle.tell(), events
            if not line.endswith(b"\n"):
                return line_start, events
            offset = handle.tell()
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = item.get("payload") or {}
            if item.get("type") != "event_msg" or payload.get("type") != "task_complete":
                continue
            answer = payload.get("last_agent_message")
            if not isinstance(answer, str) or not answer.strip():
                continue
            turn_id = payload.get("turn_id")
            if not turn_id:
                seed = f"{payload.get('completed_at', '')}\n{answer}".encode("utf-8")
                turn_id = hashlib.sha256(seed).hexdigest()
            events.append({"turn_id": str(turn_id), "answer": answer.strip()})


def pending_turn_ids(state: dict[str, Any]) -> set[str]:
    return {str(item.get("turn_id")) for item in state["pending"]}


def poll_threads(
    config: dict[str, Any], state: dict[str, Any], logger: logging.Logger
) -> int:
    threads = read_monitored_threads(config, state)
    if not bool(state.get("automation_runs_initialized", False)):
        for thread in threads:
            if not (
                bool(thread.get("is_automation_run"))
                or bool(thread.get("is_automation_target"))
            ):
                continue
            rollout = normalized_path(str(thread["rollout_path"]))
            try:
                size = rollout.stat().st_size
            except OSError:
                continue
            state["offsets"][str(thread["id"])] = {
                "path": str(rollout),
                "offset": size,
            }
        state["automation_runs_initialized"] = True
    known_pending = pending_turn_ids(state)
    sent = state["sent_turns"]
    added = 0
    live_thread_ids: set[str] = set()

    for thread in threads:
        thread_id = str(thread["id"])
        live_thread_ids.add(thread_id)
        rollout = normalized_path(thread["rollout_path"])
        saved = state["offsets"].get(thread_id)
        if saved is None:
            offset = 0
        elif saved.get("path") != str(rollout) and not bool(
            thread.get("is_automation_run")
        ):
            try:
                offset = rollout.stat().st_size
            except OSError:
                continue
            logger.info("Rollout path changed; re-baselined thread=%s", thread_id)
        else:
            offset = int(saved.get("offset", 0))

        try:
            new_offset, events = scan_rollout(rollout, offset)
        except OSError as exc:
            logger.warning("Could not scan rollout thread=%s error=%s", thread_id, exc)
            continue
        state["offsets"][thread_id] = {"path": str(rollout), "offset": new_offset}

        if not bool(state.get("push_enabled", True)):
            continue
        if bool(thread.get("is_automation_run")):
            if not bool(thread.get("is_pinned")):
                continue
        elif not thread_push_is_enabled(thread, state):
            continue
        route_thread_id = str(thread.get("route_thread_id") or thread_id)
        title = str(thread.get("title") or "未命名任务").strip()
        for event in events:
            turn_id = event["turn_id"]
            if turn_id in sent or turn_id in known_pending:
                continue
            state["pending"].append(
                {
                    "turn_id": turn_id,
                    "thread_id": route_thread_id,
                    "source_thread_id": thread_id,
                    "title": title,
                    "answer": event["answer"],
                    "next_chunk": 0,
                    "attempts": 0,
                    "next_retry_at": 0,
                    "queued_at": int(time.time()),
                    "pin_source": (
                        "pinned_automation"
                        if bool(thread.get("is_automation_run"))
                        else (
                            "pinned_thread"
                            if bool(thread.get("is_pinned"))
                            else "pinned_project"
                        )
                    ),
                }
            )
            known_pending.add(turn_id)
            added += 1
            logger.info("Queued completed pinned turn thread=%s turn=%s", thread_id, turn_id)

    # Keep offsets for monitored source threads plus pending source threads.
    pending_threads = {
        str(item.get("source_thread_id") or item.get("thread_id"))
        for item in state["pending"]
    }
    keep = live_thread_ids | pending_threads
    state["offsets"] = {
        thread_id: value
        for thread_id, value in state["offsets"].items()
        if thread_id in keep
    }
    return added


def clean_chat_title(title: str) -> str:
    return " ".join(title.replace("】", " ").split())[:80] or "未命名任务"


def format_notification(title: str, body: str, part: str = "") -> str:
    clean_title = clean_chat_title(title)
    part_line = f"{part}\n" if part else ""
    spacing = f"\n{WECHAT_BLANK_LINE}\n"
    footer = f"（{QUOTE_FOOTER}。{QUEUE_HINT}）"
    return f"【{clean_title}】{spacing}{part_line}{body}{spacing}{footer}"


def split_answer(title: str, answer: str, max_chars: int) -> list[str]:
    clean_title = clean_chat_title(title)
    reserve = len(format_notification(clean_title, "", "（999/999）")) + 16
    body_limit = max(500, max_chars - reserve)
    if len(answer) <= body_limit:
        return [format_notification(clean_title, answer)]

    bodies: list[str] = []
    remaining = answer
    while remaining:
        if len(remaining) <= body_limit:
            bodies.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, body_limit)
        if split_at < body_limit // 2:
            split_at = body_limit
        bodies.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    total = len(bodies)
    return [
        format_notification(clean_title, body, f"（{index}/{total}）")
        for index, body in enumerate(bodies, 1)
    ]


def normalize_quote_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(line.rstrip() for line in lines).strip()
    return normalized


def canonical_quote_text(text: str) -> str:
    """Strip transport-added sender prefixes from a quoted Codex notification."""
    normalized = normalize_quote_text(text)
    footer_index = normalized.find(QUOTE_FOOTER)
    if footer_index < 0:
        return normalized
    title_start = normalized.find("【", 0, footer_index)
    if title_start < 0:
        return normalized
    return normalized[title_start:]


def quote_fingerprint(text: str) -> str:
    return hashlib.sha256(canonical_quote_text(text).encode("utf-8")).hexdigest()


def quote_chat_title(text: str) -> str:
    normalized = normalize_quote_text(text)
    title_start = normalized.find("【")
    if title_start < 0:
        return ""
    title_end = normalized.find("】", title_start + 1)
    if title_end <= title_start + 1:
        return ""
    return clean_chat_title(normalized[title_start + 1 : title_end])


def remember_quote_route(
    state: dict[str, Any], item: dict[str, Any], message: str, limit: int
) -> None:
    fingerprint = quote_fingerprint(message)
    state["quote_routes"] = [
        route for route in state["quote_routes"] if route.get("fingerprint") != fingerprint
    ]
    state["quote_routes"].append(
        {
            "fingerprint": fingerprint,
            "thread_id": str(item["thread_id"]),
            "turn_id": str(item["turn_id"]),
            "title": clean_chat_title(str(item["title"])),
            "sent_at": int(time.time()),
            "sent_at_ms": int(time.time() * 1000),
        }
    )
    if len(state["quote_routes"]) > limit:
        state["quote_routes"] = state["quote_routes"][-limit:]


def find_quote_route(
    state: dict[str, Any],
    quote_text: str = "",
    referenced_message_id: str = "",
    referenced_create_time_ms: int = 0,
) -> dict[str, Any] | None:
    routes = state.get("quote_routes", [])
    if referenced_message_id:
        for route in reversed(routes):
            if hmac.compare_digest(
                str(route.get("wechat_message_id") or ""), referenced_message_id
            ):
                return route
    if quote_text:
        fingerprint = quote_fingerprint(quote_text)
        for route in reversed(routes):
            if hmac.compare_digest(str(route.get("fingerprint", "")), fingerprint):
                return route
    if referenced_create_time_ms > 0:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for route in routes:
            sent_at_ms = int(
                route.get("sent_at_ms") or int(route.get("sent_at") or 0) * 1000
            )
            distance = abs(sent_at_ms - referenced_create_time_ms)
            if sent_at_ms > 0 and distance <= 30_000:
                candidates.append((distance, route))
        if candidates:
            nearest_distance = min(distance for distance, _ in candidates)
            nearest = [route for distance, route in candidates if distance == nearest_distance]
            thread_ids = {str(route.get("thread_id") or "") for route in nearest}
            if len(thread_ids) == 1:
                return nearest[-1]
    if quote_text:
        title = quote_chat_title(quote_text)
        if title:
            matches = [
                route
                for route in routes
                if clean_chat_title(str(route.get("title") or "")) == title
            ]
            thread_ids = {str(route.get("thread_id") or "") for route in matches}
            if len(thread_ids) == 1:
                return matches[-1]
    return None


def send_via_cc_connect(
    config: dict[str, Any], message: str, session_key: str = ""
) -> tuple[bool, str]:
    command = [
        str(config["cc_connect"]),
        "send",
        "--stdin",
        "--project",
        str(config["cc_project"]),
    ]
    if session_key:
        command.extend(["--session", session_key])
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            input=message,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=float(config["send_timeout_seconds"]),
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stderr or result.stdout or "").strip().replace("\r", " ").replace("\n", " ")
    return result.returncode == 0, detail[-500:]


class AppServerClient:
    def __init__(
        self,
        codex_cli: str,
        timeout_seconds: float,
        request_timeout_seconds: float = 30.0,
        websocket_url: str | None = None,
    ):
        self.process: subprocess.Popen[str] | None = None
        self.websocket: WebSocketConnection | None = None
        if websocket_url:
            self.websocket = WebSocketConnection(
                websocket_url, timeout_seconds=request_timeout_seconds
            )
        else:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                [codex_cli, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        self.timeout_seconds = timeout_seconds
        self.request_timeout_seconds = min(timeout_seconds, request_timeout_seconds)
        self.notifications: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.response_queues: dict[int, queue.Queue[dict[str, Any] | None]] = {}
        self.response_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.id_lock = threading.Lock()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.next_id = 1

    def _read_stdout(self) -> None:
        if self.websocket is not None:
            lines = self.websocket.iter_text()
        else:
            assert self.process is not None and self.process.stdout is not None
            lines = self.process.stdout
        try:
            for line in lines:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self.send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32603,
                                "message": "Interactive requests are disabled for WeChat quote routing",
                            },
                        }
                    )
                    continue
                response_id = message.get("id")
                if isinstance(response_id, int):
                    with self.response_lock:
                        response_queue = self.response_queues.get(response_id)
                    if response_queue is not None:
                        response_queue.put(message)
                        continue
                self.notifications.put(message)
        finally:
            with self.response_lock:
                queues = list(self.response_queues.values())
            for response_queue in queues:
                response_queue.put(None)
            self.notifications.put(None)

    def send(self, message: dict[str, Any]) -> None:
        if self.websocket is not None:
            self.websocket.send_json(message)
            return
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        with self.send_lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.id_lock:
            request_id = self.next_id
            self.next_id += 1
        response_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self.response_lock:
            self.response_queues[request_id] = response_queue
        try:
            self.send({"method": method, "id": request_id, "params": params})
            try:
                message = response_queue.get(timeout=self.request_timeout_seconds)
            except queue.Empty as exc:
                raise TimeoutError(
                    f"Codex app-server request timed out method={method} "
                    f"after {self.request_timeout_seconds:g}s"
                ) from exc
            if message is None:
                raise RuntimeError(self.connection_closed_detail())
            if "error" in message:
                error = message.get("error") or {}
                raise RuntimeError(str(error.get("message") or error))
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self.response_lock:
                self.response_queues.pop(request_id, None)

    def wait_for_turn(self, thread_id: str, turn_id: str) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server timed out")
            try:
                message = self.notifications.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex app-server timed out") from exc
            if message is None:
                raise RuntimeError(self.connection_closed_detail())
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params") or {}
            turn = params.get("turn") or {}
            if str(params.get("threadId") or turn.get("threadId") or thread_id) != thread_id:
                continue
            if str(turn.get("id")) != turn_id:
                continue
            status = str(turn.get("status") or "")
            if status == "completed":
                return status
            error = turn.get("error") or {}
            raise RuntimeError(str(error.get("message") or status or "turn failed"))

    def close(self) -> None:
        if self.websocket is not None:
            self.websocket.close()
            return
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def connection_closed_detail(self) -> str:
        if self.websocket is not None:
            return "Codex app-server WebSocket connection closed"
        exit_code = self.process.poll() if self.process is not None else None
        return f"Codex app-server exited with code {exit_code}"


def run_codex_reply(
    config: dict[str, Any],
    thread_id: str,
    reply: str,
    active_sessions: dict[str, dict[str, Any]] | None = None,
    active_sessions_lock: threading.RLock | None = None,
) -> tuple[bool, str]:
    if config.get("codex_submit_transport") == "desktop-cdp":
        thread = read_desktop_thread(config, thread_id)
        rollout_path = str((thread or {}).get("rollout_path") or "")
        previous_turn_id = str(latest_thread_runtime(rollout_path).get("turn_id") or "")
        try:
            client = DesktopCdpClient(
                str(config["codex_desktop_cdp_url"]),
                float(config["codex_desktop_cdp_timeout_seconds"]),
            )
            client.send_follow_up(
                thread_id,
                reply,
                model=str((thread or {}).get("model") or "") or None,
                reasoning_effort=(
                    str((thread or {}).get("reasoning_effort") or "") or None
                ),
            )
            turn_id = wait_for_desktop_turn_completion(
                rollout_path,
                previous_turn_id,
                float(config["codex_turn_timeout_seconds"]),
                max(0.25, min(1.0, float(config["poll_seconds"]) / 3)),
            )
            return True, turn_id
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            return False, str(exc)

    client: AppServerClient | None = None
    registered_turn_id = ""
    try:
        client = AppServerClient(
            str(config["codex_cli"]),
            float(config["codex_turn_timeout_seconds"]),
            float(config["codex_app_server_request_timeout_seconds"]),
            (
                str(config["codex_app_server_ws_url"])
                if config["codex_app_server_transport"]
                == "desktop-shared-websocket"
                else None
            ),
        )
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "cc-connect-codex-notifier",
                    "title": "WeChat Quote Router",
                    "version": "1.0.0",
                }
            },
        )
        client.send({"method": "initialized", "params": {}})
        client.request("thread/resume", {"threadId": thread_id})
        result = client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": reply}],
                "approvalPolicy": "never",
            },
        )
        turn = result.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise RuntimeError("Codex app-server did not return a turn id")
        if active_sessions is not None and active_sessions_lock is not None:
            with active_sessions_lock:
                active_sessions[thread_id] = {
                    "client": client,
                    "turn_id": turn_id,
                    "started_at": time.time(),
                }
            registered_turn_id = turn_id
        client.wait_for_turn(thread_id, turn_id)
        return True, turn_id
    except (OSError, RuntimeError, TimeoutError) as exc:
        return False, str(exc)
    finally:
        if (
            registered_turn_id
            and active_sessions is not None
            and active_sessions_lock is not None
        ):
            with active_sessions_lock:
                session = active_sessions.get(thread_id)
                if session is not None and session.get("turn_id") == registered_turn_id:
                    active_sessions.pop(thread_id, None)
        if client is not None:
            client.close()


def wait_for_desktop_turn_completion(
    rollout_path: str,
    previous_turn_id: str,
    timeout_seconds: float,
    poll_seconds: float = 0.5,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    observed_turn_id = ""
    while time.monotonic() < deadline:
        runtime = latest_thread_runtime(rollout_path)
        turn_id = str(runtime.get("turn_id") or "")
        if turn_id and turn_id != previous_turn_id:
            observed_turn_id = turn_id
            if not runtime.get("active"):
                return observed_turn_id
        time.sleep(poll_seconds)
    if observed_turn_id:
        raise TimeoutError(
            f"Codex Desktop turn timed out turn={observed_turn_id}"
        )
    raise TimeoutError("Codex Desktop did not start a new turn")


def steer_active_reply(
    thread_id: str,
    reply: str,
    active_sessions: dict[str, dict[str, Any]],
    active_sessions_lock: threading.RLock,
) -> tuple[bool, str]:
    with active_sessions_lock:
        session = active_sessions.get(thread_id)
        if session is None:
            return False, "no active WeChat-owned turn"
        client = session.get("client")
        turn_id = str(session.get("turn_id") or "")
    if client is None or not hasattr(client, "request") or not turn_id:
        return False, "invalid active turn"
    try:
        client.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": reply}],
                "expectedTurnId": turn_id,
            },
        )
        return True, turn_id
    except (OSError, RuntimeError, TimeoutError) as exc:
        return False, str(exc)


def submit_desktop_reply(
    config: dict[str, Any], thread_id: str, reply: str
) -> tuple[bool, str]:
    if config.get("codex_submit_transport") != "desktop-cdp":
        return False, "Codex Desktop submit transport is disabled"
    try:
        thread = read_desktop_thread(config, thread_id)
        if thread is None:
            return False, "target Codex Desktop task was not found"
        client = DesktopCdpClient(
            str(config["codex_desktop_cdp_url"]),
            float(config["codex_desktop_cdp_timeout_seconds"]),
        )
        result = client.send_follow_up(
            thread_id,
            reply,
            model=str(thread.get("model") or "") or None,
            reasoning_effort=str(thread.get("reasoning_effort") or "") or None,
        )
        return True, str(result.get("requestExport") or "desktop-cdp")
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return False, str(exc)


def read_desktop_queued_follow_up_count(
    config: dict[str, Any], thread_id: str
) -> int:
    base_url = str(config.get("codex_desktop_cdp_url") or "")
    if not base_url:
        return 0
    try:
        client = DesktopCdpClient(
            base_url,
            float(config.get("codex_desktop_cdp_timeout_seconds", 30)),
        )
        return client.get_queued_follow_up_count(thread_id)
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        logging.getLogger("codex_pinned_wechat_notifier").warning(
            "Could not read Codex Desktop queued follow-up count thread=%s: %s",
            thread_id,
            exc,
        )
        return 0


def read_desktop_queued_follow_up_ids(
    config: dict[str, Any], thread_id: str
) -> set[str] | None:
    base_url = str(config.get("codex_desktop_cdp_url") or "")
    if not base_url:
        return None
    try:
        client = DesktopCdpClient(
            base_url,
            float(config.get("codex_desktop_cdp_timeout_seconds", 30)),
        )
        return set(client.get_queued_follow_up_ids(thread_id))
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        logging.getLogger("codex_pinned_wechat_notifier").warning(
            "Could not read Codex Desktop queued follow-up IDs thread=%s: %s",
            thread_id,
            exc,
        )
        return None


def read_desktop_queued_follow_ups(
    config: dict[str, Any], thread_id: str
) -> list[dict[str, Any]] | None:
    base_url = str(config.get("codex_desktop_cdp_url") or "")
    if not base_url:
        return None
    try:
        client = DesktopCdpClient(
            base_url,
            float(config.get("codex_desktop_cdp_timeout_seconds", 30)),
        )
        return client.get_queued_follow_ups(thread_id)
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        logging.getLogger("codex_pinned_wechat_notifier").warning(
            "Could not read Codex Desktop queued follow-ups thread=%s: %s",
            thread_id,
            exc,
        )
        return None


def enqueue_desktop_queued_follow_up(
    config: dict[str, Any],
    thread: dict[str, Any],
    reply: str,
    native_message_id: str,
    created_at_ms: int,
) -> tuple[bool, int, str, list[dict[str, Any]]]:
    base_url = str(config.get("codex_desktop_cdp_url") or "")
    cwd = str(thread.get("cwd") or "")
    if not base_url:
        return False, 0, "Codex Desktop queued follow-up transport is unavailable", []
    if not cwd:
        return False, 0, "target Codex Desktop task has no working directory", []
    try:
        client = DesktopCdpClient(
            base_url,
            float(config.get("codex_desktop_cdp_timeout_seconds", 30)),
        )
        result = client.enqueue_queued_follow_up(
            str(thread["id"]),
            reply,
            str(normalized_path(cwd)),
            native_message_id,
            created_at_ms,
        )
        count = result.get("queuedCount")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise RuntimeError("Codex Desktop returned an invalid queued count")
        queued_items = result.get("queuedItems")
        if not isinstance(queued_items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("createdAt"), (int, float))
            and not isinstance(item.get("createdAt"), bool)
            for item in queued_items
        ):
            raise RuntimeError("Codex Desktop returned invalid queued follow-ups")
        return (
            True,
            count,
            str(result.get("queuedMessageId") or native_message_id),
            [
                {
                    "id": str(item["id"]),
                    "text": str(item["text"]),
                    "createdAt": int(item["createdAt"]),
                }
                for item in queued_items
            ],
        )
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        return False, 0, str(exc), []


def remove_desktop_queued_follow_up(
    config: dict[str, Any], thread_id: str, native_message_id: str
) -> tuple[bool, bool, str]:
    base_url = str(config.get("codex_desktop_cdp_url") or "")
    if not base_url:
        return False, False, "Codex Desktop queued follow-up transport is unavailable"
    try:
        client = DesktopCdpClient(
            base_url,
            float(config.get("codex_desktop_cdp_timeout_seconds", 30)),
        )
        result = client.remove_queued_follow_up(thread_id, native_message_id)
        removed = result.get("removed")
        if not isinstance(removed, bool):
            raise RuntimeError("Codex Desktop returned an invalid removal result")
        return True, removed, "desktop-cdp"
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        return False, False, str(exc)


def format_queue_acknowledgement(
    title: str,
    ahead: int,
    queue_items: list[dict[str, Any]],
    max_chars: int,
) -> str:
    header = (
        f"收到，已提交【{title}】，排队中（前方{ahead}条）。\n"
        f'引用这条提示回复"/y"直接提交本条消息。\n{WECHAT_BLANK_LINE}\n'
        "当前队列："
    )
    if not queue_items:
        return header + "\n（暂时无法读取队列内容）"

    indexed = list(enumerate(queue_items))
    indexed.sort(
        key=lambda pair: (
            int(pair[1].get("createdAt") or 0) <= 0,
            int(pair[1].get("createdAt") or 0) or pair[0],
            pair[0],
        )
    )
    visible = indexed[:20]
    hidden_count = max(0, len(indexed) - len(visible))
    suffix = f"\n（另有{hidden_count}条未显示）" if hidden_count else ""
    available = max(0, max_chars - len(header) - len(suffix) - len(visible))
    per_item_limit = max(16, min(300, available // max(1, len(visible)) - 8))
    lines: list[str] = []
    for display_index, (_, item) in enumerate(visible, 1):
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            text = "（空消息）"
        if len(text) > per_item_limit:
            text = text[: max(1, per_item_limit - 1)].rstrip() + "…"
        lines.append(f"{display_index}.{text}")
    return header + "\n" + "\n".join(lines) + suffix


def trim_sent_history(state: dict[str, Any], limit: int) -> None:
    sent = state["sent_turns"]
    if len(sent) <= limit:
        return
    newest = sorted(sent.items(), key=lambda pair: pair[1], reverse=True)[:limit]
    state["sent_turns"] = dict(newest)


def trim_timestamp_dict(values: dict[str, Any], limit: int) -> dict[str, Any]:
    if len(values) <= limit:
        return values
    newest = sorted(values.items(), key=lambda pair: int(pair[1]), reverse=True)[:limit]
    return dict(newest)


def remember_queue_ack_route(
    state: dict[str, Any],
    request_id: str,
    title: str,
    message: str,
    limit: int,
) -> None:
    routes = state["queue_ack_routes"]
    routes.append(
        {
            "request_id": request_id,
            "title": title,
            "fingerprint": quote_fingerprint(message),
            "sent_at_ms": int(time.time() * 1000),
        }
    )
    if len(routes) > limit:
        del routes[: len(routes) - limit]


def find_queue_ack_route(
    state: dict[str, Any],
    quote_text: str,
    referenced_create_time_ms: int = 0,
) -> dict[str, Any] | None:
    fingerprint = quote_fingerprint(quote_text) if quote_text else ""
    title = quote_chat_title(quote_text)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for route in reversed(state.get("queue_ack_routes", [])):
        if fingerprint and route.get("fingerprint") == fingerprint:
            return route
        if title and clean_chat_title(str(route.get("title") or "")) != title:
            continue
        if referenced_create_time_ms > 0:
            distance = abs(
                int(route.get("sent_at_ms") or 0) - referenced_create_time_ms
            )
            if distance <= 120_000:
                candidates.append((distance, route))
    if candidates:
        return min(candidates, key=lambda pair: pair[0])[1]
    return None


def promote_queued_reply(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    quote_text: str,
    request_id: str,
    referenced_create_time_ms: int,
) -> tuple[int, str]:
    with state_lock:
        if request_id in state["handled_message_ids"]:
            return 200, "收到"
        route = find_queue_ack_route(
            state, quote_text, referenced_create_time_ms
        )
        if route is None:
            return 404, "无法识别这条排队确认；请引用带有 /y 提示的完整消息。"
        queued_request_id = str(route.get("request_id") or "")
        item = next(
            (
                entry
                for entry in state["reply_queue"]
                if entry.get("request_id") == queued_request_id
            ),
            None,
        )
        if item is None:
            state["handled_message_ids"][request_id] = int(time.time())
            save_state(state_path, state)
            return 200, "这条排队消息已提交或处理完成。"
        title = clean_chat_title(str(item.get("title") or route.get("title") or "未命名任务"))
        if item.get("status") in {"running", "promoting"}:
            return 200, f"【{title}】这条消息已在提交中。"
        native_queued = item.get("status") in {
            "desktop_queued",
            "native_queuing",
        }
        item["status"] = "promoting"
        save_state(state_path, state)
        thread_id = str(item.get("thread_id") or "")
        reply = str(item.get("reply") or "")
        native_message_id = str(item.get("native_message_id") or "")
        created_at_ms = int(item.get("queued_at_ms") or 0)

    if native_queued:
        remove_ok, removed, remove_detail = remove_desktop_queued_follow_up(
            config, thread_id, native_message_id
        )
        if not remove_ok:
            with state_lock:
                item = next(
                    (
                        entry
                        for entry in state["reply_queue"]
                        if entry.get("request_id") == queued_request_id
                    ),
                    None,
                )
                if item is not None:
                    item["status"] = "desktop_queued"
                save_state(state_path, state)
            return 200, f"【{title}】暂时无法从 Codex 排队队列取出：{remove_detail[-200:]}"
        if not removed:
            with state_lock:
                item = next(
                    (
                        entry
                        for entry in state["reply_queue"]
                        if entry.get("request_id") == queued_request_id
                    ),
                    None,
                )
                if item is not None:
                    state["reply_queue"].remove(item)
                state["handled_message_ids"][queued_request_id] = int(time.time())
                state["handled_message_ids"][request_id] = int(time.time())
                state["handled_message_ids"] = trim_timestamp_dict(
                    state["handled_message_ids"],
                    int(config.get("handled_message_history_limit", 2000)),
                )
                save_state(state_path, state)
            return 200, "这条排队消息已提交、处理完成或已从 Codex 队列删除。"

    ok, _ = submit_desktop_reply(config, thread_id, reply)
    native_requeued = False
    native_detail = ""
    if not ok and native_queued:
        thread = read_desktop_thread(config, thread_id)
        if thread is not None:
            native_requeued, _, native_detail, _ = enqueue_desktop_queued_follow_up(
                config,
                thread,
                reply,
                native_message_id,
                created_at_ms or int(time.time() * 1000),
            )
    with state_lock:
        item = next(
            (
                entry
                for entry in state["reply_queue"]
                if entry.get("request_id") == queued_request_id
            ),
            None,
        )
        if ok:
            if item is not None:
                state["reply_queue"].remove(item)
            state["handled_message_ids"][queued_request_id] = int(time.time())
            state["handled_message_ids"][request_id] = int(time.time())
            state["handled_message_ids"] = trim_timestamp_dict(
                state["handled_message_ids"],
                int(config.get("handled_message_history_limit", 2000)),
            )
            save_state(state_path, state)
            return 200, f"收到，已直接提交给【{title}】。"
        if item is not None:
            item["status"] = "desktop_queued" if native_requeued else "queued"
            item["mode"] = "queue" if native_requeued else "direct"
            item["next_retry_at"] = 0
            if native_detail and not native_requeued:
                item["native_queue_error"] = native_detail[-500:]
        save_state(state_path, state)
    if native_requeued:
        return 200, f"【{title}】直接提交未成功，已重新加入 Codex 排队队列。"
    return 200, f"【{title}】直接提交未成功，已优先排队。"


def remember_wechat_session(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id or len(user_id) > 500 or any(char in user_id for char in "\r\n\x00"):
        return False
    session_key = f"weixin:dm:{user_id}"
    if state.get("wechat_session_key") == session_key:
        return False
    state["wechat_session_key"] = session_key
    return True


def parse_reply_mode(reply_text: str) -> tuple[str, str]:
    text = reply_text.strip()
    if text.lower() == "/y":
        return "direct", ""
    if len(text) > 2 and text[:2].lower() == "/y" and text[2].isspace():
        return "direct", text[2:].strip()
    return "queue", text


def enqueue_quote_reply(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    payload: dict[str, Any],
    active_sessions: dict[str, dict[str, Any]] | None = None,
    active_sessions_lock: threading.RLock | None = None,
) -> tuple[int, str]:
    quote_text = str(payload.get("quote_text") or "")
    referenced_message_id = str(payload.get("referenced_message_id") or "").strip()
    referenced_create_time_ms = int(payload.get("referenced_create_time_ms") or 0)
    raw_reply_text = str(payload.get("reply_text") or "").strip()
    mode, reply_text = parse_reply_mode(raw_reply_text)
    message_id = str(payload.get("message_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    has_reference = bool(referenced_message_id or referenced_create_time_ms > 0)
    if not has_reference and (
        not quote_text
        or (QUOTE_FOOTER not in quote_text and not quote_chat_title(quote_text))
    ):
        return 404, "这不是可续接的 Codex 通知。"
    request_id = f"{user_id}|{message_id}" if message_id else hashlib.sha256(
        f"{user_id}\n{raw_reply_text}\n{quote_fingerprint(quote_text)}".encode("utf-8")
    ).hexdigest()

    if raw_reply_text.lower() == "/z":
        return 400, '请引用这条排队提示回复"/y"直接提交本条消息。'

    if raw_reply_text.lower() == "/y":
        with state_lock:
            if remember_wechat_session(state, payload):
                save_state(state_path, state)
            is_queue_ack = find_queue_ack_route(
                state, quote_text, referenced_create_time_ms
            ) is not None
        if is_queue_ack:
            return promote_queued_reply(
                config,
                state,
                state_path,
                state_lock,
                quote_text,
                request_id,
                referenced_create_time_ms,
            )

    if not reply_text:
        if mode == "direct":
            return 400, "请在 /y 后面写上要直接提交的内容。"
        return 400, "回复内容不能为空。"
    if len(reply_text) > 20_000:
        return 400, "回复内容过长，请缩短后重试。"

    with state_lock:
        if remember_wechat_session(state, payload):
            save_state(state_path, state)
        if request_id in state["handled_message_ids"] or any(
            item.get("request_id") == request_id for item in state["reply_queue"]
        ):
            return 200, "收到"
        route = find_quote_route(
            state,
            quote_text,
            referenced_message_id,
            referenced_create_time_ms,
        )
        if route is None:
            return 404, "无法识别这条通知；请引用最近一次收到的完整 Codex 答复。"
        if referenced_message_id and not route.get("wechat_message_id"):
            route["wechat_message_id"] = referenced_message_id
            save_state(state_path, state)
        thread_id = str(route["thread_id"])

    thread = read_desktop_thread(config, thread_id)
    if (
        thread is None
        or not thread_push_is_enabled(thread, state)
        or bool(thread.get("archived"))
        or thread.get("thread_source") not in {"user", "automation"}
    ):
        return 409, "这个 Codex 任务已不在允许推送的置顶范围内或已经归档，未继续回复。"

    return enqueue_thread_reply(
        config,
        state,
        state_path,
        state_lock,
        payload,
        thread,
        mode,
        reply_text,
        request_id,
        str(route.get("title") or ""),
        active_sessions,
        active_sessions_lock,
    )


def enqueue_pinned_task_reply(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    payload: dict[str, Any],
    active_sessions: dict[str, dict[str, Any]] | None = None,
    active_sessions_lock: threading.RLock | None = None,
) -> tuple[int, str]:
    try:
        pinned_index = int(payload.get("pinned_index") or 0)
    except (TypeError, ValueError):
        pinned_index = 0
    if pinned_index <= 0:
        return 400, "编号无效，请先发送 /rw 查看当前置顶任务。"
    mode, reply_text = parse_reply_mode(str(payload.get("reply_text") or ""))
    if not reply_text:
        if mode == "direct":
            return 400, "请在 /y 后面写上要直接提交的内容。"
        return 400, "用法：/rw编号 内容"
    if len(reply_text) > 20_000:
        return 400, "回复内容过长，请缩短后重试。"
    message_id = str(payload.get("message_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    request_id = f"{user_id}|{message_id}" if message_id else hashlib.sha256(
        f"pinned:{pinned_index}\n{user_id}\n{reply_text}".encode("utf-8")
    ).hexdigest()
    with state_lock:
        if remember_wechat_session(state, payload):
            save_state(state_path, state)
        if request_id in state["handled_message_ids"] or any(
            item.get("request_id") == request_id for item in state["reply_queue"]
        ):
            return 200, "收到"
    pinned = [thread for thread in read_desktop_threads(config) if bool(thread.get("is_pinned"))]
    if pinned_index > len(pinned):
        return 404, "编号无效，请先发送 /rw 查看当前置顶任务。"
    thread = pinned[pinned_index - 1]
    return enqueue_thread_reply(
        config,
        state,
        state_path,
        state_lock,
        payload,
        thread,
        mode,
        reply_text,
        request_id,
        "",
        active_sessions,
        active_sessions_lock,
    )


def enqueue_thread_reply(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    payload: dict[str, Any],
    thread: dict[str, Any],
    mode: str,
    reply_text: str,
    request_id: str,
    fallback_title: str = "",
    active_sessions: dict[str, dict[str, Any]] | None = None,
    active_sessions_lock: threading.RLock | None = None,
) -> tuple[int, str]:
    thread_id = str(thread["id"])
    message_id = str(payload.get("message_id") or "").strip()
    title = clean_chat_title(str(thread.get("title") or fallback_title or "未命名任务"))
    active_sessions = active_sessions if active_sessions is not None else {}
    active_sessions_lock = active_sessions_lock or threading.RLock()

    if mode == "direct":
        with state_lock:
            if request_id in state["handled_message_ids"] or any(
                item.get("request_id") == request_id for item in state["reply_queue"]
            ):
                return 200, "收到"
            direct_item = {
                "request_id": request_id,
                "message_id": message_id,
                "thread_id": thread_id,
                "title": title,
                "reply": reply_text,
                "mode": "direct",
                "status": "promoting",
                "attempts": 0,
                "next_retry_at": 0,
                "queued_at": int(time.time()),
            }
            state["reply_queue"].append(direct_item)
            save_state(state_path, state)
        ok, detail = steer_active_reply(
            thread_id, reply_text, active_sessions, active_sessions_lock
        )
        if not ok:
            ok, detail = submit_desktop_reply(config, thread_id, reply_text)
        with state_lock:
            item = next(
                (
                    entry
                    for entry in state["reply_queue"]
                    if entry.get("request_id") == request_id
                ),
                None,
            )
            if ok:
                if item is not None:
                    state["reply_queue"].remove(item)
                state["handled_message_ids"][request_id] = int(time.time())
                state["handled_message_ids"] = trim_timestamp_dict(
                    state["handled_message_ids"],
                    int(config.get("handled_message_history_limit", 2000)),
                )
                save_state(state_path, state)
                return 200, f"收到，已直接提交给【{title}】。"
            if item is not None:
                item["status"] = "queued"
                item["next_retry_at"] = 0
            save_state(state_path, state)
        return 200, f"【{title}】直接提交未成功，已优先排队。"

    runtime = latest_thread_runtime(str(thread.get("rollout_path") or ""))
    queued_at = int(time.time())
    queued_at_ms = int(time.time() * 1000)
    native_message_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"cc-connect-wechat:{request_id}")
    )
    with state_lock:
        if request_id in state["handled_message_ids"] or any(
            item.get("request_id") == request_id for item in state["reply_queue"]
        ):
            return 200, "收到"
        item = {
            "request_id": request_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "title": title,
            "reply": reply_text,
            "mode": mode,
            "status": "native_queuing",
            "native_message_id": native_message_id,
            "attempts": 0,
            "next_retry_at": 0,
            "queued_at": queued_at,
            "queued_at_ms": queued_at_ms,
        }
        state["reply_queue"].append(item)
        save_state(state_path, state)

    native_ok, native_count, native_detail, native_queue_items = (
        enqueue_desktop_queued_follow_up(
            config, thread, reply_text, native_message_id, queued_at_ms
        )
    )
    desktop_ahead = 0
    if not native_ok:
        desktop_queue_items = read_desktop_queued_follow_ups(config, thread_id)
        if desktop_queue_items is None:
            desktop_ahead = read_desktop_queued_follow_up_count(config, thread_id)
        else:
            native_queue_items = desktop_queue_items
            desktop_ahead = len(desktop_queue_items)

    with state_lock:
        item = next(
            (
                entry
                for entry in state["reply_queue"]
                if entry.get("request_id") == request_id
            ),
            None,
        )
        if item is None:
            return 200, "收到"
        if native_ok:
            item["status"] = "desktop_queued"
            item["native_message_id"] = native_detail
        else:
            item["status"] = "queued"
            item["native_queue_error"] = native_detail[-500:]
        save_state(state_path, state)
        wechat_ahead = sum(
            1
            for item in state["reply_queue"]
            if str(item.get("thread_id")) == thread_id
            and item.get("status") == "queued"
            and item.get("request_id") != request_id
        )
        ahead = (
            max(0, native_count - 1) + wechat_ahead
            if native_ok
            else desktop_ahead + wechat_ahead
        )
        fallback_queue_items = [
            {
                "id": str(entry.get("native_message_id") or entry.get("request_id") or ""),
                "text": str(entry.get("reply") or ""),
                "createdAt": int(
                    entry.get("queued_at_ms")
                    or int(entry.get("queued_at") or 0) * 1000
                ),
            }
            for entry in state["reply_queue"]
            if str(entry.get("thread_id")) == thread_id
            and entry.get("status") == "queued"
        ]
    if runtime.get("active") or ahead:
        known_ids = {
            str(item.get("id") or "") for item in native_queue_items if item.get("id")
        }
        queue_items = [*native_queue_items]
        queue_items.extend(
            item
            for item in fallback_queue_items
            if not item.get("id") or str(item.get("id")) not in known_ids
        )
        response = format_queue_acknowledgement(
            title,
            ahead,
            queue_items,
            int(config.get("max_message_chars", 3400)),
        )
        with state_lock:
            remember_queue_ack_route(
                state,
                request_id,
                title,
                response,
                int(config.get("quote_route_history_limit", 2000)),
            )
            save_state(state_path, state)
        return 200, response
    return 200, f"收到，已提交【{title}】。"


def toggle_pinned_push(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    payload: dict[str, Any],
) -> tuple[int, str]:
    message_id = str(payload.get("message_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    request_id = f"push-toggle|{user_id}|{message_id}" if message_id else ""
    with state_lock:
        remember_wechat_session(state, payload)
        if request_id and request_id in state["handled_message_ids"]:
            enabled = bool(state.get("push_enabled", True))
            return 200, f"置顶任务回复推送已{'开启' if enabled else '关闭'}"
        enabled = not bool(state.get("push_enabled", True))
        state["push_enabled"] = enabled
        if not enabled:
            state["pending"].clear()
        if request_id:
            state["handled_message_ids"][request_id] = int(time.time())
            state["handled_message_ids"] = trim_timestamp_dict(
                state["handled_message_ids"],
                int(config.get("handled_message_history_limit", 2000)),
            )
        save_state(state_path, state)
    return 200, f"置顶任务回复推送已{'开启' if enabled else '关闭'}"


def toggle_pinned_project_push(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    payload: dict[str, Any],
) -> tuple[int, str]:
    message_id = str(payload.get("message_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    request_id = f"project-push-toggle|{user_id}|{message_id}" if message_id else ""
    with state_lock:
        remember_wechat_session(state, payload)
        if request_id and request_id in state["handled_message_ids"]:
            enabled = bool(state.get("pinned_project_push_enabled", False))
            return 200, f"置顶文件夹任务回复推送已{'开启' if enabled else '关闭'}"
        enabled = not bool(state.get("pinned_project_push_enabled", False))
        state["pinned_project_push_enabled"] = enabled
        if not enabled:
            state["pending"] = [
                item
                for item in state["pending"]
                if item.get("pin_source") != "pinned_project"
            ]
        if request_id:
            state["handled_message_ids"][request_id] = int(time.time())
            state["handled_message_ids"] = trim_timestamp_dict(
                state["handled_message_ids"],
                int(config.get("handled_message_history_limit", 2000)),
            )
        save_state(state_path, state)
    return 200, f"置顶文件夹任务回复推送已{'开启' if enabled else '关闭'}"


def health_status(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "version": NOTIFIER_VERSION,
        "pending_notifications": len(state.get("pending", [])),
        "queued_wechat_replies": len(state.get("reply_queue", [])),
        "push_enabled": bool(state.get("push_enabled", True)),
        "pinned_project_push_enabled": bool(
            state.get("pinned_project_push_enabled", False)
        ),
        "submit_transport": str(config.get("codex_submit_transport") or ""),
    }


def _selftest_check(
    checks: list[dict[str, Any]], name: str, action: Any
) -> None:
    try:
        detail = action()
        checks.append({"name": name, "ok": True, "detail": str(detail or "ok")})
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        checks.append({"name": name, "ok": False, "detail": str(exc)[:500]})


def _configured_quote_router_token(config: dict[str, Any]) -> str:
    config_path = Path(str(config["cc_connect_config"]))
    with config_path.open("rb") as handle:
        document = tomllib.load(handle)
    project_name = str(config.get("cc_project") or "")
    for project in document.get("projects", []):
        if not isinstance(project, dict) or str(project.get("name") or "") != project_name:
            continue
        for platform in project.get("platforms", []):
            if not isinstance(platform, dict) or platform.get("type") != "weixin":
                continue
            options = platform.get("options") or {}
            return str(options.get("codex_quote_router_token") or "")
    return ""


def selftest(config: dict[str, Any], state_path: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check_database() -> str:
        db_path = Path(str(config["codex_db"]))
        db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            db.execute("SELECT 1").fetchone()
        finally:
            db.close()
        return "readable"

    def check_state_storage() -> str:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            prefix=".notifier-selftest-", dir=state_path.parent, delete=False
        )
        temporary_path = Path(temporary.name)
        temporary.close()
        temporary_path.unlink()
        return "writable"

    def check_cc_connect() -> str:
        result = subprocess.run(
            [str(config["cc_connect"]), "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "version check failed").strip())
        first_line = (result.stdout or result.stderr or "").strip().splitlines()
        return first_line[0] if first_line else "executable"

    def check_router_token() -> str:
        notifier_token = str(config.get("router_token") or "")
        connector_token = _configured_quote_router_token(config)
        if not notifier_token or not connector_token:
            raise RuntimeError("router token is missing")
        if not hmac.compare_digest(notifier_token, connector_token):
            raise RuntimeError("router token does not match cc-connect config")
        return "matches cc-connect config"

    def check_desktop_transport() -> str:
        if config.get("codex_submit_transport") != "desktop-cdp":
            return "not configured"
        client = DesktopCdpClient(
            str(config["codex_desktop_cdp_url"]),
            float(config["codex_desktop_cdp_timeout_seconds"]),
        )
        result = client.probe()
        return str(result.get("requestExport") or "desktop-cdp ready")

    _selftest_check(checks, "codex_database", check_database)
    _selftest_check(checks, "state_storage", check_state_storage)
    _selftest_check(checks, "cc_connect", check_cc_connect)
    _selftest_check(checks, "router_token", check_router_token)
    _selftest_check(checks, "desktop_transport", check_desktop_transport)
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


class QuoteRouterHandler(BaseHTTPRequestHandler):
    server_version = "CodexQuoteRouter/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self._respond(404, {"ok": False, "message": "not found"})
            return
        router = self.server
        expected = str(router.router_token)
        supplied = self.headers.get("X-Codex-Quote-Token", "")
        if expected and not hmac.compare_digest(expected, supplied):
            self._respond(401, {"ok": False, "message": "unauthorized"})
            return
        with router.state_lock:
            payload = health_status(router.config, router.state)
        self._respond(200, payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        router = self.server
        if self.path not in {
            "/route",
            "/status",
            "/toggle",
            "/folder-toggle",
            "/task",
        }:
            self._respond(404, {"handled": False, "message": "not found"})
            return
        expected = str(router.router_token)
        supplied = self.headers.get("X-Codex-Quote-Token", "")
        if expected and not hmac.compare_digest(expected, supplied):
            self._respond(401, {"handled": False, "message": "unauthorized"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 100_000:
            self._respond(400, {"handled": False, "message": "invalid body"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"handled": False, "message": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._respond(400, {"handled": False, "message": "invalid payload"})
            return
        try:
            if self.path == "/status":
                with router.active_sessions_lock:
                    sessions = dict(router.active_sessions)
                with router.state_lock:
                    if remember_wechat_session(router.state, payload):
                        save_state(router.state_path, router.state)
                    message = format_pinned_task_status(
                        router.config, router.state, sessions
                    )
                status_code = 200
            elif self.path == "/toggle":
                status_code, message = toggle_pinned_push(
                    router.config,
                    router.state,
                    router.state_path,
                    router.state_lock,
                    payload,
                )
            elif self.path == "/folder-toggle":
                status_code, message = toggle_pinned_project_push(
                    router.config,
                    router.state,
                    router.state_path,
                    router.state_lock,
                    payload,
                )
            elif self.path == "/task":
                status_code, message = enqueue_pinned_task_reply(
                    router.config,
                    router.state,
                    router.state_path,
                    router.state_lock,
                    payload,
                    router.active_sessions,
                    router.active_sessions_lock,
                )
            else:
                status_code, message = enqueue_quote_reply(
                    router.config,
                    router.state,
                    router.state_path,
                    router.state_lock,
                    payload,
                    router.active_sessions,
                    router.active_sessions_lock,
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            router.logger.warning("Quote route enqueue failed error=%s", exc)
            status_code, message = 503, "本机 Codex 路由暂时不可用，请稍后重试。"
        self._respond(status_code, {"handled": status_code == 200, "message": message})

    def _respond(self, status_code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_quote_router(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    active_sessions: dict[str, dict[str, Any]],
    active_sessions_lock: threading.RLock,
    logger: logging.Logger,
) -> ThreadingHTTPServer | None:
    if not bool(config.get("router_enabled", True)):
        return None
    host = str(config["router_host"])
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("router_host must be loopback")
    server = ThreadingHTTPServer((host, int(config["router_port"])), QuoteRouterHandler)
    server.daemon_threads = True
    server.config = config
    server.state = state
    server.state_path = state_path
    server.state_lock = state_lock
    server.active_sessions = active_sessions
    server.active_sessions_lock = active_sessions_lock
    server.router_token = str(config.get("router_token") or "")
    server.logger = logger
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Quote router started address=%s:%s", host, config["router_port"])
    return server


def dispatch_reply_requests(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    workers: dict[str, threading.Thread],
    active_sessions: dict[str, dict[str, Any]],
    active_sessions_lock: threading.RLock,
    logger: logging.Logger,
) -> int:
    started = 0
    now = int(time.time())
    with state_lock:
        busy_threads = {
            str(item["thread_id"])
            for item in state["reply_queue"]
            if item.get("status") in {"running", "promoting", "dispatching"}
        }
        candidate_ids = [
            str(item.get("request_id") or "")
            for item in sorted(
                state["reply_queue"],
                key=lambda entry: (
                    0 if entry.get("mode") == "direct" else 1,
                    int(entry.get("queued_at", 0)),
                ),
            )
        ]

    for request_id in candidate_ids:
        with state_lock:
            item = next(
                (
                    entry
                    for entry in state["reply_queue"]
                    if str(entry.get("request_id") or "") == request_id
                ),
                None,
            )
            if item is None:
                continue
            thread_id = str(item["thread_id"])
            if item.get("status") != "queued" or int(item.get("next_retry_at", 0)) > now:
                continue
            if thread_id in busy_threads or thread_id in workers:
                continue
            item["status"] = "dispatching"
            save_state(state_path, state)
            mode = str(item.get("mode") or "queue")

        thread = read_desktop_thread(config, thread_id)
        runtime = (
            latest_thread_runtime(str(thread.get("rollout_path") or ""))
            if thread is not None
            else {}
        )
        should_start = thread is not None and (
            not runtime.get("active") or mode == "direct"
        )

        with state_lock:
            item = next(
                (
                    entry
                    for entry in state["reply_queue"]
                    if str(entry.get("request_id") or "") == request_id
                ),
                None,
            )
            if item is None or item.get("status") != "dispatching":
                continue
            if not should_start or thread_id in workers:
                item["status"] = "queued"
                save_state(state_path, state)
                continue
            item["status"] = "running"
            save_state(state_path, state)
            worker = threading.Thread(
                target=run_reply_worker,
                args=(
                    config,
                    state,
                    state_path,
                    state_lock,
                    workers,
                    active_sessions,
                    active_sessions_lock,
                    item["request_id"],
                    logger,
                ),
                daemon=True,
            )
            workers[thread_id] = worker
            busy_threads.add(thread_id)
            worker.start()
            started += 1
    return started


def reconcile_desktop_queued_replies(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    logger: logging.Logger,
) -> tuple[int, int]:
    with state_lock:
        tracked = [
            {
                "request_id": str(item.get("request_id") or ""),
                "thread_id": str(item.get("thread_id") or ""),
                "native_message_id": str(item.get("native_message_id") or ""),
                "status": str(item.get("status") or ""),
                "reply": str(item.get("reply") or ""),
                "queued_at": int(item.get("queued_at") or 0),
            }
            for item in state.get("reply_queue", [])
            if item.get("status") in {"desktop_queued", "native_queuing"}
            and item.get("native_message_id")
        ]
    if not tracked:
        return 0, 0

    queued_ids_by_thread: dict[str, set[str]] = {}
    for thread_id in {item["thread_id"] for item in tracked}:
        queued_ids = read_desktop_queued_follow_up_ids(config, thread_id)
        if queued_ids is not None:
            queued_ids_by_thread[thread_id] = queued_ids

    submitted = 0
    recovered = 0
    now = int(time.time())
    native_enqueue_grace = max(
        5,
        int(float(config.get("codex_desktop_cdp_timeout_seconds", 30)) * 2),
    )
    for snapshot in tracked:
        thread_id = snapshot["thread_id"]
        queued_ids = queued_ids_by_thread.get(thread_id)
        if queued_ids is None:
            continue
        native_message_id = snapshot["native_message_id"]
        if native_message_id in queued_ids:
            if snapshot["status"] == "native_queuing":
                with state_lock:
                    item = next(
                        (
                            entry
                            for entry in state["reply_queue"]
                            if entry.get("request_id") == snapshot["request_id"]
                        ),
                        None,
                    )
                    if item is not None and item.get("status") == "native_queuing":
                        item["status"] = "desktop_queued"
                        save_state(state_path, state)
                        recovered += 1
            continue

        turn_id = ""
        if snapshot["status"] == "native_queuing":
            queued_at = snapshot["queued_at"]
            if queued_at > 0 and now - queued_at < native_enqueue_grace:
                continue
            thread = read_desktop_thread(config, thread_id)
            if thread is not None:
                turn_id = find_submitted_reply_turn(
                    str(thread.get("rollout_path") or ""),
                    snapshot["reply"],
                    snapshot["queued_at"],
                )

        with state_lock:
            item = next(
                (
                    entry
                    for entry in state["reply_queue"]
                    if entry.get("request_id") == snapshot["request_id"]
                ),
                None,
            )
            if item is None or item.get("status") not in {
                "desktop_queued",
                "native_queuing",
            }:
                continue
            if snapshot["status"] == "native_queuing" and not turn_id:
                item["status"] = "queued"
                item["next_retry_at"] = 0
                recovered += 1
                logger.warning(
                    "Recovered unconfirmed native queue item into fallback queue "
                    "thread=%s request=%s",
                    thread_id,
                    snapshot["request_id"],
                )
            else:
                state["reply_queue"].remove(item)
                state["handled_message_ids"][snapshot["request_id"]] = int(time.time())
                state["handled_message_ids"] = trim_timestamp_dict(
                    state["handled_message_ids"],
                    int(config.get("handled_message_history_limit", 2000)),
                )
                submitted += 1
                logger.info(
                    "Native Codex queued reply left queue thread=%s request=%s turn=%s",
                    thread_id,
                    snapshot["request_id"],
                    turn_id or "desktop-managed",
                )
            save_state(state_path, state)
    return submitted, recovered


def run_reply_worker(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    workers: dict[str, threading.Thread],
    active_sessions: dict[str, dict[str, Any]],
    active_sessions_lock: threading.RLock,
    request_id: str,
    logger: logging.Logger,
) -> None:
    with state_lock:
        item = next(
            (entry for entry in state["reply_queue"] if entry.get("request_id") == request_id),
            None,
        )
        if item is None:
            return
        thread_id = str(item["thread_id"])
        reply = str(item["reply"])
        queued_at = int(item.get("queued_at") or 0)

    thread = read_desktop_thread(config, thread_id)
    if (
        thread is None
        or not thread_push_is_enabled(thread, state)
        or bool(thread.get("archived"))
    ):
        ok, detail = False, "target task is no longer in the enabled pinned scope"
    else:
        logger.info("Starting quoted WeChat reply thread=%s request=%s", thread_id, request_id)
        ok, detail = run_codex_reply(
            config,
            thread_id,
            reply,
            active_sessions,
            active_sessions_lock,
        )
        if not ok:
            refreshed_thread = read_desktop_thread(config, thread_id)
            submitted_turn_id = (
                find_submitted_reply_turn(
                    str(refreshed_thread.get("rollout_path") or ""),
                    reply,
                    queued_at,
                )
                if refreshed_thread is not None
                else ""
            )
            if submitted_turn_id:
                ok = True
                detail = submitted_turn_id
                logger.info(
                    "Recovered submitted WeChat reply after transport failure "
                    "thread=%s request=%s turn=%s",
                    thread_id,
                    request_id,
                    submitted_turn_id,
                )

    notify_error = ""
    with state_lock:
        item = next(
            (entry for entry in state["reply_queue"] if entry.get("request_id") == request_id),
            None,
        )
        if item is not None:
            if ok:
                state["reply_queue"].remove(item)
                state["handled_message_ids"][request_id] = int(time.time())
                state["handled_message_ids"] = trim_timestamp_dict(
                    state["handled_message_ids"],
                    int(config["handled_message_history_limit"]),
                )
                logger.info("Quoted WeChat reply completed thread=%s request=%s", thread_id, request_id)
            else:
                transient = reply_failure_is_transient(config, detail)
                attempt_key = "transient_attempts" if transient else "attempts"
                attempts = int(item.get(attempt_key, 0)) + 1
                item[attempt_key] = attempts
                item["status"] = "queued"
                item["next_retry_at"] = int(time.time()) + min(1800, 15 * (2 ** min(attempts - 1, 7)))
                logger.warning(
                    "Quoted WeChat reply failed thread=%s request=%s attempt=%s "
                    "transient=%s error=%s",
                    thread_id,
                    request_id,
                    attempts,
                    transient,
                    detail[-500:],
                )
                if not transient and attempts >= int(config["reply_retry_limit"]):
                    state["reply_queue"].remove(item)
                    state["handled_message_ids"][request_id] = int(time.time())
                    notify_error = f"【{clean_chat_title(str(item['title']))}】\n微信回复未能转交给 Codex：{detail[-300:]}"
            workers.pop(thread_id, None)
            save_state(state_path, state)
    if notify_error:
        with state_lock:
            session_key = str(state.get("wechat_session_key") or "")
        send_via_cc_connect(config, notify_error, session_key)


def deliver_pending(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    state_lock: threading.RLock,
    logger: logging.Logger,
) -> int:
    delivered = 0
    while True:
        now = int(time.time())
        with state_lock:
            if not bool(state.get("push_enabled", True)):
                return delivered
            item = next(
                (
                    entry
                    for entry in state["pending"]
                    if entry.get("delivery_status", "queued") == "queued"
                    and int(entry.get("next_retry_at", 0)) <= now
                ),
                None,
            )
            if item is None:
                return delivered
            item["delivery_status"] = "sending"
            claim = dict(item)
            session_key = str(state.get("wechat_session_key") or "")
            save_state(state_path, state)

        chunks = split_answer(
            str(claim["title"]), str(claim["answer"]), int(config["max_message_chars"])
        )
        next_chunk = int(claim.get("next_chunk", 0))
        failed = False
        while next_chunk < len(chunks):
            ok, detail = send_via_cc_connect(
                config,
                chunks[next_chunk],
                session_key,
            )
            with state_lock:
                item = next(
                    (
                        entry
                        for entry in state["pending"]
                        if str(entry.get("turn_id") or "") == str(claim["turn_id"])
                    ),
                    None,
                )
                if not ok:
                    if item is not None:
                        attempts = int(item.get("attempts", 0)) + 1
                        item["attempts"] = attempts
                        item["delivery_status"] = "queued"
                        item["next_retry_at"] = int(time.time()) + min(
                            3600, 30 * (2 ** min(attempts - 1, 7))
                        )
                        save_state(state_path, state)
                    logger.warning(
                        "Send failed thread=%s turn=%s chunk=%s/%s error=%s",
                        claim["thread_id"],
                        claim["turn_id"],
                        next_chunk + 1,
                        len(chunks),
                        detail or "unknown error",
                    )
                    failed = True
                else:
                    next_chunk += 1
                    route_item = item if item is not None else claim
                    remember_quote_route(
                        state,
                        route_item,
                        chunks[next_chunk - 1],
                        int(config["quote_route_history_limit"]),
                    )
                    if item is not None:
                        item["next_chunk"] = next_chunk
                        item["attempts"] = 0
                        item["next_retry_at"] = 0
                    save_state(state_path, state)
                    logger.info(
                        "Sent thread=%s turn=%s chunk=%s/%s",
                        claim["thread_id"],
                        claim["turn_id"],
                        next_chunk,
                        len(chunks),
                    )
            if failed or item is None:
                break
        if failed:
            continue
        with state_lock:
            item = next(
                (
                    entry
                    for entry in state["pending"]
                    if str(entry.get("turn_id") or "") == str(claim["turn_id"])
                ),
                None,
            )
            if item is not None:
                state["pending"].remove(item)
            state["sent_turns"][str(claim["turn_id"])] = int(time.time())
            trim_sent_history(state, int(config["sent_history_limit"]))
            save_state(state_path, state)
            delivered += 1


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def status(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    threads = read_desktop_threads(config)
    return {
        "initialized": bool(state.get("initialized")),
        "eligible_desktop_threads": len(threads),
        "pinned_desktop_threads": sum(bool(thread.get("is_pinned")) for thread in threads),
        "pinned_project_desktop_threads": sum(
            bool(thread.get("is_project_pinned")) for thread in threads
        ),
        "pinned_automation_desktop_threads": sum(
            bool(thread.get("is_pinned")) and bool(thread.get("is_automation_target"))
            for thread in threads
        ),
        "pending_notifications": len(state.get("pending", [])),
        "sent_turns_remembered": len(state.get("sent_turns", {})),
        "quote_routes_remembered": len(state.get("quote_routes", [])),
        "queued_wechat_replies": len(state.get("reply_queue", [])),
        "quote_router_enabled": bool(config.get("router_enabled", True)),
        "pinned_reply_push_enabled": bool(state.get("push_enabled", True)),
        "pinned_project_reply_push_enabled": bool(
            state.get("pinned_project_push_enabled", False)
        ),
    }


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    state_path = Path(config["state_file"])
    logger = setup_logging(Path(config["log_file"]), args.verbose)
    state = load_state(state_path)
    state_lock = threading.RLock()
    workers: dict[str, threading.Thread] = {}
    active_sessions: dict[str, dict[str, Any]] = {}
    active_sessions_lock = threading.RLock()

    if args.status:
        print(json.dumps(status(config, state), ensure_ascii=False, indent=2))
        return 0
    if args.selftest:
        result = selftest(config, state_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_handle = acquire_lock(lock_path)
    if lock_handle is None:
        logger.info("Another notifier instance is already running")
        return 0

    recovered, requeued = recover_interrupted_reply_requests(config, state, logger)
    if recovered or requeued:
        save_state(state_path, state)

    router: ThreadingHTTPServer | None = None
    shared_app_server: SharedAppServerProcess | None = None
    try:
        if config["codex_app_server_transport"] == "desktop-shared-websocket":
            shared_app_server = SharedAppServerProcess(
                str(config["codex_cli"]),
                str(config["codex_app_server_ws_url"]),
                float(config["codex_app_server_start_timeout_seconds"]),
            )
            shared_app_server.ensure_running()
            logger.info(
                "Shared Codex app-server ready url=%s",
                config["codex_app_server_ws_url"],
            )
        router = start_quote_router(
            config,
            state,
            state_path,
            state_lock,
            active_sessions,
            active_sessions_lock,
            logger,
        )
        if not state.get("initialized"):
            threads = read_monitored_threads(config, state)
            with state_lock:
                baseline_state(state, threads)
                state["automation_runs_initialized"] = True
                save_state(state_path, state)
            logger.info("Initial baseline complete threads=%s", len(threads))
            if args.once:
                return 0

        logger.info("Notifier started poll_seconds=%s", config["poll_seconds"])
        while True:
            try:
                if shared_app_server is not None and shared_app_server.ensure_running():
                    logger.warning(
                        "Shared Codex app-server restarted url=%s",
                        config["codex_app_server_ws_url"],
                    )
                with state_lock:
                    poll_threads(config, state, logger)
                    save_state(state_path, state)
                deliver_pending(config, state, state_path, state_lock, logger)
                reconcile_desktop_queued_replies(
                    config,
                    state,
                    state_path,
                    state_lock,
                    logger,
                )
                dispatch_reply_requests(
                    config,
                    state,
                    state_path,
                    state_lock,
                    workers,
                    active_sessions,
                    active_sessions_lock,
                    logger,
                )
            except (OSError, RuntimeError, TimeoutError, sqlite3.Error, ValueError) as exc:
                logger.warning("Poll failed error=%s", exc)
            if args.once:
                return 0
            time.sleep(max(1.0, float(config["poll_seconds"])))
    except KeyboardInterrupt:
        logger.info("Notifier stopped")
        return 0
    finally:
        if router is not None:
            router.shutdown()
            router.server_close()
        if shared_app_server is not None:
            shared_app_server.close()
        lock_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send completed pinned Codex Desktop turns to WeChat through cc-connect."
    )
    parser.add_argument("--config", required=True, help="Path to notifier JSON config")
    parser.add_argument("--once", action="store_true", help="Poll and deliver once, then exit")
    parser.add_argument("--status", action="store_true", help="Print read-only status and exit")
    parser.add_argument("--selftest", action="store_true", help="Run local integration checks and exit")
    parser.add_argument("--verbose", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
