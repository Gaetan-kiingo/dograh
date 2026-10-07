"""P-32 (Swiss Voice Platform, ADR-002; CF-164): a step with a write tool cannot be left
before that tool has answered. The step's transition functions are kept out of the model's
list until one of the step's write tools (marked `svp_kind: write` by the platform) has
answered - a proposal, a result, a refusal or an error alike. Unset = stock behaviour.

The failure this prevents (2026-10-07, « Coiffeur Nico », runs 37 and 1375): the model
arrived in the booking step, said the appointment was confirmed and moved on, the booking
tool never called - the model that talks also chose the transition, in one completion."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

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
from api.services.workflow.pipecat_engine import PipecatEngine, write_step_gate_enabled
from api.services.workflow.pipecat_engine_custom_tools import is_svp_write_tool
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.conftest import MockToolModel

BOOK = MockToolModel(
    tool_uuid="book-uuid",
    name="book_appointment",
    description="Book the confirmed slot",
    definition={
        "schema_version": 1,
        "type": "http_api",
        "config": {
            "method": "POST",
            "url": "http://gateway/execute/book",
            "svp_kind": "write",
            "parameters": [{"name": "date", "type": "string", "description": "d", "required": True}],
        },
    },
)
SLOTS = MockToolModel(
    tool_uuid="slots-uuid",
    name="find_available_slots",
    description="Find free slots",
    definition={
        "schema_version": 1,
        "type": "http_api",
        "config": {"method": "POST", "url": "http://gateway/execute/slots", "svp_kind": "read", "parameters": []},
    },
)


def workflow(tool_uuids: list[str]) -> WorkflowGraph:
    """start -> booking (the step under test, two ways out) -> end / other."""
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(id="start", type="startCall", position=Position(x=0, y=0),
                      data=StartCallNodeData(name="Start", prompt="Greet.", is_start=True,
                                             allow_interrupt=False, add_global_prompt=False)),
            RFNodeDTO(id="booking", type="agentNode", position=Position(x=0, y=100),
                      data=AgentNodeData(name="Prise de rendez-vous - Confirmation", prompt="Book it.",
                                         allow_interrupt=False, add_global_prompt=False,
                                         tool_uuids=tool_uuids)),
            RFNodeDTO(id="other", type="agentNode", position=Position(x=0, y=200),
                      data=AgentNodeData(name="Autre chose", prompt="Anything else?",
                                         allow_interrupt=False, add_global_prompt=False)),
            RFNodeDTO(id="end", type="endCall", position=Position(x=0, y=300),
                      data=EndCallNodeData(name="End", prompt="Bye.", is_end=True,
                                           allow_interrupt=False, add_global_prompt=False)),
        ],
        edges=[
            RFEdgeDTO(id="s-b", source="start", target="booking",
                      data=EdgeDataDTO(label="Réserver", condition="the caller wants to book")),
            RFEdgeDTO(id="b-o", source="booking", target="other",
                      data=EdgeDataDTO(label="Autre chose", condition="the booking is done")),
            RFEdgeDTO(id="b-e", source="booking", target="end",
                      data=EdgeDataDTO(label="Prendre congé", condition="the caller says goodbye")),
        ],
    )
    return WorkflowGraph(dto)


def tool_names(context: LLMContext) -> set[str]:
    return {f.name for f in context.tools.standard_tools}


async def engine_in_booking_step(tools: list[MockToolModel], tool_uuids: list[str]) -> PipecatEngine:
    llm = Mock()
    llm.register_function = Mock()
    llm._update_settings = AsyncMock()
    llm._context = None
    wf = workflow(tool_uuids)
    engine = PipecatEngine(llm=llm, context=LLMContext(), workflow=wf, call_context_vars={}, workflow_run_id=1)
    with patch("api.db:db_client.get_organization_id_by_workflow_run_id", new_callable=AsyncMock, return_value=1), \
         patch("api.services.workflow.pipecat_engine_custom_tools.db_client") as db:
        db.get_tools_by_uuids = AsyncMock(return_value=tools)
        await engine.initialize()
        node = wf.nodes["booking"]
        engine._current_node = node
        await engine._setup_llm_context(node)
    return engine


def test_p32_switch(monkeypatch):
    monkeypatch.delenv("SVP_WRITE_STEP_GATE", raising=False)
    assert write_step_gate_enabled() is False
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    assert write_step_gate_enabled() is True
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "off")
    assert write_step_gate_enabled() is False


def test_p32_the_platform_marks_a_write_tool():
    assert is_svp_write_tool(BOOK) is True
    assert is_svp_write_tool(SLOTS) is False
    assert is_svp_write_tool(MockToolModel("x", "x", "x", {"config": {}})) is False
    assert is_svp_write_tool(MockToolModel("x", "x", "x", {})) is False  # a stock tool


@pytest.mark.asyncio
async def test_p32_the_transitions_are_held_until_the_write_tool_answers(monkeypatch):
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    engine = await engine_in_booking_step([BOOK, SLOTS], ["book-uuid", "slots-uuid"])
    # entering the step: the tools, no way out
    assert tool_names(engine.context) == {"book_appointment", "find_available_slots"}
    assert engine.write_gate_holds()
    # the transition handlers are still registered - only their schemas are held back
    registered = {c.args[0] for c in engine.llm.register_function.call_args_list}
    transitions = {e.get_function_name() for e in engine.workflow.nodes["booking"].out_edges}
    assert len(transitions) == 2 and transitions <= registered
    # the write tool answers (a proposal, a result, an error - anything): the ways out return
    await engine.release_write_gate("book_appointment")
    assert not engine.write_gate_holds()
    assert tool_names(engine.context) == {"book_appointment", "find_available_slots"} | transitions


@pytest.mark.asyncio
async def test_p32_a_step_without_a_write_tool_is_untouched(monkeypatch):
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    engine = await engine_in_booking_step([SLOTS], ["slots-uuid"])
    assert not engine.write_gate_holds()
    assert len(tool_names(engine.context)) == 3  # the read tool and both transitions


@pytest.mark.asyncio
async def test_p32_unset_is_stock_behaviour(monkeypatch):
    monkeypatch.delenv("SVP_WRITE_STEP_GATE", raising=False)
    engine = await engine_in_booking_step([BOOK], ["book-uuid"])
    assert not engine.write_gate_holds()
    assert len(tool_names(engine.context)) == 3  # the write tool and both transitions at once


@pytest.mark.asyncio
async def test_p32_the_gate_is_released_before_the_model_reads_the_answer(monkeypatch):
    """The handler gives the transitions back, then hands the answer to the model, so the
    completion that reads « proposal » (or « booked ») already has the ways out."""
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    engine = await engine_in_booking_step([BOOK], ["book-uuid"])
    assert engine.write_gate_holds()
    handler = engine._custom_tool_manager._create_http_tool_handler(BOOK, "book_appointment")
    seen: list[tuple[bool, dict]] = []

    async def result_callback(result, **_):
        seen.append((engine.write_gate_holds(), result))

    params = Mock(arguments={"date": "2026-10-08"}, result_callback=result_callback)
    answer = {"status": "success", "status_code": 200, "data": {"status": "proposal", "confirm": "t"}}
    with patch("api.services.workflow.pipecat_engine_custom_tools.execute_http_tool", new_callable=AsyncMock, return_value=answer), \
         patch("api.services.workflow.pipecat_engine_custom_tools.db_client") as db, \
         patch("api.db:db_client.get_organization_id_by_workflow_run_id", new_callable=AsyncMock, return_value=1):
        db.get_workflow_run_configurations = AsyncMock(return_value={})
        await handler(params)
    assert seen == [(False, answer)]  # released, then answered
    assert len(tool_names(engine.context)) == 3


@pytest.mark.asyncio
async def test_p32_a_failed_call_releases_too(monkeypatch):
    """The tool was called: an exception on the way is an answer as well - the step must
    not trap the caller when the gateway is unreachable."""
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    engine = await engine_in_booking_step([BOOK], ["book-uuid"])
    handler = engine._custom_tool_manager._create_http_tool_handler(BOOK, "book_appointment")
    results: list[dict] = []

    async def result_callback(result, **_):
        results.append(result)

    params = Mock(arguments={}, result_callback=result_callback)
    with patch("api.services.workflow.pipecat_engine_custom_tools.execute_http_tool", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("api.services.workflow.pipecat_engine_custom_tools.db_client") as db, \
         patch("api.db:db_client.get_organization_id_by_workflow_run_id", new_callable=AsyncMock, return_value=1):
        db.get_workflow_run_configurations = AsyncMock(return_value={})
        await handler(params)
    assert results[0]["status"] == "error" and not engine.write_gate_holds()


@pytest.mark.asyncio
async def test_p32_an_answer_from_another_step_changes_nothing(monkeypatch):
    monkeypatch.setenv("SVP_WRITE_STEP_GATE", "on")
    engine = await engine_in_booking_step([BOOK], ["book-uuid"])
    held = engine._svp_gate_node_id
    engine._current_node = engine.workflow.nodes["other"]  # a late answer after a step change
    await engine.release_write_gate("book_appointment")
    assert engine._svp_gate_node_id == held  # the booking step's record is left as it was


def test_p32_the_tool_schema_keeps_the_marker():
    """The runtime validates a tool's config through HttpApiConfig and drops unknown keys:
    the platform's marker must be a declared field, or the gate never engages (found on
    the first measured call, 2026-10-07)."""
    from api.schemas.tool import HttpApiConfig

    kept = HttpApiConfig.model_validate({"method": "POST", "url": "http://gw/execute/x", "svp_kind": "write"})
    assert kept.svp_kind == "write"
    assert HttpApiConfig.model_validate({"method": "GET", "url": "http://gw/x"}).svp_kind is None
    assert "svp_kind" in kept.model_dump()
