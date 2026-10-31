import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import (
    generate_typescript_client,
    render_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)

_RUNNER = """
import {
  GreetingClient,
  Socket,
  RpcStreamClosed,
  RpcStreamFailed,
  RpcStreamRefused,
  RpcStreamsUnavailableError,
} from "./generated";

type Listener = (...args: any[]) => void;
type Message = string | ArrayBuffer | ArrayBufferView;

const sockets: FakeMessageSocket[] = [];
const encoder = new TextEncoder();
const decoder = new TextDecoder();

// Echoes every message with its original type, like a transparent proxy.
class FakeMessageSocket {
  readyState = 1;
  binaryType: BinaryType = "blob";
  readonly received: Message[] = [];
  closedByClient = false;
  readonly #listeners = new Map<string, Listener[]>();

  constructor(readonly url: string) {
    sockets.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const listeners = this.#listeners.get(type) ?? [];
    listeners.push(listener);
    this.#listeners.set(type, listeners);
  }

  send(data: Message): void {
    this.received.push(data);
    setTimeout(() => {
      if (data === "done") this.#close(1000, "");
      else if (data === "missing") this.#close(1008, "Unknown browser");
      else if (data === "fail") this.#close(1011, "Browser CDP unavailable");
      else this.#emit(typeof data === "string" ? data : toBuffer(data));
    });
  }

  close(): void {
    this.closedByClient = true;
    this.readyState = 3;
  }

  #emit(data: string | ArrayBuffer): void {
    for (const listener of this.#listeners.get("message") ?? []) {
      listener({ data });
    }
  }

  #close(code: number, reason: string): void {
    this.readyState = 3;
    for (const listener of this.#listeners.get("close") ?? []) {
      listener({ code, reason });
    }
  }
}

function toBuffer(data: ArrayBuffer | ArrayBufferView): ArrayBuffer {
  if (data instanceof ArrayBuffer) return data;
  return new Uint8Array(data.buffer, data.byteOffset, data.byteLength).slice()
    .buffer;
}

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("Expected a rejection");
}

async function main(): Promise<void> {
  const client = await GreetingClient.connect({
    lazy: true,
    socketConnectionFactory: (url) => new FakeMessageSocket(String(url)) as never,
  });

  {
    await using cdp = await client.browser.cdp();
    assert(cdp instanceof Socket, "sockets open a Socket");
    assert(cdp.endpoint.protocol?.name === "cdp", "declared protocol name");
    assert(cdp.endpoint.protocol?.version === "1.3", "declared protocol version");
    assert(sockets.at(-1)!.url === "wss://api/browser/cdp", "socket url");
    await cdp.send('{"id":1,"method":"Page.enable"}');
    await cdp.send(encoder.encode("frame"));
    const text = await cdp.receive();
    assert(text === '{"id":1,"method":"Page.enable"}', `text ${String(text)}`);
    const binary = await cdp.receive();
    assert(binary instanceof ArrayBuffer, "binary messages stay binary");
    assert(decoder.decode(binary) === "frame", "binary content");
    await cdp.send("done");
    const rest: unknown[] = [];
    for await (const message of cdp) rest.push(message);
    assert(rest.length === 0, "iteration ends on a normal close");
    const closed = await rejection(cdp.receive());
    assert(closed instanceof RpcStreamClosed, "receive() after close");
  }

  const missing = await client.browser.cdp();
  await missing.send("missing");
  const refusal = await rejection(missing.receive());
  assert(refusal instanceof RpcStreamRefused, "1008 is a refusal");
  assert(refusal.reason === "Unknown browser", "refusal reason");

  const failing = await client.browser.cdp();
  await failing.send("fail");
  const failure = await rejection(failing.receive());
  assert(failure instanceof RpcStreamFailed, "1011 fails the socket");
  assert(failure.code === 1011, `code ${failure.code}`);

  const tab = await client.browser.tabs.cdp({ tabId: "tab-7" });
  assert(sockets.at(-1)!.url === "wss://api/tabs/tab-7/cdp", "call variables");
  const textOnly = await rejection(tab.send(encoder.encode("binary")));
  assert(textOnly instanceof TypeError, "undeclared message types fail");
  await tab.close();
  assert(sockets.at(-1)!.closedByClient, "close() closes the socket");

  const custom = GreetingClient.withTransports({
    request: async () => undefined,
    notifications: async function* () {},
    close: async () => undefined,
  });
  const unavailable = await rejection(custom.browser.cdp());
  assert(
    unavailable instanceof RpcStreamsUnavailableError,
    "clients without an opener cannot open raw sockets",
  );
}

void main();
"""


@pytest.fixture
def deployed(document: dict[str, Any]) -> dict[str, Any]:
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "primary",
            "url": "wss://api/rpc",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    deployed["x-rpckit-sockets"] = [
        {
            "name": "browser.cdp",
            "url": "wss://api/browser/cdp",
            "messageTypes": ["text", "binary"],
            "summary": "Proxy the Chrome DevTools Protocol.",
            "x-rpckit-protocol": {
                "name": "cdp",
                "version": "1.3",
                "encoding": "json",
                "schemas": [
                    "https://example.com/devtools-protocol/json/browser_protocol.json",
                    "https://example.com/devtools-protocol/json/js_protocol.json",
                ],
            },
            "closes": [
                {"code": 1008, "x-rpckit-code": "browser_not_found", "reason": "x"}
            ],
        },
        {
            "name": "browser.tabs.cdp",
            "url": "wss://api/tabs/{tabId}/cdp",
            "messageTypes": ["text"],
            "variables": {"tabId": {"default": "{tabId}"}},
            "x-rpckit-protocol": {"name": "cdp", "schemas": ["./cdp.json"]},
        },
    ]
    return deployed


def _tools() -> tuple[str, str]:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    return npx, node


def test_generated_typescript_sockets_pass_messages_through(
    deployed: dict[str, Any], tmp_path: Path
) -> None:
    npx, node = _tools()
    generate_typescript_client(
        deployed,
        tmp_path / "generated",
        TypeScriptClientOptions(
            client_name="GreetingClient",
            source="greeting.openrpc.json",
            with_transport="websocket",
        ),
    )
    runner = tmp_path / "raw.test.ts"
    runner.write_text(dedent(_RUNNER), encoding="utf-8", newline="\n")

    compiled = tmp_path / "compiled"
    type_check = subprocess.run(
        [
            npx,
            "--yes",
            "--package",
            "typescript",
            "tsc",
            "--strict",
            "--target",
            "ES2022",
            "--module",
            "Node16",
            "--moduleResolution",
            "Node16",
            "--lib",
            "ES2022,DOM,ESNext.Disposable",
            "--skipLibCheck",
            "--outDir",
            str(compiled),
            str(runner),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert type_check.returncode == 0, type_check.stdout + type_check.stderr

    behavior = subprocess.run(
        [node, str(compiled / "raw.test.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert behavior.returncode == 0, behavior.stdout + behavior.stderr


@pytest.mark.parametrize("with_transport", [None, "websocket"])
def test_generated_typescript_sockets_are_prettier_formatted(
    deployed: dict[str, Any], tmp_path: Path, with_transport: str | None
) -> None:
    npx, _ = _tools()
    output = tmp_path / "generated"
    generate_typescript_client(
        deployed, output, TypeScriptClientOptions(with_transport=with_transport)
    )

    formatting = subprocess.run(
        [npx, "prettier", "--check", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert formatting.returncode == 0, formatting.stdout + formatting.stderr


def test_sockets_are_exported_without_binary_streams(
    deployed: dict[str, Any],
) -> None:
    files = render_typescript_client(
        deployed, TypeScriptClientOptions(with_transport="websocket")
    )

    assert "streams.ts" not in files
    assert "export class RpcStreamClosed" in files["core.ts"]
    assert 'from "./sockets"' in files["index.ts"]
    assert "socketConnectionFactory?: SocketConnectionFactory" in files["client.ts"]
