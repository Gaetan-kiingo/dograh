"""P-33 (Swiss Voice Platform, ADR-002 / ADR-052): the platform told a call's life -
started at the answer (CALL-001), the opening spoken (CALL-002, PRV-002), recording
started (CALL-013), ended on every path (CALL-015) - with the runtime's bearer, off the
call's path, three attempts; unset = nothing is sent. CF-007: recording on the caller's
yes through the opening step's `recording_consent` function."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services import svp_call_events as events
from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    calls: list[dict] = []
    answers: list = []  # per attempt: an int status or an exception

    def __init__(self, *a, **k):
        _FakeClient.timeout = k.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None):
        answer = _FakeClient.answers.pop(0) if _FakeClient.answers else 202
        if isinstance(answer, Exception):
            raise answer
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(answer)


class _Db:
    """The run as the runtime holds it: pinned definition with the platform's marker."""

    def __init__(self):
        self.updates: list[dict] = []
        self.stale: list = []

    async def get_workflow_run_with_context(self, run_id):
        definition = SimpleNamespace(
            version_number=3,
            workflow_configurations={
                "svp_publish": {"publish_id": "pub-1", "agent_id": "agent-1", "version": 7},
                "recording_enabled": True,
                "svp_recording_consent": "ask",
            },
        )
        run = SimpleNamespace(
            workflow_id=9,
            definition_id=31,
            definition=definition,
            mode="ari",
            initial_context={
                "caller_number": "+41791234567",
                "called_number": "+41445550000",
                "direction": "inbound",
                "provider": "ari",
            },
        )
        return run, 5

    async def update_workflow_run(self, run_id, **kw):
        self.updates.append({"run_id": run_id, **kw})

    async def get_stale_incomplete_voice_runs(self, *, created_before, exclude_modes, limit=200):
        return [r for r in self.stale if r.mode not in exclude_modes]


@pytest.fixture
def platform(monkeypatch):
    import api.db as db_module

    db = _Db()
    monkeypatch.setattr(events.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(db_module, "db_client", db)
    monkeypatch.setattr(events, "ATTEMPT_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setenv("SVP_KEY_SERVICE_TOKEN", "service-bearer")
    monkeypatch.setenv("SVP_CALL_EVENTS_URL", "http://platform/v1/internal/calls/events")
    _FakeClient.calls, _FakeClient.answers = [], []
    events._runs.clear()
    yield db
    events._runs.clear()


def kinds() -> list[str]:
    return [c["json"]["kind"] for c in _FakeClient.calls]


@pytest.mark.asyncio
async def test_p33_unset_sends_nothing(platform, monkeypatch):
    monkeypatch.delenv("SVP_CALL_EVENTS_URL", raising=False)
    events.media_connected(41)
    events.opening_spoken(41)
    events.ended(41, "user_hangup")
    await events.flush()
    assert _FakeClient.calls == []
    assert await events.send("started", 41) is False


@pytest.mark.asyncio
async def test_call001_the_answer_says_started_with_the_pinned_version_and_the_numbers(platform):
    events.media_connected(41)
    await events.flush()
    assert kinds() == ["started"]
    body = _FakeClient.calls[0]["json"]
    assert body["organization_id"] == 5 and body["workflow_id"] == 9 and body["run_id"] == 41
    assert body["definition_id"] == 31 and body["definition_version"] == 3
    assert body["publish"] == {"publish_id": "pub-1", "agent_id": "agent-1", "version": 7}
    assert body["caller_number"] == "+41791234567" and body["called_number"] == "+41445550000"
    assert body["mode"] == "ari" and body["direction"] == "inbound" and body["provider"] == "ari"
    assert body["recording_enabled"] is True and body["recording_consent"] == "ask"
    assert body["at"].endswith("+00:00")
    assert _FakeClient.calls[0]["headers"] == {"Authorization": "Bearer service-bearer"}
    assert _FakeClient.timeout == events.POST_TIMEOUT


@pytest.mark.asyncio
async def test_call002_the_opening_spoken_to_its_end_says_disclosure_played_once(platform):
    events.media_connected(41)
    events.opening_queued(41, "Coiffeur Nico, bonjour.  Je suis un assistant virtuel. ")
    events.first_sound(41)
    events.opening_spoken(41)
    events.opening_spoken(41)  # the next utterances are not the opening
    await events.flush()
    assert kinds() == ["started", "disclosure_played"]
    body = _FakeClient.calls[1]["json"]
    assert body["outcome"] == "played"
    # the fingerprint of the normalised text, never the text
    assert body["text_hash"] == events.text_hash("Coiffeur Nico, bonjour. Je suis un assistant virtuel.")
    assert "bonjour" not in str(body)
    assert isinstance(body["first_sound_after_media_seconds"], float)
    assert body["first_sound_after_media_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_call015_ended_says_whether_the_opening_was_ever_heard(platform):
    events.media_connected(41)
    events.ended(41, "user_hangup")
    await events.flush()
    assert kinds() == ["started", "ended"]
    assert _FakeClient.calls[1]["json"]["reason"] == "user_hangup"
    assert _FakeClient.calls[1]["json"]["disclosure"] == "not_played"
    # the run's state is forgotten with it
    assert 41 not in events._runs

    _FakeClient.calls.clear()
    events.media_connected(42)
    events.opening_spoken(42)
    events.ended(42, "user_qualified")
    await events.flush()
    assert kinds() == ["started", "disclosure_played", "ended"]
    assert _FakeClient.calls[2]["json"]["disclosure"] == "played"


@pytest.mark.asyncio
async def test_call013_recording_started_names_its_trigger(platform):
    events.recording_started(41, "consent")
    await events.flush()
    assert kinds() == ["recording_started"]
    assert _FakeClient.calls[0]["json"]["trigger"] == "consent"


@pytest.mark.asyncio
async def test_p33_delivery_tries_three_times_then_gives_up_without_raising(platform):
    _FakeClient.answers = [httpx.ConnectError("down"), 503, 202]
    assert await events.send("started", 41) is True
    assert len(_FakeClient.calls) == 2  # the 503 and the 202 reached the fake
    _FakeClient.calls.clear()
    _FakeClient.answers = [httpx.ConnectError("down")] * 3
    assert await events.send("ended", 41, reason="x") is False
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_p33_a_refusal_is_said_once(platform):
    _FakeClient.answers = [404, 202, 202]
    assert await events.send("started", 41) is False
    assert len(_FakeClient.calls) == 1


@pytest.mark.asyncio
async def test_call015_a_failure_before_the_start_says_ended(platform):
    from api.services import workflow_run_failure

    with patch.object(workflow_run_failure, "db_client", platform):
        with patch("api.tasks.arq.enqueue_job", new_callable=AsyncMock):
            await workflow_run_failure.mark_workflow_run_failed(41, "Quota exceeded")
    assert kinds() == ["ended"]
    body = _FakeClient.calls[0]["json"]
    assert body["reason"] == "failed_before_start" and body["error"] == "Quota exceeded"
    assert body["disclosure"] == "not_played"
    assert platform.updates[0]["is_completed"] is True


@pytest.mark.asyncio
async def test_call015_the_sweep_finishes_the_runs_a_crash_left_open_as_lost(platform):
    from api.tasks import svp_stale_runs

    old = datetime.now(UTC) - timedelta(hours=3)
    platform.stale = [
        SimpleNamespace(id=7, mode="ari", gathered_context={"call_id": "c7"}, created_at=old),
        SimpleNamespace(id=8, mode="textchat", gathered_context={}, created_at=old),
    ]
    with patch.object(svp_stale_runs, "db_client", platform):
        assert await svp_stale_runs.sweep_stale_voice_runs({}) == 1
    assert platform.updates[0]["run_id"] == 7
    assert platform.updates[0]["gathered_context"]["call_disposition"] == "lost"
    assert platform.updates[0]["gathered_context"]["call_id"] == "c7"
    assert kinds() == ["ended"]
    assert _FakeClient.calls[0]["json"]["reason"] == "lost"


@pytest.mark.asyncio
async def test_call015_the_sweep_is_a_no_op_without_the_switch(platform, monkeypatch):
    from api.tasks import svp_stale_runs

    monkeypatch.delenv("SVP_CALL_EVENTS_URL", raising=False)
    platform.stale = [SimpleNamespace(id=7, mode="ari", gathered_context={}, created_at=datetime.now(UTC))]
    with patch.object(svp_stale_runs, "db_client", platform):
        assert await svp_stale_runs.sweep_stale_voice_runs({}) == 0
    assert platform.updates == []


# --- CF-007: recording on the caller's yes --------------------------------------------------


def workflow() -> WorkflowGraph:
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(id="start", type="startCall", position=Position(x=0, y=0),
                      data=StartCallNodeData(name="Accueil", prompt="Greet.", is_start=True,
                                             allow_interrupt=False, add_global_prompt=False)),
            RFNodeDTO(id="msg", type="agentNode", position=Position(x=0, y=100),
                      data=AgentNodeData(name="Message", prompt="Take it.",
                                         allow_interrupt=False, add_global_prompt=False)),
            RFNodeDTO(id="end", type="endCall", position=Position(x=0, y=200),
                      data=EndCallNodeData(name="Fin", prompt="Bye.", is_end=True,
                                           allow_interrupt=False, add_global_prompt=False)),
        ],
        edges=[
            RFEdgeDTO(id="s-m", source="start", target="msg",
                      data=EdgeDataDTO(label="Message", condition="a message")),
            RFEdgeDTO(id="m-e", source="msg", target="end",
                      data=EdgeDataDTO(label="Fin", condition="goodbye")),
        ],
    )  # fmt: skip
    return WorkflowGraph(dto)


async def engine_on(node_id: str, consent: str | None) -> tuple[PipecatEngine, Mock]:
    llm = Mock()
    llm.register_function = Mock()
    llm._update_settings = AsyncMock()
    llm._context = None
    wf = workflow()
    engine = PipecatEngine(llm=llm, context=LLMContext(), workflow=wf, call_context_vars={}, workflow_run_id=41)
    engine._svp_recording_consent = consent
    with patch("api.db:db_client.get_organization_id_by_workflow_run_id", new_callable=AsyncMock, return_value=1):
        await engine.initialize()
        node = wf.nodes[node_id]
        engine._current_node = node
        await engine._setup_llm_context(node)
    return engine, llm


def tool_names(context: LLMContext) -> set[str]:
    return {f.name for f in context.tools.standard_tools}


@pytest.mark.asyncio
async def test_cf007_the_opening_step_offers_the_consent_function_only_when_the_policy_asks():
    engine, llm = await engine_on("start", "ask")
    assert "recording_consent" in tool_names(engine.context)
    assert any(c.args[0] == "recording_consent" for c in llm.register_function.call_args_list)
    engine, _ = await engine_on("msg", "ask")  # a later step: no
    assert "recording_consent" not in tool_names(engine.context)
    engine, _ = await engine_on("start", None)  # a policy without the question: no
    assert "recording_consent" not in tool_names(engine.context)


@pytest.mark.asyncio
async def test_cf007_the_callers_yes_starts_the_recording_and_a_no_leaves_it_off():
    engine, llm = await engine_on("start", "ask")
    consent_func = next(c.args[1] for c in llm.register_function.call_args_list if c.args[0] == "recording_consent")
    started = []

    async def start():
        started.append(True)

    engine._svp_start_recording = start
    result_callback = AsyncMock()
    await consent_func(SimpleNamespace(arguments={"granted": False}, result_callback=result_callback))
    assert started == []
    assert result_callback.call_args.args[0]["recording"] == "off"
    assert engine._svp_consent_answered is True

    engine, llm = await engine_on("start", "ask")
    consent_func = next(c.args[1] for c in llm.register_function.call_args_list if c.args[0] == "recording_consent")
    engine._svp_start_recording = start
    result_callback = AsyncMock()
    await consent_func(SimpleNamespace(arguments={"granted": True}, result_callback=result_callback))
    assert started == [True]
    assert result_callback.call_args.args[0]["recording"] == "started"


def test_p35_the_carrier_legs_codec_comes_from_the_edge_on_the_ari_path():
    """CF-213 (2026-10-28): the owner's Peoplefone calls ran A-law on the wire while the
    session said PCMU/8000 - this leg's format. The edge sets SVP_CODEC for the runtime."""
    from api.services.svp_call_events import codec_of

    assert codec_of({"svp_codec": "(alaw)"}, "ari") == "PCMA/8000"
    assert codec_of({"svp_codec": "alaw"}, "ari") == "PCMA/8000"
    assert codec_of({"svp_codec": "(ulaw|alaw)"}, "ari") == "PCMU/8000"
    assert codec_of({"svp_codec": "(speex)"}, "ari") == "PCMU/8000"  # unknown: this leg's
    assert codec_of({}, "ari") == "PCMU/8000" and codec_of({}, "twilio") == "PCMU/8000"
    assert codec_of({}, "smallwebrtc") == "opus/48000"
