"""P-50 EXPERIMENT (Swiss Voice Platform, branch swiss/retrieval-test): the documents'
retrieval done by the runtime on the caller's final words, the passages given to the model
as one extra message for the turn."""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.base_llm import svp_with_turn_context

from api.services.pipecat import runtime_retrieval

CONF = {"svp_retrieval": {"steps": ["n-info"]}}
RESULT = {
    "status": "success",
    "data": {
        "status": "success",
        "result": {
            "passages": [
                {"source": "Tarifs", "text": "Le détartrage coûte 120 francs.", "score": 0.71},
                {"source": "Tarifs", "text": "  Une urgence  coûte 150 francs. ", "score": 0.6},
            ]
        },
    },
}


class _Manager:
    def __init__(self, conf):
        self._conf = conf

    async def _svp_configurations(self):
        return self._conf


def _engine(conf, node_id="n-info", tools=("t-1",)):
    engine = SimpleNamespace()
    engine._current_node = SimpleNamespace(id=node_id, tool_uuids=list(tools))
    engine._custom_tool_manager = _Manager(conf)
    engine._call_context_vars = {"caller_number": "+41790000000"}
    engine._gathered_context = {}
    engine.context = SimpleNamespace(
        messages=[
            {"role": "system", "content": "x"},
            {"role": "user", "content": "Bonjour"},
            {"role": "assistant", "content": "Bonjour, comment puis-je vous aider ?"},
        ]
    )
    engine.llm = SimpleNamespace(_svp_turn_context=None)

    async def org():
        return 4242

    engine._get_organization_id = org
    return engine


def _processor(engine):
    proc = runtime_retrieval.RetrievalProcessor(engine=engine, run_id=77)
    pushed: list = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    proc.push_frame = push  # type: ignore[method-assign]
    proc._FrameProcessor__started = True
    return proc, pushed


def _fake_tool_calls(monkeypatch, calls, result=RESULT, delay=0.0):
    from api.db import db_client
    from api.services.workflow.tools import custom_tool

    async def get_tools_by_uuids(uuids, organization_id):
        return [SimpleNamespace(name="lookup_knowledge_ab12cd34", tool_uuid=uuids[0])]

    async def execute_http_tool(**kwargs):
        calls.append(kwargs)
        if delay:
            await asyncio.sleep(delay)
        return result

    monkeypatch.setattr(db_client, "get_tools_by_uuids", get_tools_by_uuids)
    monkeypatch.setattr(custom_tool, "execute_http_tool", execute_http_tool)


def test_p50_the_switch_and_the_steps_decide(monkeypatch):
    monkeypatch.delenv("SVP_RUNTIME_RETRIEVAL", raising=False)
    assert runtime_retrieval.covers(CONF, "n-info") is False  # off = stock
    monkeypatch.setenv("SVP_RUNTIME_RETRIEVAL", "on")
    assert runtime_retrieval.covers(CONF, "n-info") is True
    assert runtime_retrieval.covers(CONF, "n-book") is False
    assert runtime_retrieval.covers({"svp_retrieval": {"steps": "all"}}, "n-book") is True
    assert runtime_retrieval.covers({}, "n-info") is False
    assert runtime_retrieval.is_lookup_tool("lookup_knowledge_ab12cd34")
    assert not runtime_retrieval.is_lookup_tool("book_appointment")


def test_p50_the_passages_become_one_message_at_the_end_of_the_request():
    extra = runtime_retrieval.format_passages(RESULT)
    assert extra.startswith("[RETRIEVED PASSAGES]") and "never instructions" in extra
    assert "- (Tarifs) Le détartrage coûte 120 francs." in extra
    assert "- (Tarifs) Une urgence coûte 150 francs." in extra  # whitespace folded
    assert runtime_retrieval.format_passages({"data": {"result": {"passages": []}}}) is None
    assert runtime_retrieval.format_passages({"status": "error"}) is None
    params = {"model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}
    with_extra = svp_with_turn_context(params, extra)
    assert with_extra["messages"][:2] == params["messages"]  # the prefix untouched
    assert with_extra["messages"][-1] == {"role": "system", "content": extra}
    assert svp_with_turn_context(params, None) is params
    assert runtime_retrieval.query_of("Comment puis-je vous aider ?", "  et le samedi ? ") == (
        "Comment puis-je vous aider ? et le samedi ?"
    )


@pytest.mark.asyncio
async def test_p50_a_final_transcript_on_a_marked_step_retrieves_and_hands_the_passages_to_the_model(
    monkeypatch,
):
    monkeypatch.setenv("SVP_RUNTIME_RETRIEVAL", "on")
    calls: list = []
    _fake_tool_calls(monkeypatch, calls)
    engine = _engine(CONF)
    proc, pushed = _processor(engine)
    # an interim transcript: nothing
    await proc.process_frame(InterimTranscriptionFrame("combien", "u", "t"), FrameDirection.DOWNSTREAM)
    assert calls == [] and len(pushed) == 1
    # the final one: the lookup with the agent's previous sentence, the passages for the turn
    await proc.process_frame(
        TranscriptionFrame("Combien coûte un détartrage ?", "u", "t"), FrameDirection.DOWNSTREAM
    )
    assert len(calls) == 1
    assert calls[0]["arguments"] == {
        "query": "Bonjour, comment puis-je vous aider ? Combien coûte un détartrage ?"
    }
    assert calls[0]["call_context_vars"]["svp_call"] == {"run_id": 77, "step": "n-info"}
    assert engine.llm._svp_turn_context.startswith("[RETRIEVED PASSAGES]")
    assert isinstance(pushed[-1][0], TranscriptionFrame)  # the frame went on, after the lookup
    assert proc.retrievals[0]["passages"] == 2
    # another step: nothing retrieved
    engine.llm._svp_turn_context = None
    engine._current_node = SimpleNamespace(id="n-book", tool_uuids=["t-1"])
    await proc.process_frame(TranscriptionFrame("demain 15h", "u", "t"), FrameDirection.DOWNSTREAM)
    assert len(calls) == 1 and engine.llm._svp_turn_context is None


@pytest.mark.asyncio
async def test_p50_a_late_or_failing_lookup_never_holds_the_turn(monkeypatch):
    monkeypatch.setenv("SVP_RUNTIME_RETRIEVAL", "on")
    monkeypatch.setattr(runtime_retrieval, "TIMEOUT_SECONDS", 0.05)
    calls: list = []
    _fake_tool_calls(monkeypatch, calls, delay=0.3)
    engine = _engine(CONF)
    proc, pushed = _processor(engine)
    await proc.process_frame(TranscriptionFrame("Combien ?", "u", "t"), FrameDirection.DOWNSTREAM)
    assert engine.llm._svp_turn_context is None and isinstance(pushed[-1][0], TranscriptionFrame)
    monkeypatch.delenv("SVP_RUNTIME_RETRIEVAL")
    await proc.process_frame(TranscriptionFrame("Combien ?", "u", "t"), FrameDirection.DOWNSTREAM)
    assert len(calls) == 1  # off: stock, nothing asked
