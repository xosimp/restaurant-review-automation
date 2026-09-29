"""Memory audit 9/29/26 (workstream M2, "assembler"): memory_context is the
one reader every prompt builder calls — relevance order, per-section
budgets, viewer scoping once, and each section's size logged.
"""
import logging
import sys
import types
from datetime import date, datetime, timedelta

import pytest

import ai_guard
import memory_context as mc


def _with(monkeypatch, providers, surfaces=None):
    monkeypatch.setattr(mc, "PROVIDERS", providers)
    monkeypatch.setattr(mc, "SURFACE_SECTIONS", surfaces or {})


def _module(monkeypatch, name, **fns):
    mod = types.ModuleType(name)
    for k, v in fns.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)


def _owner(uid=1):
    return {"id": uid, "role": "client", "is_admin": 0}


def _manager(uid=2):
    return {"id": uid, "role": "manager", "is_admin": 0}


def test_a_relevant_old_line_outranks_an_irrelevant_recent_one(monkeypatch):
    today = date(2026, 9, 29)
    lines = [{"text": "Recent note about the patio", "date": today, "subject": "marketing:patio", "trusted": True},
             {"text": "Old decline: never cut the Friday closer", "date": today - timedelta(days=200),
              "subject": "labor:day:friday", "trusted": True}]
    _module(monkeypatch, "mc_rel", lines=lambda req: list(lines))
    _with(monkeypatch, {"d": ("mc_rel:lines", 1)})
    block = mc.memory_context(5, "schedule", subjects=["labor:day:friday"], now=datetime(2026, 9, 29, 9))
    assert block.text.index("Friday closer") < block.text.index("patio")
    # ...and a budget with room for one keeps the relevant one.
    tight = mc.memory_context(5, "schedule", subjects=["labor"], now=datetime(2026, 9, 29, 9), budget_chars=60)
    assert "Friday closer" in tight.text and "patio" not in tight.text
    assert tight.dropped["d"] == 1


def test_each_section_keeps_a_share_so_a_big_one_cannot_starve_the_rest(monkeypatch):
    big = [{"text": "x" * 60 + str(i), "trusted": True, "weight": 1} for i in range(40)]
    small = [{"text": "The one goal that matters", "trusted": True}]
    _module(monkeypatch, "mc_share", big=lambda req: list(big), small=lambda req: list(small))
    _with(monkeypatch, {"big": ("mc_share:big", 1), "small": ("mc_share:small", 2)})
    block = mc.memory_context(5, "ask", budget_chars=600)
    assert "The one goal that matters" in block.text, "the lower-priority section kept its share"
    assert len(block.text) <= 600 + 10
    assert block.dropped["big"] > 0


def test_unused_budget_flows_back_to_a_section_that_had_to_drop(monkeypatch):
    lines = [{"text": "y" * 60 + str(i), "trusted": True} for i in range(12)]
    _module(monkeypatch, "mc_flow", many=lambda req: list(lines), none=lambda req: [{"text": "tiny", "trusted": True}])
    _with(monkeypatch, {"many": ("mc_flow:many", 1), "none": ("mc_flow:none", 2)})
    block = mc.memory_context(5, "ask", budget_chars=700)
    # 'none' used almost nothing of its share; 'many' got the rest back.
    assert len(block.sections["many"]) >= 8


def test_viewer_scoping_happens_once_inside_the_assembler(monkeypatch):
    lines = [{"text": "Team fact", "audience": "team"},
             {"text": "Private to the owner", "audience": "principals", "author_id": 1},
             {"text": "Dana's own reminder", "audience": "author", "author_id": 2},
             {"text": "Food cost is 31%", "module": "food", "trusted": True}]
    _module(monkeypatch, "mc_view", lines=lambda req: [dict(l) for l in lines])
    _with(monkeypatch, {"x": ("mc_view:lines", 1)})
    owner = mc.memory_context(5, "ask", viewer=_owner()).text
    manager = mc.memory_context(5, "ask", viewer=_manager()).text
    internal = mc.memory_context(5, "ask").text
    assert "Private to the owner" in owner and "Private to the owner" not in manager
    assert "Dana's own reminder" in manager and "Dana's own reminder" not in owner
    assert "Food cost is 31%" in owner and "Food cost is 31%" not in manager, "a manager has no Food Cost view"
    assert "Team fact" in manager and "Private to the owner" in internal
    block = mc.memory_context(5, "ask", viewer=_manager())
    assert block.dropped["x"] == 2


def test_a_viewer_restaurant_is_read_as_the_login_on_it(monkeypatch):
    lines = [{"text": "Private to the owner", "audience": "principals"}]
    _module(monkeypatch, "mc_vr", lines=lambda req: [dict(l) for l in lines])
    _with(monkeypatch, {"x": ("mc_vr:lines", 1)})
    view = types.SimpleNamespace(_ask_dsr_user=_manager())
    assert mc.memory_context(5, "ask", viewer=view).text == ""


def test_untrusted_lines_share_one_fence_and_carry_who_and_until_outside_the_words(monkeypatch):
    lines = [{"text": "We close Mondays in January", "who": "Erik, owner", "date": date(2026, 9, 1),
              "until": date(2026, 1, 31)},
             {"text": "Never cut the host", "who": "Erik, owner"}]
    _module(monkeypatch, "mc_fence", lines=lambda req: [dict(l) for l in lines])
    _with(monkeypatch, {"constraints": ("mc_fence:lines", 1)})
    text = mc.memory_context(5, "ask").text
    assert text.count(ai_guard.UNTRUSTED_OPEN) == 1
    assert "(9/1/26 · Erik, owner · until 1/31/26)" in text
    assert text.startswith("WHAT THE OWNER AND THE TEAM HAVE TOLD CAVNAR AI")
    assert "2026-" not in text


def test_each_sections_size_is_logged_and_kept_in_the_stats(monkeypatch, caplog):
    _module(monkeypatch, "mc_log", lines=lambda req: [{"text": "A logged line", "trusted": True}])
    _with(monkeypatch, {"logged": ("mc_log:lines", 1)})
    with caplog.at_level(logging.INFO, logger="memory_context"):
        block = mc.memory_context(5, "digest")
    assert block.sizes["logged"] > 0
    assert any("surface=digest" in r.getMessage() and "logged" in r.getMessage() for r in caplog.records)
    assert mc.size_stats()["digest"]["logged"]["calls"] >= 1


def test_the_assembler_never_raises_into_a_model_call(monkeypatch):
    _module(monkeypatch, "mc_bad", lines=lambda req: [{"text": "fine", "trusted": True}, object()])
    _with(monkeypatch, {"x": ("mc_bad:lines", 1)})
    monkeypatch.setattr(mc, "_relevance", lambda *a: 1 / 0)
    block = mc.memory_context(5, "ask")
    assert block.text == "" and "_assembler" in block.errors


def test_the_real_registry_serves_ask_without_duplicating_its_own_sections():
    assert mc.SURFACE_SECTIONS["ask"] and "decisions" not in mc.SURFACE_SECTIONS["ask"]
    assert mc.SURFACE_SECTIONS["ask_conversation"] == ("conversation",)
    assert set(mc.SECTION_SHARES) >= set(mc.PROVIDERS)
