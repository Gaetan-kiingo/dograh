"""P-10 (ADR-002; plan package C9): what the caller gave in earlier steps is usable in
later steps' instructions. SVP_COLLECTED_IN_PROMPTS=1 renders a step's {{key}} from the
values extracted so far (the call's own variables win), and a background extraction
that finishes after the next step started renders that step's instructions again when
they use one of its keys. Unset = stock behaviour: prompts render from the initial
context only."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    EndCallNodeData,
    ExtractionVariableDTO,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
    VariableType,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph

KNOWN = "[ALREADY KNOWN]\n- Name: {{user_name | fallback:(not given yet)}}"


def _workflow(agent_prompt: str = KNOWN) -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start", type="startCall", position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Start", prompt="Ask the caller's name.", is_start=True,
                        add_global_prompt=False, extraction_enabled=True,
                        extraction_prompt="Extract the caller's name.",
                        extraction_variables=[ExtractionVariableDTO(
                            name="user_name", type=VariableType.string, prompt="The name")],
                    ),
                ),
                RFNodeDTO(
                    id="agent", type="agentNode", position=Position(x=0, y=200),
                    data=AgentNodeData(name="Agent", prompt=agent_prompt, add_global_prompt=False),
                ),
                RFNodeDTO(
                    id="end", type="endCall", position=Position(x=0, y=400),
                    data=EndCallNodeData(name="End", prompt="Say goodbye.", is_end=True,
                                         add_global_prompt=False),
                ),
            ],
            edges=[
                RFEdgeDTO(id="a", source="start", target="agent",
                          data=EdgeDataDTO(label="Next", condition="name given")),
                RFEdgeDTO(id="b", source="agent", target="end",
                          data=EdgeDataDTO(label="End", condition="done")),
            ],
        )
    )


def _engine(monkeypatch, *, on: bool, agent_prompt: str = KNOWN) -> PipecatEngine:
    if on:
        monkeypatch.setenv("SVP_COLLECTED_IN_PROMPTS", "1")
    else:
        monkeypatch.delenv("SVP_COLLECTED_IN_PROMPTS", raising=False)
    llm = MagicMock()
    llm._update_settings = AsyncMock()
    engine = PipecatEngine(
        llm=llm, context=MagicMock(), workflow=_workflow(agent_prompt),
        call_context_vars={"caller_number": "+41790000000"}, workflow_run_id=1,
    )
    manager = MagicMock()
    manager._perform_extraction = AsyncMock(return_value={"user_name": "Anna Muster"})
    engine._variable_extraction_manager = manager
    return engine


def test_p10_a_later_step_renders_a_value_an_earlier_step_collected(monkeypatch):
    engine = _engine(monkeypatch, on=True)
    engine._gathered_context["extracted_variables"] = {"user_name": "Anna Muster"}
    assert engine._format_prompt(KNOWN).endswith("- Name: Anna Muster")
    # not given yet: the fallback says so, never an empty line
    engine._gathered_context["extracted_variables"] = {}
    assert engine._format_prompt(KNOWN).endswith("- Name: (not given yet)")


def test_p10_the_calls_own_variables_are_never_replaced_by_a_collected_value(monkeypatch):
    engine = _engine(monkeypatch, on=True)
    engine._gathered_context["extracted_variables"] = {"caller_number": "invented"}
    assert engine._format_prompt("{{caller_number}}") == "+41790000000"


def test_p10_unset_is_stock_behaviour(monkeypatch):
    engine = _engine(monkeypatch, on=False)
    engine._gathered_context["extracted_variables"] = {"user_name": "Anna Muster"}
    assert engine._format_prompt(KNOWN).endswith("- Name: (not given yet)")


@pytest.mark.asyncio
async def test_p10_a_background_extraction_renders_the_current_steps_instructions_again(
    monkeypatch,
):
    # the voice path: the next step started with « (not given yet) »; the value arrives
    engine = _engine(monkeypatch, on=True)
    engine._current_node = engine.workflow.nodes["agent"]
    await engine._perform_variable_extraction_if_needed(
        engine.workflow.nodes["start"], run_in_background=True
    )
    await engine._await_pending_extractions()
    [call] = engine.llm._update_settings.await_args_list
    assert "- Name: Anna Muster" in call.args[0].system_instruction


@pytest.mark.asyncio
async def test_p10_instructions_that_use_no_new_value_are_left_alone(monkeypatch):
    engine = _engine(monkeypatch, on=True, agent_prompt="Answer questions.")
    engine._current_node = engine.workflow.nodes["agent"]
    await engine._perform_variable_extraction_if_needed(
        engine.workflow.nodes["start"], run_in_background=True
    )
    await engine._await_pending_extractions()
    engine.llm._update_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_p10_unset_never_renders_again(monkeypatch):
    engine = _engine(monkeypatch, on=False)
    engine._current_node = engine.workflow.nodes["agent"]
    await engine._perform_variable_extraction_if_needed(
        engine.workflow.nodes["start"], run_in_background=True
    )
    await engine._await_pending_extractions()
    engine.llm._update_settings.assert_not_awaited()
    # the value is still recorded, as stock does
    assert engine._gathered_context["extracted_variables"] == {"user_name": "Anna Muster"}
