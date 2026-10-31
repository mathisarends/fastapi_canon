"""Serve the example sessions plus the compiled browser test page.

Run from the package directory: ``python -m tests.browser.server``.
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from examples.practice.app import rtc

HERE = Path(__file__).parent

app = FastAPI()
app.include_router(rtc)
app.mount("/static", StaticFiles(directory=HERE / "dist", check_dir=False))


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
