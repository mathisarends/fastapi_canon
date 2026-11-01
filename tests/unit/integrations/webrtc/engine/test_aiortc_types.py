"""The reference adapter converts native strings to the public engine enums."""

from unittest.mock import Mock

import pytest
from aiortc import AudioStreamTrack, RTCPeerConnection, VideoStreamTrack
from fastapi_canon.integrations.webrtc import (
    ChannelState,
    ConnectionState,
    DescriptionType,
    MediaKind,
    SignalingState,
    TransceiverDirection,
)
from fastapi_canon.integrations.webrtc.engine import ChannelParameters
from fastapi_canon.integrations.webrtc.engines.aiortc import (
    AiortcEngine,
    AiortcPeer,
    AiortcTrack,
)

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("kind", list(MediaKind))
async def test_native_track_kinds_are_converted(kind: MediaKind) -> None:
    track = AudioStreamTrack() if kind is MediaKind.AUDIO else VideoStreamTrack()
    connection = RTCPeerConnection()
    peer = AiortcPeer(connection, Mock(), {})
    try:
        assert AiortcEngine().track_kind(track) is kind
        assert AiortcTrack(track).kind is kind
        assert peer.add_track(track).kind is kind
    finally:
        await peer.close()
        track.stop()


async def test_native_states_and_enum_inputs_across_negotiation() -> None:
    connection = RTCPeerConnection()
    observer = Mock()
    peer = AiortcPeer(connection, observer, {MediaKind.AUDIO: ("audio/opus",)})
    try:
        initial_signaling_state = peer.signaling_state
        initial_connection_state = peer.connection_state
        assert initial_signaling_state is SignalingState.STABLE
        assert initial_connection_state is ConnectionState.NEW
        channel = peer.create_data_channel(ChannelParameters("chat"))
        initial_channel_state = channel.ready_state
        assert initial_channel_state is ChannelState.CONNECTING
        peer.add_transceiver(MediaKind.AUDIO, TransceiverDirection.RECVONLY)
        offer = await peer.create_offer()
        assert offer.type is DescriptionType.OFFER
        offered_state = peer.signaling_state
        assert offered_state is SignalingState.HAVE_LOCAL_OFFER
        assert connection.getTransceivers()[0].direction == "recvonly"
        assert (await peer.stats()).connection_state is ConnectionState.NEW
    finally:
        await peer.close()
    assert peer.signaling_state is SignalingState.CLOSED
    assert peer.connection_state is ConnectionState.CLOSED
    assert observer.connection_state.call_args.args[0] is ConnectionState.CLOSED
