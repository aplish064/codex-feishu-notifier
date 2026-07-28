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
    for name in ("sessions", "outbox", "sent", "logs"):
        (STATE_HOME / name).mkdir(parents=True, exist_ok=True)


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
    if state.get("goal_task_title"):
        return state["goal_task_title"]
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
    turn_started_at = int(
        state.get("turn_started_at")
        or payload.get("started_at")
        or state.get("last_completed_at", state.get("started_at", now))
    )
    thread_id = (
        payload.get("thread-id") or payload.get("thread_id")
        or payload.get("session-id") or payload.get("session_id")
    )
    rollout_path = find_rollout_path(thread_id)
    if thread_id:
        state["thread_id"] = str(thread_id)
    if rollout_path:
        state["rollout_path"] = str(rollout_path)
        state["rollout_offset"] = rollout_path.stat().st_size
        goal_record = goal_record_for_rollout(rollout_path)
        if goal_record:
            apply_goal_record(state, goal_record)

    active_goal = bool(state.get("goal_id") and state.get("goal_status") == "active")
    common = {
        "active": True,
        "turn_active": False,
        "updated_at": now,
        "last_completed_at": now,
        "turn_started_at": turn_started_at,
        "task_title": extract_task_title(payload, state),
        "result_summary": extract_result_summary(payload),
    }
    if active_goal:
        common.update({
            "status": "running",
            "goal_running": True,
            "current_step": "本轮已完成，等待 Goal 自动续跑",
            "final_duration_seconds": None,
        })
        event_kind = "progress"
    else:
        common.update({
            "status": "completed",
            "goal_running": False,
            "completed_at": int(payload.get("completed_at", now)),
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
    if status in {
        "completed", "stopped", "closed", "paused", "blocked",
        "usage_limited", "budget_limited", "archived",
    }:
        return status
    return fallback


def render_card(event, monitor=True):
    state = event["state"]
    kind = card_kind_for_state(state, event["kind"])
    styles = {
        "started": ("Codex 任务已启动", "运行中", "blue", "blue-50", "blue"),
        "completed": ("Codex 任务已完成", "已完成", "green", "green-50", "green"),
        "stopped": ("Codex 任务意外中断", "需检查", "red", "red-50", "red"),
        "closed": ("Codex 终端已关闭", "已关闭", "grey", "grey-50", "neutral"),
        "paused": ("Codex Goal 已暂停", "已暂停", "orange", "orange-50", "orange"),
        "blocked": ("Codex Goal 已阻塞", "需处理", "red", "red-50", "red"),
        "usage_limited": ("Codex Goal 用量受限", "需处理", "orange", "orange-50", "orange"),
        "budget_limited": ("Codex Goal 预算受限", "需处理", "orange", "orange-50", "orange"),
        "archived": ("Codex Goal 历史卡片", "已归档", "grey", "grey-50", "neutral"),
    }
    title, status_label, template, background, accent = styles.get(kind, styles["started"])
    if state.get("goal_id") and state.get("goal_created_at_ms"):
        started = int(state["goal_created_at_ms"]) // 1000
    else:
        started = int(state.get("turn_started_at", state.get("started_at", event["created_at"])))
    terminal = state.get("tmux_pane") or state.get("tty") or "unknown"
    task_title = state.get("task_title")
    if not task_title or (kind == "started" and task_title == state.get("label")):
        task_title = "等待 Codex 接收并执行任务"
    elapsed_seconds = state.get("final_duration_seconds")
    if elapsed_seconds is None:
        elapsed_seconds = event["created_at"] - started
    elapsed = duration_text(elapsed_seconds)
    project = Path(state.get("cwd", "unknown")).name
    metric_values = [(status_label, "状态"), (elapsed, "本轮耗时")]
    if kind == "stopped":
        if state.get("abort_reason"):
            reason = "用户停止" if state["abort_reason"] == "interrupted" else state["abort_reason"]
            metric_values.append((str(reason), "中断原因"))
        elif state.get("signal"):
            metric_values.append((str(state["signal"]), "中断信号"))
        else:
            metric_values.append((str(state.get("exit_code", "unknown")), "退出码"))
    elif kind in {"paused", "blocked", "usage_limited", "budget_limited"}:
        metric_values.append((status_label, "Goal 状态"))
    else:
        metric_values.append(("运行中" if state.get("active") else "已结束", "终端"))

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
    goal_id = state.get("goal_id")
    if goal_id:
        return "goal:%s" % goal_id
    return turn_key_from_state(state, fallback)


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
    existing_cards = latest.get("turn_cards", {})
    if (card_metadata
            and (not isinstance(existing_cards, dict) or card_key not in existing_cards)):
        save_turn_card_metadata(identifier, card_key, **card_metadata)
    message_id = card_metadata.get("message_id")
    if message_id:
        patch_card(message_id, render_card(live_event, monitor=True), config)
    else:
        message_id = send_new_card(
            render_card(live_event, monitor=True),
            turn_card_idempotency_key(identifier, card_key),
            config,
        )
        card_metadata["message_id"] = message_id
        save_turn_card_metadata(identifier, card_key, message_id=message_id)

    if live_event["kind"] == "started":
        if not card_metadata.get("pinned_at"):
            pin_message(message_id, config)
            pinned_at = int(time.time())
            card_metadata["pinned_at"] = pinned_at
            save_turn_card_metadata(identifier, card_key, pinned_at=pinned_at, unpinned_at=None)
    elif card_metadata.get("pinned_at"):
        unpin_message(message_id, config)
        unpinned_at = int(time.time())
        card_metadata["pinned_at"] = None
        card_metadata["unpinned_at"] = unpinned_at
        save_turn_card_metadata(identifier, card_key, pinned_at=None, unpinned_at=unpinned_at)

    # Use the original event kind: progress refreshes can render a running card,
    # but only real lifecycle nodes should alert the user's phone.
    if should_urgent_event(event, state, config):
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
    ):
        state.pop(key, None)


def migrate_current_card_to_goal(state, goal_id):
    old_key = turn_key_from_state(state)
    new_key = "goal:%s" % goal_id
    cards = state.get("turn_cards", {})
    if not old_key or not isinstance(cards, dict) or new_key in cards or old_key not in cards:
        return
    cards = dict(cards)
    cards[new_key] = cards.pop(old_key)
    state["turn_cards"] = cards


def goal_elapsed_seconds(state, now):
    created_at_ms = state.get("goal_created_at_ms")
    if created_at_ms:
        return max(0, now - int(created_at_ms) // 1000)
    return max(0, now - int(state.get("turn_started_at", now)))


def synchronize_goal_state(state, record, now):
    previous_goal_id = state.get("goal_id")
    previous_goal_status = state.get("goal_status")
    previous_status = state.get("status")
    goal_id = record["goal_id"]
    if previous_goal_id != goal_id:
        migrate_current_card_to_goal(state, goal_id)
        state.pop("goal_task_title", None)
        if state.get("task_goal_source") == "assistant_commentary":
            state["goal_task_title"] = state.get("task_title")
    apply_goal_record(state, record)
    if state.get("goal_task_title"):
        state["task_title"] = state["goal_task_title"]
        state["task_goal_pending"] = False
    goal_status = record["goal_status"]
    event_kind = ""

    if goal_status == "active":
        state["goal_running"] = True
        if previous_status in {
            "completed", "stopped", "paused", "blocked", "usage_limited", "budget_limited",
        }:
            state.update({
                "status": "running",
                "current_step": "Goal 正在继续执行",
                "final_duration_seconds": None,
            })
            event_kind = "started"
    elif goal_status == "complete":
        state.update({
            "status": "completed",
            "turn_active": False,
            "goal_running": False,
            "current_step": "Goal 已完成",
            "final_duration_seconds": goal_elapsed_seconds(state, now),
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
            "final_duration_seconds": goal_elapsed_seconds(state, now),
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
        rollout_path = Path(state.get("rollout_path", ""))
        if not rollout_path.is_file():
            continue
        record = goal_record_for_rollout(rollout_path)
        if not record:
            continue
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
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
        rollout_path = Path(state.get("rollout_path", ""))
        if not rollout_path.is_file():
            rollout_path, thread_id = assign_rollout(state)
            if not rollout_path:
                continue
            state["thread_id"] = thread_id
            state["rollout_path"] = str(rollout_path)
            state["rollout_offset"] = 0

        goal_record = goal_record_for_rollout(rollout_path)
        if goal_record:
            goal_event_kind = synchronize_goal_state(state, goal_record, now)
            if goal_event_kind:
                enqueue(goal_event_kind, state, {
                    "event-id": "goal-%s-%s" % (
                        goal_record["goal_updated_at_ms"], goal_event_kind
                    ),
                    "turn-id": state.get("turn_id"),
                })

        old_offset = int(state.get("rollout_offset", 0))
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
                ):
                    state.pop(key, None)
                active_goal_title = state.get("goal_task_title") if (
                    state.get("goal_id") and state.get("goal_status") == "active"
                ) else ""
                state.update({
                    "status": "running",
                    "turn_active": True,
                    "updated_at": now,
                    "turn_started_at": turn_started_at,
                    "turn_id": payload.get("turn_id"),
                    "last_started_turn_id": payload.get("turn_id"),
                    "task_title": active_goal_title or "正在整理任务目标",
                    "task_goal_source": "assistant_commentary" if active_goal_title else "pending",
                    "task_goal_pending": not bool(active_goal_title),
                    "current_step": "正在分析任务",
                    "recent_action": "",
                    "result_summary": "",
                    "final_duration_seconds": None,
                    "goal_running": bool(
                        state.get("goal_id") and state.get("goal_status") == "active"
                    ),
                })
                changed = True
                event_kind = "started"
                event_marker = line_offset
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
                        if state.get("goal_id") and state.get("goal_status") == "active":
                            state["goal_task_title"] = task_goal
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
                if state.get("goal_id") and state.get("goal_status") == "active":
                    state.update({
                        "status": "running",
                        "turn_active": False,
                        "goal_running": True,
                        "updated_at": now,
                        "last_completed_at": int(payload.get("completed_at", now)),
                        "current_step": "本轮已完成，等待 Goal 自动续跑",
                        "result_summary": concise_title(
                            payload.get("last_agent_message", ""), limit=110
                        ),
                        "final_duration_seconds": None,
                    })
                    completion_kind = "progress"
                else:
                    state.update({
                        "status": "completed",
                        "turn_active": False,
                        "goal_running": False,
                        "updated_at": now,
                        "last_completed_at": int(payload.get("completed_at", now)),
                        "completed_at": int(payload.get("completed_at", now)),
                        "current_step": "任务已完成",
                        "result_summary": concise_title(
                            payload.get("last_agent_message", ""), limit=110
                        ),
                        "final_duration_seconds": max(0, int(duration_ms) // 1000)
                        if duration_ms is not None else max(
                            0, now - int(state.get("turn_started_at", now))
                        ),
                    })
                    completion_kind = "completed"
                changed = True
                event_kind = ""
                event_marker = line_offset
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
                })
                changed = True
                event_kind = ""
                event_marker = line_offset
                enqueue("stopped", dict(state), {"turn-id": aborted_turn_id})

        state["rollout_path"] = str(rollout_path)
        state["rollout_offset"] = new_offset
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
    now = int(time.time())
    for path in (STATE_HOME / "sessions").glob("*.json"):
        state = read_json(path, {})
        if (not state.get("managed") or not state.get("active")
                or not (state.get("turn_active") or state.get("goal_running"))):
            continue
        if now - int(state.get("last_elapsed_enqueue_at", 0)) < 5:
            continue
        state["last_elapsed_enqueue_at"] = now
        state["updated_at"] = now
        atomic_json(path, state)
        enqueue("progress", state, {"event-id": "elapsed-%s" % (now // 5)})


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
