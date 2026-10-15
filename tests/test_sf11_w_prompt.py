"""Schedule fix round 10/11/26, workstream W (wave 3): AI-15 finished — each
rule appears once in the schedule prompt (blind re-audit 10/11/26).

Availability and the roles a person may work were stated in the ROSTER
head ([HARD]/[SOFT] and "and nothing else"), in the rules block (with why)
and — the roles — once more in the standing defaults. The rule now lives in
the restaurant's rules block alone (schedule_rules.prompt_block: the
sweep's own tag and the why); the ROSTER head says what its columns hold;
PRIORITIES only ranks them. No model call."""
import schedule_prompt as sp
import schedule_rules as sr
from test_schedfix_c1_prompt import NOTE_WORDS, WEEK

# Measured 10/14/26 (40 people, sr.DEFAULTS), before P's wave-3 PRIORITIES
# wording merged in: static 11,781 + ROSTER head 1,771 chars before; 11,721
# + 1,673 after. The ROSTER head is held to its own figure; the static block
# to the line this round took out of it (other workstreams change it too).
HEAD_BEFORE = 1771
OLD_ROLES_LINE = "- Roles: code keeps each person to the roles their CAN WORK lists; each role is paid its own rate.\n"
NEW_PAY_LINE = "- Pay: each role is paid its own rate.\n"


def _blocks():
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(sr.DAYS),
                       roster_names=[f"P{i}" for i in range(40)], active={f"p{i}" for i in range(40)})
    c.compliance = dict(sr.DEFAULTS)
    c.managers = {"p0": "Manager"}
    return sp.static_block(True, NOTE_WORDS), sr.prompt_block(c), sp.ROSTER_HEAD


def test_availability_is_stated_once_as_a_rule():
    static, rules, head = _blocks()
    # The hard rule and the soft one, each once, in the rules block with
    # their tags and why.
    assert rules.count("Availability: nobody works outside their AVAILABLE column") == 1
    assert "[HARD] Availability:" in rules and rules.count("asked off") >= 1
    assert "avoid it if the day can be covered" in rules
    # The ROSTER head says what AVAILABLE holds, never the rule or its tag.
    for said in ("[HARD]", "[SOFT]", "avoid it", "unless it says otherwise"):
        assert said not in head, said
    assert "AVAILABLE lists what limits their days" in head
    assert "Availability" not in static


def test_the_roles_a_person_may_work_are_stated_once_as_a_rule():
    static, rules, head = _blocks()
    assert rules.count("Roles: only a role in the person's CAN WORK") == 1
    assert "nothing else" not in head and "CAN WORK lists the roles they may be scheduled in" in head
    assert "roles their CAN WORK lists" not in static and "CAN WORK" not in static


def test_the_standing_text_is_smaller_for_it():
    static, _rules, head = _blocks()
    assert len(head) < HEAD_BEFORE
    assert NEW_PAY_LINE in static and OLD_ROLES_LINE not in static
    assert len(static) < len(static.replace(NEW_PAY_LINE, OLD_ROLES_LINE))
