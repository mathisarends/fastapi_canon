"""Read-only SDP inspection for policy checks and stale-candidate filtering.

The runtime never rewrites SDP or tracks negotiation state from it; the engine
remains the only owner of the protocol state machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi_canon.integrations.webrtc.engine import TransceiverDirection


@dataclass(frozen=True)
class MediaSection:
    kind: str
    mid: str | None
    direction: TransceiverDirection
    """The direction from the perspective of the description's author."""
    port: int
    ufrag: str | None

    @property
    def rejected(self) -> bool:
        return self.port == 0

    @property
    def author_sends(self) -> bool:
        return not self.rejected and self.direction in (
            TransceiverDirection.SENDRECV,
            TransceiverDirection.SENDONLY,
        )

    @property
    def author_receives(self) -> bool:
        return not self.rejected and self.direction in (
            TransceiverDirection.SENDRECV,
            TransceiverDirection.RECVONLY,
        )


@dataclass(frozen=True)
class DescriptionSummary:
    sections: tuple[MediaSection, ...]

    @property
    def ufrags(self) -> frozenset[str]:
        return frozenset(s.ufrag for s in self.sections if s.ufrag is not None)

    def sending(self, kind: str) -> int:
        return sum(1 for s in self.sections if s.kind == kind and s.author_sends)


def summarize(sdp: str) -> DescriptionSummary:
    session_direction = TransceiverDirection.SENDRECV
    session_ufrag: str | None = None
    raw: list[dict[str, str | int | None]] = []
    current: dict[str, str | int | None] | None = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            parts = line[2:].split()
            port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            current = {
                "kind": parts[0] if parts else "",
                "port": port,
                "mid": None,
                "direction": None,
                "ufrag": None,
            }
            raw.append(current)
            continue
        if not line.startswith("a="):
            continue
        attribute, _, value = line[2:].partition(":")
        if attribute in TransceiverDirection:
            if current is None:
                session_direction = TransceiverDirection(attribute)
            else:
                current["direction"] = attribute
        elif attribute == "ice-ufrag":
            if current is None:
                session_ufrag = value
            else:
                current["ufrag"] = value
        elif attribute == "mid" and current is not None:
            current["mid"] = value
    sections = tuple(
        MediaSection(
            kind=str(item["kind"]),
            mid=None if item["mid"] is None else str(item["mid"]),
            direction=TransceiverDirection(str(item["direction"] or session_direction)),
            port=int(item["port"] or 0),
            ufrag=session_ufrag if item["ufrag"] is None else str(item["ufrag"]),
        )
        for item in raw
    )
    return DescriptionSummary(sections=sections)
