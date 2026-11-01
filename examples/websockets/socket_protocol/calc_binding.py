"""Typed ``calc`` clients plugged into Canon's WebSocket generators.

``rpcgen.toml`` lists ``calc_binding:BINDING``. The generators then emit each
source below as ``bindings/calc`` of the client, and ``calculator.session()``
returns a ``CalcSession`` instead of a generic socket.

A CDP binding has the same shape: its Python source would wrap an existing CDP
client such as cdpify around the socket, and its TypeScript source would type
commands with the devtools-protocol package.
"""

from fastapi_canon.integrations.websockets.codegen.bindings import (
    BindingContext,
    SocketBinding,
)

PYTHON = '''
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from ..sockets import Socket


class CalcError(Exception):
    """A command the server rejected."""


class CalcSession:
    """Runs calc commands concurrently over one socket."""

    def __init__(self, socket: Socket) -> None:
        self._socket = socket
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}

    async def add(self, a: float, b: float) -> float:
        return await self.call("add", a, b)

    async def multiply(self, a: float, b: float) -> float:
        return await self.call("multiply", a, b)

    async def call(self, method: str, *params: float) -> Any:
        self._next_id += 1
        command_id = self._next_id
        reply = asyncio.get_running_loop().create_future()
        self._pending[command_id] = reply
        command = {"id": command_id, "method": method, "params": params}
        await self._socket.send(json.dumps(command))
        return await reply

    async def _read(self) -> None:
        try:
            async for message in self._socket:
                reply = json.loads(message)
                pending = self._pending.pop(reply["id"])
                if "error" in reply:
                    pending.set_exception(CalcError(reply["error"]))
                else:
                    pending.set_result(reply["result"])
        finally:
            for pending in self._pending.values():
                pending.set_exception(ConnectionError("The calc socket closed"))
            self._pending.clear()


@asynccontextmanager
async def open_calc(
    socket: AbstractAsyncContextManager[Socket],
) -> AsyncIterator[CalcSession]:
    async with socket as opened:
        session = CalcSession(opened)
        reader = asyncio.create_task(session._read())
        try:
            yield session
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
'''

TYPESCRIPT = """
import type { Socket } from "../sockets";

/** A command the server rejected. */
export class CalcError extends Error {}

type Reply = { id: number; result?: number; error?: string };
type Pending = { resolve(result: number): void; reject(error: Error): void };

/** Runs calc commands concurrently over one socket. */
export class CalcSession {
  readonly #socket: Socket;
  readonly #pending = new Map<number, Pending>();
  readonly #reading: Promise<void>;
  #nextId = 0;

  constructor(socket: Socket) {
    this.#socket = socket;
    this.#reading = this.#read();
  }

  add(a: number, b: number): Promise<number> {
    return this.call("add", a, b);
  }

  multiply(a: number, b: number): Promise<number> {
    return this.call("multiply", a, b);
  }

  async call(method: string, ...params: number[]): Promise<number> {
    const id = ++this.#nextId;
    const reply = new Promise<number>((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
    });
    await this.#socket.send(JSON.stringify({ id, method, params }));
    return reply;
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.#socket.close();
    await this.#reading;
  }

  async #read(): Promise<void> {
    try {
      for await (const message of this.#socket) {
        const reply = JSON.parse(String(message)) as Reply;
        const pending = this.#pending.get(reply.id);
        this.#pending.delete(reply.id);
        if (reply.error === undefined) pending?.resolve(reply.result!);
        else pending?.reject(new CalcError(reply.error));
      }
    } catch {
      // A failed socket rejects the pending commands below.
    } finally {
      for (const pending of this.#pending.values()) {
        pending.reject(new Error("The calc socket closed"));
      }
      this.#pending.clear();
    }
  }
}

export function openCalc(socket: Socket): CalcSession {
  return new CalcSession(socket);
}
"""


class CalcBinding:
    protocol = "calc"

    def bind(self, context: BindingContext) -> SocketBinding:
        if context.language == "python":
            return SocketBinding("calc", PYTHON, "open_calc", "CalcSession")
        return SocketBinding("calc", TYPESCRIPT, "openCalc", "CalcSession")


BINDING = CalcBinding()
