"""P-49 (Swiss Voice Platform, ADR-074): typed variable bindings - a tool's parameter filled
from the call's variables by the runtime, not written by the model."""

from api.services.workflow.tool_bindings import (
    CONFIRM_DESCRIPTION,
    apply_bindings,
    bindings_for,
    bound_schema,
    confirm_argument,
    remember_proposal,
)

CONF = {
    "svp_tool_bindings": {
        "uuid-book": {
            "date": {"variable": "creneau", "type": "datetime", "part": "date", "label": "Créneau"},
            "time": {"variable": "creneau", "type": "datetime", "part": "time", "label": "Créneau"},
            "customer_name": {"variable": "nom", "type": "text", "label": "Nom"},
            "customer_phone": {"variable": "caller_number", "type": "phone", "label": "Numéro"},
            "seats": {"variable": "places", "type": "number", "label": "Places"},
        }
    }
}


def test_p49_off_is_stock(monkeypatch):
    monkeypatch.delenv("SVP_TOOL_BINDINGS", raising=False)
    assert bindings_for(CONF, "uuid-book") == {}
    args, missing = apply_bindings({}, {"date": "x"}, {"creneau": "2026-11-04T15:45"}, {})
    assert args == {"date": "x"} and missing == []


def test_p49_a_bound_parameter_is_not_in_the_models_schema(monkeypatch):
    """ADR-074 amendment of 2026-11-04: optional and told « leave it out », DeepSeek wrote
    every bound parameter anyway (the owner's two calls) - a bound parameter is gone."""
    monkeypatch.setenv("SVP_TOOL_BINDINGS", "on")
    bound = bindings_for(CONF, "uuid-book")
    properties = {
        "date": {"type": "string", "description": "d"},
        "reason": {"type": "string"},
        "confirm": {"type": "string", "description": "the token the proposal gave you"},
    }
    props, required = bound_schema(properties, ["date", "reason"], bound)
    assert required == ["reason"]
    assert "date" not in props and props["reason"] == {"type": "string"}
    assert props["confirm"] == {"type": "string", "description": CONFIRM_DESCRIPTION}


def test_p49_off_the_schema_is_untouched(monkeypatch):
    monkeypatch.delenv("SVP_TOOL_BINDINGS", raising=False)
    properties = {"date": {"type": "string"}, "confirm": {"type": "string", "description": "x"}}
    assert bound_schema(properties, ["date"], {"date": {"variable": "d"}}) == (properties, ["date"])


TOKEN = "p1.1.1793754006.2e7409295fd18ad4f1b0c169e843dd3a.zoJoa7yz1i2S-yyqzR6VbXtPLAvk12zB0aYceWR5oZ0"
PROPOSAL = {"status": "success", "data": {"status": "proposal", "confirm": TOKEN, "read_back": "…"}}


def test_p49_the_confirmation_token_is_the_runtimes(monkeypatch):
    """The model says « yes »; the runtime puts the proposal's token in (80 characters the
    model no longer writes)."""
    monkeypatch.setenv("SVP_TOOL_BINDINGS", "on")
    memory: dict[str, str] = {}
    # before any proposal, a word is an empty confirm: the gateway proposes
    assert confirm_argument(memory, "uuid-book", {"date": "x", "confirm": "yes"}) == {"date": "x", "confirm": ""}
    assert confirm_argument(memory, "uuid-book", {"date": "x", "confirm": ""}) == {"date": "x", "confirm": ""}
    shown = remember_proposal(memory, "uuid-book", {**PROPOSAL, "data": {**PROPOSAL["data"], "message": "…this `confirm` value."}})
    assert memory == {"uuid-book": TOKEN}
    # the model never sees the token: the proposal it reads says « yes »
    assert shown["data"]["confirm"] == "yes" and shown["data"]["read_back"] == "…"
    assert "'yes'" in shown["data"]["message"] and TOKEN not in str(shown)
    assert confirm_argument(memory, "uuid-book", {"confirm": "oui"}) == {"confirm": TOKEN}
    assert confirm_argument(memory, "uuid-book", {"confirm": TOKEN}) == {"confirm": TOKEN}  # copied
    assert confirm_argument(memory, "uuid-other", {"confirm": "yes"}) == {"confirm": ""}  # another tool
    assert confirm_argument(memory, "uuid-book", {"reason": "r"}) == {"reason": "r"}  # no confirm
    # the write's answer forgets the proposal
    done = {"status": "success", "data": {"status": "success", "booked": True}}
    assert remember_proposal(memory, "uuid-book", done) == done
    assert memory == {}


def test_p49_off_the_confirmation_goes_as_the_model_wrote_it(monkeypatch):
    monkeypatch.delenv("SVP_TOOL_BINDINGS", raising=False)
    memory: dict[str, str] = {}
    assert remember_proposal(memory, "uuid-book", PROPOSAL) == PROPOSAL
    assert memory == {}
    assert confirm_argument(memory, "uuid-book", {"confirm": "yes"}) == {"confirm": "yes"}


def test_p49_the_call_takes_the_variables_and_splits_a_datetime(monkeypatch):
    monkeypatch.setenv("SVP_TOOL_BINDINGS", "on")
    bound = bindings_for(CONF, "uuid-book")
    args, missing = apply_bindings(
        bound,
        {"date": "wrong", "customer_name": "typed by the model", "reason": "démo"},
        {"creneau": "2026-11-04T15:45", "nom": "Gaétan Rieben", "places": "2"},
        {"caller_number": "+41791234567"},
    )
    assert missing == []
    assert args == {
        "date": "2026-11-04",
        "time": "15:45",
        "customer_name": "Gaétan Rieben",  # the extracted value wins over the model's
        "customer_phone": "+41791234567",
        "seats": 2,
        "reason": "démo",
    }


def test_p49_a_missing_variable_stops_the_call_unless_the_model_gave_it(monkeypatch):
    monkeypatch.setenv("SVP_TOOL_BINDINGS", "on")
    bound = bindings_for(CONF, "uuid-book")
    _, missing = apply_bindings(bound, {}, {"creneau": "demain après-midi"}, {})
    assert missing == ["Créneau", "Créneau", "Nom", "Numéro", "Places"]  # words, not a date
    args, missing = apply_bindings(
        bound,
        {"date": "2026-11-05", "time": "10:00", "customer_name": "X", "customer_phone": "y", "seats": 1},
        {},
        {},
    )
    assert missing == [] and args["date"] == "2026-11-05"  # the model's, nothing extracted
