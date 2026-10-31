from __future__ import annotations

import asyncio
import importlib
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from fastapi_canon.codegen.config import SourceConfig, TargetConfig
from fastapi_canon.codegen.planner import build_plan
from fastapi_canon.codegen.render import render


class InvoiceLine(BaseModel):
    quantity: Decimal
    unit_price: Decimal | None


def test_python_decimal_schema_imports_validates_and_round_trips(
    tmp_path: Path,
) -> None:
    schema = InvoiceLine.model_json_schema(mode="serialization")
    # Earlier Pydantic exporters include this valid decimal-string constraint;
    # retain it explicitly so the regression also runs on newer exporters.
    decimal_pattern = r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"
    schema["properties"]["quantity"]["pattern"] = decimal_pattern
    schema["properties"]["unit_price"]["anyOf"][0]["pattern"] = decimal_pattern
    document = {
        "openapi": "3.1.0",
        "paths": {
            "/line": {
                "get": {
                    "operationId": "invoices.line",
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/InvoiceLine"
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {"schemas": {"InvoiceLine": schema}},
    }
    files = render(
        build_plan(((SourceConfig("api", "openapi"), document),)),
        TargetConfig("python", "python", ("api",), tmp_path, package="invoice_sdk"),
    )
    for name, content in files.items():
        path = tmp_path / "invoice_sdk" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        sdk: Any = importlib.import_module("invoice_sdk")

        async def exercise() -> None:
            wire = {"quantity": "2.125", "unit_price": "220.10"}
            response: dict[str, Any] = wire.copy()

            def respond(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=response)

            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
                sdk.ApiClient("https://test", http_client=http) as client,
            ):
                line = await client.invoices.line()
                assert line.quantity == wire["quantity"]
                assert line.unit_price == wire["unit_price"]
                assert line.model_dump() == wire
                response["unit_price"] = None
                assert (await client.invoices.line()).unit_price is None
                for invalid in ("", "+", ".", "2.1.3", "abc"):
                    response["quantity"] = invalid
                    with pytest.raises(sdk.ProtocolError):
                        await client.invoices.line()

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    finally:
        sys.path.remove(str(tmp_path))
        for name in tuple(sys.modules):
            if name == "invoice_sdk" or name.startswith("invoice_sdk."):
                del sys.modules[name]


def test_typescript_distinguishes_json_null_empty_and_mixed_responses(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    compiler = (
        Path(__file__).resolve().parents[4]
        / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
    )
    if node is None or not compiler.exists():
        pytest.skip("Run npm ci --ignore-scripts --prefix packages/fastapi-streams")
    responses = {
        "empty": {"204": {"description": "Removed"}},
        "jsonNull": {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": {"type": "null"}}},
            }
        },
        "mixed": {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": {"type": "string"}}},
            },
            "204": {"description": "No value"},
        },
        "emptyOk": {"200": {"description": "No body"}},
    }
    document = {
        "openapi": "3.1.0",
        "paths": {
            "/" + name: {"get": {"operationId": "records." + name, "responses": value}}
            for name, value in responses.items()
        },
    }
    files = render(
        build_plan(((SourceConfig("api", "openapi"), document),)),
        TargetConfig("typescript", "typescript", ("api",), tmp_path),
    )
    for name, content in files.items():
        path = tmp_path / "sdk" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (tmp_path / "usage.ts").write_text(
        """import {createClient} from "./sdk/index.js";
const client = createClient({baseUrl: "https://test"});
const empty: Promise<void> = client.records.empty();
const jsonNull: Promise<null> = client.records.jsonNull();
const mixed: Promise<string | void> = client.records.mixed();
void [empty, jsonNull, mixed];
""",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [
            node,
            str(compiler),
            "--strict",
            "--target",
            "es2022",
            "--module",
            "commonjs",
            "--lib",
            "es2022,dom,esnext.disposable",
            "--outDir",
            str(tmp_path / "js"),
            str(tmp_path / "usage.ts"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    (tmp_path / "runner.cjs").write_text(
        """
const assert = require("node:assert/strict");
const {createClient, ProtocolError} = require("./js/sdk/index.js");
let invalid = false, empty = false;
const client = createClient({baseUrl:"https://test", fetch: async input => {
  const path = new URL(String(input)).pathname;
  if(path === "/empty" || (path === "/mixed" && empty)) return new Response(null,{status:204});
  if(path === "/emptyOk") return new Response(invalid ? '"unexpected"' : null);
  return new Response(invalid ? "" : path === "/jsonNull" ? "null" : '"record"', {headers:{"content-type":"application/json"}});
}});
(async () => {
  assert.equal(await client.records.empty(), undefined);
  assert.equal(await client.records.emptyOk(), undefined);
  assert.equal(await client.records.jsonNull(), null);
  assert.equal(await client.records.mixed(), "record");
  empty = true;
  assert.equal(await client.records.mixed(), undefined);
  invalid = true;
  await assert.rejects(client.records.jsonNull(), ProtocolError);
  await assert.rejects(client.records.emptyOk(), ProtocolError);
  await client.close();
})().catch(error => {console.error(error); process.exitCode = 1;});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(tmp_path / "runner.cjs")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
