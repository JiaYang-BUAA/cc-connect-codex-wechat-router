"""Execute generated CDP JavaScript against isolated Desktop-shaped fixtures.

These tests never connect to a browser, send a prompt, or touch live queues.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_cdp_transport import (  # noqa: E402
    build_enqueue_queued_follow_up_expression,
    build_probe_expression,
    build_queued_follow_up_count_expression,
    build_queued_follow_up_ids_expression,
    build_queued_follow_up_items_expression,
    build_remove_queued_follow_up_expression,
)


NODE = shutil.which("node")
LOCKS = r"""
const lockNames = [], heldLocks = new Set();
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
  locks: { request: async (name, fn) => {
    assert(!heldLocks.has(name), 'Do not nest the Desktop storage lock');
    lockNames.push(name); heldLocks.add(name);
    try { return await fn(); } finally { heldLocks.delete(name); }
  } },
} });
"""

MODERN = LOCKS + r"""
let state = { other: [{ id: 'other-existing', text: 'unrelated' }] };
let updates = 0, loads = 0, failAfterEnqueue = false, consumeAfterEnqueue = false;
let rejectEnqueue = false, sequence = 0, pageSize = 1, repeatCursor = false;
const requests = [], enqueueCalls = [], removeCalls = [];
const rawByThread = { target: [], other: [{ id: 'other-server', input: [] }] };
const cache = new Map();
const server = {
  isEnabled: () => true,
  load: async id => {
    loads += 1;
    if (failAfterEnqueue && enqueueCalls.length) throw Error('Read failed after acknowledgement');
    assert(Array.isArray(rawByThread[id]));
  },
  read: id => rawByThread[id].map(raw => cache.get(raw.id) ?? { id: raw.id }),
  enqueue: async (...args) => {
    enqueueCalls.push(args);
    assert.equal(args.length, 2, 'No edit-position or default-mode-dependent submission');
    if (rejectEnqueue) throw Error('Acknowledgement unavailable');
    const [id, message] = args;
    const serverId = 'server-' + (++sequence);
    const raw = { id: serverId, clientUserMessageId: message.id,
      input: [{ type: 'text', text: message.text }] };
    rawByThread[id].push(raw);
    cache.set(serverId, { ...message, id: serverId });
    if (consumeAfterEnqueue) rawByThread[id] = rawByThread[id].filter(x => x.id !== serverId);
    return { status: 'queued', messageId: serverId };
  },
  remove: async (id, messageId) => {
    removeCalls.push([id, messageId]);
    const index = rawByThread[id].findIndex(x => x.id === messageId);
    if (index < 0) return null;
    const [raw] = rawByThread[id].splice(index, 1);
    return { index, message: { id: raw.id }, serverSubmission: raw };
  },
};
const manager = {
  hostId: 'local', threadStore: {}, turnCoordinator: { serverQueue: server },
  requestClient: {
    sendRequest: async (method, params) => {
      requests.push({ method, params });
      assert.equal(method, 'thread/queue/list', 'Queue access must not start/steer a turn');
      const start = params.cursor === null ? 0 : Number(params.cursor);
      const all = rawByThread[params.threadId];
      const data = all.slice(start, start + pageSize);
      const nextCursor = repeatCursor ? 'repeat' :
        (start + pageSize < all.length ? String(start + pageSize) : null);
      return { data, nextCursor };
    },
  },
  storage: {
    loadQueuedFollowUps: async () => state,
    updateQueuedFollowUps: async fn => navigator.locks.request(
      'codex-queued-follow-up-state', async () => { updates++; state = fn(state); }),
  },
};
globalThis.window = { __codexRoot: { _internalRoot: {
  current: { memoizedState: { manager } },
} } };
"""

LEGACY = LOCKS + r"""
let state = { target: [{ id: 'desktop-1', text: 'Desktop message' }],
  other: [{ id: 'other-existing', text: 'unrelated' }] };
let saves = 0, cacheWrites = 0;
const query = { queryKey: ['get-global-state', { key: 'queued-follow-ups' }],
  state: { data: { value: state } } };
const queryClient = {
  getQueryCache: () => ({ getAll: () => [query] }),
  setQueryData: (key, data) => {
    assert.deepEqual(key, query.queryKey); cacheWrites++; query.state.data = data;
  },
};
const manager = { hostId: 'local', threadStore: {}, requestClient: {}, scope: {},
  fetchFromHost: async (method, { params }) => {
    assert.equal(params.key, 'queued-follow-ups');
    if (method === 'get-global-state') return { value: state };
    assert.equal(method, 'set-global-state'); saves++; state = params.value;
    return { success: true };
  },
};
globalThis.window = { __codexRoot: { _internalRoot: {
  current: { memoizedState: { manager, queryClient } },
} } };
"""


@unittest.skipUnless(NODE, "Node.js is required to execute generated CDP JavaScript")
class DesktopCdpRuntimeTests(unittest.TestCase):
    def run_js(self, setup: str, body: str) -> None:
        script = (
            "const assert = require('node:assert/strict');\n"
            "(async () => {\n" + setup + "\n" + body + "\n"
            "process.stdout.write('ok');\n"
            "})().catch(error => { console.error(error.stack); process.exitCode = 1; });"
        )
        result = subprocess.run(
            [NODE, "-"], input=script, text=True, encoding="utf-8",
            capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout, "ok", result.stderr)

    @staticmethod
    def enqueue(message_id: str = "wechat-stable") -> str:
        return build_enqueue_queued_follow_up_expression(
            "target", "继续处理 'quoted'\n第二行", "E:/workspace", message_id, 1234
        )

    def test_live_fiber_seed_bypasses_large_root_graph(self):
        self.run_js(
            r"""
const client = { hostId: 'local', requestPromises: new Map(), sendRequest() {} };
const largeGraph = Array.from({ length: 220000 }, () => ({}));
const root = { largeGraph, child: { memoizedState: { client } } };
globalThis.window = { __codexRoot: { _internalRoot: { current: root } } };
let descriptorReads = 0;
const getDescriptors = Object.getOwnPropertyDescriptors;
Object.getOwnPropertyDescriptors = obj => { descriptorReads++; return getDescriptors(obj); };
""",
            f"const result = await ({build_probe_expression()});\n"
            "assert(result.ok); assert(descriptorReads < 100, 'Should prioritize live fiber state');",
        )

    def test_request_client_beyond_previous_200k_limit_is_found(self):
        self.run_js(
            r"""
const client = { hostId: 'local', requestPromises: new Map(), sendRequest() {} };
let chain = client;
for (let n = 0; n < 200010; n++) chain = { next: chain };
globalThis.window = { __codexRoot: { _internalRoot: { current: { chain } } } };
""",
            f"assert((await ({build_probe_expression()})).ok);",
        )

    def test_remote_client_is_not_mistaken_for_local_client(self):
        self.run_js(
            r"""
const remote = { hostId: 'remote', requestPromises: new Map(), sendRequest() {} };
globalThis.window = { __codexRoot: { _internalRoot: { current: { memoizedState: remote } } } };
""",
            f"await assert.rejects(({build_probe_expression()}), /request client was not found/);",
        )

    def test_lookup_deadline_stops_safely(self):
        self.run_js(
            r"""
let clock = 0; Date.now = () => { clock += 3000; return clock; };
globalThis.window = { __codexRoot: { _internalRoot: { current: { child: {} } } } };
""",
            f"await assert.rejects(({build_probe_expression()}), /safe lookup budget exceeded/);",
        )

    def test_server_enqueue_records_server_id_and_deduplicates_client_id(self):
        self.run_js(
            MODERN,
            f"const first = await ({self.enqueue()});\n"
            f"const retry = await ({self.enqueue()});\n"
            f"const ids = await ({build_queued_follow_up_ids_expression('target')});\n"
            "assert.equal(first.queuedMessageId, 'server-1'); assert(first.inserted);\n"
            "assert.equal(retry.queuedMessageId, 'server-1'); assert(!retry.inserted);\n"
            "assert.equal(enqueueCalls.length, 1); assert.equal(updates, 0);\n"
            "assert.deepEqual(new Set(ids.queuedIds), new Set(['server-1', 'wechat-stable']));\n"
            "assert.deepEqual(state.other, [{id:'other-existing',text:'unrelated'}]);\n"
            "assert.deepEqual(rawByThread.other, [{id:'other-server',input:[]}]);\n"
            "assert(lockNames.includes('cc-connect-queue-target'));",
        )

    def test_server_pagination_keeps_desktop_messages_and_counts_once(self):
        self.run_js(
            MODERN + r"""
rawByThread.target = [
 {id:'desktop-a',clientUserMessageId:'client-a',input:[{type:'text',text:'one'}]},
 {id:'desktop-b',clientUserMessageId:'client-b',input:[{type:'text',text:'two'}]},
 {id:'desktop-c',clientUserMessageId:'client-c',input:[{type:'text',text:'three'}]},
];
""",
            f"const items = await ({build_queued_follow_up_items_expression('target')});\n"
            f"const count = await ({build_queued_follow_up_count_expression('target')});\n"
            f"const added = await ({self.enqueue()});\n"
            "assert.equal(count.queuedCount, 3); assert.equal(added.queuedCount, 4);\n"
            "assert.deepEqual(items.queuedItems.map(x=>x.text), ['one','two','three']);\n"
            "assert.deepEqual(rawByThread.target.map(x=>x.id), ['desktop-a','desktop-b','desktop-c','server-1']);\n"
            "assert(requests.some(x=>x.params.cursor === '2'));",
        )

    def test_server_retry_finds_client_id_on_later_page(self):
        self.run_js(
            MODERN + r"""
rawByThread.target = [
 {id:'desktop-first',clientUserMessageId:'desktop-client',input:[]},
 {id:'already-persisted',clientUserMessageId:'wechat-stable',input:[]},
];
""",
            f"const result = await ({self.enqueue()});\n"
            "assert(!result.inserted); assert.equal(result.queuedMessageId,'already-persisted');\n"
            "assert.equal(enqueueCalls.length,0); assert.equal(result.queuedCount,2);",
        )

    def test_server_remove_maps_client_id_and_keeps_other_messages(self):
        self.run_js(
            MODERN + r"""
rawByThread.target = [
 {id:'keep',clientUserMessageId:'desktop-client',input:[]},
 {id:'server-real',clientUserMessageId:'wechat-stable',input:[]},
];
""",
            f"const removed = await ({build_remove_queued_follow_up_expression('target', 'wechat-stable')});\n"
            f"const again = await ({build_remove_queued_follow_up_expression('target', 'wechat-stable')});\n"
            "assert(removed.removed); assert.equal(removed.queuedCount,1); assert(!again.removed);\n"
            "assert.deepEqual(removeCalls,[['target','server-real']]);\n"
            "assert.deepEqual(rawByThread.target.map(x=>x.id),['keep']);\n"
            "assert.deepEqual(rawByThread.other,[{id:'other-server',input:[]}]);",
        )

    def test_server_acceptance_remains_success_if_followup_read_fails(self):
        self.run_js(
            MODERN + "failAfterEnqueue = true;",
            f"const result = await ({self.enqueue()});\n"
            "assert(result.ok); assert(result.inserted); assert.equal(result.queuedMessageId,'server-1');\n"
            "assert.equal(result.queuedCount,1); assert.equal(enqueueCalls.length,1);\n"
            "assert.equal(updates,0);",
        )

    def test_server_acceptance_remains_success_if_already_consumed(self):
        self.run_js(
            MODERN + "consumeAfterEnqueue = true;",
            f"const result = await ({self.enqueue()});\n"
            "assert(result.ok); assert.equal(result.queuedMessageId,'server-1');\n"
            "assert.equal(enqueueCalls.length,1); assert.equal(rawByThread.target.length,0);\n"
            "assert(result.queuedItems.some(x=>x.clientMessageId==='wechat-stable'));",
        )

    def test_server_enqueue_error_is_not_retried_via_local_storage(self):
        self.run_js(
            MODERN + "rejectEnqueue = true;",
            f"await assert.rejects(({self.enqueue()}), /Acknowledgement unavailable/);\n"
            "assert.equal(enqueueCalls.length,1); assert.equal(updates,0);\n"
            "assert.equal(rawByThread.target.length,0);",
        )

    def test_server_nonqueued_acknowledgement_is_not_success(self):
        self.run_js(
            MODERN + "server.enqueue = async () => ({status:'error',messageId:'not-confirmed'});",
            f"await assert.rejects(({self.enqueue()}), /acknowledge|acknowledgement/);\n"
            "assert.equal(updates,0);",
        )

    def test_remote_manager_is_not_used_for_queue_changes(self):
        self.run_js(
            MODERN + "manager.hostId = 'remote';",
            f"await assert.rejects(({self.enqueue()}), /local manager was not found/);\n"
            "assert.equal(updates,0); assert.equal(enqueueCalls.length,0);",
        )

    def test_server_repeated_pagination_cursor_fails_without_mutation(self):
        self.run_js(
            MODERN + "repeatCursor = true;",
            f"await assert.rejects(({self.enqueue()}), /Invalid Desktop server queue pagination/);\n"
            "assert.equal(enqueueCalls.length,0); assert.equal(updates,0); assert(requests.length<=2);",
        )

    def test_existing_modern_local_queue_preserves_desktop_backend(self):
        self.run_js(
            MODERN + "state.target = [{id:'desktop-local',text:'existing'}];",
            f"const first = await ({self.enqueue()});\n"
            f"const again = await ({self.enqueue()});\n"
            f"const removed = await ({build_remove_queued_follow_up_expression('target', 'wechat-stable')});\n"
            "assert(first.inserted); assert(!again.inserted); assert(removed.removed);\n"
            "assert.equal(enqueueCalls.length,0); assert.equal(requests.length,0);\n"
            "assert.deepEqual(state.target,[{id:'desktop-local',text:'existing'}]);\n"
            "assert.deepEqual(state.other,[{id:'other-existing',text:'unrelated'}]);\n"
            "assert.deepEqual(lockNames,Array(3).fill('codex-queued-follow-up-state'));",
        )

    def test_modern_storage_rechecks_latest_state_inside_update(self):
        self.run_js(
            MODERN + r"""
server.isEnabled = () => false;
const update = manager.storage.updateQueuedFollowUps;
manager.storage.updateQueuedFollowUps = async fn => {
 state.target = [...(state.target ?? []), {id:'concurrent-desktop',text:'just queued'}];
 state.newThread = [{id:'new-thread-message',text:'other task'}];
 return update(fn);
};
""",
            f"const result = await ({self.enqueue()});\n"
            "assert.equal(result.queuedCount,2);\n"
            "assert.deepEqual(state.target.map(x=>x.id),['concurrent-desktop','wechat-stable']);\n"
            "assert.equal(state.newThread[0].id,'new-thread-message'); assert.equal(enqueueCalls.length,0);",
        )

    def test_legacy_fetch_cache_queue_keeps_others_and_deduplicates(self):
        self.run_js(
            LEGACY,
            f"const first = await ({self.enqueue()});\n"
            f"const again = await ({self.enqueue()});\n"
            f"const removed = await ({build_remove_queued_follow_up_expression('target', 'wechat-stable')});\n"
            "assert(first.inserted); assert.equal(first.queuedCount,2); assert(!again.inserted);\n"
            "assert(removed.removed); assert.equal(saves,2); assert.equal(cacheWrites,3);\n"
            "assert.deepEqual(state.target,[{id:'desktop-1',text:'Desktop message'}]);\n"
            "assert.deepEqual(state.other,[{id:'other-existing',text:'unrelated'}]);\n"
            "assert.deepEqual(query.state.data.value,state);\n"
            "assert.deepEqual(lockNames,Array(3).fill('codex-queued-follow-up-state'));",
        )


if __name__ == "__main__":
    unittest.main()
