import assert from "node:assert/strict";
import { test } from "node:test";
import { createClient, HttpError, isHttpError, TransportError } from "../../examples/agent_runs/generated/index.js";
import { consumeWithErrors, decodeValidationError } from "../../examples/agent_runs/errors.js";

type MissingProblem = {
  code: "missing";
  status: 404;
  title: "Not found";
  type: "urn:example:missing";
};

if (false) {
  // @ts-expect-error A generic argument does not replace body validation.
  isHttpError<MissingProblem>(new Error());
}

function isMissingProblem(value: unknown): value is MissingProblem {
  return typeof value === "object" && value !== null
    && "code" in value && value.code === "missing"
    && "status" in value && value.status === 404
    && "title" in value && value.title === "Not found"
    && "type" in value && value.type === "urn:example:missing";
}

test("HTTP error guards validate bodies before narrowing details", () => {
  const body: MissingProblem = {
    code: "missing", status: 404, title: "Not found", type: "urn:example:missing",
  };
  const error: unknown = new HttpError(404, body, new Response(null, { status: 404 }));
  if (!isHttpError(error, isMissingProblem)) assert.fail("Expected a validated problem");
  const code: "missing" = error.details.code;
  assert.equal(code, "missing");
  for (const details of ["<html>Bad gateway</html>", undefined, null, {}, { code: "missing" }, { ...body, status: 500 }]) {
    const unexpected = new HttpError(404, details, new Response(null, { status: 404 }));
    assert.equal(isHttpError(unexpected, isMissingProblem), false);
    assert.ok(unexpected instanceof HttpError);
    assert.equal(unexpected.details, details);
  }
  for (const other of [body, new Error("network"), new TransportError("network"), null]) {
    assert.equal(isHttpError(other, isMissingProblem), false);
  }
});

test("HTTP error guards do not invoke body validators for non-HTTP failures", () => {
  let calls = 0;
  const validator = (_value: unknown): _value is MissingProblem => { calls++; return false; };
  assert.equal(isHttpError(new TransportError("offline"), validator), false);
  assert.equal(calls, 0);
});

test("body guards leave status and operation checks to callers", () => {
  const body = { code: "missing", status: 404, title: "Not found", type: "urn:example:missing" };
  const error = new HttpError(502, body, new Response(null, { status: 502 }));
  assert.equal(isHttpError(error, isMissingProblem), true);
  assert.equal(error.status, 502);
});

test("consumer example handles validated HTTP errors and rethrows unexpected bodies", async t => {
  t.mock.method(console, "error", () => {});
  const makeClient = (body: unknown) => createClient({
    baseUrl: "http://example.test",
    fetch: async () => new Response(JSON.stringify(body), { status: 422 }),
  });
  await consumeWithErrors(makeClient({ detail: [{ loc: ["run_id"], msg: "Invalid", type: "value_error" }] }), "demo");
  for (const unexpected of [[], "<html>Bad gateway</html>", null, { detail: "invalid" }, { detail: [{}] }]) {
    await assert.rejects(() => consumeWithErrors(makeClient(unexpected), "demo"), HttpError);
  }
});

test("consumer decoder retains generic failures and checks the actual status", () => {
  const body = { detail: [{ loc: ["run_id", 0], msg: "Invalid", type: "value_error" }] };
  const response = new Response(null, { status: 422 });
  const expected = new HttpError(422, body, response);
  assert.equal(decodeValidationError(expected), body);
  for (const details of ["Bad gateway", undefined, null, [], { detail: [{}] }]) {
    const error = new HttpError(422, details, response);
    assert.equal(decodeValidationError(error), undefined);
    assert.equal(error.details, details);
    assert.equal(error.response, response);
  }
  assert.equal(decodeValidationError(new HttpError(502, body, response)), undefined);
  assert.equal(decodeValidationError(new TransportError("offline")), undefined);
});
