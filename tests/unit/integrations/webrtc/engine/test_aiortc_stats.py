"""Normalize only metrics whose units the engine can establish reliably."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiortc import RTCPeerConnection
from aiortc.stats import (
    RTCInboundRtpStreamStats,
    RTCOutboundRtpStreamStats,
    RTCStatsReport,
)
from fastapi_canon.integrations.webrtc import MediaKind
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcPeer


@pytest.mark.parametrize(("kind", "jitter"), [("audio", 480), ("video", 900)])
async def test_jitter_without_a_stream_clock_rate_is_unavailable(
    kind: str, jitter: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = RTCPeerConnection()
    report = RTCStatsReport()
    report.add(
        RTCInboundRtpStreamStats(
            timestamp=datetime.now(UTC),
            type="inbound-rtp",
            id="inbound",
            ssrc=42,
            kind=kind,
            transportId="transport",
            packetsReceived=20,
            packetsLost=2,
            jitter=jitter,
        )
    )
    report.add(
        RTCOutboundRtpStreamStats(
            timestamp=datetime.now(UTC),
            type="outbound-rtp",
            id="outbound",
            ssrc=43,
            kind=kind,
            transportId="transport",
            packetsSent=10,
            bytesSent=100,
            trackId="track",
        )
    )
    monkeypatch.setattr(connection, "getStats", AsyncMock(return_value=report))
    peer = AiortcPeer(connection, Mock(), {})
    try:
        stats = await peer.stats()
        assert len(stats.inbound) == 1
        media = stats.inbound[0]
        assert media.kind is MediaKind(kind)
        assert media.packets == 20
        assert media.packets_lost == 2
        assert media.jitter_seconds is None
        assert stats.outbound[0].kind is MediaKind(kind)
    finally:
        await peer.close()
