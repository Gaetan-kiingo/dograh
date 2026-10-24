"""D2b (Swiss Voice Platform, ADR-021 point 2, ADR-053; ADR-002 P-37, P-38, P-39). P-37: the
ARI manager resolves an edge's `secretref:` password like a carrier's token (CF-173). P-38:
the pipeline's heartbeat is not held behind the audio an output transport paces in real
time (CF-174). P-39: chan_websocket's plain-text messages are read, a keypad press becomes
a DTMF frame (CF-175)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony import ari_manager
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import HeartbeatFrame, InputDTMFFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.asterisk import AsteriskFrameSerializer
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

# --- P-37 ----------------------------------------------------------------------------------------


def _row(password):
    return SimpleNamespace(
        id=7,
        organization_id=174,
        credentials={
            "ari_endpoint": "http://asterisk:8088",
            "app_name": "svp",
            "app_password": password,
            "ws_client_name": "svp-media",
            "stasis_app_name": "dograh_abc",
        },
    )


@pytest.mark.asyncio
async def test_p37_the_ari_password_reference_is_resolved_only_with_the_switch(monkeypatch):
    monkeypatch.setenv("SVP_PROVIDER_PEOPLEFONE_SIP_PASSWORD", "the-real-ari-password")
    reference = "secretref:SVP_PROVIDER_PEOPLEFONE_SIP_PASSWORD"
    manager = ari_manager.ARIManager()
    with patch.object(
        ari_manager.db_client,
        "list_active_telephony_configurations_by_provider",
        AsyncMock(return_value=[_row(reference)]),
    ), patch.object(ari_manager, "external_pbx_integrations_enabled", AsyncMock(return_value=False)):
        monkeypatch.delenv("SVP_TELEPHONY_SECRET_REFS", raising=False)
        stock = await manager._load_ari_configs()
        assert stock[0]["app_password"] == reference  # stock: the value as stored
        monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
        resolved = await manager._load_ari_configs()
        assert resolved[0]["app_password"] == "the-real-ari-password"
        assert resolved[0]["stasis_app_name"] == "dograh_abc"
        assert _row(reference).credentials["app_password"] == reference  # the row untouched


@pytest.mark.asyncio
async def test_p37_an_unresolvable_reference_connects_nothing(monkeypatch):
    monkeypatch.setenv("SVP_TELEPHONY_SECRET_REFS", "on")
    monkeypatch.delenv("SVP_PROVIDER_PEOPLEFONE_SIP_PASSWORD", raising=False)
    manager = ari_manager.ARIManager()
    rows = [_row("secretref:SVP_PROVIDER_PEOPLEFONE_SIP_PASSWORD"), _row("a-literal-for-the-local-proof")]
    with patch.object(
        ari_manager.db_client,
        "list_active_telephony_configurations_by_provider",
        AsyncMock(return_value=rows),
    ), patch.object(ari_manager, "external_pbx_integrations_enabled", AsyncMock(return_value=False)), patch(
        "api.services.configuration.secret_refs._fetch", AsyncMock(side_effect=RuntimeError("key service down"))
    ):
        configs = await manager._load_ari_configs()
    # the bad row is skipped, the loop survives, the other row connects
    assert [c["app_password"] for c in configs] == ["a-literal-for-the-local-proof"]


# --- P-38 ----------------------------------------------------------------------------------------


class _Transport(BaseOutputTransport):
    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True))
        self.pushed: list = []
        self.handled: list = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    async def _handle_frame(self, frame):
        self.handled.append(frame)


@pytest.mark.asyncio
async def test_p38_the_heartbeat_skips_the_paced_audio_queue_only_with_the_switch(monkeypatch):
    heartbeat = HeartbeatFrame(timestamp=1)
    monkeypatch.delenv("SVP_HEARTBEAT_BYPASS", raising=False)
    stock = _Transport()
    await stock.process_frame(heartbeat, FrameDirection.DOWNSTREAM)
    assert stock.handled == [heartbeat] and stock.pushed == []  # stock: queued behind the audio
    monkeypatch.setenv("SVP_HEARTBEAT_BYPASS", "on")
    patched = _Transport()
    await patched.process_frame(heartbeat, FrameDirection.DOWNSTREAM)
    assert patched.pushed == [heartbeat] and patched.handled == []


class _Paused(FrameProcessor):
    """A processor paused the way the TTS service pauses itself while the bot speaks."""

    def __init__(self):
        super().__init__()
        self.pushed: list = []
        self.queued: list = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    async def process_frame(self, frame, direction):
        self.queued.append(frame)


@pytest.mark.asyncio
async def test_p38_a_paused_processor_lets_the_heartbeat_through_only_with_the_switch(monkeypatch):
    heartbeat = HeartbeatFrame(timestamp=2)
    for switch, expect_pushed in (("", False), ("on", True)):
        if switch:
            monkeypatch.setenv("SVP_HEARTBEAT_BYPASS", switch)
        else:
            monkeypatch.delenv("SVP_HEARTBEAT_BYPASS", raising=False)
        processor = _Paused()
        processor._enable_direct_mode = True  # frames are processed inline, no task
        await processor.pause_processing_frames()
        await processor.queue_frame(heartbeat)
        assert (processor.pushed == [heartbeat]) is expect_pushed
        assert (processor.queued == []) is expect_pushed  # stock: it waits with the others


# --- P-39 ----------------------------------------------------------------------------------------


def _serializer():
    return AsteriskFrameSerializer(channel_id="c1", ari_endpoint="http://asterisk:8088", app_name="svp", app_password="x")


@pytest.mark.asyncio
async def test_p39_a_keypad_press_becomes_a_dtmf_frame(monkeypatch):
    monkeypatch.setenv("SVP_ARI_TEXT_MESSAGES", "on")
    frame = await _serializer().deserialize("DTMF_END digit:5")
    assert isinstance(frame, InputDTMFFrame) and frame.button == KeypadEntry.FIVE
    assert await _serializer().deserialize("DTMF_END digit:#") is not None
    assert await _serializer().deserialize("DTMF_END digit:Z") is None


@pytest.mark.asyncio
async def test_p39_the_media_messages_are_read_quietly_and_stock_stays_stock(monkeypatch):
    monkeypatch.setenv("SVP_ARI_TEXT_MESSAGES", "on")
    warnings: list[str] = []
    from loguru import logger

    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        s = _serializer()
        for text in (
            "MEDIA_START connection_id:svp-media channel:WebSocket/svp-media/0x1 format:ulaw optimal_frame_size:160 ptime:20",
            "MEDIA_XOFF",
            "MEDIA_XON",
            "STATUS queue_length:3 xon_level:10 xoff_level:100 queue_full:false bulk_media:false media_paused:false",
            "QUEUE_DRAINED",
        ):
            assert await s.deserialize(text) is None
        assert warnings == []
        assert await s.deserialize("SOMETHING_NEW a:b") is None
        assert any("Unknown text message" in w for w in warnings)
        monkeypatch.delenv("SVP_ARI_TEXT_MESSAGES", raising=False)
        warnings.clear()
        assert await s.deserialize("MEDIA_START connection_id:x") is None
        assert any("Failed to parse JSON" in w for w in warnings)  # stock behaviour unset
    finally:
        logger.remove(sink)
