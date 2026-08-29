from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_cdp_transport import (  # noqa: E402
    DesktopCdpClient,
    build_enqueue_queued_follow_up_expression,
    build_follow_up_expression,
    build_probe_expression,
    build_queued_follow_up_count_expression,
    build_queued_follow_up_ids_expression,
    build_queued_follow_up_items_expression,
    build_remove_queued_follow_up_expression,
    select_primary_codex_target,
    validate_loopback_http_url,
)


class DesktopCdpTransportTests(unittest.TestCase):
    def test_loopback_http_validation(self):
        self.assertEqual(
            validate_loopback_http_url("http://127.0.0.1:9335"),
            ("127.0.0.1", 9335),
        )
        with self.assertRaises(ValueError):
            validate_loopback_http_url("http://192.168.1.5:9335")
        with self.assertRaises(ValueError):
            validate_loopback_http_url("https://127.0.0.1:9335")

    def test_selects_primary_page_not_overlay_or_browser(self):
        target = select_primary_codex_target(
            [
                {
                    "type": "page",
                    "url": "https://example.com",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/a",
                },
                {
                    "type": "page",
                    "url": "app://-/index.html?initialRoute=%2Favatar-overlay",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/b",
                },
                {
                    "type": "page",
                    "url": (
                        "app://-/index.html?initialRoute="
                        "%2Flocal%2F01a017a2-6cc1-75a3-9436-943af1bd2518"
                    ),
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9335/devtools/page/c",
                },
            ]
        )
        self.assertTrue(target["webSocketDebuggerUrl"].endswith("/c"))

    def test_discovers_codex_cdp_when_configured_port_changes(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        codex_targets = [
            {
                "type": "page",
                "url": "app://-/index.html?initialRoute=%2Flocal%2Fthread-1",
                "webSocketDebuggerUrl": (
                    "ws://127.0.0.1:9354/devtools/page/codex"
                ),
            }
        ]

        def fetch(port, _timeout):
            if port == 9354:
                return codex_targets
            raise RuntimeError("unavailable")

        with mock.patch.object(client, "_fetch_targets", side_effect=fetch) as fetch_targets:
            self.assertEqual(client.list_targets(), codex_targets)
        self.assertEqual(client.port, 9354)
        self.assertEqual(fetch_targets.call_args_list[0].args[0], 9335)

    def test_cdp_discovery_rejects_non_codex_debug_targets(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        browser_targets = [
            {
                "type": "page",
                "url": "https://example.com",
                "webSocketDebuggerUrl": (
                    "ws://127.0.0.1:9336/devtools/page/browser"
                ),
            }
        ]

        def fetch(port, _timeout):
            if port == 9335:
                raise RuntimeError("configured port unavailable")
            if port == 9336:
                return browser_targets
            raise RuntimeError("unavailable")

        with mock.patch.object(client, "_fetch_targets", side_effect=fetch):
            with self.assertRaisesRegex(RuntimeError, "configured port unavailable"):
                client.list_targets()
        self.assertEqual(client.port, 9335)

    def test_expression_uses_json_payload_without_string_injection(self):
        expression = build_follow_up_expression(
            "thread-1",
            "引号'\"和换行\n测试",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        self.assertIn("findDesktopRequestClient", expression)
        self.assertIn("thread/resume", expression)
        self.assertIn("turn/start", expression)
        self.assertIn(json.dumps("thread-1"), expression)
        self.assertIn("requestState", expression)
        self.assertIn('"model":"gpt-5.6-sol"', expression)
        self.assertIn('"effort":"high"', expression)
        self.assertNotIn("引号'\"和换行\n测试", expression)

    def test_expression_omits_unavailable_model_preferences(self):
        expression = build_follow_up_expression("thread-1", "继续")
        self.assertNotIn('\\"model\\":null', expression)
        self.assertNotIn('\\"effort\\":null', expression)

    def test_probe_expression_does_not_submit(self):
        expression = build_probe_expression()
        self.assertIn("requestExport", expression)
        self.assertIn("findDesktopRequestClient", expression)
        self.assertNotIn("turn/start", expression)

    def test_queued_follow_up_expression_returns_only_count(self):
        expression = build_queued_follow_up_count_expression("thread-1")
        self.assertIn("findDesktopQueuedFollowUpsContext", expression)
        self.assertIn(json.dumps("thread-1"), expression)
        self.assertIn("queuedCount", expression)
        self.assertNotIn("turn/start", expression)
        self.assertNotIn("queuedForThread,", expression)

    def test_queued_follow_up_ids_expression_returns_only_ids(self):
        expression = build_queued_follow_up_ids_expression("thread-1")
        self.assertIn("findDesktopQueuedFollowUpsContext", expression)
        self.assertIn(json.dumps("thread-1"), expression)
        self.assertIn("queuedIds", expression)
        self.assertNotIn("item?.text", expression)

    def test_queued_follow_up_items_expression_returns_ordered_previews(self):
        expression = build_queued_follow_up_items_expression("thread-1")
        self.assertIn("findDesktopQueuedFollowUpsContext", expression)
        self.assertIn("queuedItems", expression)
        self.assertIn("item?.text", expression)
        self.assertIn("item?.context?.prompt", expression)
        self.assertIn("createdAt", expression)

    def test_enqueue_queued_follow_up_uses_native_state_and_cache(self):
        expression = build_enqueue_queued_follow_up_expression(
            "thread-1",
            "继续处理",
            r"E:\\codex",
            "message-1",
            1_700_000_000_000,
        )
        self.assertIn("findDesktopManager", expression)
        self.assertIn("'get-global-state'", expression)
        self.assertIn("'set-global-state'", expression)
        self.assertIn("'queued-follow-ups'", expression)
        self.assertIn("'codex-queued-follow-up-state'", expression)
        self.assertIn("queryClient.setQueryData", expression)
        self.assertIn('"id":"message-1"', expression)
        self.assertIn('"prompt":' + json.dumps("继续处理", ensure_ascii=True), expression)
        self.assertIn("queuedItems: messages.map", expression)
        self.assertNotIn("turn/start", expression)

    def test_remove_queued_follow_up_uses_native_state_and_cache(self):
        expression = build_remove_queued_follow_up_expression(
            "thread-1", "message-1"
        )
        self.assertIn("findDesktopManager", expression)
        self.assertIn("'set-global-state'", expression)
        self.assertIn("'codex-queued-follow-up-state'", expression)
        self.assertIn("queryClient.setQueryData", expression)
        self.assertIn('"messageId":"message-1"', expression)
        self.assertNotIn("turn/start", expression)

    def test_send_follow_up_returns_runtime_value(self):
        connection = mock.MagicMock()
        connection.iter_text.return_value = iter(
            [
                json.dumps(
                    {
                        "id": 1,
                        "result": {
                            "result": {
                                "type": "object",
                                "value": {"ok": True, "requestExport": "qTt"},
                            }
                        },
                    }
                )
            ]
        )
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with (
            mock.patch.object(
                client,
                "list_targets",
                return_value=[
                    {
                        "type": "page",
                        "url": "app://-/index.html",
                        "webSocketDebuggerUrl": (
                            "ws://127.0.0.1:9335/devtools/page/test"
                        ),
                    }
                ],
            ),
            mock.patch(
                "desktop_cdp_transport.WebSocketConnection",
                return_value=connection,
            ),
        ):
            result = client.send_follow_up(
                "thread-1", "继续", model="gpt-5.6-sol", reasoning_effort="high"
            )
        self.assertEqual(result["requestExport"], "qTt")
        sent = connection.send_json.call_args.args[0]
        self.assertEqual(sent["method"], "Runtime.evaluate")
        self.assertIn('"effort":"high"', sent["params"]["expression"])
        connection.close.assert_called_once_with()

    def test_evaluate_enforces_total_deadline_while_events_keep_arriving(self):
        connection = mock.MagicMock()
        connection.iter_text.return_value = iter(
            [
                json.dumps({"method": "Runtime.consoleAPICalled"}),
                json.dumps({"method": "Runtime.bindingCalled"}),
            ]
        )
        client = DesktopCdpClient("http://127.0.0.1:9335", timeout_seconds=1)
        with (
            mock.patch.object(
                client,
                "list_targets",
                return_value=[
                    {
                        "type": "page",
                        "url": "app://-/index.html?initialRoute=%2Flocal%2Fthread-1",
                        "webSocketDebuggerUrl": (
                            "ws://127.0.0.1:9335/devtools/page/test"
                        ),
                    }
                ],
            ),
            mock.patch(
                "desktop_cdp_transport.WebSocketConnection",
                return_value=connection,
            ),
            mock.patch(
                "desktop_cdp_transport.time.monotonic",
                side_effect=[0.0, 0.2, 1.1],
            ),
        ):
            with self.assertRaisesRegex(TimeoutError, "request timed out"):
                client.evaluate("Promise.resolve({ok: true})")
        connection.close.assert_called_once_with()

    def test_get_queued_follow_up_count_returns_runtime_value(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with mock.patch.object(
            client,
            "evaluate",
            return_value={"ok": True, "queuedCount": 2},
        ) as evaluate:
            count = client.get_queued_follow_up_count("thread-1")
        self.assertEqual(count, 2)
        self.assertIn("queuedCount", evaluate.call_args.args[0])

    def test_get_queued_follow_up_count_rejects_invalid_value(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with mock.patch.object(
            client,
            "evaluate",
            return_value={"ok": True, "queuedCount": "2"},
        ):
            with self.assertRaises(RuntimeError):
                client.get_queued_follow_up_count("thread-1")

    def test_native_queue_client_methods_validate_runtime_values(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with mock.patch.object(
            client,
            "evaluate",
            side_effect=[
                {"ok": True, "queuedIds": ["message-1"]},
                {
                    "ok": True,
                    "inserted": True,
                    "queuedMessageId": "message-1",
                    "queuedCount": 1,
                    "queuedItems": [
                        {"id": "message-1", "text": "继续", "createdAt": 123}
                    ],
                },
                {"ok": True, "removed": True, "queuedCount": 0},
            ],
        ) as evaluate:
            self.assertEqual(
                client.get_queued_follow_up_ids("thread-1"), ["message-1"]
            )
            self.assertTrue(
                client.enqueue_queued_follow_up(
                    "thread-1", "继续", r"E:\\codex", "message-1", 123
                )["inserted"]
            )
            self.assertTrue(
                client.remove_queued_follow_up("thread-1", "message-1")["removed"]
            )
        self.assertEqual(evaluate.call_count, 3)

    def test_get_queued_follow_ups_validates_runtime_value(self):
        client = DesktopCdpClient("http://127.0.0.1:9335")
        with mock.patch.object(
            client,
            "evaluate",
            return_value={
                "ok": True,
                "queuedItems": [
                    {"id": "one", "text": "第一条", "createdAt": 100},
                    {"id": "two", "text": "第二条", "createdAt": 200.0},
                ],
            },
        ):
            items = client.get_queued_follow_ups("thread-1")
        self.assertEqual([item["text"] for item in items], ["第一条", "第二条"])
        self.assertEqual(items[1]["createdAt"], 200)


if __name__ == "__main__":
    unittest.main()
