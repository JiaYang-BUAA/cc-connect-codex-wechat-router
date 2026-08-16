from __future__ import annotations

import http.client
import json
import socket
from typing import Any
from urllib.parse import urlsplit

from websocket_transport import WebSocketConnection


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
    candidates = [
        target
        for target in targets
        if target.get("type") == "page"
        and str(target.get("url") or "") == "app://-/index.html"
        and str(target.get("webSocketDebuggerUrl") or "").startswith("ws://")
    ]
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
        "hostId": "local",
        "conversationId": thread_id,
        "messageMetadata": None,
        "prompt": prompt,
    }
    if model:
        request_payload["model"] = model
    if reasoning_effort:
        request_payload["reasoningEffort"] = reasoning_effort
    payload = json.dumps(
        request_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""
(async () => {{
  const entryUrl = [...document.scripts]
    .map((script) => script.src)
    .find((src) => src.includes('/assets/index-'));
  if (!entryUrl) throw new Error('Codex Desktop entry module was not found');
  const entrySource = await fetch(entryUrl).then((response) => response.text());
  const moduleMatch = entrySource.match(/\\.\\/(app-initial-[A-Za-z0-9_-]+\\.js)/);
  if (!moduleMatch) throw new Error('Codex Desktop app module was not found');
  const scriptUrl = new URL(moduleMatch[1], entryUrl).href;
  const appSource = await fetch(scriptUrl).then((response) => response.text());
  const bridgeMatch = appSource.match(
    /function ([A-Za-z_$][A-Za-z0-9_$]*)\\(e,t,n\\)\\{{return n==null\\?([A-Za-z_$][A-Za-z0-9_$]*)\\.sendRequest\\(e,t\\):\\2\\.sendRequest\\(e,t,n\\)\\}}/
  );
  if (!bridgeMatch) throw new Error('Codex Desktop request bridge was not found');
  const exportMatch = appSource.match(
    new RegExp(bridgeMatch[1] + ' as ([A-Za-z_$][A-Za-z0-9_$]*)')
  );
  if (!exportMatch) throw new Error('Codex Desktop request export was not found');
  const module = await import(scriptUrl);
  const request = module[exportMatch[1]];
  if (typeof request !== 'function') {{
    throw new Error('Codex Desktop request export is not callable');
  }}
  const requestOutcome = Promise.resolve(
    request('send-follow-up-message', {payload})
  ).then(
    () => ({{ state: 'completed' }}),
    (error) => {{ throw error; }}
  );
  const outcome = await Promise.race([
    requestOutcome,
    new Promise((resolve) => setTimeout(
      () => resolve({{ state: 'dispatched' }}),
      1000
    )),
  ]);
  return {{
    ok: true,
    requestExport: exportMatch[1],
    requestState: outcome.state,
  }};
}})()
""".strip()


def build_probe_expression() -> str:
    return """
(async () => {
  const entryUrl = [...document.scripts]
    .map((script) => script.src)
    .find((src) => src.includes('/assets/index-'));
  if (!entryUrl) throw new Error('Codex Desktop entry module was not found');
  const entrySource = await fetch(entryUrl).then((response) => response.text());
  const moduleMatch = entrySource.match(/\\.\\/(app-initial-[A-Za-z0-9_-]+\\.js)/);
  if (!moduleMatch) throw new Error('Codex Desktop app module was not found');
  const scriptUrl = new URL(moduleMatch[1], entryUrl).href;
  const appSource = await fetch(scriptUrl).then((response) => response.text());
  const bridgeMatch = appSource.match(
    /function ([A-Za-z_$][A-Za-z0-9_$]*)\\(e,t,n\\)\\{return n==null\\?([A-Za-z_$][A-Za-z0-9_$]*)\\.sendRequest\\(e,t\\):\\2\\.sendRequest\\(e,t,n\\)\\}/
  );
  if (!bridgeMatch) throw new Error('Codex Desktop request bridge was not found');
  const exportMatch = appSource.match(
    new RegExp(bridgeMatch[1] + ' as ([A-Za-z_$][A-Za-z0-9_$]*)')
  );
  if (!exportMatch) throw new Error('Codex Desktop request export was not found');
  const module = await import(scriptUrl);
  const request = module[exportMatch[1]];
  if (typeof request !== 'function') {
    throw new Error('Codex Desktop request export is not callable');
  }
  return { ok: true, requestExport: exportMatch[1], functionName: request.name };
})()
""".strip()


class DesktopCdpClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0):
        self.host, self.port = validate_loopback_http_url(base_url)
        self.timeout_seconds = timeout_seconds

    def list_targets(self) -> list[dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout_seconds
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

    def evaluate(self, expression: str) -> dict[str, Any]:
        target = select_primary_codex_target(self.list_targets())
        websocket_url = str(target["webSocketDebuggerUrl"])
        connection = WebSocketConnection(
            websocket_url, timeout_seconds=self.timeout_seconds
        )
        try:
            connection.socket.settimeout(self.timeout_seconds)
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
        except (json.JSONDecodeError, OSError, socket.timeout) as exc:
            raise RuntimeError(f"Codex Desktop CDP request failed: {exc}") from exc
        finally:
            connection.close()
        raise TimeoutError("Codex Desktop CDP request timed out")

    def probe(self) -> dict[str, Any]:
        return self.evaluate(build_probe_expression())

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
