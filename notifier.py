#!/usr/bin/env python3
"""Track Codex sessions and deliver one-way Feishu status notifications."""

import fcntl
import hashlib
import html
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
WORKSPACE = Path(os.environ.get("CODEX_TASK_WORKSPACE", os.getcwd())).resolve()
STATE_HOME = Path(
    os.environ.get(
        "CODEX_TASK_NOTIFY_HOME",
        str(Path.home() / ".local/share/codex-feishu-notifier"),
    )
)
CONFIG_PATH = STATE_HOME / "config.env"
SESSIONS_HOME = Path(
    os.environ.get("CODEX_TASK_SESSIONS_HOME", str(CODEX_HOME / "sessions"))
)
SHELL_SNAPSHOTS_HOME = CODEX_HOME / "shell_snapshots"
GOALS_DB_PATH = Path(
    os.environ.get("CODEX_TASK_GOALS_DB", str(CODEX_HOME / "goals_1.sqlite"))
)
DEFAULT_LARK_CLI = SCRIPT_DIR / "bin" / "lark-cli"
MAX_LIVE_SWEEP_RECORDS = 500
MAX_LIVE_SWEEP_BYTES = 512 * 1024
TERMINAL_CARD_KINDS = {"completed", "stopped", "closed", "waiting", "paused", "blocked",
                       "usage_limited", "rate_limited", "budget_limited", "archived"}
LAST_CARD_CLEANUP_SWEEP = 0


def load_config():
    config = {}
    if CONFIG_PATH.exists():
        for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def is_true(value, default=False):
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def ensure_dirs():
    for name in ("sessions", "outbox", "sent", "logs", "probes"):
        (STATE_HOME / name).mkdir(parents=True, exist_ok=True)


def nested_number(value, key):
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return int(candidate)
        for child in value.values():
            found = nested_number(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_number(child, key)
            if found is not None:
                return found
    return None


def classify_completion(payload):
    error = payload.get("error")
    if not error:
        return "", "", None
    if isinstance(error, dict):
        message = str(error.get("message") or "")
    else:
        message = str(error)
    http_status = nested_number(error, "http_status_code")
    if http_status is None:
        match = re.search(r"(?:status(?: code)?[: ]+|http[/ ]?)(429)\b|\b(429)\b", message, re.I)
        if match:
            http_status = 429
    lowered = message.lower()
    if http_status == 429 or "too many requests" in lowered or "rate limit" in lowered:
        return "rate_limited", concise_title(message, limit=180), 429
    return "stopped", concise_title(message or "Codex 请求异常结束", limit=180), http_status


def token_count_text(value):
    if value is None:
        return "--"
    number = max(0, int(value))
    if number >= 1_000_000_000:
        return "%.1fB" % (number / 1_000_000_000.0)
    if number >= 1_000_000:
        return "%.1fM" % (number / 1_000_000.0)
    if number >= 1_000:
        return "%.1fK" % (number / 1_000.0)
    return str(number)


def atomic_json(path, data):
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def inside_workspace(raw_path):
    try:
        Path(raw_path).resolve().relative_to(WORKSPACE)
        return True
    except (ValueError, OSError):
        return False


def instance_id(payload=None):
    payload = payload or {}
    value = os.environ.get("CODEX_TASK_INSTANCE_ID")
    value = value or payload.get("thread-id") or payload.get("thread_id")
    value = value or payload.get("session_id") or payload.get("session-id")
    if value:
        return str(value).replace("/", "_")
    seed = "%s:%s:%s" % (os.getppid(), os.getcwd(), time.time_ns())
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def session_path(identifier):
    return STATE_HOME / "sessions" / (identifier + ".json")


def enqueue(kind, state, payload=None):
    config = load_config()
    if not is_true(config.get("ENABLED"), False):
        return
    now = int(time.time())
    marker = (payload or {}).get("event-id")
    marker = marker or (payload or {}).get("turn-id", (payload or {}).get("turn_id", now))
    event_key = "%s:%s:%s" % (
        state.get("instance_id", "unknown"),
        kind,
        marker,
    )
    event_id = hashlib.sha256(event_key.encode()).hexdigest()[:32]
    event = {
        "id": event_id,
        "kind": kind,
        "created_at": now,
        "attempts": 0,
        "next_attempt": now,
        "state": state,
    }
    target = STATE_HOME / "outbox" / (event_id + ".json")
    if not target.exists() and not (STATE_HOME / "sent" / target.name).exists():
        atomic_json(target, event)
    ensure_worker()


def lifecycle_start():
    ensure_dirs()
    identifier = instance_id()
    now = int(time.time())
    state = {
        "instance_id": identifier,
        "status": "idle",
        "active": True,
        "turn_active": False,
        "managed": True,
        "pid": int(os.environ.get("CODEX_TASK_WRAPPER_PID", os.getppid())),
        "cwd": os.environ.get("CODEX_TASK_CWD", os.getcwd()),
        "label": os.environ.get("CODEX_TASK_NAME", Path(os.getcwd()).name),
        "tty": os.environ.get("CODEX_TASK_TTY", "unknown"),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "hostname": os.uname().nodename,
        "started_at": int(os.environ.get("CODEX_TASK_STARTED_AT", now)),
        "updated_at": now,
    }
    atomic_json(session_path(identifier), state)
    ensure_worker()


def parse_hook_payload():
    candidates = []
    if len(sys.argv) > 2:
        candidates.append(sys.argv[-1])
    if not sys.stdin.isatty():
        candidates.append(sys.stdin.read())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (TypeError, ValueError):
            pass
    return {}


def payload_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content") or value.get("text") or value.get("message")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(payload_text(item) for item in content)
    if isinstance(value, list):
        return " ".join(payload_text(item) for item in value)
    return ""


def concise_title(text, limit=72):
    text = str(text or "")
    text = re.sub(r"<environment_context>.*?</environment_context>", " ", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"</?[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"^[#>*\-\d.\s]+", "", raw_line).strip()
        if line:
            lines.append(line)
    if not lines:
        return ""
    for title in lines:
        title = title.strip("*_` ")
        title = re.sub(r"^(完成了?|已完成|结果[：:]|结论[：:]|好的?[，,。\s]*)", "", title).strip()
        title = re.sub(r"\s+", " ", title)
        if not title or title.lower() in {"done", "completed", "complete"}:
            continue
        if len(title) > limit:
            title = title[: limit - 1].rstrip("，,。；;：: ") + "…"
        return title
    return ""


def concise_detail(text, limit=260, max_lines=4):
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    text = "\n".join(line for line in lines if line)[:limit].rstrip()
    lines = text.splitlines()[:max_lines]
    result = "\n".join(lines)
    if len(text.splitlines()) > max_lines or len(str(text or "")) > limit:
        result = result.rstrip("，,。；;：: ") + "…"
    return result


def organized_task_goal(text):
    """Turn Codex's public execution summary into a stable card objective."""
    text = re.sub(r"<environment_context>.*?</environment_context>", " ", str(text or ""),
                  flags=re.DOTALL)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"</?[^>]+>", " ", text)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"^[#>*\-\d.\s]+", "", raw_line).strip()
        if line:
            lines.append(re.sub(r"\s+", " ", line).strip("*_` "))
    goal = "\n".join(lines).strip()
    goal = re.sub(
        r"^.{1,12}?[，,]\s*(?=我(?:会|将|先|准备|正在))",
        "",
        goal,
    )
    goal = re.sub(
        r"^(?:好的?[，,。\s]*)?(?:接下来[，,\s]*)?"
        r"(?:我(?:会|将|先|准备|正在)|先)[，,\s]*",
        "",
        goal,
    ).strip()
    return goal


def extract_task_title(payload, state):
    if state.get("task_goal_source") == "assistant_commentary" and state.get("task_title"):
        return state["task_title"]
    assistant = payload_text(
        payload.get("last-assistant-message") or payload.get("last_assistant_message")
    )
    title = organized_task_goal(assistant)
    if title:
        return title
    return state.get("task_title") or "Codex 任务"


def extract_result_summary(payload):
    assistant = payload_text(
        payload.get("last-assistant-message") or payload.get("last_assistant_message")
    )
    return concise_title(assistant, limit=110)


def find_rollout_path(thread_id):
    if not thread_id or not SESSIONS_HOME.exists():
        return None
    matches = list(SESSIONS_HOME.rglob("*-%s.jsonl" % thread_id))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def latest_turn_start_offset(path):
    latest = None
    try:
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw_line = stream.readline()
                if not raw_line:
                    break
                if b'"task_started"' not in raw_line:
                    continue
                try:
                    item = json.loads(raw_line)
                except (UnicodeError, ValueError):
                    continue
                payload = item.get("payload", {})
                if item.get("type") == "event_msg" and payload.get("type") == "task_started":
                    latest = offset
    except OSError:
        return 0
    return latest if latest is not None else path.stat().st_size


def event_rollout_for_state(state, terminal_rollout, goal_record):
    if not goal_record or goal_record.get("goal_status") != "active":
        return terminal_rollout, "rollout_offset"
    goal_thread_id = str(goal_record.get("goal_thread_id") or "")
    cached_path = Path(state.get("goal_rollout_path", ""))
    if (goal_thread_id and cached_path.is_file()
            and state.get("goal_rollout_thread_id") == goal_thread_id):
        goal_rollout = cached_path
    else:
        goal_rollout = find_rollout_path(goal_thread_id)
    if not goal_rollout or not goal_rollout.is_file():
        return terminal_rollout, "rollout_offset"
    goal_rollout_text = str(goal_rollout)
    if state.get("goal_rollout_path") != goal_rollout_text:
        state["goal_rollout_path"] = goal_rollout_text
        state["goal_rollout_offset"] = latest_turn_start_offset(goal_rollout)
    state["goal_rollout_thread_id"] = goal_thread_id
    return goal_rollout, "goal_rollout_offset"


def normalized_rollout_offset(state, path, key):
    try:
        size = path.stat().st_size
    except OSError:
        return int(state.get(key, 0))
    offset = int(state.get(key, 0))
    if offset < 0 or offset > size:
        state[key] = size
        return size
    return offset


def hook_complete():
    payload = parse_hook_payload()
    cwd = payload.get("cwd") or os.environ.get("CODEX_TASK_CWD") or os.getcwd()
    if not inside_workspace(cwd):
        return
    identifier = instance_id(payload)
    path = session_path(identifier)
    state = read_json(path, {})
    now = int(time.time())
    if not state:
        state = {
            "instance_id": identifier,
            "pid": None,
            "managed": False,
            "cwd": cwd,
            "label": Path(cwd).name,
            "tty": os.environ.get("CODEX_TASK_TTY", "unknown"),
            "tmux_pane": os.environ.get("TMUX_PANE", ""),
            "hostname": os.uname().nodename,
            "started_at": now,
        }
    payload_turn_id = str(payload.get("turn-id") or payload.get("turn_id") or "")
    same_turn = not payload_turn_id or payload_turn_id == str(state.get("turn_id") or "")
    turn_started_at = int(
        (state.get("turn_started_at") if same_turn else None)
        or payload.get("started_at")
        or state.get("last_completed_at", state.get("started_at", now))
    )
    thread_id = (
        payload.get("thread-id") or payload.get("thread_id")
        or payload.get("session-id") or payload.get("session_id")
    )
    rollout_path = find_rollout_path(thread_id)
    goal_record = goal_record_for_rollout(rollout_path) if rollout_path else {}
    goal_record, _ = relevant_goal_record(state, goal_record)
    if (state.get("managed") and goal_record
            and goal_record.get("goal_status") == "active"
            and thread_id
            and str(thread_id) != str(goal_record.get("goal_thread_id"))):
        return
    if thread_id and (not state.get("managed") or not state.get("thread_id")):
        state["thread_id"] = str(thread_id)
    if rollout_path:
        thread_changed = bool(thread_id and str(thread_id) != str(state.get("thread_id") or ""))
        if thread_changed:
            clear_goal_record(state)
            state["thread_id"] = str(thread_id)
            state["rollout_path"] = str(rollout_path)
            state["rollout_offset"] = rollout_path.stat().st_size
        elif not state.get("managed") or not state.get("rollout_path"):
            state["rollout_path"] = str(rollout_path)
        if not state.get("managed"):
            state["rollout_offset"] = rollout_path.stat().st_size
        if goal_record:
            apply_goal_record(state, goal_record)

    active_goal = bool(state.get("goal_id") and state.get("goal_status") == "active")
    failure_kind, failure_message, failure_http_status = classify_completion(payload)
    completed_at = int(payload.get("completed_at", now))
    common = {
        "active": True,
        "turn_active": False,
        "updated_at": now,
        "last_completed_at": completed_at,
        "turn_started_at": turn_started_at,
        "turn_id": payload_turn_id or state.get("turn_id"),
        "last_started_turn_id": payload_turn_id or state.get("last_started_turn_id"),
        "task_title": extract_task_title(payload, state),
        "result_summary": extract_result_summary(payload),
    }
    if failure_kind:
        common.update({
            "status": failure_kind,
            "goal_running": False,
            "completed_at": completed_at,
            "failure_kind": failure_kind,
            "failure_message": failure_message,
            "failure_http_status": failure_http_status,
            "failure_at": completed_at,
            "failure_at_ms": completed_at * 1000,
            "abort_reason": "HTTP 429" if failure_kind == "rate_limited" else "request_error",
            "current_step": "API 用量受限，等待服务恢复" if failure_kind == "rate_limited"
            else "任务因请求异常中断",
            "result_summary": failure_message,
            "final_duration_seconds": max(0, int(payload.get("duration_ms", 0)) // 1000)
            if payload.get("duration_ms") is not None else max(0, now - turn_started_at),
        })
        event_kind = "stopped"
        if failure_kind == "rate_limited":
            register_rate_limit_probe(state)
    elif active_goal:
        turn_duration = max(0, int(payload.get("duration_ms", 0)) // 1000) \
            if payload.get("duration_ms") is not None else max(0, now - turn_started_at)
        common.update({
            "status": "waiting",
            "goal_running": True,
            "current_step": "本轮已完成，等待 Goal 自动续跑",
            "turn_duration_seconds": turn_duration,
            "final_duration_seconds": turn_duration,
        })
        event_kind = "waiting"
    else:
        common.update({
            "status": "completed",
            "goal_running": False,
            "completed_at": completed_at,
            "current_step": "任务已完成",
            "final_duration_seconds": max(0, int(payload.get("duration_ms", 0)) // 1000)
            if payload.get("duration_ms") is not None else max(0, now - turn_started_at),
        })
        event_kind = "completed"
    state.update(common)
    atomic_json(path, state)
    enqueue(event_kind, state, payload)


def lifecycle_exit(exit_code, signal_name=""):
    identifier = instance_id()
    path = session_path(identifier)
    state = read_json(path, {})
    if not state:
        return
    now = int(time.time())
    monitor_active = bool(state.get("turn_active") or state.get("goal_running"))
    if monitor_active:
        status = "stopped"
    else:
        status = state.get("status", "idle")
    state.update({
        "active": False,
        "turn_active": False,
        "goal_running": False,
        "status": status,
        "exit_code": exit_code,
        "signal": signal_name,
        "updated_at": now,
    })
    if monitor_active:
        state["final_duration_seconds"] = max(
            0, now - int(state.get("turn_started_at", now))
        )
    atomic_json(path, state)
    if monitor_active:
        enqueue("stopped", state, {"event-id": "exit-%s" % now})


def duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, seconds)
    return "%ds" % seconds


def md_escape(value):
    value = html.escape(str(value or ""), quote=False)
    replacements = {
        "*": "&#42;", "~": "&#126;", "[": "&#91;", "]": "&#93;",
        "(": "&#40;", ")": "&#41;", "#": "&#35;", "_": "&#95;",
    }
    return "".join(replacements.get(char, char) for char in value)


def card_kind_for_state(state, fallback):
    status = state.get("status")
    if status in {"running", "started"}:
        return "started"
    if status == "recovered":
        return "recovered"
    if status in TERMINAL_CARD_KINDS:
        return status
    return fallback


def render_card(event, monitor=True):
    state = event["state"]
    kind = card_kind_for_state(state, event["kind"])
    styles = {
        "started": ("Codex 任务已启动", "运行中", "blue", "blue-50", "blue"),
        "completed": ("Codex 任务已完成", "已完成", "green", "green-50", "green"),
        "waiting": ("Codex Goal 阶段已完成", "等待续跑", "grey", "grey-50", "neutral"),
        "stopped": ("Codex 任务意外中断", "需检查", "red", "red-50", "red"),
        "closed": ("Codex 终端已关闭", "已关闭", "grey", "grey-50", "neutral"),
        "paused": ("Codex Goal 已暂停", "已暂停", "orange", "orange-50", "orange"),
        "blocked": ("Codex Goal 已阻塞", "需处理", "red", "red-50", "red"),
        "usage_limited": ("Codex Goal 用量受限", "需处理", "orange", "orange-50", "orange"),
        "rate_limited": ("Codex API 用量受限", "等待恢复", "orange", "orange-50", "orange"),
        "budget_limited": ("Codex Goal 预算受限", "需处理", "orange", "orange-50", "orange"),
        "archived": ("Codex Goal 历史卡片", "已归档", "grey", "grey-50", "neutral"),
        "recovered": ("Codex API 服务已恢复", "已恢复", "green", "green-50", "green"),
    }
    title, status_label, template, background, accent = styles.get(kind, styles["started"])
    started = int(state.get("turn_started_at", state.get("started_at", event["created_at"])))
    terminal = state.get("tmux_pane") or state.get("tty") or "unknown"
    task_title = state.get("task_title")
    if not task_title or (kind == "started" and task_title == state.get("label")):
        task_title = "等待 Codex 接收并执行任务"
    elapsed_seconds = (state.get("turn_duration_seconds") if state.get("goal_id")
                       else state.get("final_duration_seconds"))
    if elapsed_seconds is None:
        elapsed_seconds = event["created_at"] - started
    elapsed = duration_text(elapsed_seconds)
    project = Path(state.get("cwd", "unknown")).name
    duration_label = "当前阶段耗时" if state.get("goal_id") else "本轮耗时"
    metric_values = [(status_label, "状态"), (elapsed, duration_label)]
    if kind == "recovered":
        metric_values.append(("正常", "API 状态"))
    else:
        token_value = state.get("goal_tokens_used") if state.get("goal_id") \
            else state.get("turn_tokens_used")
        token_label = "Goal 累计 Token" if state.get("goal_id") else "本轮 Token"
        if state.get("goal_id") and state.get("goal_token_budget"):
            token_text = "%s / %s" % (
                token_count_text(token_value), token_count_text(state["goal_token_budget"])
            )
        else:
            token_text = token_count_text(token_value)
        metric_values.append((token_text, token_label))

    focus_elements = [
        {"tag": "markdown", "content": "**<font color='%s'>任务目标</font>**" % accent},
        {"tag": "markdown", "content": md_escape(task_title), "text_size": "normal"},
    ]
    current_step = concise_detail(state.get("current_step"))
    if current_step:
        focus_elements.extend([
            {"tag": "markdown", "content": "**当前步骤**", "text_size": "notation"},
            {"tag": "markdown", "content": md_escape(current_step), "text_size": "notation"},
        ])
    if state.get("recent_action") and kind == "started":
        focus_elements.extend([
            {"tag": "markdown", "content": "**最近动作**", "text_size": "notation"},
            {"tag": "markdown", "content": md_escape(state["recent_action"]), "text_size": "notation"},
        ])
    if state.get("result_summary") and kind == "completed":
        focus_elements.extend([
            {"tag": "markdown", "content": "**完成结果**", "text_size": "notation"},
            {"tag": "markdown", "content": md_escape(state["result_summary"]), "text_size": "notation"},
        ])
    if state.get("failure_message") and kind in {"stopped", "rate_limited"}:
        focus_elements.extend([
            {"tag": "markdown", "content": "**中断原因**", "text_size": "notation"},
            {"tag": "markdown", "content": md_escape(state["failure_message"]),
             "text_size": "notation"},
        ])

    columns = []
    for value, label in metric_values:
        columns.append({
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": "grey-50", "padding": "8px", "vertical_spacing": "2px",
            "elements": [
                {"tag": "markdown", "content": "**%s**" % md_escape(value), "text_align": "center"},
                {"tag": "markdown", "content": "<font color='grey'>%s</font>" % md_escape(label),
                 "text_align": "center", "text_size": "notation"},
            ],
        })

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": "**终端**\n%s" % md_escape(terminal)}},
        {"is_short": True, "text": {"tag": "lark_md", "content": "**项目**\n%s" % md_escape(project)}},
        {"is_short": False, "text": {"tag": "lark_md", "content": "**目录**\n%s" % md_escape(state.get("cwd", "unknown"))}},
    ]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "enable_forward": not monitor,
            "summary": {"content": "%s · %s" % (
                title, concise_title(task_title, limit=110) or task_title
            )},
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": "%s · %s" % (project, terminal)},
            "icon": {"tag": "standard_icon", "token": "ai-common_colorful"},
            "text_tag_list": [{
                "tag": "text_tag", "text": {"tag": "plain_text", "content": status_label},
                "color": accent,
            }],
        },
        "body": {
            "direction": "vertical", "padding": "12px 12px 20px 12px", "vertical_spacing": "12px",
            "elements": [
                {"tag": "column_set", "flex_mode": "none", "columns": [{
                    "tag": "column", "width": "weighted", "weight": 1,
                    "background_style": background, "padding": "12px", "vertical_spacing": "4px",
                    "elements": focus_elements,
                }]},
                {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns},
                {"tag": "div", "fields": fields},
                {"tag": "markdown", "content": "<font color='grey'>最后更新：%s%s</font>" % (
                    datetime.fromtimestamp(event["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    " · 置顶监控卡" if monitor else " · 节点通知",
                ), "text_size": "notation"},
            ],
        },
    }


def run_cli(args, timeout=30):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-2000:])
    try:
        response = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("lark-cli returned invalid JSON: %s" % result.stdout[-500:]) from exc
    if response.get("ok") is not True:
        raise RuntimeError("lark-cli request failed: %s" % result.stdout[-1000:])
    return response


def nested_value(value, key):
    if isinstance(value, dict):
        if value.get(key):
            return value[key]
        for child in value.values():
            found = nested_value(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_value(child, key)
            if found:
                return found
    return None


def save_card_metadata(identifier, **metadata):
    path = session_path(identifier)
    latest = read_json(path, {})
    latest.update(metadata)
    atomic_json(path, latest)


def turn_key_from_state(state, fallback=""):
    return str(
        state.get("turn_id")
        or state.get("last_started_turn_id")
        or fallback
    )


def card_key_from_state(state, fallback=""):
    return turn_key_from_state(state, fallback)


def adopt_legacy_goal_card(identifier, latest, event_state, card_key):
    goal_id = event_state.get("goal_id") or latest.get("goal_id")
    legacy_key = "goal:%s" % goal_id if goal_id else ""
    cards = latest.get("turn_cards", {})
    if (not legacy_key or not card_key or not isinstance(cards, dict)
            or card_key in cards or legacy_key not in cards):
        return latest
    cards = dict(cards)
    cards[card_key] = cards.pop(legacy_key)
    latest = dict(latest)
    latest["turn_cards"] = cards
    if latest.get("active_card_key") in {None, "", legacy_key}:
        latest["active_card_key"] = card_key
    atomic_json(session_path(identifier), latest)
    return latest


def turn_card_metadata(latest, event_state, card_key):
    cards = latest.get("turn_cards", {})
    metadata = dict(cards.get(card_key, {})) if isinstance(cards, dict) else {}
    if metadata:
        return metadata

    # Adopt the pre-turn-card state for a task that was already running when
    # this version was deployed. New turns explicitly clear these legacy keys.
    latest_key = card_key_from_state(latest)
    event_key = card_key_from_state(event_state)
    source = latest if latest_key == card_key else event_state if event_key == card_key else {}
    if source.get("message_id"):
        return {
            key: source[key]
            for key in ("message_id", "pinned_at", "unpinned_at", "last_urgent_node")
            if key in source
        }
    return {}


def save_turn_card_metadata(identifier, card_key, **metadata):
    path = session_path(identifier)
    latest = read_json(path, {})
    cards = latest.get("turn_cards", {})
    cards = dict(cards) if isinstance(cards, dict) else {}
    card = dict(cards.get(card_key, {}))
    card.update(metadata)
    cards[card_key] = card
    latest["turn_cards"] = cards
    atomic_json(path, latest)


def turn_card_idempotency_key(identifier, card_key):
    digest = hashlib.sha256((identifier + ":" + card_key).encode()).hexdigest()[:40]
    return "turn-" + digest


def send_new_card(card, event_id, config):
    recipient = config.get("LARK_USER_OPEN_ID", "")
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    if not recipient.startswith("ou_"):
        raise RuntimeError("LARK_USER_OPEN_ID is not configured")
    response = run_cli(
        [cli, "im", "+messages-send", "--user-id", recipient, "--msg-type", "interactive",
         "--content", json.dumps(card, ensure_ascii=False, separators=(",", ":")),
         "--idempotency-key", event_id[:50], "--as", "bot"]
    )
    message_id = nested_value(response.get("data", response), "message_id")
    if not message_id or not str(message_id).startswith("om_"):
        raise RuntimeError("message send response did not contain message_id")
    return str(message_id)


def patch_card(message_id, card, config):
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    body = {"content": json.dumps(card, ensure_ascii=False, separators=(",", ":"))}
    run_cli([cli, "api", "PATCH", "/open-apis/im/v1/messages/%s" % message_id,
             "--data", json.dumps(body, ensure_ascii=False, separators=(",", ":")), "--as", "bot"])


def pin_message(message_id, config):
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    run_cli([cli, "im", "pins", "create", "--data",
             json.dumps({"message_id": message_id}, separators=(",", ":")), "--as", "bot"])


def unpin_message(message_id, config):
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    run_cli([cli, "im", "pins", "delete", "--message-id", message_id,
             "--as", "bot", "--yes"])


def delete_message(message_id, config):
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    run_cli([cli, "im", "messages", "delete", "--message-id", message_id,
             "--as", "bot", "--yes"])


def recall_error_code(exc):
    match = re.search(r'\b(230009)\b', str(exc))
    return match.group(1) if match else ""


def recall_previous_card(identifier, current_key, config):
    path = session_path(identifier)
    latest = read_json(path, {})
    cards = latest.get("turn_cards", {})
    if not isinstance(cards, dict):
        cards = {}
    current = dict(cards.get(current_key, {}))
    previous_key = current.get("previous_card_key") or latest.get("active_card_key")
    if not previous_key:
        candidates = [key for key, metadata in cards.items()
                      if key != current_key and isinstance(metadata, dict)
                      and metadata.get("message_id")]
        previous_key = candidates[-1] if candidates else ""
    if not previous_key or previous_key == current_key:
        latest["active_card_key"] = current_key
        atomic_json(path, latest)
        return False
    previous = dict(cards.get(previous_key, {}))
    message_id = previous.get("message_id")
    if not message_id or previous.get("recalled_at") or previous.get("recall_permanent_error"):
        latest["active_card_key"] = current_key
        atomic_json(path, latest)
        return False
    now = int(time.time())
    try:
        delete_message(message_id, config)
        previous.update({"message_id": None, "recalled_message_id": message_id,
                         "recalled_at": now, "recall_reason": "superseded"})
        previous.pop("recall_error", None)
    except Exception as exc:
        code = recall_error_code(exc)
        previous["recall_error"] = "230009: message expired" if code else concise_title(
            " ".join(str(exc).split()), limit=180)
        previous["recall_attempts"] = int(previous.get("recall_attempts", 0)) + 1
        if code:
            previous["recall_permanent_error"] = code
            previous["recall_expired_at"] = now
        else:
            cards[previous_key] = previous
            latest["turn_cards"] = cards
            atomic_json(path, latest)
            raise
    cards[previous_key] = previous
    latest["turn_cards"] = cards
    latest["active_card_key"] = current_key
    atomic_json(path, latest)
    return True


def codex_provider_settings():
    codex_home = CODEX_HOME
    config_path = Path(os.environ.get("CODEX_CONFIG_PATH", str(codex_home / "config.toml")))
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    provider_match = re.search(r'^model_provider\s*=\s*["\']([^"\']+)', text, re.M)
    model_match = re.search(r'^model\s*=\s*["\']([^"\']+)', text, re.M)
    if not provider_match or not model_match:
        return {}
    provider = provider_match.group(1)
    section = re.search(
        r'^\[model_providers\.%s\]\s*$([\s\S]*?)(?=^\[|\Z)' % re.escape(provider),
        text, re.M,
    )
    if not section:
        return {}
    body = section.group(1)
    base_match = re.search(r'^base_url\s*=\s*["\']([^"\']+)', body, re.M)
    wire_match = re.search(r'^wire_api\s*=\s*["\']([^"\']+)', body, re.M)
    if not base_match or (wire_match and wire_match.group(1) != "responses"):
        return {}
    return {"provider": provider, "model": model_match.group(1),
            "base_url": base_match.group(1).rstrip("/"),
            "auth_path": str(codex_home / "auth.json")}


def provider_fingerprint(settings):
    raw = "%s|%s|%s" % (settings.get("provider", ""), settings.get("base_url", ""),
                         settings.get("model", ""))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def register_rate_limit_probe(state):
    config = load_config()
    if not is_true(config.get("PROBE_429_ENABLED"), True):
        return
    settings = codex_provider_settings()
    if not settings:
        return
    ensure_dirs()
    now = int(time.time())
    fingerprint = provider_fingerprint(settings)
    path = STATE_HOME / "probes" / (fingerprint + ".json")
    probe = read_json(path, {})
    if probe.get("status") != "watching":
        probe = {"id": "%s-%s" % (fingerprint, now), "fingerprint": fingerprint,
                 "provider": settings["provider"], "base_url": settings["base_url"],
                 "model": settings["model"], "status": "watching", "detected_at": now,
                 "next_probe_at": now + max(60, int(config.get("PROBE_429_INTERVAL_SECONDS", "300"))),
                 "attempts": 0, "affected_tasks": []}
    affected = list(probe.get("affected_tasks", []))
    reference = {"instance_id": state.get("instance_id", ""),
                 "card_key": card_key_from_state(state)}
    if reference not in affected:
        affected.append(reference)
    probe["affected_tasks"] = affected[-50:]
    atomic_json(path, probe)


def probe_api(settings, timeout_seconds):
    try:
        api_key = read_json(Path(settings["auth_path"]), {}).get("OPENAI_API_KEY")
        if not api_key:
            return False, "missing_api_key"
        body = json.dumps({"model": settings["model"], "input": "Reply OK.",
                           "max_output_tokens": 16}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            settings["base_url"] + "/responses", data=body, method="POST",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "User-Agent": load_config().get(
                         "PROBE_429_USER_AGENT", "codex_cli_rs/0.146.0")})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response.read(1024)
            return 200 <= int(response.status) < 300, "http_%s" % response.status
    except urllib.error.HTTPError as exc:
        return False, "http_%s" % exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return False, "network_error"


def recovery_event(probe, now):
    return {"id": "recovery-" + probe["id"], "kind": "recovered", "created_at": now,
            "state": {"instance_id": "api-recovery-" + probe["fingerprint"],
                      "status": "recovered", "active": False, "turn_active": False,
                      "cwd": str(WORKSPACE), "label": probe.get("provider", "Codex API"),
                      "tty": "后台探针", "started_at": int(probe.get("detected_at", now)),
                      "turn_started_at": int(probe.get("detected_at", now)),
                      "task_title": "%s · %s" % (probe.get("provider", "Codex API"),
                                                    probe.get("model", "")),
                      "current_step": "API 探测请求已成功，服务恢复可用",
                      "final_duration_seconds": max(0, now - int(probe.get("detected_at", now)))}}


def sweep_rate_limit_probes():
    config = load_config()
    if not is_true(config.get("PROBE_429_ENABLED"), True):
        return
    now = int(time.time())
    interval = max(60, int(config.get("PROBE_429_INTERVAL_SECONDS", "300")))
    max_age = max(1, int(config.get("PROBE_429_MAX_HOURS", "24"))) * 3600
    timeout_seconds = max(3, int(config.get("PROBE_429_TIMEOUT_SECONDS", "20")))
    settings = codex_provider_settings()
    for path in (STATE_HOME / "probes").glob("*.json"):
        probe = read_json(path, {})
        if probe.get("status") == "notifying":
            if int(probe.get("notification_next_attempt", 0)) > now:
                continue
        elif probe.get("status") != "watching" or int(probe.get("next_probe_at", 0)) > now:
            continue
        elif now - int(probe.get("detected_at", now)) >= max_age:
            probe.update({"status": "expired", "expired_at": now})
            atomic_json(path, probe)
            continue
        else:
            if not settings or provider_fingerprint(settings) != probe.get("fingerprint"):
                probe.update({"last_result": "provider_config_unavailable",
                              "next_probe_at": now + interval})
                atomic_json(path, probe)
                continue
            recovered, result = probe_api(settings, timeout_seconds)
            probe.update({"attempts": int(probe.get("attempts", 0)) + 1,
                          "last_probe_at": now, "last_result": result})
            if not recovered:
                probe["next_probe_at"] = now + interval
                atomic_json(path, probe)
                continue
            probe.update({"status": "notifying", "recovered_at": now})
            atomic_json(path, probe)
        try:
            if not probe.get("recovery_message_id"):
                event = recovery_event(probe, int(probe.get("recovered_at", now)))
                probe["recovery_message_id"] = send_new_card(
                    render_card(event, monitor=False), event["id"], config)
                atomic_json(path, probe)
            if (is_true(config.get("URGENT_ON_RECOVERY"), True)
                    and not probe.get("recovery_urgent_sent")):
                urgent_message(probe["recovery_message_id"], config)
                probe["recovery_urgent_sent"] = True
            probe["status"] = "recovered"
            probe.pop("notification_next_attempt", None)
            probe.pop("notification_error", None)
            atomic_json(path, probe)
        except Exception as exc:
            probe["notification_error"] = concise_title(str(exc), limit=180)
            probe["notification_next_attempt"] = now + 300
            atomic_json(path, probe)


def should_urgent(kind, config):
    keys = {
        "started": "URGENT_ON_STARTED",
        "completed": "URGENT_ON_COMPLETED",
        "stopped": "URGENT_ON_STOPPED",
    }
    key = keys.get(kind)
    return bool(key) and is_true(config.get(key), True)


def should_urgent_event(event, state, config):
    # A Codex notify hook fires at the end of every automatic Goal turn. A
    # delayed completion event must not alert while the Goal itself is active.
    if (event.get("kind") == "completed"
            and state.get("goal_id")
            and state.get("goal_status") == "active"):
        return False
    return should_urgent(event.get("kind"), config)


def urgent_message(message_id, config):
    recipient = config.get("LARK_USER_OPEN_ID", "")
    cli = config.get("LARK_CLI", str(DEFAULT_LARK_CLI))
    if not recipient.startswith("ou_"):
        raise RuntimeError("LARK_USER_OPEN_ID is not configured")
    run_cli([
        cli, "im", "messages", "urgent_app",
        "--message-id", message_id,
        "--user-id-type", "open_id",
        "--data", json.dumps({"user_id_list": [recipient]}, separators=(",", ":")),
        "--as", "bot",
    ])


def urgent_node_key(event, state):
    event_state = event.get("state", {})
    card_key = card_key_from_state(event_state, event["id"])
    if event["kind"] == "stopped":
        return "%s:%s:%s" % (
            card_key,
            turn_key_from_state(event_state, event["id"]),
            event["kind"],
        )
    return "%s:%s" % (card_key, event["kind"])


def deliver_event(event, config):
    identifier = event["state"]["instance_id"]
    latest = read_json(session_path(identifier), {})
    event_state = event["state"]
    card_key = card_key_from_state(event_state, event["id"])
    latest = adopt_legacy_goal_card(identifier, latest, event_state, card_key)
    latest_is_same_card = card_key_from_state(latest) == card_key
    if (latest_is_same_card
            and int(latest.get("updated_at", 0)) >= int(event_state.get("updated_at", 0))):
        state = latest
    else:
        state = dict(event_state)
    live_event = dict(event)
    live_event["state"] = state
    live_event["kind"] = card_kind_for_state(state, event["kind"])

    card_metadata = turn_card_metadata(latest, event_state, card_key)
    previous_card_key = card_metadata.get("previous_card_key") or latest.get("active_card_key")
    if not previous_card_key:
        cards = latest.get("turn_cards", {})
        candidates = [key for key, metadata in cards.items()
                      if key != card_key and isinstance(metadata, dict)
                      and metadata.get("message_id")] if isinstance(cards, dict) else []
        previous_card_key = candidates[-1] if candidates else ""
    unavailable = (card_metadata.get("deleted_at") or card_metadata.get("recalled_at")
                   or card_metadata.get("recall_permanent_error"))
    if unavailable:
        if (live_event["kind"] not in TERMINAL_CARD_KINDS
                or card_metadata.get("terminal_kind") == live_event["kind"]):
            return
        # A later terminal transition still needs a visible notification even
        # when its stage card was already recalled after the inactivity window.
        card_key = "%s:%s" % (card_key, event["id"])
        card_metadata = turn_card_metadata(latest, event_state, card_key)
        previous_card_key = ""
    existing_cards = latest.get("turn_cards", {})
    if (card_metadata
            and (not isinstance(existing_cards, dict) or card_key not in existing_cards)):
        save_turn_card_metadata(identifier, card_key, **card_metadata)
    message_id = card_metadata.get("message_id")
    card_created = False
    if message_id:
        patch_card(message_id, render_card(live_event, monitor=True), config)
    else:
        message_id = send_new_card(
            render_card(live_event, monitor=True),
            turn_card_idempotency_key(identifier, card_key),
            config,
        )
        card_metadata["message_id"] = message_id
        card_created = True
        card_metadata["message_created_at"] = int(time.time())
        card_metadata["previous_card_key"] = previous_card_key
        save_turn_card_metadata(
            identifier, card_key, message_id=message_id,
            message_created_at=card_metadata["message_created_at"],
            previous_card_key=previous_card_key,
        )

    if live_event["kind"] == "started":
        if not card_metadata.get("pinned_at"):
            pin_message(message_id, config)
            pinned_at = int(time.time())
            card_metadata["pinned_at"] = pinned_at
            save_turn_card_metadata(identifier, card_key, pinned_at=pinned_at, unpinned_at=None)
        recall_previous_card(identifier, card_key, config)
    elif card_created:
        recall_previous_card(identifier, card_key, config)
    elif card_metadata.get("pinned_at"):
        unpin_message(message_id, config)
        unpinned_at = int(time.time())
        card_metadata["pinned_at"] = None
        card_metadata["unpinned_at"] = unpinned_at
        save_turn_card_metadata(identifier, card_key, pinned_at=None, unpinned_at=unpinned_at)

    if live_event["kind"] in TERMINAL_CARD_KINDS:
        terminal_at = int(card_metadata.get("terminal_at") or event.get("created_at") or time.time())
        card_metadata.update({"terminal_at": terminal_at, "terminal_kind": live_event["kind"],
                              "inactive_since": terminal_at})
        save_turn_card_metadata(identifier, card_key, terminal_at=terminal_at,
                                terminal_kind=live_event["kind"], inactive_since=terminal_at)
    elif live_event["kind"] == "started" and card_metadata.get("terminal_at"):
        card_metadata.update({"terminal_at": None, "terminal_kind": None})
        save_turn_card_metadata(identifier, card_key, terminal_at=None, terminal_kind=None)

    # Use the original event kind: progress refreshes can render a running card,
    # but only real lifecycle nodes should alert the user's phone.
    automatic_goal_continuation = bool(
        event.get("kind") == "started" and state.get("goal_id") and previous_card_key
    )
    if not automatic_goal_continuation and should_urgent_event(event, state, config):
        node_key = urgent_node_key(event, state)
        if card_metadata.get("last_urgent_node") != node_key:
            urgent_message(message_id, config)
            card_metadata["last_urgent_node"] = node_key
            save_turn_card_metadata(identifier, card_key, last_urgent_node=node_key)


def drain():
    config = load_config()
    if not is_true(config.get("ENABLED"), False):
        return
    now = int(time.time())
    for path in sorted((STATE_HOME / "outbox").glob("*.json")):
        event = read_json(path, {})
        if not event or int(event.get("next_attempt", 0)) > now:
            continue
        try:
            deliver_event(event, config)
            os.replace(str(path), str(STATE_HOME / "sent" / path.name))
        except Exception as exc:
            event["attempts"] = int(event.get("attempts", 0)) + 1
            event["next_attempt"] = now + min(300, 5 * (2 ** min(event["attempts"], 6)))
            event["last_error"] = str(exc)
            atomic_json(path, event)


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def timestamp_seconds(value):
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def rollout_metadata(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            item = json.loads(stream.readline())
    except (OSError, UnicodeError, ValueError):
        return None
    if item.get("type") != "session_meta":
        return None
    payload = item.get("payload", {})
    return {
        "thread_id": payload.get("session_id") or payload.get("id"),
        "parent_thread_id": payload.get("parent_thread_id"),
        "cwd": payload.get("cwd"),
        "started_at": timestamp_seconds(payload.get("timestamp") or item.get("timestamp")),
    }


def goal_record_for_rollout(path):
    if not GOALS_DB_PATH.is_file():
        return {}
    metadata = rollout_metadata(path)
    if not metadata:
        return {}
    thread_ids = []
    for value in (metadata.get("thread_id"), metadata.get("parent_thread_id")):
        value = str(value or "")
        if value and value not in thread_ids:
            thread_ids.append(value)
    if not thread_ids:
        return {}
    try:
        uri = GOALS_DB_PATH.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.25)
        try:
            for thread_id in thread_ids:
                row = connection.execute(
                    """
                    SELECT thread_id, goal_id, status, token_budget, tokens_used,
                           time_used_seconds, created_at_ms, updated_at_ms
                    FROM thread_goals
                    WHERE thread_id = ?
                    """,
                    (thread_id,),
                ).fetchone()
                if row:
                    keys = (
                        "goal_thread_id", "goal_id", "goal_status", "goal_token_budget",
                        "goal_tokens_used", "goal_time_used_seconds",
                        "goal_created_at_ms", "goal_updated_at_ms",
                    )
                    return dict(zip(keys, row))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    return {}


def apply_goal_record(state, record):
    for key, value in record.items():
        state[key] = value


def clear_goal_record(state):
    for key in (
        "goal_thread_id", "goal_id", "goal_status", "goal_token_budget",
        "goal_tokens_used", "goal_time_used_seconds", "goal_created_at_ms",
        "goal_updated_at_ms", "goal_running", "goal_task_title",
        "goal_rollout_path", "goal_rollout_offset", "goal_rollout_thread_id",
    ):
        state.pop(key, None)


def relevant_goal_record(state, record):
    if not record:
        return {}, False
    turn_started_at = int(state.get("turn_started_at") or 0)
    goal_updated_at_ms = int(record.get("goal_updated_at_ms") or 0)
    stale_complete = (record.get("goal_status") == "complete" and turn_started_at
                      and goal_updated_at_ms and turn_started_at * 1000 > goal_updated_at_ms)
    if not stale_complete:
        return record, False
    detached = state.get("goal_id") == record.get("goal_id")
    if detached:
        clear_goal_record(state)
        if state.get("status") == "completed" and state.get("current_step") == "Goal 已完成":
            state["current_step"] = "任务已完成"
    return {}, detached


def current_turn_elapsed_seconds(state, now):
    if state.get("turn_duration_seconds") is not None:
        return max(0, int(state["turn_duration_seconds"]))
    end = now if state.get("turn_active") else int(state.get("last_completed_at", now))
    return max(0, end - int(state.get("turn_started_at", end)))


def synchronize_goal_state(state, record, now):
    previous_goal_id = state.get("goal_id")
    previous_goal_status = state.get("goal_status")
    previous_status = state.get("status")
    goal_id = record["goal_id"]
    if previous_goal_id != goal_id:
        state.pop("goal_task_title", None)
        if state.get("task_goal_source") == "assistant_commentary":
            state["goal_task_title"] = state.get("task_title")
    apply_goal_record(state, record)
    goal_status = record["goal_status"]
    event_kind = ""
    failure_at_ms = int(state.get("failure_at_ms") or 0)
    record_updated_at_ms = int(record.get("goal_updated_at_ms") or 0)
    failure_is_newer = bool(failure_at_ms and record_updated_at_ms <= failure_at_ms)
    if failure_is_newer and goal_status in {"active", "complete"}:
        state["goal_running"] = False
        return ""

    if goal_status == "active":
        state["goal_running"] = True
        # The Goal row describes the overall lifecycle, not an individual
        # stage. Only a real rollout task_started event may start a new card.
    elif goal_status == "complete":
        for key in ("failure_kind", "failure_message", "failure_http_status",
                    "failure_at", "failure_at_ms", "abort_reason"):
            state.pop(key, None)
        state.update({
            "status": "completed",
            "turn_active": False,
            "goal_running": False,
            "current_step": "Goal 已完成",
            "turn_duration_seconds": current_turn_elapsed_seconds(state, now),
            "final_duration_seconds": current_turn_elapsed_seconds(state, now),
        })
        if previous_goal_status != goal_status or previous_status != "completed":
            event_kind = "completed"
    else:
        labels = {
            "paused": "Goal 已暂停",
            "blocked": "Goal 已阻塞，需要处理",
            "usage_limited": "Goal 因用量限制暂停",
            "budget_limited": "Goal 因预算限制暂停",
        }
        state.update({
            "status": goal_status,
            "turn_active": False,
            "goal_running": False,
            "current_step": labels.get(goal_status, "Goal 已暂停"),
            "turn_duration_seconds": current_turn_elapsed_seconds(state, now),
            "final_duration_seconds": current_turn_elapsed_seconds(state, now),
        })
        if previous_goal_status != goal_status or previous_status != goal_status:
            event_kind = "stopped"

    if previous_goal_status != goal_status or previous_goal_id != goal_id or event_kind:
        state["updated_at"] = now
    return event_kind


def sweep_goal_statuses():
    now = int(time.time())
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        if not state.get("managed") or not state.get("active"):
            continue
        if refresh_rollout_binding(state):
            atomic_json(path, state)
        rollout_path = Path(state.get("rollout_path", ""))
        if not rollout_path.is_file():
            continue
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        record, detached = relevant_goal_record(
            state, goal_record_for_rollout(rollout_path))
        if not record:
            if json.dumps(state, ensure_ascii=False, sort_keys=True) != before:
                atomic_json(path, state)
            if detached:
                enqueue("progress", state, {
                    "event-id": "goal-detached-%s" % state.get("turn_id", now),
                    "turn-id": state.get("turn_id"),
                })
            continue
        event_kind = synchronize_goal_state(state, record, now)
        if json.dumps(state, ensure_ascii=False, sort_keys=True) != before:
            atomic_json(path, state)
        if event_kind:
            enqueue(event_kind, state, {
                "event-id": "goal-%s-%s" % (record["goal_updated_at_ms"], event_kind),
                "turn-id": state.get("turn_id"),
            })


def claimed_rollouts(exclude_identifier=""):
    claimed = set()
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        if state.get("instance_id") == exclude_identifier:
            continue
        if state.get("active") and state.get("rollout_path"):
            claimed.add(str(Path(state["rollout_path"]).resolve()))
    return claimed


def thread_from_shell_snapshot(state):
    if not SHELL_SNAPSHOTS_HOME.is_dir():
        return ""
    needle = 'CODEX_TASK_INSTANCE_ID="%s"' % state.get("instance_id", "")
    candidates = sorted(
        SHELL_SNAPSHOTS_HOME.glob("*.sh"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[:100]:
        try:
            if needle in path.read_text(encoding="utf-8"):
                return path.name.split(".", 1)[0]
        except (OSError, UnicodeError):
            continue
    return ""


def refresh_rollout_binding(state):
    snapshot_thread = thread_from_shell_snapshot(state)
    if not snapshot_thread or snapshot_thread == str(state.get("thread_id") or ""):
        return False
    candidate = find_rollout_path(snapshot_thread)
    if not candidate:
        return False
    candidate_metadata = rollout_metadata(candidate)
    if (not candidate_metadata or candidate_metadata.get("parent_thread_id")
            or candidate_metadata.get("cwd") != state.get("cwd")):
        return False
    current = Path(state.get("rollout_path", ""))
    current_metadata = rollout_metadata(current) if current.is_file() else {}
    if (current_metadata and int(candidate_metadata.get("started_at") or 0)
            <= int(current_metadata.get("started_at") or 0)):
        return False
    state.update({"thread_id": snapshot_thread, "rollout_path": str(candidate),
                  "rollout_offset": 0, "thread_rebound_at": int(time.time())})
    clear_goal_record(state)
    for key in ("goal_rollout_path", "goal_rollout_offset", "goal_rollout_thread_id"):
        state.pop(key, None)
    return True


def assign_rollout(state):
    thread_id = state.get("thread_id") or thread_from_shell_snapshot(state)
    if thread_id:
        path = find_rollout_path(thread_id)
        if path and str(path.resolve()) not in claimed_rollouts(state.get("instance_id", "")):
            return path, str(thread_id)

    claimed = claimed_rollouts(state.get("instance_id", ""))
    started_at = int(state.get("started_at", 0))
    candidates = []
    if SESSIONS_HOME.is_dir():
        for path in SESSIONS_HOME.rglob("rollout-*.jsonl"):
            if str(path.resolve()) in claimed:
                continue
            metadata = rollout_metadata(path)
            if not metadata or metadata.get("cwd") != state.get("cwd"):
                continue
            delta = int(metadata.get("started_at", 0)) - started_at
            if -120 <= delta <= 600:
                candidates.append((abs(delta), path, metadata.get("thread_id")))
    if not candidates:
        return None, ""
    _, path, thread_id = min(candidates, key=lambda item: (item[0], str(item[1])))
    return path, str(thread_id or "")


def sanitized_tool_action(payload):
    name = str(payload.get("name") or payload.get("tool_name") or "").lower()
    if "apply_patch" in name or name in {"patch", "edit"}:
        return "正在修改文件"
    if "write_stdin" in name or name in {"wait", "wait_agent"}:
        return "正在等待后台任务"
    if "exec" in name or "command" in name or name in {"shell", "bash"}:
        return "正在运行命令或检查结果"
    return "正在执行工具操作"


def sweep_turn_events():
    now = int(time.time())
    for state_path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(state_path, {})
        if not state.get("managed") or not state.get("active"):
            continue
        if "turn_active" not in state:
            # One-time migration from the old terminal-based state model. Replaying
            # this terminal's rollout reconstructs the latest turn without sending
            # each historical transition.
            state["turn_active"] = False
            state["rollout_offset"] = 0
        refresh_rollout_binding(state)
        terminal_rollout = Path(state.get("rollout_path", ""))
        if not terminal_rollout.is_file():
            terminal_rollout, thread_id = assign_rollout(state)
            if not terminal_rollout:
                continue
            state["thread_id"] = thread_id
            state["rollout_path"] = str(terminal_rollout)
            state["rollout_offset"] = 0
        normalized_rollout_offset(state, terminal_rollout, "rollout_offset")

        goal_record, _ = relevant_goal_record(
            state, goal_record_for_rollout(terminal_rollout))
        if goal_record:
            goal_event_kind = synchronize_goal_state(state, goal_record, now)
            if goal_event_kind:
                enqueue(goal_event_kind, state, {
                    "event-id": "goal-%s-%s" % (
                        goal_record["goal_updated_at_ms"], goal_event_kind
                    ),
                    "turn-id": state.get("turn_id"),
                })

        rollout_path, offset_key = event_rollout_for_state(
            state, terminal_rollout, goal_record
        )
        old_offset = normalized_rollout_offset(state, rollout_path, offset_key)
        try:
            records = []
            with rollout_path.open("r", encoding="utf-8") as stream:
                stream.seek(old_offset)
                while True:
                    raw_line = stream.readline()
                    if not raw_line:
                        break
                    records.append((stream.tell(), raw_line))
                new_offset = stream.tell()
        except (OSError, UnicodeError):
            continue
        if new_offset <= old_offset:
            atomic_json(state_path, state)
            continue
        catching_up = (
            len(records) > MAX_LIVE_SWEEP_RECORDS
            or new_offset - old_offset > MAX_LIVE_SWEEP_BYTES
        )

        changed = False
        event_kind = ""
        event_marker = new_offset
        for line_offset, raw_line in records:
            try:
                item = json.loads(raw_line)
            except ValueError:
                continue
            payload = item.get("payload", {})
            item_type = item.get("type")
            payload_type = payload.get("type")

            if item_type == "event_msg" and payload_type == "task_started":
                turn_started_at = int(payload.get("started_at", now))
                if old_offset == 0 and turn_started_at < int(state.get("started_at", 0)) - 2:
                    continue
                if state.get("goal_status") == "complete":
                    clear_goal_record(state)
                for key in (
                    "message_id", "pinned_at", "unpinned_at", "last_urgent_node",
                    "abort_reason", "aborted_at", "last_aborted_at", "signal", "exit_code",
                    "failure_kind", "failure_message", "failure_http_status",
                    "failure_at", "failure_at_ms",
                ):
                    state.pop(key, None)
                active_goal = bool(
                    state.get("goal_id") and state.get("goal_status") == "active"
                )
                state.update({
                    "status": "running",
                    "turn_active": True,
                    "updated_at": now,
                    "turn_started_at": turn_started_at,
                    "turn_id": payload.get("turn_id"),
                    "last_started_turn_id": payload.get("turn_id"),
                    "task_title": "正在整理本阶段目标" if active_goal else "正在整理任务目标",
                    "task_goal_source": "pending",
                    "task_goal_pending": True,
                    "current_step": "正在分析本阶段任务" if active_goal else "正在分析任务",
                    "recent_action": "",
                    "result_summary": "",
                    "final_duration_seconds": None,
                    "turn_duration_seconds": None,
                    "turn_token_baseline": state.get("session_total_tokens"),
                    "turn_tokens_used": 0,
                    "goal_running": active_goal,
                })
                changed = True
                event_kind = "started"
                event_marker = line_offset
                continue

            if item_type == "event_msg" and payload_type == "token_count":
                info = payload.get("info", {})
                total_usage = info.get("total_token_usage", {}) if isinstance(info, dict) else {}
                last_usage = info.get("last_token_usage", {}) if isinstance(info, dict) else {}
                total_tokens = nested_number(total_usage, "total_tokens")
                last_tokens = nested_number(last_usage, "total_tokens")
                if total_tokens is not None:
                    state["session_total_tokens"] = total_tokens
                    if state.get("turn_active"):
                        baseline = state.get("turn_token_baseline")
                        if baseline is None:
                            baseline = max(0, total_tokens - int(last_tokens or 0))
                            state["turn_token_baseline"] = baseline
                        state["turn_tokens_used"] = max(0, total_tokens - int(baseline))
                    changed = True
                continue

            if not state.get("turn_active"):
                continue

            if item_type == "response_item" and payload.get("role") == "user":
                # User text can be long, conversational, or contain handoff context.
                # Wait for Codex's first public execution summary instead of
                # copying that raw input into the notification card.
                continue

            if (item_type == "response_item" and payload.get("role") == "assistant"
                    and payload.get("phase") == "commentary"):
                public_text = payload_text(payload)
                if state.get("task_goal_pending"):
                    task_goal = organized_task_goal(public_text)
                    if task_goal:
                        state["task_title"] = task_goal
                        state["task_goal_source"] = "assistant_commentary"
                        state["task_goal_pending"] = False
                        changed = True
                step = concise_detail(public_text)
                if step and step != state.get("current_step"):
                    state["current_step"] = step
                    changed = True
                    if event_kind != "started":
                        event_kind = "progress"
                    event_marker = line_offset
                continue

            if item_type == "response_item" and payload_type in {"custom_tool_call", "function_call"}:
                action = sanitized_tool_action(payload)
                if action != state.get("recent_action"):
                    state["recent_action"] = action
                    changed = True
                    if event_kind != "started":
                        event_kind = "progress"
                    event_marker = line_offset
                continue

            if item_type == "event_msg" and payload_type == "task_complete":
                duration_ms = payload.get("duration_ms")
                turn_duration = max(0, int(duration_ms) // 1000) \
                    if duration_ms is not None else max(
                        0, now - int(state.get("turn_started_at", now))
                    )
                failure_kind, failure_message, failure_http_status = classify_completion(payload)
                completed_at = int(payload.get("completed_at", now))
                if failure_kind:
                    state.update({
                        "status": failure_kind, "turn_active": False, "goal_running": False,
                        "updated_at": now, "last_completed_at": completed_at,
                        "completed_at": completed_at, "failure_kind": failure_kind,
                        "failure_message": failure_message,
                        "failure_http_status": failure_http_status,
                        "failure_at": completed_at, "failure_at_ms": completed_at * 1000,
                        "abort_reason": "HTTP 429" if failure_kind == "rate_limited"
                        else "request_error",
                        "current_step": "API 用量受限，等待服务恢复"
                        if failure_kind == "rate_limited" else "任务因请求异常中断",
                        "result_summary": failure_message,
                        "turn_duration_seconds": turn_duration,
                        "final_duration_seconds": turn_duration,
                    })
                    completion_kind = "stopped"
                    if failure_kind == "rate_limited":
                        register_rate_limit_probe(state)
                elif state.get("goal_id") and state.get("goal_status") == "active":
                    state.update({
                        "status": "waiting",
                        "turn_active": False,
                        "goal_running": True,
                        "updated_at": now,
                        "last_completed_at": completed_at,
                        "current_step": "本轮已完成，等待 Goal 自动续跑",
                        "result_summary": concise_title(
                            payload.get("last_agent_message", ""), limit=110
                        ),
                        "turn_duration_seconds": turn_duration,
                        "final_duration_seconds": turn_duration,
                    })
                    completion_kind = "waiting"
                else:
                    state.update({
                        "status": "completed",
                        "turn_active": False,
                        "goal_running": False,
                        "updated_at": now,
                        "last_completed_at": completed_at,
                        "completed_at": completed_at,
                        "current_step": "任务已完成",
                        "result_summary": concise_title(
                            payload.get("last_agent_message", ""), limit=110
                        ),
                        "turn_duration_seconds": turn_duration,
                        "final_duration_seconds": turn_duration,
                    })
                    completion_kind = "completed"
                changed = True
                event_kind = completion_kind if catching_up else ""
                event_marker = line_offset
                if not catching_up:
                    completion_payload = {"turn-id": state.get("turn_id")}
                    if completion_kind != "completed":
                        completion_payload["event-id"] = "progress-%s" % line_offset
                    enqueue(completion_kind, dict(state), completion_payload)

            if item_type == "event_msg" and payload_type == "turn_aborted":
                aborted_turn_id = str(payload.get("turn_id") or "")
                current_turn_id = turn_key_from_state(state)
                if not state.get("turn_active") or aborted_turn_id != current_turn_id:
                    continue
                duration_ms = payload.get("duration_ms")
                state.update({
                    "status": "stopped",
                    "turn_active": False,
                    "goal_running": False,
                    "updated_at": now,
                    "last_aborted_at": int(payload.get("completed_at", now)),
                    "aborted_at": int(payload.get("completed_at", now)),
                    "abort_reason": str(payload.get("reason") or "interrupted"),
                    "current_step": "任务已中断",
                    "result_summary": "用户手动停止了本轮任务",
                    "final_duration_seconds": max(0, int(duration_ms) // 1000)
                    if duration_ms is not None else max(
                        0, now - int(state.get("turn_started_at", now))
                    ),
                    "turn_duration_seconds": max(0, int(duration_ms) // 1000)
                    if duration_ms is not None else max(
                        0, now - int(state.get("turn_started_at", now))
                    ),
                })
                changed = True
                event_kind = "stopped" if catching_up else ""
                event_marker = line_offset
                if not catching_up:
                    enqueue("stopped", dict(state), {"turn-id": aborted_turn_id})

        state[offset_key] = new_offset
        atomic_json(state_path, state)
        if changed and event_kind:
            event_payload = {"turn-id": state.get("turn_id")}
            if event_kind != "completed":
                event_payload["event-id"] = "%s-%s" % (event_kind, event_marker)
            enqueue(event_kind, state, event_payload)


def sweep_turn_starts():
    """Backward-compatible alias for older callers and tests."""
    sweep_turn_events()


def sweep_elapsed():
    # Feishu surfaces every card PATCH as message activity. Timer-only refreshes
    # therefore wake the card repeatedly on mobile. Elapsed time is recomputed
    # whenever real progress or a lifecycle event updates the card.
    return


def sweep_stale_card_pins():
    config = load_config()
    if not is_true(config.get("ENABLED"), False):
        return
    now = int(time.time())
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        cards = state.get("turn_cards", {})
        if not isinstance(cards, dict):
            continue
        keep_key = ""
        if (state.get("active")
                and (state.get("turn_active") or state.get("goal_running"))):
            keep_key = card_key_from_state(state)
        changed = False
        cards = dict(cards)
        for key, raw_metadata in cards.items():
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            message_id = metadata.get("message_id")
            if key == keep_key or not metadata.get("pinned_at") or not message_id:
                continue
            try:
                unpin_message(message_id, config)
            except Exception:
                continue
            metadata["pinned_at"] = None
            metadata["unpinned_at"] = now
            cards[key] = metadata
            changed = True
        if changed:
            state["turn_cards"] = cards
            atomic_json(path, state)


def sweep_completed_card_cleanup(force=False):
    global LAST_CARD_CLEANUP_SWEEP
    config = load_config()
    enabled = config.get("RECALL_INACTIVE_CARDS")
    if enabled is None:
        enabled = config.get("DELETE_COMPLETED_CARDS")
    if not is_true(enabled, False):
        return
    now = int(time.time())
    sweep_interval = max(60, int(config.get("CARD_CLEANUP_SWEEP_SECONDS", "900")))
    if not force and now - LAST_CARD_CLEANUP_SWEEP < sweep_interval:
        return
    LAST_CARD_CLEANUP_SWEEP = now
    retention = max(60, int(config.get("RECALL_AFTER_INACTIVE_SECONDS", "7200")))
    max_age = max(60, int(config.get("RECALL_MAX_MESSAGE_AGE_SECONDS", "84600")))
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        cards = state.get("turn_cards", {})
        if not isinstance(cards, dict):
            continue
        changed = False
        cards = dict(cards)
        for key, raw_metadata in cards.items():
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            message_id = metadata.get("message_id")
            terminal_kind = metadata.get("terminal_kind")
            inactive_since = metadata.get("inactive_since") or metadata.get("terminal_at") \
                or metadata.get("unpinned_at")
            if (terminal_kind not in TERMINAL_CARD_KINDS or not message_id
                    or metadata.get("pinned_at") or metadata.get("recalled_at")
                    or metadata.get("recall_permanent_error") or not inactive_since
                    or now - int(inactive_since) < retention
                    or int(metadata.get("next_delete_attempt", 0)) > now):
                continue
            created_at = int(metadata.get("message_created_at") or 0)
            if created_at and now - created_at >= max_age:
                metadata.update({"recall_permanent_error": "230009",
                                 "recall_error": "230009: message is beyond the safe recall age",
                                 "recall_expired_at": now})
                cards[key] = metadata
                changed = True
                continue
            try:
                delete_message(message_id, config)
                metadata.update({"message_id": None, "recalled_message_id": message_id,
                                 "recalled_at": now,
                                 "delete_attempts": int(metadata.get("delete_attempts", 0)) + 1})
                metadata.pop("next_delete_attempt", None)
                metadata.pop("delete_error", None)
            except Exception as exc:
                attempts = int(metadata.get("delete_attempts", 0)) + 1
                code = recall_error_code(exc)
                metadata.update({"delete_attempts": attempts,
                                 "next_delete_attempt": now + min(3600, 60 * (2 ** min(attempts, 5))),
                                 "delete_error": "230009: message expired" if code else concise_title(
                                     " ".join(str(exc).split()), limit=180)})
                if code:
                    metadata["recall_permanent_error"] = code
                    metadata["recall_expired_at"] = now
                    metadata.pop("next_delete_attempt", None)
            cards[key] = metadata
            changed = True
        if changed:
            state["turn_cards"] = cards
            atomic_json(path, state)


def sweep_stale():
    now = int(time.time())
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        if not state.get("managed") or not state.get("active") or pid_alive(state.get("pid")):
            continue
        if now - int(state.get("updated_at", now)) < 20:
            continue
        monitor_active = bool(state.get("turn_active") or state.get("goal_running"))
        state.update({
            "active": False, "turn_active": False, "goal_running": False,
            "updated_at": now,
        })
        if monitor_active:
            state.update({
                "status": "stopped",
                "exit_code": "lost",
                "final_duration_seconds": max(
                    0, now - int(state.get("turn_started_at", now))
                ),
            })
        atomic_json(path, state)
        if monitor_active:
            enqueue("stopped", state, {"event-id": "lost-%s" % now})


def worker():
    ensure_dirs()
    source_path = Path(__file__).resolve()
    source_mtime = source_path.stat().st_mtime
    lock_file = open(str(STATE_HOME / "worker.lock"), "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    (STATE_HOME / "worker.pid").write_text(str(os.getpid()) + "\n", encoding="ascii")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        sweep_goal_statuses()
        sweep_turn_events()
        sweep_elapsed()
        drain()
        sweep_stale_card_pins()
        sweep_completed_card_cleanup()
        sweep_rate_limit_probes()
        sweep_stale()
        if source_path.stat().st_mtime != source_mtime:
            os.execv(sys.executable, [sys.executable, str(source_path), "worker"])
        time.sleep(1)


def ensure_worker():
    config = load_config()
    if not is_true(config.get("ENABLED"), False):
        return
    pid_path = STATE_HOME / "worker.pid"
    try:
        if pid_alive(int(pid_path.read_text().strip())):
            return
    except (OSError, ValueError):
        pass
    ensure_dirs()
    log = open(str(STATE_HOME / "logs" / "worker.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker"], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, close_fds=True)


def print_status():
    ensure_dirs()
    rows = []
    for path in sorted((STATE_HOME / "sessions").glob("*.json")):
        state = read_json(path, {})
        if state:
            rows.append("{status:10}  {label:24}  {cwd}".format(**state))
    print("\n".join(rows) if rows else "No tracked Codex sessions.")


def doctor():
    config = load_config()
    errors = []
    recipient = config.get("LARK_USER_OPEN_ID", "")
    cli = Path(config.get("LARK_CLI", str(DEFAULT_LARK_CLI)))
    if not CONFIG_PATH.is_file():
        errors.append("missing configuration: %s" % CONFIG_PATH)
    if not is_true(config.get("ENABLED"), False):
        errors.append("ENABLED is not true")
    if not recipient.startswith("ou_"):
        errors.append("LARK_USER_OPEN_ID must start with ou_")
    if not cli.is_file() or not os.access(str(cli), os.X_OK):
        errors.append("lark-cli wrapper is not executable: %s" % cli)
    if not WORKSPACE.is_dir():
        errors.append("workspace does not exist: %s" % WORKSPACE)
    if errors:
        for error in errors:
            print("ERROR: %s" % error, file=sys.stderr)
        return False
    result = subprocess.run(
        [str(cli), "config", "show"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", timeout=30,
    )
    if result.returncode != 0:
        print("ERROR: lark-cli is not configured: %s" % (
            result.stderr or result.stdout
        ).strip()[-1000:], file=sys.stderr)
        return False
    print("Configuration OK")
    print("Workspace: %s" % WORKSPACE)
    print("State: %s" % STATE_HOME)
    print("Codex sessions: %s" % SESSIONS_HOME)
    print("Goal database: %s%s" % (
        GOALS_DB_PATH, " (optional, not found)" if not GOALS_DB_PATH.exists() else ""
    ))
    return True


def send_test_notification():
    if not doctor():
        raise SystemExit(1)
    now = int(time.time())
    state = {
        "instance_id": "setup-test",
        "status": "completed",
        "active": False,
        "turn_active": False,
        "cwd": str(WORKSPACE),
        "label": "Setup test",
        "tty": "setup",
        "started_at": now - 5,
        "turn_started_at": now - 5,
        "task_title": "Verify Codex task notifications are configured correctly",
        "current_step": "Configuration test completed",
        "result_summary": "Feishu can receive interactive status cards",
        "final_duration_seconds": 5,
    }
    event = {"id": "setup-test-%s" % now, "kind": "completed", "created_at": now,
             "state": state}
    message_id = send_new_card(render_card(event, monitor=False), event["id"], load_config())
    print("Test notification sent: %s" % message_id)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: notifier.py hook|start|exit|worker|ensure-worker|drain|status|doctor|test"
        )
    command = sys.argv[1]
    if command == "hook":
        hook_complete()
    elif command == "start":
        lifecycle_start()
    elif command == "exit":
        lifecycle_exit(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
    elif command == "worker":
        worker()
    elif command == "ensure-worker":
        ensure_worker()
    elif command == "drain":
        ensure_dirs(); drain()
    elif command == "status":
        print_status()
    elif command == "doctor":
        raise SystemExit(0 if doctor() else 1)
    elif command == "test":
        send_test_notification()
    else:
        raise SystemExit("unknown command: " + command)


if __name__ == "__main__":
    main()
