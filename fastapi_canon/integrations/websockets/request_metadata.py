import json
from collections.abc import Mapping
from types import MappingProxyType

REQUEST_METADATA_KEYS = ("traceparent", "tracestate", "baggage")
REQUEST_METADATA_MAX_BYTES = 4096
REQUEST_METADATA_CAPABILITY = {
    "version": 1,
    "location": "envelope",
    "member": "meta",
    "maxBytes": REQUEST_METADATA_MAX_BYTES,
    "allowedKeys": list(REQUEST_METADATA_KEYS),
}


def validate_request_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(
        key not in REQUEST_METADATA_KEYS or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("Request meta must contain only supported string fields")
    result = dict(value)
    try:
        size = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        )
    except UnicodeEncodeError as error:
        raise ValueError("Request meta must contain valid Unicode") from error
    if size > REQUEST_METADATA_MAX_BYTES:
        raise ValueError("Request meta exceeds 4096 UTF-8 bytes")
    return result


def observed_request_metadata(request: object) -> Mapping[str, str] | None:
    if not isinstance(request, dict) or "meta" not in request:
        return None
    try:
        return MappingProxyType(validate_request_metadata(request["meta"]))
    except ValueError:
        return None
