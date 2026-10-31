import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import fastapi_canon.integrations.sse
from fastapi import FastAPI
from fastapi_canon.integrations.sse import StreamRouter
from pydantic import BaseModel


class Event(BaseModel):
    type: Literal["event"] = "event"
    text: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--http-client", choices=["httpx", "httpx2"], default="httpx")
    args = parser.parse_args()
    assert "site-packages" in str(
        Path(fastapi_canon.integrations.sse.__file__).resolve()
    )
    assert (
        version("fastapi-canon")
        == fastapi_canon.integrations.sse.__version__
        == args.expected_version
    )
    router = StreamRouter()

    @router.stream("/events", operation_id="events")
    async def events() -> AsyncIterator[Event]:
        yield Event(text="packaged")

    app = FastAPI()
    app.include_router(router)
    assert "/events" in app.openapi()["paths"]
    cli = Path(sys.executable).with_name("canon.exe" if os.name == "nt" else "canon")
    assert cli.is_file(), f"Missing installed console script: {cli}"
    with tempfile.TemporaryDirectory() as directory:
        working = Path(directory)
        (working / "app.py").write_text(
            "from collections.abc import AsyncIterator\n"
            "from typing import Annotated, Literal\n"
            "from fastapi import FastAPI, Path, Query\n"
            "from pydantic import BaseModel, Field\n"
            "from fastapi_canon.integrations.sse import StreamRouter\n"
            "class Event(BaseModel):\n"
            '    type: Literal["event"]\n'
            "    text: str = Field(min_length=2, max_length=20, pattern='^[a-z]+$')\n"
            "class Done(BaseModel):\n"
            '    type: Literal["done"]\n'
            "    count: int = Field(ge=0)\n"
            "type Payload = Annotated[Event | Done, Field(discriminator='type')]\n"
            "router = StreamRouter()\n"
            '@router.stream("/events/{run_id}", operation_id="events")\n'
            "async def events(run_id: Annotated[str, Path(pattern='^run_[a-z]+$')], "
            "limit: Annotated[int, Query(ge=1)] = 1) -> AsyncIterator[Payload]:\n"
            '    yield Event(type="event", text="packaged")\n'
            "class Request(BaseModel):\n"
            "    prompt: str = Field(min_length=2)\n"
            '@router.stream("/events", methods=["POST"], '
            'operation_id="create_events")\n'
            "async def create_events(body: Request) -> AsyncIterator[Payload]:\n"
            '    yield Event(type="event", text="packaged")\n'
            "app = FastAPI()\n"
            "app.include_router(router)\n",
            encoding="utf-8",
        )

        def run(*arguments, expected=0):
            result = subprocess.run(
                [str(cli), "sse", *arguments],
                cwd=working,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == expected, result.stdout + result.stderr
            return result

        run("--help")
        if args.core_only:
            assert importlib.util.find_spec("jinja2") is None
            assert importlib.util.find_spec("jsonschema") is None
            assert importlib.util.find_spec("httpx") is None
            assert importlib.util.find_spec("httpx2") is None
            failed = run(
                "schema", "--app", "app:app", "--output", "openapi.json", expected=2
            )
            assert "fastapi-canon[codegen]" in failed.stderr
            print("Installed core CLI reports the missing codegen extra clearly")
            return
        assert importlib.util.find_spec(args.http_client) is not None
        unselected = "httpx" if args.http_client == "httpx2" else "httpx2"
        assert importlib.util.find_spec(unselected) is None
        run("schema", "--app", "app:app", "--output", "openapi.json")
        assert json.loads((working / "openapi.json").read_text())["openapi"] == "3.2.0"
        for language, output, client_file in [
            ("typescript", "typescript", "client.ts"),
            ("python", "sdk", "client.py"),
        ]:
            flags = [
                "--schema",
                "openapi.json",
                "--language",
                language,
                "--output",
                output,
                "--diagnostics",
                f"{language}-diagnostics.json",
            ]
            if language == "python":
                flags += ["--python-http-client", args.http_client]
                flags += ["--strict"]
            run("generate", *flags)
            report = json.loads((working / f"{language}-diagnostics.json").read_text())
            assert report["version"] == 1
            assert report["generation"]["generator_version"] == args.expected_version
            assert report["generation"]["target"] == language
            assert report["generation"]["python_http_client"] == (
                args.http_client if language == "python" else None
            )
            assert len(report["generation"]["schema_sha256"]) == 64
            manifest = json.loads(
                (working / output / ".fastapi-streams.json").read_text()
            )
            assert manifest["generation"] == report["generation"]
            assert client_file in manifest["files"]
            assert report["validation"]["full_json_schema"] is False
            assert report["validation"]["events"] == {
                "phase": "iteration",
                "json_decoding": True,
                "schema_validation": language == "python",
            }
            if language == "python":
                assert report["diagnostics"] == []
            run("check", "--app", "app:app", *flags)
            target = working / output / client_file
            original = target.read_bytes()
            target.write_bytes(original + b"\n")
            run("check", *flags, expected=1)
            assert target.read_bytes() == original + b"\n"
            run("generate", *flags)
        consumer = """
import asyncio
import httpx
from pydantic import ValidationError
from sdk import Client, RequestOptions, StreamParseError
from sdk.models import Event
async def main():
    for invalid in ('x', 'Bad', 'x' * 21):
        try:
            Event(type='event', text=invalid)
        except ValidationError:
            pass
        else:
            raise AssertionError('Generated scalar constraint was lost')
    payload = 'data: {"type":"event","text":"packaged"}\\n\\n'
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type":"text/event-stream"},
            content=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with Client("http://example.test", http_client=http) as client:
            for arguments in ({'run_id': 'bad'}, {'run_id': 'run_ok', 'limit': 0}):
                try:
                    client.streams.events(**arguments)
                except ValidationError:
                    pass
                else:
                    raise AssertionError('Invalid request must fail at method call')
            try:
                client.streams.create_events(body={'prompt': 'x'})
            except ValidationError:
                pass
            else:
                raise AssertionError('Invalid body must fail at method call')
            assert not requests
            events = [event async for event in client.streams.events(run_id='run_ok')]
            assert events[0].text == "packaged"
            payload = 'data: {"type":"future"}\\n\\n' + payload
            try:
                await anext(client.streams.events(run_id='run_ok'))
            except StreamParseError:
                pass
            else:
                raise AssertionError('Default union must reject unknown events')
            events = [event async for event in client.streams.events(
                run_id='run_ok',
                options=RequestOptions(unknown_events='ignore'))]
            assert len(events) == 1 and events[0].text == 'packaged'
            payload = 'data: {"type":"event","text":"Bad"}\\n\\n'
            try:
                await anext(client.streams.events(
                    run_id='run_ok',
                    options=RequestOptions(unknown_events='ignore')))
            except StreamParseError as error:
                assert error.operation_id == 'events'
                assert error.category == 'invalid_event_schema'
                assert error.validation_errors
                assert all('input' not in detail for detail in error.validation_errors)
            else:
                raise AssertionError('Known invalid events must still fail')
asyncio.run(main())
"""
        consumer = consumer.replace(
            "import httpx\n", f"import {args.http_client} as httpx\n", 1
        )
        subprocess.run(
            [sys.executable, "-c", consumer], cwd=working, check=True, timeout=30
        )
        compiler = (
            Path(__file__).resolve().parents[1] / "node_modules/typescript/bin/tsc"
        )
        node = shutil.which("node")
        assert node and compiler.is_file(), "Run npm ci before package smoke tests"
        subprocess.run(
            [
                node,
                str(compiler),
                "--strict",
                "--target",
                "ES2022",
                "--module",
                "ESNext",
                "--moduleResolution",
                "Bundler",
                "--skipLibCheck",
                "--noEmit",
                "typescript/index.ts",
            ],
            cwd=working,
            check=True,
            timeout=30,
        )
    print(
        f"Installed CLI produces usable clients with only {args.http_client} installed"
    )


if __name__ == "__main__":
    main()
