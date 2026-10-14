"""Schedule fix round 10/11/26, workstream W (wave 3): the iOS controls S's
LOGIC-19 / LOGIC-24 left web-only — the Team person sheet's "Doubles"
choice and the per-restaurant role weights (how hard each role is to run
without) on Roster & rules — and the rules payload they read.

The Swift is compile-checked by the iOS build (ios/CavnarAI, never run here);
these hold the wire: the server answers what the phone reads, the phone
sends what the server reads, in the web's words."""
import pathlib
import types

import models
import strategy_routes
from tests.test_sf10_j2_floor import db  # noqa: F401 — db is the fixture (every bound get_conn on the test file)

ROOT = pathlib.Path(__file__).resolve().parent.parent
LABOR = ROOT / "ios" / "CavnarAI" / "CavnarAI" / "Features" / "Labor"


def test_the_rules_payload_carries_the_role_weights_their_defaults_and_range(db, monkeypatch):
    db_path = db
    rid = models.create_restaurant(models.Restaurant(name="Weights", owner_email="w@example.com"), db_path)
    for name, role in (("Ana", "Server"), ("Dee", "Dishwasher"), ("Hal", "Host")):
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    models.update_restaurant(rid, {"role_criticality_json": '{"Host": 2.0}'}, db_path=db_path)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    got = strategy_routes._criticality_payload(rid, models.get_restaurant(rid, db_path))
    assert got["role_criticality"] == {"Host": 2.0}
    assert got["criticality_bounds"] == [0.3, 3.0]
    assert got["criticality_defaults"]["Dishwasher"] == 1.4 and got["criticality_defaults"]["Host"] == 0.8
    assert got["criticality_defaults"]["Server"] == 1.1
    # The GET carries it (the iOS rules read decodes these three keys).
    src = (ROOT / "strategy_routes.py").read_text()
    body = src[src.index("def _do_compliance_get"):src.index("def _do_compliance_get") + 6000]
    assert "**_criticality_payload(_rid(u), r)" in body


def test_the_phone_sets_doubles_in_the_webs_words_and_sends_what_the_route_reads():
    model = (LABOR / "ScheduleSetupViewModel.swift").read_text()
    facts = (LABOR / "PersonScheduleFacts.swift").read_text()
    sheet = (LABOR / "RosterSection.swift").read_text()
    # Read (true / false / null = from their history) and sent as true,
    # false or "auto" — what staff_settings.upsert takes.
    assert "doubles = c.setupBool(.doubles)" in model
    assert 'case .automatic?: try c.encode("auto", forKey: .doubles)' in model
    assert "if let v = patch.doubles { settings.doubles = v.stored }" in model
    # The web Team sheet's three chips, the same words.
    web = (ROOT / "templates" / "dashboard.html").read_text()
    for words in ("From their history", "Works doubles", "Never"):
        assert words in web and f'label: "{words}"' in facts
    assert "struct DoublesSection" in facts and "A minor never works a double." in facts
    assert "DoublesSection(stored: member?.settings?.doubles" in sheet


def test_the_phone_edits_each_roles_weight_inside_the_servers_range():
    quick = (LABOR / "ScheduleRulesQuickEdits.swift").read_text()
    store = (LABOR / "TeamSetupStore.swift").read_text()
    sheet = (LABOR / "RosterSection.swift").read_text()
    for key in ('"role_criticality"', '"criticality_defaults"', '"criticality_bounds"'):
        assert key in store
    # One role per save, {role_criticality: {role: weight | null}}: the
    # route merges role by role and a null goes back to the default.
    assert 'case roleCriticality = "role_criticality"' in quick and "struct RoleWeightBody" in quick
    assert "try inner.encode(weight, forKey: Key(stringValue: role))" in quick
    assert "criticalityBounds.lowerBound" in quick and "criticalityBounds.upperBound" in quick
    # Numbers in Space Grotesk (DESIGN_SYSTEM: every figure).
    weights = quick[quick.index("struct RulesRoleWeightsSection"):quick.index("// MARK: - Kitchen stations")]
    assert ".font(.cavnarNumber(" in weights
    assert "RulesRoleWeightsSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules)" in sheet


def test_the_route_takes_the_phones_weight_and_clears_it_with_null(db, monkeypatch):
    db_path = db
    rid = models.create_restaurant(models.Restaurant(name="Weights", owner_email="w@example.com"), db_path)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    for body, want in (({"role_criticality": {"Host": 1.7}}, {"Host": 1.7}),
                       ({"role_criticality": {"Host": None}}, None)):
        monkeypatch.setattr(strategy_routes, "_body", lambda b=body: b)
        out, status = strategy_routes._do_compliance_set(types.SimpleNamespace(id=1, role="client"))
        assert status == 200, out
        assert (out.get("role_criticality") or None) == want
