"""The Monday weekly plan runs Ask with no login (user=None), which reads
memory as the owner's view — but its items become issues every console login
reads, so owner-only memory must not reach it (docs wave 9/29/26, the
weekly_plan surface is in memory_context.SHARED_SURFACES)."""
import dataclasses

import ask_cavnar
import memory_context


def test_the_plan_reads_ask_memory_as_the_team(monkeypatch):
    seen = []
    monkeypatch.setattr(memory_context, "memory_context",
                        lambda rid, surface, viewer=None, **kw: seen.append(viewer) or
                        type("B", (), {"empty": True, "text": ""})())

    @dataclasses.dataclass
    class R:
        id: int = 7

    r = R()
    r._ask_memory_viewer = "team"
    # Updated (memory re-audit PROMPTS-14): the plan's snapshot carries no
    # memory block of its own — strategy_jobs.plan_memory (surface
    # "weekly_plan", read as the team) is the plan's one block, instead of
    # the same constraints, goals and claims paid for twice.
    assert ask_cavnar._memory_context(7, viewer=r) == "" and seen == []
    # An ordinary owner call is unchanged: the account holders' view. Since
    # 10/7/26 it goes through restaurant_context's memory section, which
    # hands memory_context PRINCIPALS for "no login" — what memory_context
    # itself reads None as.
    import restaurant_context
    restaurant_context.invalidate(7)
    ask_cavnar._memory_context(7, viewer=R())
    assert seen[-1] is None or memory_context.is_principals(seen[-1])


def test_ask_with_tools_stamps_the_plan_on_a_copy():
    src = open(ask_cavnar.__file__, encoding="utf-8").read()
    body = src.split("def ask_with_tools")[1]
    stamp = body.index('restaurant._ask_memory_viewer = "team"')
    assert 'if action == "weekly_plan":' in body[:stamp] and "_dc.replace(restaurant)" in body[:stamp]
    # outside the `if user is not None` block: the plan has no login
    guard = body[:stamp].rindex('if action == "weekly_plan":')
    assert body[guard - 5:guard] == "\n    "
    key = src.split("def build_context")[1].split("cached = _CONTEXT_CACHE.get(key)")[0]
    assert '"_ask_memory_viewer"' in key
