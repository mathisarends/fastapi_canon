import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import generate_typescript_client
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)

_RUNNER = """
import {
  BinaryChannel,
  BinarySender,
  GreetingClient,
  RpcStreamClosed,
  RpcStreamFailed,
  RpcStreamRefused,
  RpcStreamTimeout,
  RpcStreamsUnavailableError,
} from "./generated";

type Listener = (...args: any[]) => void;
type Frame = string | ArrayBuffer | ArrayBufferView;

const sockets: FakeStreamSocket[] = [];
const encoder = new TextEncoder();
const decoder = new TextDecoder();

// Follows the Canon stream wire protocol: binary frames carry data, the
// text message {"type":"end"} ends the input, and the server closes.
class FakeStreamSocket {
  readyState = 1;
  binaryType: BinaryType = "blob";
  readonly received: string[] = [];
  inputEnded = false;
  closedByClient = false;
  readonly #listeners = new Map<string, Listener[]>();

  constructor(readonly url: string) {
    sockets.push(this);
    setTimeout(() => {
      if (url.includes("refused")) this.#close(1008, "Session has an owner");
      else if (url.endsWith("/media")) this.#frame("ready");
    });
  }

  addEventListener(type: string, listener: Listener): void {
    const listeners = this.#listeners.get(type) ?? [];
    listeners.push(listener);
    this.#listeners.set(type, listeners);
  }

  send(data: Frame): void {
    if (typeof data === "string") {
      if (data !== '{"type":"end"}') throw new Error(`Unexpected text ${data}`);
      this.inputEnded = true;
      setTimeout(() => this.#end());
      return;
    }
    const bytes =
      data instanceof ArrayBuffer
        ? new Uint8Array(data)
        : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    const text = decoder.decode(bytes);
    this.received.push(text);
    if (this.url.endsWith("/media")) {
      setTimeout(() => this.#frame(text.toUpperCase()));
    }
  }

  close(): void {
    this.closedByClient = !this.inputEnded;
    this.readyState = 3;
  }

  #end(): void {
    if (this.url.endsWith("/media")) {
      this.#frame("bye");
      this.#close(1000, "");
    } else if (this.received.includes("hang")) {
      return;
    } else if (this.received.includes("fail")) {
      this.#close(1011, "Internal error");
    } else {
      this.#close(1000, "");
    }
  }

  #frame(text: string): void {
    const data = encoder.encode(text).buffer;
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

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function text(frame: ArrayBuffer): string {
  return decoder.decode(frame);
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
    streamSocketFactory: (url) => new FakeStreamSocket(String(url)) as never,
  });

  const sink = await client.uploads.audio({ sessionId: "s1" });
  assert(sink instanceof BinarySender, "uploads return a sink");
  await sink.send(encoder.encode("one"));
  await sink.send(encoder.encode("two").buffer);
  await sink.end();
  await sink.close();
  const upload = sockets.at(-1)!;
  assert(upload.url === "wss://media/s1/upload", `url ${upload.url}`);
  assert(upload.received.join() === "one,two", "frames arrive in order");
  assert(upload.inputEnded && !upload.closedByClient, "the sink ended");

  const failing = await client.uploads.audio({ sessionId: "s2" });
  await failing.send(encoder.encode("fail"));
  const failure = await rejection(failing.end());
  assert(failure instanceof RpcStreamFailed, "end() reports failures");
  assert(failure.code === 1011, `code ${failure.code}`);
  assert(failure.reason === "Internal error", `reason ${failure.reason}`);

  const hanging = await client.uploads.audio({ sessionId: "s4" });
  await hanging.send(encoder.encode("hang"));
  const timeout = await rejection(hanging.end({ timeoutMs: 10 }));
  assert(timeout instanceof RpcStreamTimeout, "end() gives up");
  assert(timeout.timeoutMs === 10, `timeout ${timeout.timeoutMs}`);
  await hanging.close();

  const aborted = await client.uploads.audio({ sessionId: "s3" });
  await aborted.send(encoder.encode("partial"));
  await aborted.close();
  assert(sockets.at(-1)!.closedByClient, "closing without end() aborts");

  {
    await using media = await client.voice.media();
    assert(media instanceof BinaryChannel, "duplex connection");
    assert(text(await media.receive()) === "ready", "server output first");
    await media.send(encoder.encode("hello"));
    assert(text(await media.receive()) === "HELLO", "concurrent echo");
    await media.endInput();
    const late = await rejection(media.send(encoder.encode("late")));
    assert(late instanceof RpcStreamClosed, "no input after endInput()");
    const rest: string[] = [];
    for await (const frame of media) rest.push(text(frame));
    assert(rest.join() === "bye", `output after end ${rest.join()}`);
  }

  const refused = await client.voice.media({ room: "refused" });
  const refusal = await rejection(refused.receive());
  assert(refusal instanceof RpcStreamRefused, "1008 is a refusal");
  assert(refusal.reason === "Session has an owner", "refusal reason");

  let receiveOnlyClosed = false;
  const custom = GreetingClient.withTransports(
    {
      request: async () => undefined,
      notifications: async function* () {},
      close: async () => undefined,
    },
    {
      streamOpener: async () => ({
        receive: async () => new ArrayBuffer(0),
        close: async () => {
          receiveOnlyClosed = true;
        },
      }),
    },
  );
  const unavailable = await rejection(custom.uploads.audio());
  assert(
    unavailable instanceof RpcStreamsUnavailableError,
    "receive-only transports cannot upload",
  );
  assert(receiveOnlyClosed, "the unusable transport is closed");
}

void main();
"""


def test_generated_typescript_input_streams_follow_the_wire_protocol(
    document: dict[str, Any],
    tmp_path: Path,
) -> None:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")

    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "primary",
            "url": "wss://api/rpc",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    deployed["x-rpckit-binary-streams"] = [
        {
            "name": "uploads.audio",
            "url": "wss://media/{sessionId}/upload",
            "direction": "client-to-server",
            "inputContentType": "audio/pcm",
            "frameType": "binary",
            "variables": {"sessionId": {"default": "demo"}},
        },
        {
            "name": "voice.media",
            "url": "wss://media/{room}/media",
            "direction": "bidirectional",
            "contentType": "audio/opus",
            "inputContentType": "audio/pcm",
            "frameType": "binary",
            "variables": {"room": {"default": "talk"}},
        },
    ]
    generate_typescript_client(
        deployed,
        tmp_path / "generated",
        TypeScriptClientOptions(
            client_name="GreetingClient",
            source="greeting.openrpc.json",
            with_transport="websocket",
        ),
    )
    runner = tmp_path / "streams.test.ts"
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
        [node, str(compiled / "streams.test.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert behavior.returncode == 0, behavior.stdout + behavior.stderr
