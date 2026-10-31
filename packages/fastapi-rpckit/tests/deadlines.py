import asyncio

import fastapi_canon.integrations.websockets.server as server_module


class ControlledDeadlines:
    def __init__(self, monkeypatch):
        self.created = asyncio.Queue()
        self.real_timeout_at = asyncio.timeout_at
        monkeypatch.setattr(server_module, "timeout_at", self.timeout_at)

    def timeout_at(self, deadline):
        waiting = self.real_timeout_at(None)
        if deadline is not None:
            self.created.put_nowait((deadline, waiting))
        return waiting

    async def next(self):
        return await asyncio.wait_for(self.created.get(), 1)

    def expire(self, waiting):
        waiting.reschedule(asyncio.get_running_loop().time())
