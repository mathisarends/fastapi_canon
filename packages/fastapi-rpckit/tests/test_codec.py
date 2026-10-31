import sys

import pytest
from fastapi_canon.integrations.websockets import RpcCodec, RpcParseError, RpcSuccess


@pytest.mark.parametrize(
    "message",
    [
        '{"value":"Grüße"}',
        b'{"value":"Gr\xc3\xbc\xc3\x9fe"}',
        bytearray(b'{"value":"ok"}'),
    ],
)
def test_decode_accepts_supported_json_frame_types(message) -> None:
    assert "value" in RpcCodec().decode(message)


@pytest.mark.parametrize(
    "message", ["{", b"\xff", "[" * 100_000], ids=["syntax", "utf8", "deep"]
)
def test_decode_translates_invalid_json_to_a_parse_error(message) -> None:
    with pytest.raises(RpcParseError):
        RpcCodec().decode(message)


def test_decode_normalizes_the_interpreter_integer_digit_limit():
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        with pytest.raises(RpcParseError):
            RpcCodec().decode('{"id":' + "9" * 641 + "}")
    finally:
        sys.set_int_max_str_digits(previous)


def test_encode_is_compact_unicode_json_for_single_and_batch_responses() -> None:
    response = RpcSuccess(id=1, result="Grüße")
    codec = RpcCodec()

    assert codec.encode(response) == '{"jsonrpc":"2.0","id":1,"result":"Grüße"}'
    assert codec.encode([response]) == ('[{"jsonrpc":"2.0","id":1,"result":"Grüße"}]')
