from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from websocket_transport import WebSocketConnection


DESKTOP_REQUEST_CLIENT_LOOKUP = r"""
  function findDesktopRequestClient() {
    const root = window.__codexRoot?._internalRoot?.current;
    if (!root) throw new Error('Codex Desktop React root was not found');
    const queue = [root];
    const seen = new WeakSet();
    let cursor = 0;
    let visited = 0;
    while (cursor < queue.length && visited < 200000) {
      const value = queue[cursor++];
      if (
        value == null ||
        (typeof value !== 'object' && typeof value !== 'function') ||
        seen.has(value)
      ) continue;
      seen.add(value);
      visited += 1;
      try {
        if (
          typeof value.sendRequest === 'function' &&
          value.hostId === 'local' &&
          value.requestPromises instanceof Map
        ) return value;
      } catch {}
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
        ) queue.push(child);
      }
      if (value instanceof Map) {
        for (const [key, child] of value) queue.push(key, child);
      } else if (value instanceof Set) {
        for (const child of value) queue.push(child);
      }
    }
    throw new Error('Codex Desktop AppServer request client was not found');
  }
""".strip()


DESKTOP_QUEUED_FOLLOW_UP_LOOKUP = r"""
  function findDesktopQueuedFollowUpsContext() {
    const root = window.__codexRoot?._internalRoot?.current;
    if (!root) throw new Error('Codex Desktop React root was not found');
    const queue = [root];
    const seen = new WeakSet();
    let cursor = 0;
    let visited = 0;
    while (cursor < queue.length && visited < 200000) {
      const value = queue[cursor++];
      if (
        value == null ||
        (typeof value !== 'object' && typeof value !== 'function') ||
        seen.has(value)
      ) continue;
      seen.add(value);
      visited += 1;
      try {
        if (typeof value.getQueryCache === 'function') {
          const queries = value.getQueryCache().getAll();
          if (Array.isArray(queries)) {
            const query = queries.find((candidate) => {
              const key = candidate?.queryKey;
              return (
                Array.isArray(key) &&
                key.includes('get-global-state') &&
                JSON.stringify(key).includes('queued-follow-ups')
              );
            });
            if (query) return { query, queryClient: value };
          }
        }
      } catch {}
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
        ) queue.push(child);
      }
      if (value instanceof Map) {
        for (const [key, child] of value) queue.push(key, child);
      } else if (value instanceof Set) {
        for (const child of value) queue.push(child);
      }
    }
    throw new Error('Codex Desktop queued follow-up cache was not found');
  }
""".strip()


DESKTOP_MANAGER_LOOKUP = r"""
  function findDesktopManager() {
    const root = window.__codexRoot?._internalRoot?.current;
    if (!root) throw new Error('Codex Desktop React root was not found');
    const queue = [root];
    const seen = new WeakSet();
    let cursor = 0;
    let visited = 0;
    while (cursor < queue.length && visited < 200000) {
      const value = queue[cursor++];
      if (
        value == null ||
        (typeof value !== 'object' && typeof value !== 'function') ||
        seen.has(value)
      ) continue;
      seen.add(value);
      visited += 1;
      try {
        if (
          typeof value.fetchFromHost === 'function' &&
          value.hostId === 'local' &&
          value.scope &&
          value.threadStore &&
          value.requestClient
        ) return value;
      } catch {}
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
        ) queue.push(child);
      }
      if (value instanceof Map) {
        for (const [key, child] of value) queue.push(key, child);
      } else if (value instanceof Set) {
        for (const child of value) queue.push(child);
      }
    }
    throw new Error('Codex Desktop local manager was not found');
  }
""".strip()


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
(() => {{
  {DESKTOP_QUEUED_FOLLOW_UP_LOOKUP}
  const {{ query }} = findDesktopQueuedFollowUpsContext();
  const queuedByThread = query?.state?.data?.value;
  const queuedForThread = queuedByThread?.[{encoded_thread_id}];
  return {{
    ok: true,
    queuedCount: Array.isArray(queuedForThread) ? queuedForThread.length : 0,
  }};
}})()
""".strip()


def build_queued_follow_up_ids_expression(thread_id: str) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    return f"""
(() => {{
  {DESKTOP_QUEUED_FOLLOW_UP_LOOKUP}
  const {{ query }} = findDesktopQueuedFollowUpsContext();
  const queuedByThread = query?.state?.data?.value;
  const queuedForThread = queuedByThread?.[{encoded_thread_id}];
  return {{
    ok: true,
    queuedIds: Array.isArray(queuedForThread)
      ? queuedForThread.map((item) => String(item?.id ?? '')).filter(Boolean)
      : [],
  }};
}})()
""".strip()


def build_queued_follow_up_items_expression(thread_id: str) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    return f"""
(() => {{
  {DESKTOP_QUEUED_FOLLOW_UP_LOOKUP}
  const {{ query }} = findDesktopQueuedFollowUpsContext();
  const queuedByThread = query?.state?.data?.value;
  const queuedForThread = queuedByThread?.[{encoded_thread_id}];
  return {{
    ok: true,
    queuedItems: Array.isArray(queuedForThread)
      ? queuedForThread.map((item) => ({{
          id: String(item?.id ?? ''),
          text: String(item?.text ?? item?.context?.prompt ?? ''),
          createdAt: Number(item?.createdAt ?? 0),
        }}))
      : [],
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
  {DESKTOP_QUEUED_FOLLOW_UP_LOOKUP}
  {DESKTOP_MANAGER_LOOKUP}
  const payload = {payload};
  const operation = async () => {{
    const manager = findDesktopManager();
    const {{ query, queryClient }} = findDesktopQueuedFollowUpsContext();
    const fetched = await manager.fetchFromHost(
      'get-global-state',
      {{ params: {{ key: 'queued-follow-ups' }} }}
    );
    const queuedByThread = {{ ...(fetched?.value ?? {{}}) }};
    const current = Array.isArray(queuedByThread[payload.threadId])
      ? queuedByThread[payload.threadId]
      : [];
    const existing = current.find((item) => item?.id === payload.message.id);
    const messages = existing ? current : [...current, payload.message];
    const next = {{ ...queuedByThread, [payload.threadId]: messages }};
    if (!existing) {{
      const saved = await manager.fetchFromHost(
        'set-global-state',
        {{ params: {{ key: 'queued-follow-ups', value: next }} }}
      );
      if (saved?.success !== true) {{
        throw new Error('Codex Desktop did not persist the queued follow-up');
      }}
    }}
    queryClient.setQueryData(query.queryKey, {{ value: next }});
    return {{
      ok: true,
      inserted: !existing,
      queuedMessageId: payload.message.id,
      queuedCount: messages.length,
      queuedItems: messages.map((item) => ({{
        id: String(item?.id ?? ''),
        text: String(item?.text ?? item?.context?.prompt ?? ''),
        createdAt: Number(item?.createdAt ?? 0),
      }})),
    }};
  }};
  return globalThis.navigator?.locks
    ? globalThis.navigator.locks.request('codex-queued-follow-up-state', operation)
    : operation();
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
  {DESKTOP_QUEUED_FOLLOW_UP_LOOKUP}
  {DESKTOP_MANAGER_LOOKUP}
  const payload = {payload};
  const operation = async () => {{
    const manager = findDesktopManager();
    const {{ query, queryClient }} = findDesktopQueuedFollowUpsContext();
    const fetched = await manager.fetchFromHost(
      'get-global-state',
      {{ params: {{ key: 'queued-follow-ups' }} }}
    );
    const queuedByThread = {{ ...(fetched?.value ?? {{}}) }};
    const current = Array.isArray(queuedByThread[payload.threadId])
      ? queuedByThread[payload.threadId]
      : [];
    const messages = current.filter((item) => item?.id !== payload.messageId);
    const removed = messages.length !== current.length;
    if (!removed) {{
      queryClient.setQueryData(query.queryKey, {{ value: queuedByThread }});
      return {{ ok: true, removed: false, queuedCount: current.length }};
    }}
    const next = {{ ...queuedByThread }};
    if (messages.length === 0) delete next[payload.threadId];
    else next[payload.threadId] = messages;
    const saved = await manager.fetchFromHost(
      'set-global-state',
      {{ params: {{ key: 'queued-follow-ups', value: next }} }}
    );
    if (saved?.success !== true) {{
      throw new Error('Codex Desktop did not persist the queued follow-up removal');
    }}
    queryClient.setQueryData(query.queryKey, {{ value: next }});
    return {{ ok: true, removed: true, queuedCount: messages.length }};
  }};
  return globalThis.navigator?.locks
    ? globalThis.navigator.locks.request('codex-queued-follow-up-state', operation)
    : operation();
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
        return self.evaluate(
            build_enqueue_queued_follow_up_expression(
                thread_id, prompt, cwd, message_id, created_at_ms
            )
        )

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
