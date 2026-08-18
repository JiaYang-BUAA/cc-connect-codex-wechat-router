from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "notifier.py"
SPEC = importlib.util.spec_from_file_location("notifier", MODULE_PATH)
assert SPEC and SPEC.loader
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


class NotifierTests(unittest.TestCase):
    def test_health_status_exposes_only_operational_metadata(self):
        state = notifier.empty_state()
        state["pending"].append({"turn_id": "turn-1"})
        result = notifier.health_status(
            {"codex_submit_transport": "desktop-cdp"}, state
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pending_notifications"], 1)
        self.assertEqual(result["submit_transport"], "desktop-cdp")
        self.assertNotIn("token", result)

    def test_selftest_checks_router_token_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            token = "test-secret-token"
            connector_config = root / "cc-connect.toml"
            connector_config.write_text(
                """
[[projects]]
name = "yang"

[[projects.platforms]]
type = "weixin"

[projects.platforms.options]
codex_quote_router_token = "test-secret-token"
""".strip(),
                encoding="utf-8",
            )
            config.update(
                {
                    "cc_connect": str(root / "cc-connect.exe"),
                    "cc_project": "yang",
                    "cc_connect_config": str(connector_config),
                    "router_token": token,
                    "codex_submit_transport": "desktop-cdp",
                    "codex_desktop_cdp_url": "http://127.0.0.1:9335",
                    "codex_desktop_cdp_timeout_seconds": 3,
                }
            )
            completed = notifier.subprocess.CompletedProcess(
                args=[], returncode=0, stdout="cc-connect 1.4.1+qr10\n", stderr=""
            )
            client = mock.MagicMock()
            client.probe.return_value = {"requestExport": "request"}
            with (
                mock.patch.object(notifier.subprocess, "run", return_value=completed),
                mock.patch.object(notifier, "DesktopCdpClient", return_value=client),
            ):
                result = notifier.selftest(config, root / "state.json")
            self.assertTrue(result["ok"])
            self.assertNotIn(token, json.dumps(result))

    def test_save_state_skips_unchanged_disk_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            state = notifier.empty_state()
            original_replace = notifier.os.replace
            with mock.patch.object(
                notifier.os, "replace", side_effect=original_replace
            ) as replace:
                notifier.save_state(path, state)
                notifier.save_state(path, state)
            self.assertEqual(replace.call_count, 1)

    def test_desktop_cdp_submit_transport_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            config.update(
                {
                    "cc_connect": "cc-connect.exe",
                    "cc_project": "test",
                    "state_file": str(root / "state.json"),
                    "log_file": str(root / "notifier.log"),
                    "codex_cli": "codex.exe",
                    "codex_submit_transport": "desktop-cdp",
                }
            )
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = notifier.load_config(path)
            self.assertEqual(loaded["codex_submit_transport"], "desktop-cdp")

    def test_desktop_submit_uses_target_thread_model_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            config.update(
                {
                    "codex_submit_transport": "desktop-cdp",
                    "codex_desktop_cdp_url": "http://127.0.0.1:9335",
                    "codex_desktop_cdp_timeout_seconds": 3,
                }
            )
            client = mock.MagicMock()
            client.send_follow_up.return_value = {"requestExport": "request"}
            with mock.patch.object(notifier, "DesktopCdpClient", return_value=client):
                ok, _ = notifier.submit_desktop_reply(config, "thread-1", "继续")
            self.assertTrue(ok)
            client.send_follow_up.assert_called_once_with(
                "thread-1",
                "继续",
                model="gpt-5.6-sol",
                reasoning_effort="high",
            )

    def test_app_server_uses_independent_request_timeout(self):
        process = mock.MagicMock()
        process.stdout = []
        process.poll.return_value = 0
        with mock.patch.object(notifier.subprocess, "Popen", return_value=process):
            client = notifier.AppServerClient("codex.exe", 7200, 30)
            client.reader.join(timeout=1)
        self.assertEqual(client.timeout_seconds, 7200)
        self.assertEqual(client.request_timeout_seconds, 30)

    def test_app_server_uses_shared_websocket_without_spawning_cli(self):
        websocket = mock.MagicMock()
        websocket.iter_text.return_value = iter([])
        with (
            mock.patch.object(notifier, "WebSocketConnection", return_value=websocket),
            mock.patch.object(notifier.subprocess, "Popen") as popen,
        ):
            client = notifier.AppServerClient(
                "codex.exe",
                7200,
                30,
                "ws://127.0.0.1:18766",
            )
            client.reader.join(timeout=1)
            client.close()
        popen.assert_not_called()
        websocket.send_json.assert_not_called()
        websocket.close.assert_called_once_with()

    def test_setup_logging_without_console_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "notifier.log"
            with mock.patch.object(notifier.sys, "stdout", None):
                logger = notifier.setup_logging(log_path)
            try:
                logger.info("background logging works")
                for handler in logger.handlers:
                    handler.flush()
                self.assertIn("background logging works", log_path.read_text(encoding="utf-8"))
            finally:
                for handler in logger.handlers:
                    handler.close()
                logger.handlers.clear()

    def make_fixture(self, root: Path):
        rollout = root / "rollout.jsonl"
        db_path = root / "state.sqlite"
        db = sqlite3.connect(db_path)
        db.execute(
            """
            create table threads (
                id text primary key,
                title text,
                rollout_path text,
                is_pinned integer,
                archived integer,
                thread_source text,
                source text,
                model text,
                reasoning_effort text
            )
            """
        )
        db.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-1",
                "测试任务",
                str(rollout),
                1,
                0,
                "user",
                "vscode",
                "gpt-5.6-sol",
                "high",
            ),
        )
        db.commit()
        db.close()
        config = {
            "codex_db": str(db_path),
            "allowed_sources": ["vscode"],
        }
        return rollout, db_path, config

    def configure_pinned_automation(
        self, root: Path, db_path: Path, config: dict, automation_id: str = "daily"
    ) -> None:
        automations_dir = root / "automations"
        definition_dir = automations_dir / automation_id
        definition_dir.mkdir(parents=True)
        (definition_dir / "automation.toml").write_text(
            "\n".join(
                [
                    "version = 1",
                    f'id = "{automation_id}"',
                    'name = "每日总结"',
                    'status = "ACTIVE"',
                    'target_thread_id = "thread-1"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        global_state = root / ".codex-global-state.json"
        global_state.write_text(
            json.dumps({"pinned-thread-ids": ["thread-1"]}), encoding="utf-8"
        )
        config["codex_automations_dir"] = str(automations_dir)
        config["codex_global_state"] = str(global_state)
        db = sqlite3.connect(db_path)
        db.execute(
            "update threads set thread_source='automation', "
            "title=? where id='thread-1'",
            (
                f"Automation: 每日总结\nAutomation ID: {automation_id}\n"
                "Automation memory: memory.md",
            ),
        )
        db.commit()
        db.close()

    @staticmethod
    def append_completion(path: Path, turn_id: str, answer: str):
        item = {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": answer,
            },
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def append_started(path: Path, turn_id: str, timestamp: str = "2026-08-15T12:00:00Z"):
        item = {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id},
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def test_baseline_does_not_queue_history_and_new_completion_is_queued(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            self.append_completion(rollout, "old-turn", "历史答复")
            state = notifier.empty_state()
            threads = notifier.read_desktop_threads(config)
            notifier.baseline_state(state, threads)

            logger = notifier.logging.getLogger("test-baseline")
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            self.assertEqual(state["pending"], [])

            self.append_completion(rollout, "new-turn", "新的最终答复")
            self.assertEqual(notifier.poll_threads(config, state, logger), 1)
            self.assertEqual(state["pending"][0]["turn_id"], "new-turn")
            self.assertEqual(state["pending"][0]["answer"], "新的最终答复")

    def test_push_toggle_is_persistent_idempotent_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            config["handled_message_history_limit"] = 20
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["pending"].append({"turn_id": "old-turn"})
            lock = threading.RLock()

            status, message = notifier.toggle_pinned_push(
                config,
                state,
                state_path,
                lock,
                {"message_id": "m1", "user_id": "u1"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(message, "置顶任务回复推送已关闭")
            self.assertFalse(state["push_enabled"])
            self.assertEqual(state["pending"], [])
            self.assertEqual(state["wechat_session_key"], "weixin:dm:u1")

            _, repeated = notifier.toggle_pinned_push(
                config,
                state,
                state_path,
                lock,
                {"message_id": "m1", "user_id": "u1"},
            )
            self.assertEqual(repeated, "置顶任务回复推送已关闭")
            self.assertFalse(state["push_enabled"])

            _, enabled = notifier.toggle_pinned_push(
                config,
                state,
                state_path,
                lock,
                {"message_id": "m2", "user_id": "u1"},
            )
            self.assertEqual(enabled, "置顶任务回复推送已开启")
            self.assertTrue(notifier.load_state(state_path)["push_enabled"])

    def test_pinned_project_push_toggle_is_independent_and_persistent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            config["handled_message_history_limit"] = 20
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["pending"] = [
                {"turn_id": "exact", "pin_source": "pinned_thread"},
                {"turn_id": "folder", "pin_source": "pinned_project"},
            ]
            lock = threading.RLock()

            status, enabled = notifier.toggle_pinned_project_push(
                config,
                state,
                state_path,
                lock,
                {"message_id": "m1", "user_id": "u1"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(enabled, "置顶文件夹任务回复推送已开启")
            self.assertTrue(state["pinned_project_push_enabled"])
            self.assertEqual(len(state["pending"]), 2)

            _, disabled = notifier.toggle_pinned_project_push(
                config,
                state,
                state_path,
                lock,
                {"message_id": "m2", "user_id": "u1"},
            )
            self.assertEqual(disabled, "置顶文件夹任务回复推送已关闭")
            self.assertFalse(state["pinned_project_push_enabled"])
            self.assertEqual([item["turn_id"] for item in state["pending"]], ["exact"])
            self.assertFalse(
                notifier.load_state(state_path)["pinned_project_push_enabled"]
            )

    def test_proactive_send_uses_explicit_wechat_session(self):
        config = {
            "cc_connect": "cc-connect.exe",
            "cc_project": "test",
            "send_timeout_seconds": 5,
        }
        completed = notifier.subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(notifier.subprocess, "run", return_value=completed) as run:
            ok, _ = notifier.send_via_cc_connect(
                config, "通知", "weixin:dm:user-1"
            )
        self.assertTrue(ok)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--session", "weixin:dm:user-1"])

    def test_completions_during_disabled_push_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            state = notifier.empty_state()
            notifier.baseline_state(state, notifier.read_desktop_threads(config))
            state["push_enabled"] = False
            self.append_completion(rollout, "off-turn", "关闭期间答复")
            logger = notifier.logging.getLogger("test-push-disabled")
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            self.assertEqual(state["pending"], [])

            state["push_enabled"] = True
            self.append_completion(rollout, "on-turn", "开启后的答复")
            self.assertEqual(notifier.poll_threads(config, state, logger), 1)
            self.assertEqual(state["pending"][0]["turn_id"], "on-turn")

    def test_unpinned_completion_is_not_queued(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, db_path, config = self.make_fixture(root)
            state = notifier.empty_state()
            notifier.baseline_state(state, notifier.read_desktop_threads(config))

            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()
            self.append_completion(rollout, "turn-2", "不应发送")

            logger = notifier.logging.getLogger("test-unpinned")
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            self.assertEqual(state["pending"], [])

    def test_sidebar_global_state_is_authoritative_for_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, db_path, config = self.make_fixture(root)
            global_state = root / ".codex-global-state.json"
            config["codex_global_state"] = str(global_state)
            catalog_path = root / "codex-dev.db"
            config["codex_catalog_db"] = str(catalog_path)

            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()
            global_state.write_text(
                json.dumps({"pinned-thread-ids": ["thread-1"]}), encoding="utf-8"
            )
            catalog = sqlite3.connect(catalog_path)
            catalog.execute(
                "create table local_thread_catalog "
                "(host_id text, thread_id text, display_title text)"
            )
            catalog.execute(
                "insert into local_thread_catalog values ('local', 'thread-1', '侧栏名称')"
            )
            catalog.commit()
            catalog.close()

            threads = notifier.read_desktop_threads(config)
            self.assertTrue(threads[0]["is_pinned"])
            self.assertEqual(threads[0]["pinned_index"], 0)
            self.assertEqual(threads[0]["title"], "侧栏名称")
            self.assertTrue(notifier.read_desktop_thread(config, "thread-1")["is_pinned"])

            global_state.write_text(
                json.dumps({"pinned-thread-ids": []}), encoding="utf-8"
            )
            self.assertFalse(notifier.read_desktop_thread(config, "thread-1")["is_pinned"])

    def test_pinned_automation_target_is_numbered_and_quote_replyable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, db_path, config = self.make_fixture(root)
            self.configure_pinned_automation(root, db_path, config)

            threads = notifier.read_desktop_threads(config)
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0]["is_pinned"])
            self.assertTrue(threads[0]["is_automation_target"])
            self.assertEqual(threads[0]["title"], "每日总结")
            self.assertIn(
                "1. 【每日总结】空闲",
                notifier.format_pinned_task_status(config, notifier.empty_state()),
            )

            state_path = root / "state.json"
            state = notifier.empty_state()
            message = "【每日总结】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "每日总结"},
                message,
                20,
            )
            status, _ = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": message,
                    "reply_text": "继续总结",
                    "message_id": "automation-reply",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(state["reply_queue"][0]["thread_id"], "thread-1")

    def test_pinned_automation_run_baselines_history_and_routes_new_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_rollout, db_path, config = self.make_fixture(root)
            self.configure_pinned_automation(root, db_path, config)
            self.append_completion(target_rollout, "target-history", "目标历史答复")
            run_rollout = root / "automation-run.jsonl"
            self.append_completion(run_rollout, "historical-turn", "历史自动答复")
            db = sqlite3.connect(db_path)
            db.execute(
                "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "automation-run-1",
                    "Automation: 每日总结\nAutomation ID: daily",
                    str(run_rollout),
                    0,
                    1,
                    "automation",
                    "vscode",
                    "gpt-5.6-sol",
                    "high",
                ),
            )
            db.execute(
                "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "stale-automation-run",
                    "Automation: 旧任务名称\nAutomation ID: daily",
                    str(root / "stale-run.jsonl"),
                    0,
                    1,
                    "automation",
                    "vscode",
                    "gpt-5.6-sol",
                    "high",
                ),
            )
            db.commit()
            db.close()

            runs = notifier.read_automation_runs(config)
            self.assertEqual([run["id"] for run in runs], ["automation-run-1"])

            state = notifier.empty_state()
            logger = notifier.logging.getLogger("test-automation-run")
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            self.assertTrue(state["automation_runs_initialized"])
            self.assertEqual(state["pending"], [])

            Path(config["codex_global_state"]).write_text(
                json.dumps({"pinned-thread-ids": []}), encoding="utf-8"
            )
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            Path(config["codex_global_state"]).write_text(
                json.dumps({"pinned-thread-ids": ["thread-1"]}), encoding="utf-8"
            )
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)

            self.append_completion(run_rollout, "new-turn", "新的自动答复")
            self.assertEqual(notifier.poll_threads(config, state, logger), 1)
            pending = state["pending"][0]
            self.assertEqual(pending["thread_id"], "thread-1")
            self.assertEqual(pending["source_thread_id"], "automation-run-1")
            self.assertEqual(pending["pin_source"], "pinned_automation")
            self.assertEqual(pending["title"], "每日总结")

    def test_pinned_project_membership_is_read_from_sidebar_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, db_path, config = self.make_fixture(root)
            global_state = root / ".codex-global-state.json"
            config["codex_global_state"] = str(global_state)
            global_state.write_text(
                json.dumps(
                    {
                        "pinned-thread-ids": [],
                        "pinned-project-ids": ["project-1"],
                        "thread-project-assignments": {
                            "thread-1": {
                                "projectKind": "local",
                                "projectId": "project-1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()

            thread = notifier.read_desktop_threads(config)[0]
            self.assertFalse(thread["is_pinned"])
            self.assertTrue(thread["is_project_pinned"])
            self.assertTrue(
                notifier.read_desktop_thread(config, "thread-1")["is_project_pinned"]
            )

    def test_project_thread_completion_is_queued_only_when_folder_push_is_on(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, db_path, config = self.make_fixture(root)
            global_state = root / ".codex-global-state.json"
            config["codex_global_state"] = str(global_state)
            global_state.write_text(
                json.dumps(
                    {
                        "pinned-thread-ids": [],
                        "pinned-project-ids": ["project-1"],
                        "thread-project-assignments": {
                            "thread-1": {"projectId": "project-1"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()
            state = notifier.empty_state()
            notifier.baseline_state(state, notifier.read_desktop_threads(config))
            logger = notifier.logging.getLogger("test-project-push")

            self.append_completion(rollout, "off-turn", "关闭时不发送")
            self.assertEqual(notifier.poll_threads(config, state, logger), 0)
            state["pinned_project_push_enabled"] = True
            self.append_completion(rollout, "on-turn", "开启后发送")
            self.assertEqual(notifier.poll_threads(config, state, logger), 1)
            self.assertEqual(state["pending"][0]["turn_id"], "on-turn")
            self.assertEqual(state["pending"][0]["pin_source"], "pinned_project")

    def test_chunking_preserves_answer(self):
        answer = "甲" * 1300
        chunks = notifier.split_answer("长任务", answer, 600)
        self.assertGreater(len(chunks), 1)
        recovered_parts = []
        for chunk in chunks:
            self.assertTrue(chunk.startswith("【长任务】\n"))
            self.assertTrue(
                chunk.endswith(
                    "\n\u200b\n（↩ 引用此条信息进行回复。"
                    "如任务正在处理，则默认排队，直接提交请加前缀“/y”）"
                )
            )
            self.assertLessEqual(len(chunk), 600)
            content = chunk.split("】\n\u200b\n", 1)[1].rsplit(
                "\n\u200b\n（↩", 1
            )[0]
            content = content.split("\n", 1)[1]
            recovered_parts.append(content)
        recovered = "".join(recovered_parts)
        self.assertEqual(recovered, answer)

    def test_single_notification_uses_requested_format(self):
        chunks = notifier.split_answer("科研", "这是最终答复……", 3400)
        self.assertEqual(
            chunks,
            [
                "【科研】\n\u200b\n这是最终答复……\n\u200b\n"
                "（↩ 引用此条信息进行回复。如任务正在处理，则默认排队，"
                "直接提交请加前缀“/y”）"
            ],
        )

    def test_quote_fingerprint_ignores_transport_sender_prefix(self):
        notification = notifier.format_notification("cc-connect优化", "这是最终答复")
        self.assertEqual(
            notifier.quote_fingerprint(notification),
            notifier.quote_fingerprint("Codex: " + notification),
        )

    def test_latest_runtime_and_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            rollout = Path(temp) / "rollout.jsonl"
            self.append_completion(rollout, "turn-1", "完成")
            self.append_started(rollout, "turn-2")
            runtime = notifier.latest_thread_runtime(rollout)
            self.assertTrue(runtime["active"])
            self.assertEqual(runtime["turn_id"], "turn-2")
            self.assertEqual(notifier.format_duration(65), "1分05秒")
            self.append_completion(rollout, "turn-2", "再次完成")
            completed = notifier.latest_thread_runtime(rollout)
            self.assertFalse(completed["active"])
            self.assertEqual(completed["turn_id"], "turn-2")

    def test_status_lists_pinned_tasks_and_queue_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            self.append_started(rollout, "turn-1", "2026-08-15T12:00:00Z")
            state = notifier.empty_state()
            state["reply_queue"].append(
                {"thread_id": "thread-1", "status": "queued", "reply": "继续"}
            )
            with mock.patch.object(notifier.time, "time", return_value=1776254465):
                message = notifier.format_pinned_task_status(config, state)
            self.assertIn("【测试任务】运行中｜已处理", message)
            self.assertIn("排队 1", message)
            self.assertIn("置顶文件夹推送：已关闭", message)

    def test_quote_route_matches_without_storing_answer(self):
        state = notifier.empty_state()
        item = {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "title": "日常",
        }
        message = "【日常】\n答复正文\n\n↩ 引用此条信息进行回复"
        notifier.remember_quote_route(state, item, message, 20)
        route = notifier.find_quote_route(state, message.replace("\n", "\r\n"))
        self.assertIsNotNone(route)
        self.assertEqual(route["thread_id"], "thread-1")
        self.assertNotIn("答复正文", json.dumps(state, ensure_ascii=False))

    def test_quote_route_matches_truncated_weixin_preview_by_title(self):
        state = notifier.empty_state()
        notifier.remember_quote_route(
            state,
            {"thread_id": "thread-1", "turn_id": "turn-1", "title": "了解我"},
            notifier.format_notification("了解我", "500万左右，对你来说像是一条安全线。"),
            20,
        )
        route = notifier.find_quote_route(
            state, "Codex: 【了解我】 500万左右，对你来说像是一条……"
        )
        self.assertIsNotNone(route)
        self.assertEqual(route["thread_id"], "thread-1")

    def test_quote_route_rejects_ambiguous_duplicate_titles(self):
        state = notifier.empty_state()
        for thread_id, turn_id in (("thread-1", "turn-1"), ("thread-2", "turn-2")):
            notifier.remember_quote_route(
                state,
                {"thread_id": thread_id, "turn_id": turn_id, "title": "科研"},
                notifier.format_notification("科研", f"答复 {turn_id}"),
                20,
            )
        route = notifier.find_quote_route(state, "Codex: 【科研】 答复预览……")
        self.assertIsNone(route)

    def test_quote_route_matches_id_only_reference_by_send_time_then_remembers_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            state_path = root / "state.json"
            state = notifier.empty_state()
            with mock.patch.object(notifier.time, "time", return_value=1786855678.0):
                notifier.remember_quote_route(
                    state,
                    {"thread_id": "thread-1", "turn_id": "turn-1", "title": "了解我"},
                    notifier.format_notification("了解我", "答复"),
                    20,
                )
            status, response = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": "",
                    "reply_text": "继续",
                    "message_id": "in-1",
                    "user_id": "u1",
                    "referenced_message_id": "7494615923961113736",
                    "referenced_create_time_ms": 1786855680000,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(response, "收到，已提交【测试任务】。")
            self.assertEqual(state["reply_queue"][0]["thread_id"], "thread-1")
            self.assertEqual(
                state["quote_routes"][0]["wechat_message_id"],
                "7494615923961113736",
            )
            route = notifier.find_quote_route(
                state,
                referenced_message_id="7494615923961113736",
            )
            self.assertEqual(route["thread_id"], "thread-1")

    def test_state_v1_is_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "initialized": True,
                        "offsets": {},
                        "pending": [],
                        "sent_turns": {},
                    }
                ),
                encoding="utf-8",
            )
            state = notifier.load_state(path)
            self.assertEqual(state["version"], notifier.STATE_VERSION)
            self.assertEqual(state["quote_routes"], [])
            self.assertEqual(state["reply_queue"], [])

    def test_state_load_requeues_interrupted_notification_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            state = notifier.empty_state()
            state["pending"] = [{"turn_id": "turn-1", "delivery_status": "sending"}]
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = notifier.load_state(path)
            self.assertEqual(loaded["pending"][0]["delivery_status"], "queued")

    def test_deliver_pending_releases_state_lock_while_sending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["pending"] = [
                {
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "title": "科研",
                    "answer": "完成",
                    "next_chunk": 0,
                    "attempts": 0,
                    "next_retry_at": 0,
                    "delivery_status": "queued",
                }
            ]
            config = {
                "max_message_chars": 3400,
                "quote_route_history_limit": 20,
                "sent_history_limit": 20,
            }
            state_lock = threading.RLock()
            sending = threading.Event()
            release_send = threading.Event()

            def blocked_send(*_args, **_kwargs):
                sending.set()
                release_send.wait(timeout=2)
                return True, "ok"

            result: list[int] = []
            with mock.patch.object(notifier, "send_via_cc_connect", blocked_send):
                worker = threading.Thread(
                    target=lambda: result.append(
                        notifier.deliver_pending(
                            config,
                            state,
                            state_path,
                            state_lock,
                            notifier.logging.getLogger("test-delivery-lock"),
                        )
                    )
                )
                worker.start()
                self.assertTrue(sending.wait(timeout=1))
                acquired = state_lock.acquire(timeout=0.2)
                self.assertTrue(acquired)
                if acquired:
                    state_lock.release()
                release_send.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [1])
            self.assertEqual(state["pending"], [])

    def test_dispatch_releases_state_lock_while_reading_desktop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["reply_queue"] = [
                {
                    "request_id": "request-1",
                    "thread_id": "thread-1",
                    "reply": "继续",
                    "mode": "queue",
                    "status": "queued",
                    "queued_at": 1,
                    "next_retry_at": 0,
                }
            ]
            state_lock = threading.RLock()
            reading = threading.Event()
            release_read = threading.Event()

            def blocked_read(*_args, **_kwargs):
                reading.set()
                release_read.wait(timeout=2)
                return None

            with mock.patch.object(notifier, "read_desktop_thread", blocked_read):
                worker = threading.Thread(
                    target=notifier.dispatch_reply_requests,
                    args=(
                        {},
                        state,
                        state_path,
                        state_lock,
                        {},
                        {},
                        threading.RLock(),
                        notifier.logging.getLogger("test-dispatch-lock"),
                    ),
                )
                worker.start()
                self.assertTrue(reading.wait(timeout=1))
                acquired = state_lock.acquire(timeout=0.2)
                self.assertTrue(acquired)
                if acquired:
                    state_lock.release()
                release_read.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(state["reply_queue"][0]["status"], "queued")

    def test_running_reply_recovery_avoids_duplicate_desktop_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            submitted = {
                "timestamp": "2026-08-15T12:00:10Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "已提交的回复"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn-submitted"
                    },
                },
            }
            rollout.write_text(
                json.dumps(submitted, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            state = notifier.empty_state()
            state["reply_queue"] = [
                {
                    "request_id": "request-submitted",
                    "thread_id": "thread-1",
                    "reply": "已提交的回复",
                    "status": "running",
                    "queued_at": 1776427200,
                },
                {
                    "request_id": "request-not-submitted",
                    "thread_id": "thread-1",
                    "reply": "尚未提交",
                    "status": "running",
                    "queued_at": 1776427200,
                },
            ]
            recovered, requeued = notifier.recover_interrupted_reply_requests(
                config,
                state,
                notifier.logging.getLogger("test-recovery"),
            )
            self.assertEqual((recovered, requeued), (1, 1))
            self.assertIn("request-submitted", state["handled_message_ids"])
            self.assertEqual(len(state["reply_queue"]), 1)
            self.assertEqual(state["reply_queue"][0]["status"], "queued")

    def test_enqueue_quote_reply_routes_only_to_pinned_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, db_path, config = self.make_fixture(root)
            state_path = root / "state.json"
            state = notifier.empty_state()
            message = "【科研】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "科研"},
                message,
                20,
            )
            status, _ = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": message,
                    "reply_text": "继续分析",
                    "message_id": "m1",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(state["reply_queue"][0]["thread_id"], "thread-1")

            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()
            status, _ = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": message,
                    "reply_text": "再次继续",
                    "message_id": "m2",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 409)

    def test_enqueue_quote_reply_accepts_task_in_enabled_pinned_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, db_path, config = self.make_fixture(root)
            config["codex_global_state"] = str(root / ".codex-global-state.json")
            Path(config["codex_global_state"]).write_text(
                json.dumps(
                    {
                        "pinned-thread-ids": [],
                        "pinned-project-ids": ["project-1"],
                        "thread-project-assignments": {
                            "thread-1": {"projectId": "project-1"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            db = sqlite3.connect(db_path)
            db.execute("update threads set is_pinned=0 where id='thread-1'")
            db.commit()
            db.close()
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["pinned_project_push_enabled"] = True
            message = "【文件夹任务】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "文件夹任务"},
                message,
                20,
            )

            status, _ = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": message,
                    "reply_text": "继续处理",
                    "message_id": "m-project",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(state["reply_queue"][0]["thread_id"], "thread-1")

    def test_default_reply_queues_while_task_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            self.append_started(rollout, "active-turn")
            state_path = root / "state.json"
            state = notifier.empty_state()
            message = "【科研】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "科研"},
                message,
                20,
            )
            with mock.patch.object(
                notifier,
                "read_desktop_queued_follow_up_count",
                return_value=0,
            ):
                status, response = notifier.enqueue_quote_reply(
                    config,
                    state,
                    state_path,
                    threading.RLock(),
                    {
                        "quote_text": message,
                        "reply_text": "继续排队",
                        "message_id": "m1",
                        "user_id": "u1",
                    },
                )
            self.assertEqual(status, 200)
            self.assertEqual(
                response,
                '收到，已提交【测试任务】，排队中（前方0条）。\n'
                '引用这条提示回复"/y"直接提交本条消息。',
            )
            self.assertEqual(len(state["queue_ack_routes"]), 1)
            self.assertEqual(state["reply_queue"][0]["mode"], "queue")
            started = notifier.dispatch_reply_requests(
                config,
                state,
                state_path,
                threading.RLock(),
                {},
                {},
                threading.RLock(),
                notifier.logging.getLogger("test-dispatch-active"),
            )
            self.assertEqual(started, 0)

            ack_time = state["queue_ack_routes"][0]["sent_at_ms"]
            status, obsolete = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": response,
                    "reply_text": "/z",
                    "message_id": "m-obsolete",
                    "user_id": "u1",
                    "referenced_create_time_ms": ack_time,
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                obsolete, '请引用这条排队提示回复"/y"直接提交本条消息。'
            )
            self.assertEqual(len(state["reply_queue"]), 1)

            with mock.patch.object(
                notifier,
                "submit_desktop_reply",
                return_value=(True, "desktop-cdp"),
            ) as submit:
                status, promoted = notifier.enqueue_quote_reply(
                    config,
                    state,
                    state_path,
                    threading.RLock(),
                    {
                        "quote_text": response,
                        "reply_text": "/y",
                        "message_id": "m2",
                        "user_id": "u1",
                        "referenced_create_time_ms": ack_time,
                    },
                )
            self.assertEqual(status, 200)
            self.assertEqual(promoted, "收到，已直接提交给【测试任务】。")
            submit.assert_called_once_with(config, "thread-1", "继续排队")
            self.assertEqual(state["reply_queue"], [])

            state["reply_queue"].append(
                {
                    "request_id": "queued-promoting",
                    "thread_id": "thread-1",
                    "title": "测试任务",
                    "reply": "不应重复提交",
                    "mode": "queue",
                    "status": "promoting",
                }
            )
            state["queue_ack_routes"].append(
                {
                    "request_id": "queued-promoting",
                    "title": "测试任务",
                    "fingerprint": notifier.quote_fingerprint(response),
                    "sent_at_ms": ack_time,
                }
            )
            status, message = notifier.promote_queued_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                response,
                "wechat-z-duplicate",
                ack_time,
            )
            self.assertEqual(status, 200)
            self.assertEqual(message, "【测试任务】这条消息已在提交中。")

    def test_pinned_task_reply_routes_by_current_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            state_path = root / "state.json"
            state = notifier.empty_state()
            status, response = notifier.enqueue_pinned_task_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "pinned_index": 1,
                    "reply_text": "按编号继续",
                    "message_id": "indexed-1",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(response, "收到，已提交【测试任务】。")
            self.assertEqual(state["reply_queue"][0]["thread_id"], "thread-1")
            self.assertEqual(state["reply_queue"][0]["reply"], "按编号继续")

            status, response = notifier.enqueue_pinned_task_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "pinned_index": 2,
                    "reply_text": "不存在",
                    "message_id": "indexed-2",
                    "user_id": "u1",
                },
            )
            self.assertEqual(status, 404)
            self.assertEqual(response, "编号无效，请先发送 /rw 查看当前置顶任务。")

    def test_pinned_task_y_prefix_uses_direct_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            state_path = root / "state.json"
            state = notifier.empty_state()
            with mock.patch.object(
                notifier, "submit_desktop_reply", return_value=(True, "desktop-cdp")
            ) as submit:
                status, response = notifier.enqueue_pinned_task_reply(
                    config,
                    state,
                    state_path,
                    threading.RLock(),
                    {
                        "pinned_index": 1,
                        "reply_text": "/y 直接补充",
                        "message_id": "indexed-y",
                        "user_id": "u1",
                    },
                )
            self.assertEqual(status, 200)
            self.assertEqual(response, "收到，已直接提交给【测试任务】。")
            submit.assert_called_once_with(config, "thread-1", "直接补充")

    def test_direct_submit_reserves_request_before_desktop_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            state_path = root / "state.json"
            state = notifier.empty_state()
            lock = threading.RLock()
            payload = {
                "pinned_index": 1,
                "reply_text": "/y 直接补充",
                "message_id": "direct-duplicate",
                "user_id": "u1",
            }

            def duplicate_during_submit(*_args):
                status, response = notifier.enqueue_pinned_task_reply(
                    config, state, state_path, lock, payload
                )
                self.assertEqual((status, response), (200, "收到"))
                return False, "desktop unavailable"

            with mock.patch.object(
                notifier, "submit_desktop_reply", side_effect=duplicate_during_submit
            ) as submit:
                status, response = notifier.enqueue_pinned_task_reply(
                    config, state, state_path, lock, payload
                )
            self.assertEqual(status, 200)
            self.assertEqual(response, "【测试任务】直接提交未成功，已优先排队。")
            submit.assert_called_once_with(config, "thread-1", "直接补充")
            self.assertEqual(len(state["reply_queue"]), 1)
            self.assertEqual(state["reply_queue"][0]["status"], "queued")

    def test_queue_count_includes_desktop_follow_ups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            self.append_started(rollout, "active-turn")
            state_path = root / "state.json"
            state = notifier.empty_state()
            message = "【科研】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "科研"},
                message,
                20,
            )

            def desktop_count_before_enqueue(_config, thread_id):
                self.assertEqual(thread_id, "thread-1")
                self.assertEqual(state["reply_queue"], [])
                return 1

            with mock.patch.object(
                notifier,
                "read_desktop_queued_follow_up_count",
                side_effect=desktop_count_before_enqueue,
            ) as desktop_count:
                status, response = notifier.enqueue_quote_reply(
                    config,
                    state,
                    state_path,
                    threading.RLock(),
                    {
                        "quote_text": message,
                        "reply_text": "微信排队",
                        "message_id": "m-desktop-ahead",
                        "user_id": "u1",
                    },
                )

            self.assertEqual(status, 200)
            self.assertIn("排队中（前方1条）", response)
            desktop_count.assert_called_once_with(config, "thread-1")

    def test_queue_count_combines_desktop_and_wechat_follow_ups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout, _, config = self.make_fixture(root)
            self.append_started(rollout, "active-turn")
            state_path = root / "state.json"
            state = notifier.empty_state()
            state["reply_queue"].append(
                {
                    "request_id": "wechat-ahead",
                    "thread_id": "thread-1",
                    "status": "queued",
                }
            )
            message = "【科研】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "科研"},
                message,
                20,
            )

            with mock.patch.object(
                notifier,
                "read_desktop_queued_follow_up_count",
                return_value=1,
            ):
                status, response = notifier.enqueue_quote_reply(
                    config,
                    state,
                    state_path,
                    threading.RLock(),
                    {
                        "quote_text": message,
                        "reply_text": "第二条微信排队",
                        "message_id": "m-combined-ahead",
                        "user_id": "u1",
                    },
                )

            self.assertEqual(status, 200)
            self.assertIn("排队中（前方2条）", response)

    def test_desktop_queue_count_failure_falls_back_to_wechat_queue(self):
        config = {
            "codex_desktop_cdp_url": "http://127.0.0.1:9335",
            "codex_desktop_cdp_timeout_seconds": 3,
        }
        client = mock.MagicMock()
        client.get_queued_follow_up_count.side_effect = RuntimeError("unavailable")
        with (
            mock.patch.object(notifier, "DesktopCdpClient", return_value=client),
            self.assertLogs("codex_pinned_wechat_notifier", level="WARNING"),
        ):
            count = notifier.read_desktop_queued_follow_up_count(
                config, "thread-1"
            )
        self.assertEqual(count, 0)

    def test_y_prefix_steers_wechat_owned_active_turn(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def request(self, method, params):
                self.calls.append((method, params))
                return {"turnId": params["expectedTurnId"]}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, config = self.make_fixture(root)
            config["handled_message_history_limit"] = 20
            state_path = root / "state.json"
            state = notifier.empty_state()
            message = "【科研】\n答复\n\n↩ 引用此条信息进行回复"
            notifier.remember_quote_route(
                state,
                {"thread_id": "thread-1", "turn_id": "turn-1", "title": "科研"},
                message,
                20,
            )
            fake = FakeClient()
            sessions = {
                "thread-1": {
                    "client": fake,
                    "turn_id": "active-turn",
                    "started_at": 1,
                }
            }
            status, response = notifier.enqueue_quote_reply(
                config,
                state,
                state_path,
                threading.RLock(),
                {
                    "quote_text": message,
                    "reply_text": "/y 直接补充这个要求",
                    "message_id": "m2",
                    "user_id": "u1",
                },
                sessions,
                threading.RLock(),
            )
            self.assertEqual(status, 200)
            self.assertEqual(response, "收到，已直接提交给【测试任务】。")
            self.assertEqual(state["reply_queue"], [])
            method, params = fake.calls[0]
            self.assertEqual(method, "turn/steer")
            self.assertEqual(params["expectedTurnId"], "active-turn")
            self.assertEqual(params["input"][0]["text"], "直接补充这个要求")


if __name__ == "__main__":
    unittest.main()
