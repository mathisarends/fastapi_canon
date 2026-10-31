import codecs
import random

import httpx

from examples.agent_runs.generated_python.runtime import iter_sse

ALPHABET = ["data", ":", " ", "x", "世", "👋", "\r", "\n", "\r\n", "id", "﻿"]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def reference_frames(raw: bytes) -> list[str]:
    """The original character-by-character parser, kept as a test oracle."""
    text = codecs.decode(raw, "utf-8-sig")
    line, data, frames, skip_lf = "", [], [], False

    def accept() -> None:
        nonlocal line, data
        current, line = line, ""
        if not current:
            if data:
                frames.append("\n".join(data))
                data = []
            return
        field, _, value = current.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)

    for char in text:
        if skip_lf:
            skip_lf = False
            if char == "\n":
                continue
        if char in "\r\n":
            skip_lf = char == "\r"
            accept()
        else:
            line += char
    if line:
        accept()
    if data:
        frames.append("\n".join(data))
    return frames


def random_chunks(raw: bytes, rng: random.Random) -> list[bytes]:
    cuts = sorted(rng.sample(range(len(raw) + 1), min(len(raw) + 1, rng.randint(0, 6))))
    edges = [0, *cuts, len(raw)]
    return [raw[start:end] for start, end in zip(edges, edges[1:], strict=False)]


async def test_line_splitting_matches_the_character_parser():
    rng = random.Random(20261025)
    for _ in range(3000):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 40)))
        raw = text.encode()
        response = httpx.Response(200, stream=Chunks(random_chunks(raw, rng)))
        assert [frame async for frame in iter_sse(response)] == reference_frames(raw)


async def test_a_line_spanning_many_chunks_is_joined_once():
    raw = b"data: " + b"x" * 100_000 + b"\r"
    chunks = [raw[index : index + 7] for index in range(0, len(raw), 7)] + [b"\n\n"]
    response = httpx.Response(200, stream=Chunks(chunks))
    assert [frame async for frame in iter_sse(response)] == ["x" * 100_000]
