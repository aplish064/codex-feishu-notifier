import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "notifier.py"
SPEC = importlib.util.spec_from_file_location("task_notifier", MODULE_PATH)
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


def event(kind, status=None):
    return {
        "id": "event-123",
        "kind": kind,
        "created_at": 370,
        "state": {
            "instance_id": "session-1",
            "status": status or ("running" if kind == "started" else kind),
            "active": kind not in {"stopped", "closed"},
            "turn_active": kind == "started",
            "cwd": "/workspace/sample-project",
            "label": "sample-project",
            "tty": "/dev/pts/9",
            "started_at": 100,
            "turn_started_at": 100,
            "turn_id": "turn-1",
            "last_started_turn_id": "turn-1",
            "task_title": "整理人才数据并生成筛选结果",
            "result_summary": "已完成人才数据清洗和结果汇总",
        },
    }


class CardTests(unittest.TestCase):
    def test_concise_title_accepts_null_agent_message(self):
        self.assertEqual(notifier.concise_title(None), "")

    def test_status_styles_use_card_2(self):
        expected = {
            "started": ("blue", "Codex 任务已启动"),
            "completed": ("green", "Codex 任务已完成"),
            "stopped": ("red", "Codex 任务意外中断"),
            "archived": ("grey", "Codex Goal 历史卡片"),
        }
        for kind, (template, title) in expected.items():
            with self.subTest(kind=kind):
                card = notifier.render_card(event(kind))
                self.assertEqual(card["schema"], "2.0")
                self.assertTrue(card["config"]["update_multi"])
                self.assertEqual(card["header"]["template"], template)
                self.assertEqual(card["header"]["title"]["content"], title)
                self.assertEqual(len(card["body"]["elements"]), 4)

    def test_dynamic_content_is_escaped(self):
        data = event("completed")
        data["state"]["task_title"] = "修复 [x](https://bad) *important*"
        card = notifier.render_card(data)
        focus = card["body"]["elements"][0]["columns"][0]["elements"][1]["content"]
        self.assertNotIn("[x](https://bad)", focus)
        self.assertIn("&#91;x&#93;", focus)

    def test_long_organized_goal_is_not_truncated_in_card_body(self):
        data = event("started")
        data["state"]["task_title"] = "整理" + "人才数据字段" * 30 + "并输出完整报告"
        card = notifier.render_card(data)
        focus = card["body"]["elements"][0]["columns"][0]["elements"][1]["content"]
        self.assertIn("并输出完整报告", focus)
        self.assertFalse(focus.endswith("…"))

    def test_organized_goal_removes_salutation_and_first_person_prefix(self):
        goal = notifier.organized_task_goal(
            "山主，我会完善学校人员发现、多源履历采集和验收网页。"
        )
        self.assertEqual(goal, "完善学校人员发现、多源履历采集和验收网页。")

    def test_manual_abort_shows_user_stop_reason(self):
        data = event("stopped")
        data["state"]["abort_reason"] = "interrupted"
        card = notifier.render_card(data)
        metrics = card["body"]["elements"][1]["columns"]
        values = [column["elements"][0]["content"] for column in metrics]
        labels = [column["elements"][1]["content"] for column in metrics]
        self.assertIn("**用户停止**", values)
        self.assertTrue(any("中断原因" in label for label in labels))

    def test_goal_card_uses_current_stage_elapsed_time(self):
        data = event("started")
        data["created_at"] = 370
        data["state"].update({
            "goal_id": "goal-1",
            "goal_created_at_ms": 1000,
            "turn_started_at": 340,
            "turn_duration_seconds": None,
        })

        card = notifier.render_card(data)
        metrics = card["body"]["elements"][1]["columns"]
        values = [column["elements"][0]["content"] for column in metrics]
        labels = [column["elements"][1]["content"] for column in metrics]

        self.assertIn("**30s**", values)
        self.assertTrue(any("当前阶段耗时" in label for label in labels))
        self.assertFalse(any("本轮耗时" in label for label in labels))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_home = Path(self.tempdir.name)
        self.state_patch = mock.patch.object(notifier, "STATE_HOME", self.state_home)
        self.config_patch = mock.patch.object(notifier, "CONFIG_PATH", self.state_home / "config.env")
        self.workspace_patch = mock.patch.object(notifier, "WORKSPACE", Path("/workspace"))
        self.state_patch.start()
        self.config_patch.start()
        self.workspace_patch.start()
        notifier.ensure_dirs()

    def tearDown(self):
        self.config_patch.stop()
        self.state_patch.stop()
        self.workspace_patch.stop()
        self.tempdir.cleanup()

    def write_session(self, data):
        notifier.atomic_json(notifier.session_path("session-1"), data["state"])

    def create_goals_db(self, rows):
        path = self.state_home / "goals.sqlite"
        connection = sqlite3.connect(str(path))
        connection.execute("""
            CREATE TABLE thread_goals (
                thread_id TEXT PRIMARY KEY NOT NULL,
                goal_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                token_budget INTEGER,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                time_used_seconds INTEGER NOT NULL DEFAULT 0,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
        """)
        connection.executemany(
            "INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        connection.commit()
        connection.close()
        return path

    def test_start_creates_and_pins_one_monitor_card(self):
        data = event("started")
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card", return_value="om_monitor") as send, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(data, {})

        self.assertEqual(send.call_count, 1)
        pin.assert_called_once_with("om_monitor", {})
        unpin.assert_not_called()
        urgent.assert_called_once_with("om_monitor", {})
        patch.assert_not_called()
        saved = notifier.read_json(notifier.session_path("session-1"))
        card = saved["turn_cards"]["turn-1"]
        self.assertEqual(card["message_id"], "om_monitor")
        self.assertIn("pinned_at", card)

    def test_completion_updates_monitor_without_node_notification(self):
        data = event("completed")
        data["state"].update({"message_id": "om_monitor", "pinned_at": 200})
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card", return_value="om_node") as send, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(data, {})

        patch.assert_called_once()
        pin.assert_not_called()
        unpin.assert_called_once_with("om_monitor", {})
        urgent.assert_called_once_with("om_monitor", {})
        send.assert_not_called()
        saved = notifier.read_json(notifier.session_path("session-1"))
        card = saved["turn_cards"]["turn-1"]
        self.assertIsNone(card["pinned_at"])
        self.assertIn("unpinned_at", card)

    def test_clean_close_after_completion_does_not_send_duplicate_node(self):
        data = event("closed", status="completed")
        data["state"].update({"active": False, "message_id": "om_monitor", "pinned_at": None})
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card") as send, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(data, {})

        patch.assert_called_once()
        pin.assert_not_called()
        unpin.assert_not_called()
        urgent.assert_not_called()
        send.assert_not_called()

    def test_completion_never_sends_node_notification(self):
        data = event("completed")
        data["state"].update({"message_id": "om_monitor", "pinned_at": 200})
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card") as send, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message"), \
             mock.patch.object(notifier, "patch_card"):
            notifier.deliver_event(data, {"NOTIFY_ON_COMPLETED": "true"})

        unpin.assert_called_once()
        send.assert_not_called()

    def test_interruption_updates_monitor_unpins_and_urgents_existing_card(self):
        data = event("stopped")
        data["state"].update({"message_id": "om_monitor", "pinned_at": 200})
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card", return_value="om_alert") as send, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(data, {})

        patch.assert_called_once()
        pin.assert_not_called()
        unpin.assert_called_once_with("om_monitor", {})
        urgent.assert_called_once_with("om_monitor", {})
        send.assert_not_called()

    def test_progress_never_urgents_even_when_card_renders_running(self):
        data = event("progress", status="running")
        data["state"].update({"message_id": "om_monitor", "pinned_at": 200})
        self.write_session(data)
        with mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "pin_message"), \
             mock.patch.object(notifier, "patch_card"):
            notifier.deliver_event(data, {})

        urgent.assert_not_called()

    def test_delayed_completion_never_urgents_while_goal_is_active(self):
        data = event("completed", status="running")
        data["state"].update({
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
            "turn_cards": {"goal:goal-1": {
                "message_id": "om_goal", "pinned_at": 200,
                "last_urgent_node": "goal:goal-1:started",
            }},
        })
        self.write_session(data)

        with mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(data, {})

        patch.assert_called_once()
        pin.assert_not_called()
        urgent.assert_not_called()

    def test_urgent_can_be_disabled_per_node(self):
        for kind in ("started", "completed", "stopped"):
            with self.subTest(kind=kind):
                data = event(kind)
                data["state"].update({"message_id": "om_monitor", "pinned_at": 200})
                self.write_session(data)
                config = {"URGENT_ON_%s" % kind.upper(): "false"}
                with mock.patch.object(notifier, "urgent_message") as urgent, \
                     mock.patch.object(notifier, "pin_message"), \
                     mock.patch.object(notifier, "unpin_message"), \
                     mock.patch.object(notifier, "patch_card"):
                    notifier.deliver_event(data, config)
                urgent.assert_not_called()

    def test_same_turn_node_is_not_urgented_twice(self):
        data = event("completed")
        data["state"].update({
            "message_id": "om_monitor", "pinned_at": None,
            "turn_id": "turn-1", "last_urgent_node": "turn-1:completed",
        })
        self.write_session(data)
        with mock.patch.object(notifier, "urgent_message") as urgent, \
             mock.patch.object(notifier, "patch_card"):
            notifier.deliver_event(data, {})

        urgent.assert_not_called()

    def test_urgent_failure_remains_retryable_without_repinning(self):
        data = event("started")
        data["state"]["turn_id"] = "turn-1"
        self.write_session(data)
        with mock.patch.object(notifier, "send_new_card", return_value="om_monitor"), \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "patch_card") as patch, \
             mock.patch.object(notifier, "urgent_message", side_effect=RuntimeError("missing scope")):
            with self.assertRaisesRegex(RuntimeError, "missing scope"):
                notifier.deliver_event(data, {})

        saved = notifier.read_json(notifier.session_path("session-1"))
        card = saved["turn_cards"]["turn-1"]
        self.assertEqual(card["message_id"], "om_monitor")
        self.assertIn("pinned_at", card)
        self.assertNotIn("last_urgent_node", card)

        with mock.patch.object(notifier, "pin_message") as retry_pin, \
             mock.patch.object(notifier, "patch_card") as retry_patch, \
             mock.patch.object(notifier, "urgent_message") as retry_urgent:
            notifier.deliver_event(data, {})

        pin.assert_called_once()
        patch.assert_not_called()
        retry_pin.assert_not_called()
        retry_patch.assert_called_once()
        retry_urgent.assert_called_once_with("om_monitor", {})

    def test_next_turn_creates_a_different_card(self):
        first = event("completed")
        first["state"].update({
            "turn_cards": {
                "turn-1": {
                    "message_id": "om_first",
                    "pinned_at": None,
                    "last_urgent_node": "turn-1:completed",
                }
            }
        })
        self.write_session(first)

        second = event("started")
        second["id"] = "event-456"
        second["state"].update({
            "turn_id": "turn-2",
            "last_started_turn_id": "turn-2",
            "turn_cards": first["state"]["turn_cards"],
        })
        for key in ("message_id", "pinned_at", "unpinned_at", "last_urgent_node"):
            second["state"].pop(key, None)
        notifier.atomic_json(notifier.session_path("session-1"), second["state"])

        with mock.patch.object(notifier, "send_new_card", return_value="om_second") as send, \
             mock.patch.object(notifier, "pin_message"), \
             mock.patch.object(notifier, "urgent_message"), \
             mock.patch.object(notifier, "patch_card") as patch:
            notifier.deliver_event(second, {})

        send.assert_called_once()
        self.assertEqual(
            send.call_args.args[1],
            notifier.turn_card_idempotency_key("session-1", "turn-2"),
        )
        patch.assert_not_called()
        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["turn_cards"]["turn-1"]["message_id"], "om_first")
        self.assertEqual(saved["turn_cards"]["turn-2"]["message_id"], "om_second")

    def test_delayed_previous_turn_event_updates_previous_card(self):
        latest = event("started")
        latest["state"].update({
            "turn_id": "turn-2",
            "last_started_turn_id": "turn-2",
            "turn_cards": {
                "turn-1": {"message_id": "om_first", "pinned_at": None},
                "turn-2": {"message_id": "om_second", "pinned_at": 300},
            },
        })
        self.write_session(latest)

        delayed = event("completed")
        delayed["state"].update({
            "turn_id": "turn-1",
            "last_started_turn_id": "turn-1",
            "updated_at": 250,
        })
        with mock.patch.object(notifier, "patch_card") as patch, \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier, "urgent_message"):
            notifier.deliver_event(delayed, {})

        self.assertEqual(patch.call_args.args[0], "om_first")
        unpin.assert_not_called()

    def test_running_legacy_card_is_persisted_into_turn_cards(self):
        data = event("progress", status="running")
        data["state"].update({"message_id": "om_legacy", "pinned_at": 200})
        self.write_session(data)

        with mock.patch.object(notifier, "patch_card"), \
             mock.patch.object(notifier, "pin_message"):
            notifier.deliver_event(data, {})

        saved = notifier.read_json(notifier.session_path("session-1"))
        card = saved["turn_cards"]["turn-1"]
        self.assertEqual(card["message_id"], "om_legacy")
        self.assertEqual(card["pinned_at"], 200)

    def test_signal_marks_zero_exit_as_stopped(self):
        data = event("started")
        self.write_session(data)
        with mock.patch.dict(os.environ, {"CODEX_TASK_INSTANCE_ID": "session-1"}), \
             mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.lifecycle_exit(0, "TERM")

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "stopped")
        self.assertEqual(saved["signal"], "TERM")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "stopped")

    def test_idle_terminal_exit_sends_nothing(self):
        data = event("started")
        data["state"].update({"status": "idle", "turn_active": False})
        self.write_session(data)
        with mock.patch.dict(os.environ, {"CODEX_TASK_INSTANCE_ID": "session-1"}), \
             mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.lifecycle_exit(0, "")

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "idle")
        self.assertFalse(saved["active"])
        enqueue.assert_not_called()

    def test_terminal_start_only_registers_idle_state(self):
        env = {
            "CODEX_TASK_INSTANCE_ID": "session-1",
            "CODEX_TASK_CWD": "/workspace",
            "CODEX_TASK_NAME": "Us",
            "CODEX_TASK_WRAPPER_PID": "123",
            "CODEX_TASK_STARTED_AT": "100",
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(notifier, "ensure_worker") as worker, \
             mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.lifecycle_start()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "idle")
        self.assertFalse(saved["turn_active"])
        worker.assert_called_once()
        enqueue.assert_not_called()

    def test_next_turn_waits_for_codex_to_organize_task_goal(self):
        data = event("completed")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text(
            '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-2","started_at":400}}\n'
            '{"type":"response_item","payload":{"type":"message","role":"user","content":'
            '[{"type":"input_text","text":"分析下一批人才数据"}]}}\n',
            encoding="utf-8",
        )
        data["state"].update({
            "active": True,
            "managed": True,
            "thread_id": "thread-1",
            "rollout_path": str(rollout),
            "rollout_offset": 0,
            "pinned_at": None,
        })
        self.write_session(data)
        with mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.sweep_turn_starts()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["task_title"], "正在整理任务目标")
        self.assertTrue(saved["task_goal_pending"])
        self.assertNotIn("分析下一批人才数据", saved["task_title"])
        self.assertEqual(saved["last_started_turn_id"], "turn-2")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "started")

    def test_commentary_and_tool_action_update_without_raw_input(self):
        data = event("completed")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2", "started_at": 400}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "分析人才数据"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "正在检查数据字段和缺失值"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "token=very-secret"}},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "active": True, "managed": True, "thread_id": "thread-1",
            "rollout_path": str(rollout), "rollout_offset": 0,
            "started_at": 100, "turn_active": False,
        })
        self.write_session(data)
        with mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["task_title"], "正在检查数据字段和缺失值")
        self.assertEqual(saved["task_goal_source"], "assistant_commentary")
        self.assertEqual(saved["current_step"], "正在检查数据字段和缺失值")
        self.assertEqual(saved["recent_action"], "正在运行命令或检查结果")
        self.assertNotIn("very-secret", json.dumps(saved))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "started")

    def test_first_commentary_becomes_complete_organized_goal(self):
        data = event("completed")
        long_goal = "我会整理人才数据库的全部字段、修复缺失值和重复记录，并生成带筛选依据的完整候选人报告。" * 3
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2", "started_at": 400}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "这是很长且不应显示的用户原话"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": long_goal}]}},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "active": True, "managed": True, "thread_id": "thread-1",
            "rollout_path": str(rollout), "rollout_offset": 0,
            "started_at": 100, "turn_active": False,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "enqueue"):
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertFalse(saved["task_title"].startswith("我会"))
        self.assertIn("完整候选人报告", saved["task_title"])
        self.assertGreater(len(saved["task_title"]), 110)
        self.assertNotIn("用户原话", saved["task_title"])

    def test_turn_aborted_marks_current_turn_stopped(self):
        data = event("started")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {
                "type": "turn_aborted", "turn_id": "turn-1",
                "reason": "interrupted", "started_at": 100,
                "completed_at": 130, "duration_ms": 30000,
            }},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=130):
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "stopped")
        self.assertFalse(saved["turn_active"])
        self.assertTrue(saved["active"])
        self.assertEqual(saved["abort_reason"], "interrupted")
        self.assertEqual(saved["final_duration_seconds"], 30)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "stopped")
        self.assertEqual(enqueue.call_args.args[1]["turn_id"], "turn-1")

    def test_abort_is_not_lost_when_next_turn_starts_in_same_sweep(self):
        data = event("started")
        data["state"].update({"abort_reason": "interrupted", "aborted_at": 130})
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {
                "type": "turn_aborted", "turn_id": "turn-1",
                "reason": "interrupted", "completed_at": 130,
                "duration_ms": 30000,
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-2", "started_at": 131,
            }},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=131):
            notifier.sweep_turn_events()

        self.assertEqual([call.args[0] for call in enqueue.call_args_list], ["stopped", "started"])
        self.assertEqual(enqueue.call_args_list[0].args[1]["turn_id"], "turn-1")
        self.assertEqual(enqueue.call_args_list[1].args[1]["turn_id"], "turn-2")
        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["turn_id"], "turn-2")
        self.assertNotIn("abort_reason", saved)
        self.assertNotIn("aborted_at", saved)

    def test_stale_abort_does_not_stop_newer_turn(self):
        data = event("started")
        data["state"].update({"turn_id": "turn-2", "last_started_turn_id": "turn-2"})
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text(json.dumps({
            "type": "event_msg", "payload": {
                "type": "turn_aborted", "turn_id": "turn-1",
                "reason": "interrupted", "completed_at": 130,
            },
        }) + "\n", encoding="utf-8")
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "running")
        self.assertTrue(saved["turn_active"])
        enqueue.assert_not_called()

    def test_goal_database_holds_multiple_goals_and_matches_rollout(self):
        db_path = self.create_goals_db([
            ("thread-1", "goal-1", "secret objective", "active", None, 10, 20, 100000, 120000),
            ("thread-2", "goal-2", "another objective", "complete", None, 30, 40, 200000, 240000),
        ])
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text(json.dumps({
            "type": "session_meta", "payload": {
                "session_id": "thread-1", "parent_thread_id": None,
                "cwd": "/workspace", "timestamp": "1970-01-01T00:01:40Z",
            },
        }) + "\n", encoding="utf-8")

        with mock.patch.object(notifier, "GOALS_DB_PATH", db_path):
            record = notifier.goal_record_for_rollout(rollout)

        self.assertEqual(record["goal_id"], "goal-1")
        self.assertEqual(record["goal_status"], "active")
        self.assertNotIn("objective", record)

    def test_active_goal_task_complete_stays_running(self):
        data = event("started")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text(json.dumps({
            "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-1",
                "completed_at": 130, "duration_ms": 30000,
                "last_agent_message": "阶段工作已完成",
            },
        }) + "\n", encoding="utf-8")
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
            "goal_id": "goal-1", "goal_thread_id": "thread-1",
            "goal_status": "active", "goal_running": True,
            "goal_created_at_ms": 100000,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value={
            "goal_thread_id": "thread-1", "goal_id": "goal-1",
            "goal_status": "active", "goal_token_budget": None,
            "goal_tokens_used": 10, "goal_time_used_seconds": 20,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 130000,
        }), mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=130):
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "running")
        self.assertFalse(saved["turn_active"])
        self.assertTrue(saved["goal_running"])
        self.assertIn("等待 Goal 自动续跑", saved["current_step"])
        self.assertEqual(enqueue.call_args.args[0], "progress")

    def test_goal_turns_reuse_goal_card_without_realerting(self):
        first = event("started")
        first["state"].update({
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
            "turn_cards": {"goal:goal-1": {
                "message_id": "om_goal", "pinned_at": 200,
                "last_urgent_node": "goal:goal-1:turn-old:stopped",
            }},
        })
        self.write_session(first)
        second = event("started")
        second["id"] = "event-turn-2"
        second["state"].update({
            "turn_id": "turn-2", "last_started_turn_id": "turn-2",
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
            "turn_cards": first["state"]["turn_cards"],
        })
        notifier.atomic_json(notifier.session_path("session-1"), second["state"])

        with mock.patch.object(notifier, "send_new_card") as send, \
             mock.patch.object(notifier, "patch_card") as patch, \
             mock.patch.object(notifier, "pin_message") as pin, \
             mock.patch.object(notifier, "urgent_message") as urgent:
            notifier.deliver_event(second, {})

        send.assert_not_called()
        patch.assert_called_once()
        self.assertEqual(patch.call_args.args[0], "om_goal")
        pin.assert_not_called()
        urgent.assert_not_called()

    def test_large_backlog_collapses_intermediate_lifecycle_events(self):
        data = event("started")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {
                "type": "turn_aborted", "turn_id": "turn-1",
                "reason": "interrupted", "completed_at": 130,
                "duration_ms": 30000,
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-2", "started_at": 131,
            }},
        ] + [{"type": "noop", "payload": {}}] * (notifier.MAX_LIVE_SWEEP_RECORDS + 1)
        rollout.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value={}), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=131):
            notifier.sweep_turn_events()

        self.assertEqual([call.args[0] for call in enqueue.call_args_list], ["started"])
        self.assertEqual(enqueue.call_args.args[1]["turn_id"], "turn-2")

    def test_goal_continuation_updates_title_to_current_stage(self):
        data = event("completed")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2", "started_at": 400}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Another language model started to solve this problem"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "正在核查本轮实现"}]}},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "active": True, "managed": True, "thread_id": "thread-1",
            "rollout_path": str(rollout), "rollout_offset": 0,
            "started_at": 100, "turn_active": False,
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
            "goal_task_title": "完善学校人员发现与人才履历采集",
        })
        self.write_session(data)

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value={
            "goal_thread_id": "thread-1", "goal_id": "goal-1",
            "goal_status": "active", "goal_token_budget": None,
            "goal_tokens_used": 10, "goal_time_used_seconds": 20,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 130000,
        }), mock.patch.object(notifier, "enqueue"):
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["task_title"], "正在核查本轮实现")
        self.assertEqual(saved["current_step"], "正在核查本轮实现")
        self.assertFalse(saved["task_goal_pending"])

    def test_active_goal_reads_stage_updates_from_root_rollout(self):
        data = event("completed")
        terminal_rollout = self.state_home / "rollout-child.jsonl"
        terminal_rollout.write_text("{}\n", encoding="utf-8")
        goal_rollout = self.state_home / "rollout-goal-root.jsonl"
        records = [
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "goal-turn-2", "started_at": 400,
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "commentary",
                "content": [{"type": "output_text", "text": "正在推进 Goal 根线程的新阶段"}],
            }},
        ]
        goal_rollout.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        data["state"].update({
            "active": True, "managed": True, "thread_id": "child-thread",
            "rollout_path": str(terminal_rollout),
            "rollout_offset": terminal_rollout.stat().st_size + 1000,
            "started_at": 100, "turn_active": False,
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
        })
        self.write_session(data)
        goal_record = {
            "goal_thread_id": "goal-root", "goal_id": "goal-1",
            "goal_status": "active", "goal_token_budget": None,
            "goal_tokens_used": 10, "goal_time_used_seconds": 20,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 130000,
        }

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value=goal_record), \
             mock.patch.object(notifier, "find_rollout_path", return_value=goal_rollout), \
             mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.sweep_turn_events()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["rollout_path"], str(terminal_rollout))
        self.assertEqual(saved["rollout_offset"], terminal_rollout.stat().st_size)
        self.assertEqual(saved["goal_rollout_path"], str(goal_rollout))
        self.assertEqual(saved["goal_rollout_thread_id"], "goal-root")
        self.assertEqual(saved["goal_rollout_offset"], goal_rollout.stat().st_size)
        self.assertEqual(saved["turn_id"], "goal-turn-2")
        self.assertTrue(saved["turn_active"])
        self.assertEqual(saved["task_title"], "正在推进 Goal 根线程的新阶段")
        self.assertEqual(enqueue.call_args.args[0], "started")

    def test_goal_complete_transition_enqueues_completion(self):
        data = event("started")
        data["state"].update({
            "managed": True, "goal_id": "goal-1", "goal_status": "active",
            "goal_running": True, "goal_created_at_ms": 100000,
            "rollout_path": str(self.state_home / "rollout.jsonl"),
        })
        Path(data["state"]["rollout_path"]).write_text("{}\n", encoding="utf-8")
        self.write_session(data)
        record = {
            "goal_thread_id": "thread-1", "goal_id": "goal-1",
            "goal_status": "complete", "goal_token_budget": None,
            "goal_tokens_used": 20, "goal_time_used_seconds": 30,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 140000,
        }

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value=record), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=140):
            notifier.sweep_goal_statuses()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "completed")
        self.assertFalse(saved["goal_running"])
        self.assertEqual(saved["final_duration_seconds"], 40)
        self.assertEqual(enqueue.call_args.args[0], "completed")

    def test_goal_blocked_transition_enqueues_attention_node(self):
        data = event("started")
        data["state"].update({
            "managed": True, "goal_id": "goal-1", "goal_status": "active",
            "goal_running": True, "goal_created_at_ms": 100000,
            "rollout_path": str(self.state_home / "rollout.jsonl"),
        })
        Path(data["state"]["rollout_path"]).write_text("{}\n", encoding="utf-8")
        self.write_session(data)
        record = {
            "goal_thread_id": "thread-1", "goal_id": "goal-1",
            "goal_status": "blocked", "goal_token_budget": None,
            "goal_tokens_used": 20, "goal_time_used_seconds": 30,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 140000,
        }

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value=record), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=140):
            notifier.sweep_goal_statuses()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "blocked")
        self.assertFalse(saved["goal_running"])
        self.assertEqual(enqueue.call_args.args[0], "stopped")

    def test_missing_goal_database_falls_back_to_turn_card(self):
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text(json.dumps({
            "type": "session_meta", "payload": {
                "session_id": "thread-1", "cwd": "/workspace",
                "timestamp": "1970-01-01T00:01:40Z",
            },
        }) + "\n", encoding="utf-8")
        with mock.patch.object(notifier, "GOALS_DB_PATH", self.state_home / "missing.sqlite"):
            self.assertEqual(notifier.goal_record_for_rollout(rollout), {})
        self.assertEqual(notifier.card_key_from_state(event("started")["state"]), "turn-1")

    def test_short_completion_always_updates_and_unpins(self):
        data = event("started")
        data["state"].update({
            "turn_started_at": 100, "message_id": "om_monitor", "pinned_at": 101,
            "task_title": "整理后的完整任务目标",
            "task_goal_source": "assistant_commentary",
        })
        self.write_session(data)
        payload = {
            "cwd": data["state"]["cwd"], "thread-id": "thread-1",
            "turn-id": "turn-1", "started_at": 100, "completed_at": 110,
            "duration_ms": 10000, "last-assistant-message": "已完成检查",
            "input-messages": ["这是一段不应覆盖目标的用户原话"],
        }
        with mock.patch.object(notifier, "parse_hook_payload", return_value=payload), \
             mock.patch.dict(os.environ, {"CODEX_TASK_INSTANCE_ID": "session-1"}), \
             mock.patch.object(notifier, "find_rollout_path", return_value=None), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=110):
            notifier.hook_complete()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertFalse(saved["turn_active"])
        self.assertEqual(saved["final_duration_seconds"], 10)
        self.assertEqual(saved["task_title"], "整理后的完整任务目标")
        self.assertNotIn("用户原话", saved["task_title"])
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "completed")

    def test_notify_hook_treats_active_goal_turn_completion_as_progress(self):
        data = event("started")
        data["state"].update({
            "managed": True,
            "goal_id": "goal-1", "goal_status": "active", "goal_running": True,
        })
        self.write_session(data)
        rollout = self.state_home / "rollout-thread-1.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        payload = {
            "cwd": data["state"]["cwd"], "thread-id": "thread-1",
            "turn-id": "turn-1", "started_at": 100, "completed_at": 130,
            "duration_ms": 30000, "last-assistant-message": "本轮阶段已完成",
        }
        goal_record = {
            "goal_thread_id": "thread-1", "goal_id": "goal-1",
            "goal_status": "active", "goal_token_budget": None,
            "goal_tokens_used": 20, "goal_time_used_seconds": 30,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 130000,
        }

        with mock.patch.object(notifier, "parse_hook_payload", return_value=payload), \
             mock.patch.dict(os.environ, {"CODEX_TASK_INSTANCE_ID": "session-1"}), \
             mock.patch.object(notifier, "find_rollout_path", return_value=rollout), \
             mock.patch.object(notifier, "goal_record_for_rollout", return_value=goal_record), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=130):
            notifier.hook_complete()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertEqual(saved["status"], "running")
        self.assertFalse(saved["turn_active"])
        self.assertTrue(saved["goal_running"])
        self.assertEqual(saved["turn_duration_seconds"], 30)
        self.assertEqual(saved["final_duration_seconds"], 30)
        self.assertEqual(saved.get("rollout_offset", 0), 0)
        self.assertIn("等待 Goal 自动续跑", saved["current_step"])
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "progress")

    def test_notify_hook_ignores_active_goal_descendant_completion(self):
        data = event("started")
        data["state"].update({
            "managed": True, "thread_id": "child-thread",
            "rollout_path": "/terminal-rollout.jsonl",
            "goal_thread_id": "goal-root", "goal_id": "goal-1",
            "goal_status": "active", "goal_running": True,
            "task_title": "根 Goal 当前阶段", "current_step": "正在执行根 Goal",
        })
        self.write_session(data)
        child_rollout = self.state_home / "rollout-child.jsonl"
        child_rollout.write_text("{}\n", encoding="utf-8")
        payload = {
            "cwd": data["state"]["cwd"], "thread-id": "child-thread",
            "turn-id": "child-turn", "completed_at": 130,
            "duration_ms": 30000, "last-assistant-message": "子线程已完成",
        }
        goal_record = {
            "goal_thread_id": "goal-root", "goal_id": "goal-1",
            "goal_status": "active", "goal_token_budget": None,
            "goal_tokens_used": 20, "goal_time_used_seconds": 30,
            "goal_created_at_ms": 100000, "goal_updated_at_ms": 130000,
        }

        with mock.patch.object(notifier, "parse_hook_payload", return_value=payload), \
             mock.patch.dict(os.environ, {"CODEX_TASK_INSTANCE_ID": "session-1"}), \
             mock.patch.object(notifier, "find_rollout_path", return_value=child_rollout), \
             mock.patch.object(notifier, "goal_record_for_rollout", return_value=goal_record), \
             mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.hook_complete()

        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertTrue(saved["turn_active"])
        self.assertEqual(saved["task_title"], "根 Goal 当前阶段")
        self.assertEqual(saved["current_step"], "正在执行根 Goal")
        enqueue.assert_not_called()

    def test_elapsed_time_does_not_generate_timer_only_card_updates(self):
        data = event("started")
        data["state"].update({"managed": True, "last_elapsed_enqueue_at": 100})
        self.write_session(data)
        with mock.patch.object(notifier, "enqueue") as enqueue:
            notifier.sweep_elapsed()
            enqueue.assert_not_called()

    def test_stale_pin_sweep_keeps_only_current_active_card(self):
        data = event("started")
        data["state"]["turn_cards"] = {
            "turn-old-1": {"message_id": "om_old_1", "pinned_at": 100},
            "turn-old-2": {"message_id": "om_old_2", "pinned_at": 110},
            "turn-1": {"message_id": "om_current", "pinned_at": 120},
        }
        self.write_session(data)

        with mock.patch.object(notifier, "load_config", return_value={"ENABLED": "true"}), \
             mock.patch.object(notifier, "unpin_message") as unpin, \
             mock.patch.object(notifier.time, "time", return_value=130):
            notifier.sweep_stale_card_pins()

        self.assertEqual(
            {call.args[0] for call in unpin.call_args_list}, {"om_old_1", "om_old_2"}
        )
        saved = notifier.read_json(notifier.session_path("session-1"))
        self.assertIsNone(saved["turn_cards"]["turn-old-1"]["pinned_at"])
        self.assertIsNone(saved["turn_cards"]["turn-old-2"]["pinned_at"])
        self.assertEqual(saved["turn_cards"]["turn-1"]["pinned_at"], 120)

    def test_completion_is_not_lost_when_next_turn_starts_in_same_sweep(self):
        data = event("started")
        rollout = self.state_home / "rollout-thread-1.jsonl"
        records = [
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-1",
                "completed_at": 130, "duration_ms": 30000,
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-2", "started_at": 131,
            }},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        data["state"].update({
            "managed": True, "rollout_path": str(rollout), "rollout_offset": 0,
        })
        self.write_session(data)

        with mock.patch.object(notifier, "goal_record_for_rollout", return_value={}), \
             mock.patch.object(notifier, "enqueue") as enqueue, \
             mock.patch.object(notifier.time, "time", return_value=131):
            notifier.sweep_turn_events()

        self.assertEqual([call.args[0] for call in enqueue.call_args_list], ["completed", "started"])
        self.assertEqual(enqueue.call_args_list[0].args[1]["turn_id"], "turn-1")
        self.assertEqual(enqueue.call_args_list[1].args[1]["turn_id"], "turn-2")

    def test_rollout_assignment_does_not_reuse_active_terminal_claim(self):
        sessions_home = self.state_home / "rollouts"
        sessions_home.mkdir()
        first = sessions_home / "rollout-first.jsonl"
        second = sessions_home / "rollout-second.jsonl"
        for path, thread_id, timestamp in (
            (first, "thread-first", "1970-01-01T00:01:40+00:00"),
            (second, "thread-second", "1970-01-01T00:01:41+00:00"),
        ):
            path.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"session_id": thread_id, "cwd": "/workspace", "timestamp": timestamp},
            }) + "\n", encoding="utf-8")

        claimed = event("started")["state"]
        claimed.update({
            "instance_id": "claimed-session", "managed": True, "active": True,
            "rollout_path": str(first),
        })
        notifier.atomic_json(notifier.session_path("claimed-session"), claimed)
        candidate = event("started")["state"]
        candidate.update({
            "instance_id": "session-1", "managed": True, "active": True,
            "cwd": "/workspace", "started_at": 100,
        })

        with mock.patch.object(notifier, "SESSIONS_HOME", sessions_home), \
             mock.patch.object(notifier, "SHELL_SNAPSHOTS_HOME", self.state_home / "no-snapshots"):
            rollout, thread_id = notifier.assign_rollout(candidate)

        self.assertEqual(rollout, second)
        self.assertEqual(thread_id, "thread-second")

    def test_rollout_assignment_skips_path_claimed_by_active_terminal(self):
        sessions_home = self.state_home / "codex-sessions"
        sessions_home.mkdir()
        first = sessions_home / "rollout-first.jsonl"
        second = sessions_home / "rollout-second.jsonl"
        first.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"session_id": "thread-1", "cwd": "/workspace", "timestamp": "1970-01-01T00:01:40Z"},
        }) + "\n", encoding="utf-8")
        second.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"session_id": "thread-2", "cwd": "/workspace", "timestamp": "1970-01-01T00:01:45Z"},
        }) + "\n", encoding="utf-8")
        notifier.atomic_json(notifier.session_path("claimed"), {
            "instance_id": "claimed", "active": True, "rollout_path": str(first),
        })
        state = {
            "instance_id": "session-1", "active": True,
            "cwd": "/workspace", "started_at": 100,
        }
        with mock.patch.object(notifier, "SESSIONS_HOME", sessions_home), \
             mock.patch.object(notifier, "SHELL_SNAPSHOTS_HOME", self.state_home / "no-snapshots"):
            rollout, thread_id = notifier.assign_rollout(state)

        self.assertEqual(rollout, second)
        self.assertEqual(thread_id, "thread-2")


if __name__ == "__main__":
    unittest.main()
