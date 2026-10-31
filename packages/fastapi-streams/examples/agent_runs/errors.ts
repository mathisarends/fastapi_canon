import {
  HttpError, isHttpError, StreamParseError, TransportError,
  type Client, type RequestOptions, type StreamRunEventsErrorBody,
} from "./generated/index.js";

export async function consumeWithErrors(
  client: Client,
  runId: string,
  options: RequestOptions = {},
): Promise<void> {
  try {
    for await (const event of client.agentRuns.streamRunEvents({ runId }, options)) {
      if (event.type === "run.failed") console.error(event.message);
      else console.log(event);
    }
  } catch (error: unknown) {
    if (options.signal?.aborted) throw error;
    const body = decodeValidationError(error);
    if (body !== undefined) {
      console.error("Invalid stream parameters", body.detail ?? []);
      return;
    }
    if (error instanceof HttpError) {
      console.error("Unexpected HTTP response", error.status, error.details);
    } else if (error instanceof StreamParseError) {
      console.error("Invalid event JSON", error.data);
    } else if (error instanceof TransportError) {
      console.error("Stream transport failed", error.message);
    }
    throw error;
  }
}

// Consumer-local decoder: scope the generated body alias to this operation/status.
export function decodeValidationError(error: unknown): StreamRunEventsErrorBody | undefined {
  if (isHttpError(error, isValidationErrorBody) && error.status === 422) return error.details;
  return undefined;
}

function isValidationErrorBody(value: unknown): value is StreamRunEventsErrorBody {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  if (!("detail" in value)) return true;
  return Array.isArray(value.detail) && value.detail.every((item: unknown) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return false;
    return "loc" in item && Array.isArray(item.loc)
      && item.loc.every((part: unknown) => typeof part === "string"
        || (typeof part === "number" && Number.isInteger(part)))
      && "msg" in item && typeof item.msg === "string"
      && "type" in item && typeof item.type === "string";
  });
}
