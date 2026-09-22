from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from websocket_transport import WebSocketConnection


DESKTOP_OBJECT_LOOKUP = r"""
  function findDesktopObject(predicate, description) {
    const root = window.__codexRoot?._internalRoot?.current;
    if (!root) throw new Error('Codex Desktop React root was not found');
    const deadline = Date.now() + 2500;
    // Start with live component state, not the DOM/history graph hanging off
    // the root. The latter can exceed hundreds of thousands of objects.
    const fibers = [root], fiberSeen = new WeakSet(), queue = [];
    for (let i = 0; i < fibers.length && i < 50000; i++) {
      if ((i & 255) === 0 && Date.now() > deadline) break;
      const fiber = fibers[i];
      if (!fiber || typeof fiber !== 'object' || fiberSeen.has(fiber)) continue;
      fiberSeen.add(fiber);
      queue.push(fiber.memoizedState, fiber.memoizedProps, fiber.dependencies);
      if (fiber.child) fibers.push(fiber.child);
      if (fiber.sibling) fibers.push(fiber.sibling);
    }
    queue.push(root);
    const seen = new WeakSet();
    let cursor = 0;
    let visited = 0;
    while (cursor < queue.length && visited < 750000) {
      if ((cursor & 255) === 0 && Date.now() > deadline) break;
      const value = queue[cursor++];
      if (
        value == null ||
        (typeof value !== 'object' && typeof value !== 'function') ||
        seen.has(value)
      ) continue;
      seen.add(value);
      visited += 1;
      try {
        if (predicate(value)) return value;
      } catch {}
      // DOM nodes and binary buffers cannot own the app services we need.
      if (ArrayBuffer.isView(value) ||
          (typeof Node === 'function' && value instanceof Node)) continue;
      let descriptors;
      try {
        descriptors = Object.getOwnPropertyDescriptors(value);
      } catch {
        continue;
      }
      for (const descriptor of Object.values(descriptors)) {
        if (!Object.prototype.hasOwnProperty.call(descriptor, 'value')) continue;
        const child = descriptor.value;
        if (
          child != null &&
          (typeof child === 'object' || typeof child === 'function')
        ) {
          if (queue.length >= 2000000) break;
          queue.push(child);
        }
      }
      if (value instanceof Map) {
        for (const [key, child] of value) {
          if (queue.length >= 2000000) break;
          queue.push(key, child);
        }
      } else if (value instanceof Set) {
        for (const child of value) {
          if (queue.length >= 2000000) break;
          queue.push(child);
        }
      }
    }
    const limited = cursor < queue.length;
    throw new Error('Codex Desktop ' + description + ' was not found' +
      (limited ? ' (safe lookup budget exceeded)' : ''));
  }
""".strip()


DESKTOP_REQUEST_CLIENT_LOOKUP = r"""
  function findDesktopRequestClient() {
    return findDesktopObject((value) => (
      typeof value.sendRequest === 'function' &&
      value.hostId === 'local' && value.requestPromises instanceof Map
    ), 'AppServer request client');
  }
""".strip()


DESKTOP_QUEUED_FOLLOW_UP_LOOKUP = r"""
  function findDesktopQueuedFollowUpsContext() {
    let query;
    const queryClient = findDesktopObject((value) => {
      if (typeof value.getQueryCache === 'function') {
        const queries = value.getQueryCache().getAll();
        if (Array.isArray(queries)) {
            query = queries.find((candidate) => {
              const key = candidate?.queryKey;
              return (
                Array.isArray(key) &&
                key.includes('get-global-state') &&
                JSON.stringify(key).includes('queued-follow-ups')
              );
            });
            return !!query;
        }
      }
      return false;
    }, 'queued follow-up cache');
    return { query, queryClient };
  }
""".strip()


DESKTOP_MANAGER_LOOKUP = r"""
  function findDesktopManager() {
    return findDesktopObject((value) => (
      value.hostId === 'local' && value.threadStore && value.requestClient && (
        (typeof value.fetchFromHost === 'function' && value.scope) ||
        (typeof value.storage?.loadQueuedFollowUps === 'function' &&
         typeof value.storage?.updateQueuedFollowUps === 'function')
      )
    ), 'local manager');
  }
""".strip()


DESKTOP_QUEUE_ACCESS = r"""
  async function queueMutation(operation) {
    try { return await operation(); }
    catch (error) {
      throw new Error('Codex Desktop queue submission outcome unknown: ' + String(error));
    }
  }
  function queuePreview(item) {
    return {
      id: String(item?.id ?? ''),
      clientMessageId: String(item?.clientMessageId ?? ''),
      text: String(item?.text ?? item?.context?.prompt ?? ''),
      createdAt: Number(item?.createdAt ?? 0),
    };
  }
  async function withBridgeQueueLock(threadId, operation) {
    return globalThis.navigator?.locks
      ? globalThis.navigator.locks.request('cc-connect-queue-' + threadId, operation)
      : operation();
  }
  async function desktopQueueAccess(threadId) {
    const manager = findDesktopManager();
    const storage = manager.storage;
    if (typeof storage?.loadQueuedFollowUps === 'function' &&
        typeof storage?.updateQueuedFollowUps === 'function') {
      const local = await storage.loadQueuedFollowUps();
      const server = manager.turnCoordinator?.serverQueue;
      if (server?.isEnabled(threadId) && !(local?.[threadId]?.length)) {
        // Desktop 26.915+ owns this queue on the app server. Writing the old
        // global-state key here would hide messages queued from Desktop.
        const read = async () => {
          await server.load(threadId);
          const cached = new Map((server.read(threadId) ?? []).map(item => [item.id, item]));
          const items = [], cursors = new Set();
          let cursor = null;
          do {
            const page = await manager.requestClient.sendRequest('thread/queue/list',
              { threadId, cursor }, { priority: 'critical', source: 'wechat_quote' });
            if (!Array.isArray(page?.data)) throw new Error('Invalid Desktop server queue');
            for (const raw of page.data) {
              const item = cached.get(raw.id);
              items.push({ ...item, id: String(raw.id),
                clientMessageId: String(raw.clientUserMessageId ?? ''),
                text: item?.text ?? (raw.input ?? []).map(part => part.text ?? '').join(''),
                createdAt: Number(item?.createdAt ?? 0) });
            }
            cursor = page.nextCursor ?? null;
            if (cursor !== null && (typeof cursor !== 'string' || cursors.has(cursor) || cursors.size >= 1000)) {
              throw new Error('Invalid Desktop server queue pagination');
            }
            cursors.add(cursor);
          } while (cursor !== null);
          return items;
        };
        return {
          read,
          enqueue: message => withBridgeQueueLock(threadId, async () => {
            const before = await read();
            const existing = before.find(item => item.id === message.id || item.clientMessageId === message.id);
            if (existing) return { inserted: false, id: existing.id, items: before };
            // This explicit queue API never consults the user's send/steer default.
            const result = await queueMutation(() => server.enqueue(threadId, message));
            if (result?.status !== 'queued' || !result.messageId) {
              throw new Error('Codex Desktop queue submission outcome unknown: server acknowledgement missing');
            }
            // A successfully accepted item may already have started or the
            // subsequent read may fail. Neither means it is safe to resubmit.
            const accepted = { ...message, id: result.messageId, clientMessageId: message.id };
            let items;
            try { items = await read(); } catch { items = before; }
            if (!items.some(item => item.id === result.messageId)) items = [...items, accepted];
            return { inserted: true, id: result.messageId, items };
          }),
          remove: id => withBridgeQueueLock(threadId, async () => {
            const before = await read();
            const item = before.find(item => item.id === id || item.clientMessageId === id);
            if (!item) return { removed: false, count: before.length };
            const removed = await server.remove(threadId, item.id);
            return { removed: removed != null, count: Math.max(0, before.length - (removed != null ? 1 : 0)) };
          }),
        };
      }
      // Earlier Desktop versions use persisted local queues. The storage
      // callback already acquires codex-queued-follow-up-state; do not nest it.
      return localQueueAccess(threadId,
        () => storage.loadQueuedFollowUps(),
        transform => storage.updateQueuedFollowUps(transform));
    }
    const { query, queryClient } = findDesktopQueuedFollowUpsContext();
    const read = async () => (await manager.fetchFromHost('get-global-state',
      { params: { key: 'queued-follow-ups' } }))?.value ?? {};
    const update = async transform => {
      const operation = async () => {
        const before = await read();
        const next = transform(before);
        if (next !== before) {
          const saved = await manager.fetchFromHost('set-global-state',
            { params: { key: 'queued-follow-ups', value: next } });
          if (saved?.success !== true) throw new Error('Codex Desktop did not persist the queued follow-up');
        }
        queryClient.setQueryData(query.queryKey, { value: next });
      };
      return globalThis.navigator?.locks
        ? globalThis.navigator.locks.request('codex-queued-follow-up-state', operation)
        : operation();
    };
    return localQueueAccess(threadId, read, update);
  }
  function localQueueAccess(threadId, readState, updateState) {
    return {
      read: async () => (await readState())?.[threadId] ?? [],
      enqueue: async message => {
        let items, inserted = false;
        await queueMutation(() => updateState(state => {
          state = state ?? {};
          const current = state[threadId] ?? [];
          inserted = !current.some(item => item.id === message.id);
          items = inserted ? [...current, message] : current;
          return inserted ? { ...state, [threadId]: items } : state;
        }));
        if (!items) throw new Error('Codex Desktop queue submission outcome unknown: update not confirmed');
        return { inserted, id: message.id, items };
      },
      remove: async id => {
        let items, removed = false;
        await updateState(state => {
          state = state ?? {};
          const current = state[threadId] ?? [];
          items = current.filter(item => item.id !== id);
          removed = items.length !== current.length;
          if (!removed) return state;
          const next = { ...state };
          if (items.length) next[threadId] = items;
          else delete next[threadId];
          return next;
        });
        if (!items) throw new Error('Desktop queue removal was not confirmed');
        return { removed, count: items.length };
      },
    };
  }
""".strip()


def queue_lookup_code() -> str:
    return "\n".join((DESKTOP_OBJECT_LOOKUP, DESKTOP_MANAGER_LOOKUP,
                      DESKTOP_QUEUED_FOLLOW_UP_LOOKUP, DESKTOP_QUEUE_ACCESS))


def validate_loopback_http_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("Codex Desktop CDP URL must use http://")
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Codex Desktop CDP must listen on loopback")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Codex Desktop CDP URL must not include a path")
    return host, parsed.port or 80


def select_primary_codex_target(targets: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for target in targets:
        target_url = str(target.get("url") or "")
        parsed = urlsplit(target_url)
        if (
            target.get("type") == "page"
            and parsed.scheme == "app"
            and parsed.netloc == "-"
            and parsed.path == "/index.html"
            and "avatar-overlay" not in parsed.query
            and str(target.get("webSocketDebuggerUrl") or "").startswith("ws://")
        ):
            candidates.append(target)
    if not candidates:
        raise RuntimeError("Codex Desktop primary page is not available through CDP")
    return candidates[0]


def build_follow_up_expression(
    thread_id: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    request_payload: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        "approvalPolicy": "never",
    }
    if model:
        request_payload["model"] = model
    if reasoning_effort:
        request_payload["effort"] = reasoning_effort
    payload = json.dumps(
        request_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""
(async () => {{
  {DESKTOP_OBJECT_LOOKUP}
  {DESKTOP_REQUEST_CLIENT_LOOKUP}
  const request = findDesktopRequestClient();
  const payload = {payload};
  await request.sendRequest(
    'thread/resume',
    {{ threadId: payload.threadId }},
    {{ priority: 'critical', source: 'wechat_quote' }}
  );
  const result = await request.sendRequest(
    'turn/start',
    payload,
    {{ priority: 'critical', source: 'wechat_quote' }}
  );
  return {{
    ok: true,
    requestExport: 'react-fiber-app-server',
    requestState: 'dispatched',
    turnId: result?.turn?.id ?? null,
  }};
}})()
""".strip()


def build_probe_expression() -> str:
    return f"""
(async () => {{
  {DESKTOP_OBJECT_LOOKUP}
  {DESKTOP_REQUEST_CLIENT_LOOKUP}
  const request = findDesktopRequestClient();
  return {{
    ok: true,
    requestExport: 'react-fiber-app-server',
    functionName: request.sendRequest.name || 'sendRequest',
  }};
}})()
""".strip()


def build_queued_follow_up_count_expression(thread_id: str) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    return f"""
(async () => {{
  {queue_lookup_code()}
  const access = await desktopQueueAccess({encoded_thread_id});
  const items = await access.read();
  return {{
    ok: true,
    queuedCount: items.length,
  }};
}})()
""".strip()


def build_queued_follow_up_ids_expression(thread_id: str) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    return f"""
(async () => {{
  {queue_lookup_code()}
  const access = await desktopQueueAccess({encoded_thread_id});
  const items = await access.read();
  return {{
    ok: true,
    queuedIds: [...new Set(items.flatMap(item => [item.id, item.clientMessageId]).filter(Boolean))],
  }};
}})()
""".strip()


def build_queued_follow_up_items_expression(thread_id: str) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    return f"""
(async () => {{
  {queue_lookup_code()}
  const access = await desktopQueueAccess({encoded_thread_id});
  const items = await access.read();
  return {{
    ok: true,
    queuedItems: items.map(queuePreview),
  }};
}})()
""".strip()


def build_enqueue_queued_follow_up_expression(
    thread_id: str,
    prompt: str,
    cwd: str,
    message_id: str,
    created_at_ms: int,
) -> str:
    payload = json.dumps(
        {
            "threadId": thread_id,
            "message": {
                "id": message_id,
                "text": prompt,
                "context": {
                    "prompt": prompt,
                    "addedFiles": [],
                    "fileAttachments": [],
                    "pastedTextAttachments": [],
                    "imageAttachments": [],
                    "appshotContexts": [],
                    "commentAttachments": [],
                    "mcpAppModelContextAttachments": [],
                    "computerUseAppMentions": [],
                    "chatGptConversationContexts": [],
                    "responseTextAnnotations": [],
                    "selectedTextAttachments": [],
                    "pullRequestChecks": [],
                    "pullRequestMergeConflict": None,
                    "threadReferences": [],
                    "workspaceRoots": [cwd] if cwd else [],
                    "collaborationMode": None,
                },
                "cwd": cwd,
                "createdAt": created_at_ms,
                "mentionedBrowserFamilies": [],
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"""
(async () => {{
  {queue_lookup_code()}
  const payload = {payload};
  const access = await desktopQueueAccess(payload.threadId);
  const result = await access.enqueue(payload.message);
  return {{
      ok: true,
      inserted: result.inserted,
      queuedMessageId: result.id,
      queuedCount: result.items.length,
      queuedItems: result.items.map(queuePreview),
  }};
}})()
""".strip()


def build_remove_queued_follow_up_expression(
    thread_id: str, message_id: str
) -> str:
    payload = json.dumps(
        {"threadId": thread_id, "messageId": message_id},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"""
(async () => {{
  {queue_lookup_code()}
  const payload = {payload};
  const access = await desktopQueueAccess(payload.threadId);
  const result = await access.remove(payload.messageId);
  return {{ ok: true, removed: result.removed, queuedCount: result.count }};
}})()
""".strip()


class DesktopCdpClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0):
        self.host, self.port = validate_loopback_http_url(base_url)
        self.timeout_seconds = timeout_seconds

    def _fetch_targets(self, port: int, timeout_seconds: float) -> list[dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.host, port, timeout=timeout_seconds
        )
        try:
            connection.request("GET", "/json/list")
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"Codex Desktop CDP returned HTTP {response.status}"
                )
        except OSError as exc:
            raise RuntimeError(f"Codex Desktop CDP is unavailable: {exc}") from exc
        finally:
            connection.close()
        try:
            targets = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex Desktop CDP returned invalid JSON") from exc
        if not isinstance(targets, list):
            raise RuntimeError("Codex Desktop CDP returned an invalid target list")
        return [target for target in targets if isinstance(target, dict)]

    def list_targets(self) -> list[dict[str, Any]]:
        configured_error: RuntimeError | None = None
        try:
            targets = self._fetch_targets(self.port, self.timeout_seconds)
            select_primary_codex_target(targets)
            return targets
        except RuntimeError as exc:
            configured_error = exc

        discovery_timeout = min(self.timeout_seconds, 0.5)
        for port in range(9335, 9355):
            if port == self.port:
                continue
            try:
                targets = self._fetch_targets(port, discovery_timeout)
                select_primary_codex_target(targets)
            except RuntimeError:
                continue
            self.port = port
            return targets
        raise configured_error

    def evaluate(self, expression: str) -> dict[str, Any]:
        target = select_primary_codex_target(self.list_targets())
        websocket_url = str(target["webSocketDebuggerUrl"])
        connection = WebSocketConnection(
            websocket_url, timeout_seconds=self.timeout_seconds
        )
        try:
            connection.socket.settimeout(self.timeout_seconds)
            deadline = time.monotonic() + self.timeout_seconds
            connection.send_json(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": False,
                    },
                }
            )
            for text in connection.iter_text():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex Desktop CDP request timed out")
                connection.socket.settimeout(remaining)
                message = json.loads(text)
                if message.get("id") != 1:
                    continue
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                result = message.get("result") or {}
                if result.get("exceptionDetails"):
                    detail = result["exceptionDetails"]
                    exception = detail.get("exception") or {}
                    raise RuntimeError(
                        str(exception.get("description") or detail.get("text") or detail)
                    )
                remote = result.get("result") or {}
                value = remote.get("value")
                if not isinstance(value, dict) or not value.get("ok"):
                    raise RuntimeError(
                        str(remote.get("description") or "Codex Desktop submit failed")
                    )
                return value
        except TimeoutError:
            raise
        except (json.JSONDecodeError, OSError, socket.timeout) as exc:
            raise RuntimeError(f"Codex Desktop CDP request failed: {exc}") from exc
        finally:
            connection.close()
        raise TimeoutError("Codex Desktop CDP request timed out")

    def probe(self) -> dict[str, Any]:
        return self.evaluate(build_probe_expression())

    def get_queued_follow_up_count(self, thread_id: str) -> int:
        result = self.evaluate(build_queued_follow_up_count_expression(thread_id))
        count = result.get("queuedCount")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise RuntimeError("Codex Desktop returned an invalid queued follow-up count")
        return count

    def get_queued_follow_up_ids(self, thread_id: str) -> list[str]:
        result = self.evaluate(build_queued_follow_up_ids_expression(thread_id))
        queued_ids = result.get("queuedIds")
        if not isinstance(queued_ids, list) or not all(
            isinstance(item, str) and item for item in queued_ids
        ):
            raise RuntimeError("Codex Desktop returned invalid queued follow-up IDs")
        return queued_ids

    def get_queued_follow_ups(self, thread_id: str) -> list[dict[str, Any]]:
        result = self.evaluate(build_queued_follow_up_items_expression(thread_id))
        queued_items = result.get("queuedItems")
        if not isinstance(queued_items, list):
            raise RuntimeError("Codex Desktop returned invalid queued follow-ups")
        normalized: list[dict[str, Any]] = []
        for item in queued_items:
            if not isinstance(item, dict):
                raise RuntimeError("Codex Desktop returned invalid queued follow-ups")
            item_id = item.get("id")
            text = item.get("text")
            created_at = item.get("createdAt")
            if (
                not isinstance(item_id, str)
                or not isinstance(text, str)
                or not isinstance(created_at, (int, float))
                or isinstance(created_at, bool)
            ):
                raise RuntimeError("Codex Desktop returned invalid queued follow-ups")
            normalized.append(
                {"id": item_id, "text": text, "createdAt": int(created_at)}
            )
        return normalized

    def enqueue_queued_follow_up(
        self,
        thread_id: str,
        prompt: str,
        cwd: str,
        message_id: str,
        created_at_ms: int,
    ) -> dict[str, Any]:
        try:
            return self.evaluate(
                build_enqueue_queued_follow_up_expression(
                    thread_id, prompt, cwd, message_id, created_at_ms
                )
            )
        except (RuntimeError, TimeoutError) as exc:
            # After dispatch, losing a CDP response is not proof that Desktop
            # rejected the enqueue. Keep the stable client ID for reconciliation.
            if isinstance(exc, TimeoutError) or "Codex Desktop CDP request failed" in str(exc):
                raise RuntimeError(
                    f"Codex Desktop queue submission outcome unknown: {exc}"
                ) from exc
            raise

    def remove_queued_follow_up(
        self, thread_id: str, message_id: str
    ) -> dict[str, Any]:
        return self.evaluate(
            build_remove_queued_follow_up_expression(thread_id, message_id)
        )

    def send_follow_up(
        self,
        thread_id: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        return self.evaluate(
            build_follow_up_expression(thread_id, prompt, model, reasoning_effort)
        )
