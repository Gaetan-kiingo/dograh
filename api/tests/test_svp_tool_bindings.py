"""P-49 (Swiss Voice Platform, ADR-074): typed variable bindings - a tool's parameter filled
from the call's variables by the runtime, not written by the model."""

from api.services.workflow.tool_bindings import apply_bindings, bindings_for, bound_schema

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


def test_p49_the_models_schema_keeps_a_bound_parameter_optional_and_told(monkeypatch):
    monkeypatch.setenv("SVP_TOOL_BINDINGS", "on")
    bound = bindings_for(CONF, "uuid-book")
    properties = {"date": {"type": "string", "description": "d"}, "reason": {"type": "string"}}
    props, required = bound_schema(properties, ["date", "reason"], bound)
    assert required == ["reason"]
    assert props["date"]["description"] == "Filled by the platform (Créneau); leave it out."
    assert props["reason"] == {"type": "string"}


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
