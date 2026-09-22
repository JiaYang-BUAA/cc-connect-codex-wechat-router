from __future__ import annotations

import http.client
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_notifier as base


notifier = base.notifier


class PushModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rollout, self.db_path, self.config = base.NotifierTests().make_fixture(self.root)
        self.rollout.touch()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN updated_at INTEGER")
            db.execute("ALTER TABLE threads ADD COLUMN updated_at_ms INTEGER")
        self.config.update({
            "handled_message_history_limit": 20,
            "reply_retry_limit": 3,
            "quota_monitor_enabled": False,
        })
        self.state = notifier.empty_state()
        self.state_path = self.root / "notifier-state.json"
        self.lock = threading.RLock()
        self.logger = notifier.logging.getLogger("test-push-modes")

    def add_thread(self, thread_id, activity, *, pinned=0, archived=0, source="user", title=None):
        rollout = self.root / f"{thread_id}.jsonl"
        rollout.touch()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO threads "
                "(id,title,rollout_path,is_pinned,archived,thread_source,source,model,reasoning_effort,cwd,updated_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (thread_id, title or thread_id, str(rollout), pinned, archived,
                 source, "vscode", "test-model", "high", str(self.root), activity),
            )
        return rollout

    def update_thread(self, thread_id, **values):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE threads SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), thread_id),
            )

    def baseline(self):
        notifier.baseline_state(self.state, notifier.read_monitored_threads(self.config, self.state))

    def switch(self, mode="", message_id="mode-1"):
        return notifier.set_push_mode(
            self.config, self.state, self.state_path, self.lock,
            {"mode": mode, "message_id": message_id, "user_id": "test-user"},
        )

    def recent_ids(self):
        return [row["id"] for row in notifier.listed_tasks(
            notifier.read_desktop_threads(self.config), self.state,
        )]

    def test_recent_orders_by_activity_limits_ten_and_includes_idle_unpinned_tasks(self):
        for index in range(1, 13):
            self.add_thread(f"recent-{index:02d}", index * 1000)
        self.add_thread("archived", 99000, archived=1)
        self.add_thread("subagent", 98000, source="subagent")
        self.state["push_mode"] = "recent"

        self.assertEqual(self.recent_ids(), [f"recent-{i:02d}" for i in range(12, 2, -1)])
        status = notifier.format_pinned_task_status(self.config, self.state)
        self.assertIn("近10条活跃任务（10）", status)
        self.assertIn("1. 【recent-12】空闲", status)
        self.assertNotIn("测试任务", status)
        self.assertEqual(status.count("空闲"), 10)

    def test_activity_uses_milliseconds_then_seconds_fallback(self):
        self.update_thread("thread-1", updated_at=200, updated_at_ms=0)
        self.add_thread("millisecond", 200001)
        self.add_thread("null-millisecond", None)
        self.update_thread("null-millisecond", updated_at=199)
        self.add_thread("unknown", None)
        self.state["push_mode"] = "recent"
        rows = {row["id"]: row for row in notifier.read_desktop_threads(self.config)}
        self.assertEqual(rows["thread-1"]["activity_at_ms"], 200000)
        self.assertEqual(rows["millisecond"]["activity_at_ms"], 200001)
        self.assertEqual(self.recent_ids()[:3], ["millisecond", "thread-1", "null-millisecond"])

    def test_legacy_database_without_activity_fields_still_works(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        _, _, config = base.NotifierTests().make_fixture(legacy_root)
        rows = notifier.read_desktop_threads(config)
        self.assertEqual(rows[0]["activity_at_ms"], 0)
        self.assertEqual(notifier.select_recent_threads(rows)[0]["id"], "thread-1")
        with closing(sqlite3.connect(config["codex_db"])) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN updated_at INTEGER")
            db.execute("UPDATE threads SET updated_at=123")
        self.assertEqual(notifier.read_desktop_threads(config)[0]["activity_at_ms"], 123000)

    def test_legacy_state_defaults_to_pinned_preserving_settings_and_queues(self):
        self.state.pop("push_mode")
        self.state.pop("recent_task_order")
        self.state["push_enabled"] = False
        self.state["pinned_project_push_enabled"] = True
        self.state["pending"] = [{"turn_id": "pending-turn", "delivery_status": "queued"}]
        notifier.save_state(self.state_path, self.state)
        loaded = notifier.load_state(self.state_path)
        self.assertEqual(loaded["push_mode"], "pinned")
        self.assertEqual(loaded["recent_task_order"], [])
        self.assertFalse(loaded["push_enabled"])
        self.assertTrue(loaded["pinned_project_push_enabled"])
        self.assertEqual(loaded["pending"], self.state["pending"])

    def test_switch_is_idempotent_and_keeps_push_settings_pending_and_replies(self):
        self.state.update({
            "push_enabled": False,
            "pinned_project_push_enabled": True,
            "pending": [{"turn_id": "awaiting-delivery", "delivery_status": "queued"}],
            "reply_queue": [{"request_id": "accepted", "thread_id": "thread-1", "mode": "queue"}],
            "pending_summary": {"active": True, "attempts": 2, "next_retry_at": 0},
        })
        code, _ = self.switch()
        self.assertEqual(code, 200)
        self.assertEqual(self.state["push_mode"], "recent")
        self.assertEqual(self.switch()[0], 200)
        self.assertEqual(self.state["push_mode"], "recent")
        loaded = notifier.load_state(self.state_path)
        self.assertFalse(loaded["push_enabled"])
        self.assertTrue(loaded["pinned_project_push_enabled"])
        self.assertEqual(loaded["pending"], self.state["pending"])
        self.assertEqual(loaded["reply_queue"], self.state["reply_queue"])
        self.assertTrue(loaded["pending_summary"]["active"])
        self.assertEqual(self.switch("", "mode-2")[0], 200)
        self.assertEqual(self.state["push_mode"], "pinned")

    def test_switch_collects_old_scope_completions_but_baselines_new_scope_history(self):
        other = self.add_thread("unpinned", 1000)
        self.baseline()
        base.NotifierTests.append_completion(self.rollout, "pinned-before-switch", "old scope")
        base.NotifierTests.append_completion(other, "unpinned-history", "historical")
        self.switch("recent")
        self.assertEqual([item["turn_id"] for item in self.state["pending"]], ["pinned-before-switch"])
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 0)
        base.NotifierTests.append_completion(other, "unpinned-after-switch", "new result")
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 1)
        self.assertEqual(self.state["pending"][-1]["turn_id"], "unpinned-after-switch")
        self.assertEqual(self.state["pending"][-1]["pin_source"], "recent")

    def test_recent_membership_changes_do_not_replay_old_completions_or_mix_pins(self):
        rollouts = {f"recent-{i:02d}": self.add_thread(f"recent-{i:02d}", i * 1000) for i in range(1, 12)}
        self.state["push_mode"] = "recent"
        self.baseline()
        base.NotifierTests.append_completion(rollouts["recent-01"], "outside-history", "outside")
        base.NotifierTests.append_completion(self.rollout, "old-pin", "pin outside ten")
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 0)
        self.update_thread("recent-01", updated_at_ms=20000)
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 0)
        base.NotifierTests.append_completion(rollouts["recent-01"], "new-inside", "inside")
        base.NotifierTests.append_completion(rollouts["recent-02"], "new-outside", "outside")
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 1)
        self.assertEqual([item["turn_id"] for item in self.state["pending"]], ["new-inside"])
        self.update_thread("recent-02", updated_at_ms=21000)
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 0)

    def test_newly_discovered_rollout_ignores_answers_older_than_recent_mode_activation(self):
        self.switch("recent")
        self.state = notifier.load_state(self.state_path)
        discovered = self.add_thread("discovered", 20000)
        now = time.time()
        events = []
        for turn_id, completed_at in (("historical-answer", now - 3600), ("current-answer", now + 2)):
            events.append(json.dumps({
                "timestamp": datetime.fromtimestamp(completed_at, timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": turn_id},
            }))
        discovered.write_text("\n".join(events) + "\n", encoding="utf-8")
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 1)
        self.assertEqual([item["turn_id"] for item in self.state["pending"]], ["current-answer"])

    def test_recent_number_uses_persisted_status_snapshot_after_activity_reorders(self):
        self.add_thread("first", 2000)
        self.add_thread("second", 1000)
        self.switch("recent")
        self.state = notifier.load_state(self.state_path)
        self.assertEqual(self.state["recent_task_order"][:2], ["first", "second"])
        self.update_thread("second", updated_at_ms=3000)
        with mock.patch.object(notifier, "enqueue_desktop_queued_follow_up", return_value=(True, 1, "native-id", [])) as enqueue:
            code, _ = notifier.enqueue_pinned_task_reply(
                self.config, self.state, self.state_path, self.lock,
                {"pinned_index": 1, "reply_text": "continue saved task", "message_id": "number-1", "user_id": "test-user"},
            )
        self.assertEqual(code, 200)
        self.assertEqual(enqueue.call_args.args[1]["id"], "first")
        self.assertEqual(self.state["reply_queue"][0]["accepted_push_mode"], "recent")
        notifier.format_pinned_task_status(self.config, self.state)
        self.assertEqual(self.state["recent_task_order"][:2], ["second", "first"])

    def test_recent_number_requires_snapshot_and_rejects_archived_saved_task(self):
        self.state["push_mode"] = "recent"
        payload = {"pinned_index": 1, "reply_text": "continue", "message_id": "number-1", "user_id": "test-user"}
        self.assertEqual(notifier.enqueue_pinned_task_reply(self.config, self.state, self.state_path, self.lock, payload)[0], 404)
        notifier.format_pinned_task_status(self.config, self.state)
        self.update_thread("thread-1", archived=1)
        self.assertEqual(notifier.enqueue_pinned_task_reply(self.config, self.state, self.state_path, self.lock, payload)[0], 409)
        self.assertEqual(self.state["reply_queue"], [])

    def test_old_known_quote_is_replyable_in_recent_mode_after_leaving_top_ten(self):
        for index in range(11):
            self.add_thread(f"recent-{index}", index + 1)
        self.update_thread("thread-1", is_pinned=0)
        self.state["push_mode"] = "recent"
        message = "【测试任务】\nanswer\n\n↩ 引用此条信息进行回复"
        notifier.remember_quote_route(self.state, {"thread_id": "thread-1", "turn_id": "old-turn", "title": "测试任务"}, message, 20)
        payload = {"quote_text": message, "reply_text": "/y continue", "message_id": "quote-1", "user_id": "test-user"}
        self.assertNotIn("thread-1", self.recent_ids())
        with mock.patch.object(notifier, "submit_desktop_reply", return_value=(True, "submitted")) as submit:
            code, _ = notifier.enqueue_quote_reply(self.config, self.state, self.state_path, self.lock, payload)
        self.assertEqual(code, 200)
        self.assertEqual(submit.call_args.args[1:3], ("thread-1", "continue"))
        self.update_thread("thread-1", archived=1)
        payload["message_id"] = "quote-2"
        self.assertEqual(notifier.enqueue_quote_reply(self.config, self.state, self.state_path, self.lock, payload)[0], 409)

    def test_known_quote_survives_mode_switch_in_both_directions(self):
        message = "【测试任务】\nanswer\n\n↩ 引用此条信息进行回复"
        self.update_thread("thread-1", is_pinned=0)
        notifier.remember_quote_route(self.state, {
            "thread_id": "thread-1", "turn_id": "old-turn", "title": "测试任务",
        }, message, 20)
        for index, target_mode in enumerate(("recent", "pinned", "recent", "pinned")):
            self.switch(target_mode, f"switch-{index}")
            self.state = notifier.load_state(self.state_path)
            for direct in (False, True):
                with self.subTest(mode=target_mode, direct=direct):
                    payload = {
                        "quote_text": message,
                        "reply_text": "/y continue" if direct else "continue",
                        "message_id": f"reply-{index}-{direct}", "user_id": "test-user",
                    }
                    with mock.patch.object(notifier, "submit_desktop_reply", return_value=(True, "submitted")) as submit, mock.patch.object(
                        notifier, "enqueue_desktop_queued_follow_up", return_value=(True, 0, "native-id", []),
                    ) as enqueue:
                        code, _ = notifier.enqueue_quote_reply(
                            self.config, self.state, self.state_path, self.lock, payload,
                        )
                    self.assertEqual(code, 200)
                    if direct:
                        self.assertEqual(submit.call_args.args[1], "thread-1")
                    else:
                        self.assertEqual(enqueue.call_args.args[1]["id"], "thread-1")
                    self.assertEqual(self.state["reply_queue"][-1]["accepted_push_mode"], target_mode)

    def test_cross_mode_quote_keeps_target_safety_checks_and_push_scope(self):
        message = "【测试任务】\nanswer\n\n↩ 引用此条信息进行回复"
        notifier.remember_quote_route(self.state, {
            "thread_id": "thread-1", "turn_id": "old-turn", "title": "测试任务",
        }, message, 20)
        self.update_thread("thread-1", is_pinned=0)
        self.switch("recent", "to-recent")
        self.switch("pinned", "to-pinned")
        self.assertFalse(notifier.thread_push_is_enabled(
            notifier.read_desktop_thread(self.config, "thread-1"), self.state,
        ))
        with mock.patch.object(notifier, "enqueue_thread_reply") as enqueue:
            for source, archived in (("user", 1), ("subagent", 0)):
                self.update_thread("thread-1", thread_source=source, archived=archived)
                code, _ = notifier.enqueue_quote_reply(self.config, self.state, self.state_path, self.lock, {
                    "quote_text": message, "reply_text": "continue", "message_id": f"blocked-{source}",
                })
                self.assertEqual(code, 409)
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("DELETE FROM threads WHERE id='thread-1'")
            self.assertEqual(notifier.enqueue_quote_reply(self.config, self.state, self.state_path, self.lock, {
                "quote_text": message, "reply_text": "continue", "message_id": "deleted",
            })[0], 409)
            self.assertEqual(notifier.enqueue_quote_reply(self.config, self.state, self.state_path, self.lock, {
                "quote_text": "【Unknown task】\nanswer\n↩ 引用此条信息进行回复",
                "reply_text": "continue", "message_id": "unknown",
            })[0], 404)
        enqueue.assert_not_called()

    def test_folder_toggle_is_ignored_in_recent_mode_and_total_toggle_still_works(self):
        self.state.update({"push_mode": "recent", "pinned_project_push_enabled": True})
        payload = {"message_id": "folder-1", "user_id": "test-user"}
        code, message = notifier.toggle_pinned_project_push(self.config, self.state, self.state_path, self.lock, payload)
        self.assertEqual(code, 200)
        self.assertIn("仅在置顶模式生效", message)
        self.assertTrue(self.state["pinned_project_push_enabled"])
        code, message = notifier.toggle_pinned_push(self.config, self.state, self.state_path, self.lock, payload)
        self.assertEqual(code, 200)
        self.assertEqual(message, "近10条活跃任务回复推送已关闭")
        self.assertFalse(self.state["push_enabled"])
        self.assertEqual(self.state["push_mode"], "recent")
        self.switch("pinned")
        self.assertTrue(self.state["pinned_project_push_enabled"])
        self.assertFalse(self.state["push_enabled"])

    def test_accepted_fallback_reply_survives_switch_to_nonmatching_mode(self):
        self.update_thread("thread-1", is_pinned=0)
        self.state["push_mode"] = "pinned"
        self.state["reply_queue"] = [{
            "request_id": "accepted-in-recent", "thread_id": "thread-1",
            "title": "测试任务", "reply": "continue", "status": "running",
            "accepted_push_mode": "recent",
        }]
        with mock.patch.object(notifier, "run_codex_reply", return_value=(True, "turn-id")) as submit:
            notifier.run_reply_worker(
                self.config, self.state, self.state_path, self.lock, {}, {}, threading.RLock(),
                "accepted-in-recent", self.logger,
            )
        submit.assert_called_once()
        self.assertEqual(self.state["reply_queue"], [])
        self.assertIn("accepted-in-recent", self.state["handled_message_ids"])

    def test_accepted_reply_still_rejects_archived_target(self):
        self.update_thread("thread-1", archived=1)
        self.state["reply_queue"] = [{
            "request_id": "accepted-before-archive", "thread_id": "thread-1",
            "title": "测试任务", "reply": "continue", "status": "running",
            "accepted_push_mode": "recent",
        }]
        with mock.patch.object(notifier, "run_codex_reply") as submit:
            notifier.run_reply_worker(
                self.config, self.state, self.state_path, self.lock, {}, {}, threading.RLock(),
                "accepted-before-archive", self.logger,
            )
        submit.assert_not_called()
        self.assertNotIn("accepted-before-archive", self.state["handled_message_ids"])

    def test_pre_upgrade_accepted_reply_survives_switch_without_losing_work(self):
        self.state["reply_queue"] = [{
            "request_id": "accepted-before-upgrade", "thread_id": "thread-1",
            "title": "测试任务", "reply": "continue", "status": "queued",
        }]
        notifier.save_state(self.state_path, self.state)
        self.state = notifier.load_state(self.state_path)
        self.switch("recent")
        with mock.patch.object(notifier, "run_codex_reply", return_value=(True, "turn-id")) as submit:
            notifier.run_reply_worker(
                self.config, self.state, self.state_path, self.lock, {}, {}, threading.RLock(),
                "accepted-before-upgrade", self.logger,
            )
        submit.assert_called_once()
        self.assertEqual(self.state["reply_queue"], [])
        self.assertIn("accepted-before-upgrade", self.state["handled_message_ids"])

    def test_automation_run_uses_targets_single_recent_slot_and_routes_completion(self):
        base.NotifierTests().configure_pinned_automation(self.root, self.db_path, self.config)
        self.update_thread("thread-1", updated_at_ms=90000)
        for index in range(11):
            self.add_thread(f"recent-{index}", 1000 + index)
        execution = self.add_thread(
            "automation-execution", 99000, source="automation",
            title="Automation: 每日总结\nAutomation ID: daily\nAutomation memory: memory.md",
        )
        self.state["push_mode"] = "recent"
        self.assertEqual(len(self.recent_ids()), 10)
        self.assertEqual(self.recent_ids()[0], "thread-1")
        self.assertNotIn("automation-execution", self.recent_ids())
        self.baseline()
        notifier.poll_threads(self.config, self.state, self.logger)
        base.NotifierTests.append_completion(execution, "automation-result", "automated answer")
        self.assertEqual(notifier.poll_threads(self.config, self.state, self.logger), 1)
        self.assertEqual(self.state["pending"][0]["thread_id"], "thread-1")
        self.assertEqual(self.state["pending"][0]["source_thread_id"], "automation-execution")

    def test_recent_automation_execution_activity_moves_its_idle_target_into_top_ten(self):
        base.NotifierTests().configure_pinned_automation(self.root, self.db_path, self.config)
        self.update_thread("thread-1", updated_at_ms=1)
        for index in range(11):
            self.add_thread(f"recent-{index}", 1000 + index)
        self.add_thread(
            "automation-execution", 99000, source="automation",
            title="Automation: 每日总结\nAutomation ID: daily\nAutomation memory: memory.md",
        )
        self.state["push_mode"] = "recent"
        ids = self.recent_ids()
        self.assertEqual(ids[0], "thread-1")
        self.assertEqual(len(ids), 10)
        self.assertNotIn("automation-execution", ids)

    def test_http_mode_and_status_authentication_and_persistent_list_snapshot(self):
        self.add_thread("first", 2000)
        self.add_thread("second", 1000)
        self.config.update({"router_host": "127.0.0.1", "router_port": 0, "router_token": "fixture-token"})
        server = notifier.start_quote_router(
            self.config, self.state, self.state_path, self.lock, {}, threading.RLock(), self.logger,
        )
        self.assertIsNotNone(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, payload, token="fixture-token"):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            try:
                connection.request("POST", path, json.dumps(payload), {
                    "Content-Type": "application/json", "X-Codex-Quote-Token": token,
                })
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        self.assertEqual(post("/mode", {"mode": "recent"}, "wrong-token")[0], 401)
        self.assertEqual(self.state["push_mode"], "pinned")
        self.assertEqual(post("/mode", {"mode": "unknown"})[0], 400)
        code, body = post("/mode", {"mode": "recent", "user_id": "test-user", "message_id": "http-mode"})
        self.assertEqual(code, 200)
        self.assertTrue(body["handled"])
        self.assertIn("当前推送模式：近10条活跃任务", body["message"])
        self.assertEqual(notifier.load_state(self.state_path)["recent_task_order"][:2], ["first", "second"])
        self.update_thread("second", updated_at_ms=3000)
        code, body = post("/status", {"user_id": "test-user"})
        self.assertEqual(code, 200)
        self.assertIn("1. 【second】空闲", body["message"])
        self.assertEqual(notifier.load_state(self.state_path)["recent_task_order"][:2], ["second", "first"])


if __name__ == "__main__":
    unittest.main()
