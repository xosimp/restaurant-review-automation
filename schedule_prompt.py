"""
schedule_prompt.py — how the week's schedule call is laid out, and the parts
of it that are the same for every call (schedule audit 10/3/26 PR-25, PR-26,
P-23, PR-33, PR-20, PR-5, PR-6, PR-9, PR-10, PR-21, PR-23, PR-24, PR-27).

The request is one user message in three content blocks, in this order:

  1. STANDING INSTRUCTIONS — identical for every restaurant and every call
     (one text per output contract): how rules are marked, how coverage is
     counted, the defaults used where a restaurant's own data says nothing,
     the notes and times rules, the output, one worked example, and the
     ranked PRIORITIES. A cache breakpoint may close it.
  2. THIS RESTAURANT'S WEEK — the owner's standing rules, the managers'
     fixed shifts, the rules the week is checked against, one ROSTER line
     per person, and the context. Identical for the date slices of one
     generation (one department's, when it is written by department) and
     their missing-day retries — not for another department's call (its
     roster, rules and schema enum are its own) nor the quality gate's
     rewrite (its manager plan is re-made for its dates). A second
     breakpoint may close it. Whether each breakpoint is set, and for how
     long, is cache_ttls: only where the calls sharing the block read it
     before it expires (schedule re-audit 10/4/26 PROMPT-8).
  3. THIS REQUEST — what this call writes: its dates, what each shift on
     them needs, what the rest of the week already gives each person, and
     anything said to this call alone.

The prompt used to open with the restaurant's name and its data window, put
the slice's own requirements a third of the way in, and repeat a fixed rule
set after ~40k characters of facts, so no two calls shared a prefix and each
of a generation's two to eight calls paid for the whole thing again. The
per-person facts sat in about nine blocks the model had to join by name
(availability, limits, scores, experience, closers, usual pattern, wants,
attendance, the seam) — one of them capped, so people past 4,000 characters
were "not listed" — and dates arrived in three formats.

Pure: no I/O, no model call. labor.generate_optimized_schedule assembles the
request with it; schedule_rules and schedule_engine hand it the facts.
"""
import re
from datetime import date, datetime

import schedule_requirements as _req
import shift_quality as _sq

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# One date format for the model (PR-20): the weekday and the ISO date,
# "Fri 2026-10-09" — the form the output schema's date enum takes. M/D/YY
# is what an OWNER reads; the model's own words reach the owner only
# through the summary, whose dates code writes as M/D/YY (labor).
CACHE_CONTROL = {"type": "ephemeral"}


def day_date(iso) -> str:
    """"Fri 2026-10-09" for an ISO date; the input as it is when unreadable."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{d.strftime('%a')} {d.isoformat()}"


def days_text(dates) -> str:
    """"Mon 2026-10-12 and Tue 2026-10-13" — dates in order, as the model reads them."""
    named = [day_date(d) for d in sorted({str(d)[:10] for d in (dates or []) if d})]
    if len(named) <= 1:
        return "".join(named)
    return ", ".join(named[:-1]) + " and " + named[-1]


# M/D/YY ("10/9/26"), a range of them ("9/14/26 – 10/4/26", "9/14/26–10/4/26"),
# and M/D/YYYY. A year is required: "24/7" and "1/2" are not dates.
_MDY = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])")
_WEEKDAY_BEFORE = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s*$")


def _mdy_to_date(m):
    try:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return date(y, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def iso_dates(text) -> str:
    """`text` with every M/D/YY date written as weekday and ISO date, except
    inside a fence: people's own words (between UNTRUSTED_GUEST_TEXT or
    OWNER_RULE markers) are theirs and stay as written. Code-built lines
    shared with owner-facing screens ("rain nights here ran 18% below a
    typical one (measured 4 times, last 9/12/26)") carry M/D/YY there and
    the one model format here (PR-20). A weekday already in front of the
    date is kept once: "Sat 9/12/26" becomes "Sat 2026-09-12"."""
    import ai_guard
    text = str(text or "")
    fences = re.compile("(" + re.escape(ai_guard.UNTRUSTED_OPEN) + ".*?" + re.escape(ai_guard.UNTRUSTED_CLOSE)
                        + "|" + re.escape(ai_guard.OWNER_RULE_OPEN) + ".*?" + re.escape(ai_guard.OWNER_RULE_CLOSE)
                        + ")", re.S)
    out = []
    for i, part in enumerate(fences.split(text)):
        if i % 2:
            out.append(part)
            continue
        pieces, last = [], 0
        for m in _MDY.finditer(part):
            d = _mdy_to_date(m)
            if d is None:
                continue
            before = part[last:m.start()]
            w = _WEEKDAY_BEFORE.search(before)
            if w:
                before = before[:w.start()]
            pieces.append(before + day_date(d.isoformat()))
            last = m.end()
        pieces.append(part[last:])
        out.append("".join(pieces))
    return "".join(out)


_ISO_FOR_OWNER = re.compile(r"\b(?:((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*),? )?(\d{4})-(\d{2})-(\d{2})\b")


def owner_dates(text) -> str:
    """The model's words as the owner reads them: every ISO date written
    M/D/YY, its weekday kept as written — "Fri 2026-10-09" becomes "Fri
    10/9/26" (DESIGN_SYSTEM.md → Dates and times). The model reads ISO
    (PR-20) and is told to name days by weekday; a date it writes anyway
    never reaches an owner as ISO."""
    from time_utils import mdy

    def _sub(m):
        try:
            d = date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            return m.group(0)
        return (m.group(1) + " " if m.group(1) else "") + mdy(d)
    return _ISO_FOR_OWNER.sub(_sub, str(text or ""))


def model_text(text, limit: int = 240) -> str:
    """Code-built words that carry a person's or an owner's phrase (an event
    label inside a requirement's reason): one line, capped, with any fence
    marker broken so it can never open or close a fence."""
    import ai_guard
    return ai_guard._neutralise_markers(" ".join(str(text or "").split())[:limit])


# ── the standing instructions (identical on every call) ─────────────────────

def clock(m) -> str:
    """"4:30pm" for minutes past midnight (a close past midnight wraps)."""
    h, mm = divmod(int(m) % (24 * 60), 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


_clock = clock


LAYOUT = (
    "You are writing next week's staff schedule for one restaurant, with a short summary of your biggest "
    "decisions.\n\n"
    "HOW THIS REQUEST IS LAID OUT\n"
    "1. These standing instructions — the same for every restaurant: how rules are marked, how coverage is counted, "
    "the defaults used where a restaurant's own data says nothing, notes and times, the output, one worked example, "
    "and PRIORITIES, the one ranked order for every conflict.\n"
    "2. THIS RESTAURANT'S WEEK — the owner's standing rules, the managers' fixed shifts (MANAGER COVERAGE), the rules "
    "the week is checked against, one ROSTER line per person, and the context: its record, the week's demand, the "
    "budget and what it has learned.\n"
    "3. THIS REQUEST — the dates to write now, what each shift on them needs (SHIFT REQUIREMENTS), and what the rest "
    "of the week already gives each person.\n"
    "Every date in this request reads as a weekday and an ISO date (\"Fri 2026-10-09\"), so you know the day; in "
    "your answer a date is the ISO date alone, without its weekday, as OUTPUT says. Work the week out before you "
    "answer: count, check and compare as much as you need.")

RULE_MARKS = (
    "HOW RULES ARE MARKED\n"
    "Every rule line says [HARD] or [SOFT], from the same lists the code checks the finished week against.\n"
    "- [HARD]: a breach means somebody can't legally or physically be there — they are not available, on time off, "
    "past a minor's or a rest limit, past the hours they may work — or the floor goes without what the owner "
    "requires every minute it trades: a manager, a staffing floor, a closer. The owner is shown every hard breach "
    "before the week goes to staff. Never break one to meet anything ranked below it.\n"
    "- [SOFT]: a cost or a preference the owner sees flagged, never a blocker. Keep it unless keeping it would "
    "break something ranked above it.")


def _coverage_text() -> str:
    late_start = _clock(_sq.LATE_WINDOW_START)
    late_after = _clock(_sq.LATE_CLOSE_AFTER)
    (lo_m, hi_m), (lo_n, hi_n) = _sq.CORE_WINDOWS["morning"], _sq.CORE_WINDOWS["night"]
    return (
        "HOW COVERAGE IS COUNTED\n"
        "- Coverage is counted by who is on the floor. " + _req.presence_rule() + "\n"
        "- A role's number on a shift counts everyone present for that daypart by that rule, not only the shifts "
        "that start in it: a shift that counts at dinner this way is already one of dinner's people — it counts "
        "toward the number, it does not add to it — and a shift that falls short of the dinner window does not "
        "count at dinner at all. Lunch and dinner are separate numbers, never one day's total to split.\n"
        "- The owner's staffing floors are held half hour by half hour through each daypart's core service window "
        f"(lunch {_clock(lo_m)}-{_clock(hi_m)}, dinner {_clock(lo_n)}-{_clock(hi_n)}, inside the day's open hours), "
        "not only at the peak; where SHIFT REQUIREMENTS give half-hour numbers, every half hour of the daypart is "
        "judged against them too.\n"
        "- \"by the half hour\" in SHIFT REQUIREMENTS is how many of a role a shift needs on at once as its sales "
        "climb and fall, read from the restaurant's own hourly sales: \"Server 2 from 11:00am, 4 from 12:00pm\" means "
        "two on from eleven and four from noon. Starts and ends follow it — the ramp in and the taper out — and the "
        "shift's number is its whole crew across the daypart.\n"
        f"- A \"late night\" line is the people on from {late_start} to close on a night that closes after "
        f"{late_after}; they count in the night's number too.\n"
        "- A shift that starts after midnight belongs to the night before: write it on that night's date (a "
        "12:30am-4:00am porter after Friday's close is dated Friday).\n"
        "- A date's close time is the latest any shift that date may end, except a role the rules say stays after "
        "close. Use the close of that exact date: a busier day changes the headcount, never the close.\n"
        "- For each date you write, count everyone who counts toward each daypart and check the count against that "
        "date's SHIFT REQUIREMENTS, and check every closer's end against that date's close.")


DEFAULTS = (
    "HOW TO WRITE SHIFTS — defaults. They apply only where this restaurant's own data (SHIFT REQUIREMENTS, its "
    "RESTAURANT HOURS & SHIFT RULES, its ROSTER and history) says nothing, and they rank last (PRIORITIES 5).\n"
    "- Starts follow the half-hour ramp: people of one role start together only when the ramp or a floor needs "
    "them on at the same time; otherwise they stagger in as the numbers climb.\n"
    "- The taper: where a shift has half-hour numbers, people leave as the numbers fall. Where it has none, keep "
    "one or two servers on through close for stragglers and closing side-work — never fewer than a floor for that "
    "time — and end the rest once that night's sales fall; a busier night takes more servers earlier, never more "
    "kept until close.\n"
    "- Shift length: most servers work a lunch/day shift or a dinner/night shift; one or two a day may work "
    "straight through, opening to close. This is about length, never headcount: each daypart keeps its full number.\n"
    "- Somebody the ROSTER shows missing shifts or arriving late: pair them with a dependable teammate of their "
    "role on the same shift, and never make them the only one of their role who opens or closes a day. Never add a "
    "person beyond SHIFT REQUIREMENTS for it — Cavnar AI recommends any standby to the owner itself.\n"
    "- Roles: schedule a person only in a role their CAN WORK lists. Each role is paid its own rate (the role rates "
    "in the context), so moving somebody to another role is a cost choice like any other.\n"
    "- Hours: SHIFT REQUIREMENTS give each shift its people and each date its hours target — the one hours figure "
    "to plan a date by; the weekly ceiling only ever removes hours. Every other hours figure in this request (sales "
    "per labor hour, past weeks, peers) is context, never a target.\n"
    "- With no RESTAURANT HOURS & SHIFT RULES, base start and end times on this restaurant's own history: prep "
    "starts before the doors open and closers stay until service ends.")


def _notes_rule(structured: bool, note_words: str) -> str:
    return ("- Notes are printed on that employee's own schedule and read by them. Leave a shift's note "
            + ("out" if structured else "empty") + " unless one applies, and then write only one of: "
            + note_words + ". Never put a rating or score, reliability or attendance, pay, performance, or "
            "anything about another person in a note.")


def _times_rule(structured: bool, enums: bool = True) -> str:
    if structured and enums:
        return ("- Times: `start` and `end` are clock times from the schema's list, 12-hour with am/pm (\"11:00am\", "
                "\"9:30pm\").")
    if structured:
        # The shape without enums (schema_enums=False, a roster too large to
        # compile): there is no list of times to pick from (PROMPT-7).
        return ("- Times: `start` and `end` are 12-hour US clock times with am/pm — \"11:00am\", \"4:00pm\", "
                "\"9:30pm\" — never 24-hour time.")
    return ("- Times: shift_start and shift_end are 12-hour US clock times with am/pm — \"11:00am\", \"4:00pm\", "
            "\"9:30pm\" — never 24-hour time.")


def _output_text(structured: bool, enums: bool = True) -> str:
    if structured:
        spec = ("OUTPUT — JSON only, matching the schema you were given. `days` has one entry per date of THIS REQUEST "
                "that you staff: its `date` — the ISO date alone, without its weekday — and its `shifts`, each shift's "
                "`employee` exactly as the ROSTER spells them, its `role`, and its `start` and `end` "
                + ("from the schema's clock times" if enums else "as 12-hour clock times with am/pm")
                + " (an end at or before the start runs past midnight) — the weekday and the hours are worked out "
                "from the date and the times, so write neither. `summary` is at most three bullets: the week's biggest decisions and why. Do not use it "
                "to report what the week misses — every requirement, target, floor and request the finished week does "
                "not meet is checked in code and shown to the owner. Write the JSON object and nothing else.")
    else:
        spec = ("OUTPUT — your entire response follows this structure, with nothing before the CSV:\n\n"
                "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                "2026-MM-DD,Day,Employee Name,Role,start,end,hours,note\n(one row for every shift)\n---SUMMARY---\n"
                "- bullet 1\n- bullet 2\n- bullet 3\n"
                "Start the response with \"date,day,employee\" and keep that format to the end: the date as the ISO "
                "date alone (2026-MM-DD, never with its weekday in front), a real weekday, a ROSTER name, then the role, "
                "times, hours and note, in that order, on every row.")
    return (spec + "\nEach summary bullet: one short clause, 10 words or fewer, plain language — the concrete change "
            "and its one-line reason, nothing more. A restaurant owner should be able to read all 3 in under 5 "
            "seconds. No full sentences, no restating these rules back, no generic scheduling advice, no emoji. Name "
            "a day by its weekday (\"Friday dinner\"), never by its date.")


# The worked example (PR-25): one made-up Friday that shows the manager
# handover, a ramp in, a server counted at lunch AND dinner, the one
# straight-through and the taper out. Its numbers are checked against the
# scorer's own counting by tests/test_schedfix_c1_prompt.py, so the example
# can never teach something the code would count differently.
EXAMPLE_OPEN, EXAMPLE_CLOSE = "11:00am", "10:00pm"
EXAMPLE_ROWS = (
    # (name, role, start, end, note, planned manager row)
    ("Dana", "Manager", "10:00am", "5:00pm", "", True),
    ("Eli", "Manager", "4:30pm", "10:30pm", "", True),
    ("Ana", "Server", "11:00am", "3:00pm", "opener", False),
    ("Ben", "Server", "11:30am", "7:00pm", "", False),
    ("Cy", "Server", "4:30pm", "10:00pm", "closer", False),
    ("Di", "Server", "6:00pm", "10:00pm", "", False),
    ("Ed", "Server", "6:00pm", "8:30pm", "", False),
    ("Fay", "Line Cook", "10:00am", "4:00pm", "opener", False),
    ("Gus", "Line Cook", "10:30am", "3:30pm", "", False),
    ("Hal", "Line Cook", "3:30pm", "10:00pm", "closer", False),
    ("Ivy", "Line Cook", "4:00pm", "9:30pm", "", False),
    ("Jo", "Line Cook", "5:00pm", "10:00pm", "", False),
)
# What the made-up Friday needed, as SHIFT REQUIREMENTS would say it.
EXAMPLE_NEEDS = {
    "morning": {"Server": 2, "Line Cook": 2},
    "night": {"Server": 4, "Line Cook": 3},
}
EXAMPLE_FLOORS = {"night": {"Server": 2}}
# (role, daypart): [(from, people)] — the half-hour numbers in the example.
EXAMPLE_RAMPS = {
    ("Server", "morning"): [("11:00am", 1), ("11:30am", 2)],
    ("Server", "night"): [("4:30pm", 2), ("6:00pm", 4), ("7:00pm", 3), ("8:30pm", 2)],
}


def _example_text() -> str:
    def ramp(role, part):
        return ", ".join(f"{n} from {t}" for t, n in EXAMPLE_RAMPS[(role, part)])
    rows = []
    for name, role, s, e, note, planned in EXAMPLE_ROWS:
        tail = "  [fixed MANAGER COVERAGE row, kept exactly]" if planned else (f"  note: {note}" if note else "")
        rows.append(f"  {name:<4} {role:<9} {s}-{e}{tail}")
    return (
        "<example>\n"
        f"A made-up Friday at a made-up restaurant — the method, never this restaurant's numbers. Open {EXAMPLE_OPEN}, "
        f"close {EXAMPLE_CLOSE}.\n"
        "What the day needed:\n"
        "  MANAGER COVERAGE (fixed): Dana 10:00am-5:00pm and Eli 4:30pm-10:30pm (Manager).\n"
        f"  lunch | Server 2, Line Cook 2 | by the half hour: Server {ramp('Server', 'morning')}\n"
        f"  dinner | Server 4 (floor 2), Line Cook 3 | by the half hour: Server {ramp('Server', 'night')}\n"
        "The shifts written:\n" + "\n".join(rows) + "\n"
        "Why they meet it:\n"
        "  - Managers: Dana is on from the first cook in at 10:00am, Eli until the last person out at 10:30pm, and "
        "they overlap 4:30pm-5:00pm — a manager every minute. Their rows were kept exactly and never written again.\n"
        "  - Lunch servers: Ana and Ben = 2, one from 11:00am and two from 11:30am, as the ramp asks.\n"
        "  - Dinner servers: Ben, Cy, Di and Ed = 4. Ben's 11:30am-7:00pm covers the whole lunch window and 90 "
        "minutes of dinner's 5:30pm-8:30pm, so he is one of lunch's 2 AND one of dinner's 4 — counted once at "
        "dinner, never as an extra person. He is the day's one straight-through server; everyone else has a clear "
        "lunch or dinner shift.\n"
        "  - The ramp and the taper: Ben and Cy make 2 from 4:30pm; Di and Ed start together at 6:00pm because the "
        "ramp needs both then; after Ben leaves at 7:00pm there are 3, and after Ed leaves at 8:30pm there are 2 — "
        "the floor of 2 holds until the 10:00pm close.\n"
        "  - Line cooks: Fay and Gus at lunch, prep from 10:00am before the doors open; Hal, Ivy and Jo at dinner; Hal "
        "closes.\n"
        "</example>")


PRIORITIES = (
    "PRIORITIES — the one ranked order for every conflict in this request. A higher item always wins over a lower "
    "one; a block that sounds absolute still sits at its rank here:\n"
    "  1. Hard constraints — never broken for anything below:\n"
    "     1a. A manager or owner on the floor every minute anyone is scheduled — the owner's highest staffing rule: "
    "somebody is always in charge of the floor. It gives way only to a manager's own availability and time off and "
    "the legal limits on their hours and rest. Where MANAGER COVERAGE plans the managers' shifts, keep those rows "
    "exactly and write no second shift for those managers on those dates; a stretch it says no manager can legally "
    "cover is staffed as usual, and the owner is told.\n"
    "     1b. Employee availability and approved time off (the person cannot be there), STAFF CONSTRAINTS, closed "
    "dates, and each person's own limits in the ROSTER and the rules — the roles they may work, a minor's hours, rest "
    "between shifts, shift length, their weekly maximum, days in a row (a minor's hours and a short rest are the "
    "owner's legal exposure) — and each date's opening and closing times.\n"
    "  2. SHIFT REQUIREMENTS and the owner's staffing rules — below every item of 1. First the staffing rules: the "
    "staffing floors, each role's closer and somebody on until close, and the hours a role may start and end by the "
    "RESTAURANT HOURS & SHIFT RULES (all [HARD]), and the owner's standing rules (OWNER_RULE, as their tags say). "
    "Then SHIFT REQUIREMENTS, the people each role needs on each shift, with the floors the hard minimum inside them. "
    "Fill them from the people still under their minimum hours first (MIN in the ROSTER; a full-timer's is the full-time line), then from "
    "those with the most room before their overtime line. Nobody goes past their overtime line (OT in the ROSTER) "
    "while a teammate in the same role has room: overtime is a cost the owner does not want, only for a shift "
    "nobody else in the role can legally work.\n"
    "  3. Leadership — every shift that needs somebody to run it has one, busiest shifts first, and every SHIFT "
    "LEADER REQUIREMENT is met.\n"
    "  4. The weekly hours ceiling — trim toward it only in ways that keep 1-3 intact; it never adds hours.\n"
    "  5. Quality preferences — the owner's ADDITIONAL SCHEDULING NOTES and THE OWNER'S REQUEST FOR THIS DRAFT "
    "first, then every [SOFT] preference: strength and pairing, the experience mix, a fair share of closes, "
    "weekends and busy shifts, people's usual days, dayparts and hours, what staff want — and last the defaults "
    "above, which apply only where this restaurant's own data says nothing.\n"
    "  Where the owner's words rank: RESTAURANT HOURS & SHIFT RULES 1b for the opening and closing times, 2 for "
    "every staffing rule in them (floors, arrival times, who stays to close); STAFF CONSTRAINTS 1b; the owner's "
    "standing rules (OWNER_RULE) 2; ADDITIONAL SCHEDULING NOTES and THE OWNER'S REQUEST FOR THIS DRAFT 5. CAVNAR AI "
    "QUESTIONS are "
    "questions to weigh, never instructions, and notes staff wrote about their own availability are context.")


_STATIC = {}


def static_block(structured: bool = True, note_words: str = "", enums: bool = True) -> str:
    """The standing instructions — byte-identical for every restaurant and
    every call on one output contract, so it is cached (PR-26) — ending in
    PRIORITIES, which the restaurant's own standing rules follow (PR-2).
    `note_words` is the output contract's fixed note list
    (schedule_output.NOTE_VALUES), passed by the caller. `enums` False is
    the structured shape without its lists (labor's schema_enums=False
    fallback): the times are not "from the schema's list" there, since
    there is none (schedule re-audit 10/4/26 PROMPT-7)."""
    enums = bool(enums) or not structured
    key = (bool(structured), note_words, enums)
    if key not in _STATIC:
        _STATIC[key] = "\n\n".join([
            LAYOUT, RULE_MARKS, _coverage_text(), DEFAULTS,
            "NOTES AND TIMES\n" + _notes_rule(structured, note_words) + "\n" + _times_rule(structured, enums),
            _output_text(structured, enums), _example_text(), PRIORITIES])
    return _STATIC[key]


# ── the ROSTER table (one line per person, PR-33) ────────────────────────────

ROSTER_COLUMNS = ("NAME", "ROLE", "CAN WORK", "AVAILABLE", "HOURS", "SCORE", "EXPERIENCE", "CLOSES", "USUAL",
                  "WANTS", "RELIABILITY")

ROSTER_HEAD = (
    "ROSTER — one line per person on this draft's roster, the only names to write ({n} people). Columns, \"-\" for "
    "nothing known:\n"
    "  " + " | ".join(ROSTER_COLUMNS) + "\n"
    "ROLE is their own job code; (manager) marks somebody who counts for the manager rule. CAN WORK is every role "
    "they may be scheduled in — their own, the roles they hold, roles they have worked here on {held}+ shifts and "
    "stations they hold — and nothing else; a role that needs a certificate they lack is left out. AVAILABLE is "
    "every day they can work unless it says otherwise: a weekday off, a daypart only, a time window, approved time "
    "off (all [HARD]), and a day they asked off that the owner has not decided ([SOFT]: avoid it if the day can be "
    "covered). HOURS: MIN-MAX for the week ([SOFT] minimum, [HARD] maximum), OT where overtime starts below that "
    "maximum, hours already published in a payroll week this week shares and the room it leaves; \"-\" is the "
    "restaurant's default — up to {ceiling}h, overtime past {ot}h. SCORE is the owner's 1-5 "
    "rating (\"-\" unrated: unknown, neither strong nor weak — never a reason to leave them off). EXPERIENCE: "
    "experienced ({exp}+ shifts here or marked by the owner) or developing (under {dev}): put a developing person on "
    "with an experienced hand of their role, never two developing together on a busy shift. CLOSES: the roles they "
    "are chosen to close for, or that they can run a shift. USUAL: the weekdays, dayparts, weekly hours and start "
    "they usually work — keeping a person on their pattern is scored as stability. WANTS: what they asked for or "
    "keep dropping and picking up — honour it where the rules and coverage allow. RELIABILITY: missed and late "
    "shifts on watched nights, recent ones counting most.")


def roster_table(people: list, held_shifts: int = 8, ceiling: float = 40.0, ot: float = 40.0) -> str:
    """The ROSTER block: one line per person, every column filled from the
    person's facts (labor._roster_people), nobody dropped by a cap (PR-33).
    `people` is [{name, role, manager, can_work, available, hours, score,
    experience, closes, usual, wants, reliability}] — each a short string
    or empty."""
    if not people:
        return ""
    lines = []
    for p in people:
        role = (p.get("role") or "-") + (" (manager)" if p.get("manager") else "")
        cells = [p.get("name") or "-", role] + [p.get(k) or "-" for k in
                                                ("can_work", "available", "hours", "score", "experience", "closes",
                                                 "usual", "wants", "reliability")]
        lines.append("  " + " | ".join(str(c).replace("|", "/") for c in cells))
    head = ROSTER_HEAD.format(n=len(people), held=held_shifts, exp=_sq.EXPERIENCE_SHIFTS, dev=_sq.DEVELOPING_SHIFTS,
                              ceiling=f"{float(ceiling):g}", ot=f"{float(ot):g}")
    return "\n\n" + head + "\n" + "\n".join(lines)


def roles_text(codes) -> str:
    """Job codes of one family read together: "Server AM/PM", "Bartender",
    "Line Cook/Prep Cook" — the codes as the restaurant spells them."""
    codes = sorted({str(c).strip() for c in codes or () if str(c or "").strip()}, key=str.lower)
    if len(codes) <= 1:
        return "".join(codes)
    first = codes[0].split()
    if len(first) > 1 and all(c.split()[:-1] == first[:-1] and len(c.split()) == len(first) for c in codes):
        return " ".join(first[:-1]) + " " + "/".join(c.split()[-1] for c in codes)
    return "/".join(codes)


# ── assembling the request ───────────────────────────────────────────────────

WEEK_HEAD = "THIS RESTAURANT'S WEEK"
REQUEST_HEAD = "THIS REQUEST"


def _marker(ttl):
    if ttl is None:
        return None
    return dict(CACHE_CONTROL, ttl="1h") if ttl == "1h" else dict(CACHE_CONTROL)


def request_content(static: str, week: str, request: str, ttls=None) -> list:
    """The user message's content: the three parts as text blocks, a cache
    breakpoint after the standing instructions and after the week (PR-26,
    P-23) — at most two of the four the API allows. The date slices of one
    generation and its missing-day retries share the first two blocks byte
    for byte; a department call shares only the first (its roster and rules
    are its own), and the quality gate's rewrite only the first (its manager
    plan is re-made for its dates). `ttls` — (standing, week), each "5m",
    "1h" or None for no breakpoint (cache_ttls) — defaults to a 5-minute
    breakpoint on both."""
    t0, t1 = ttls if ttls is not None else ("5m", "5m")
    out = []
    for text, ttl in ((static, t0), (week, t1)):
        block = {"type": "text", "text": text}
        if _marker(ttl):
            block["cache_control"] = _marker(ttl)
        out.append(block)
    out.append({"type": "text", "text": request})
    return out


# ── when a cache breakpoint pays (schedule re-audit 10/4/26 PROMPT-8) ──────
#
# A cache entry lives CACHE_TTL_SECONDS from the start of the request that
# wrote or last read it, and a schedule call that thinks runs for minutes:
# calls started one after another more than five minutes apart each WROTE
# the 5-minute entry again (1.25x input) and never read it — dearer than no
# breakpoint at all. A breakpoint is set only where the calls that share the
# block read it before it expires, at the TTL that costs least:
#     none:  n x 1        5m: 1.25 + (n-1) x read  (only if gap < 5m)
#     1h:    2 + (n-1) x read  (only if gap < 1h)
# n calls sharing the block, `gap` the seconds between their starts (a call's
# predicted length), `read` the model's cache-read rate (0.05x on Opus 5.5).
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0
CACHE_TTL_SECONDS = {"5m": 300, "1h": 3600}
# Starts closer to the edge than this are not counted on: a call that runs a
# little long would find the entry gone.
CACHE_TTL_MARGIN = 0.8


def _cheapest_ttl(n: int, gap: float, read: float):
    if n <= 1:
        return None
    best, cost = None, float(n)
    for ttl, write in (("5m", CACHE_WRITE_5M), ("1h", CACHE_WRITE_1H)):
        if gap < CACHE_TTL_SECONDS[ttl] * CACHE_TTL_MARGIN:
            c = write + (n - 1) * read
            if c < cost - 1e-9:
                best, cost = ttl, c
    return best


def cache_ttls(static_calls: int, week_calls: int, call_seconds: float, read_rate: float = 0.1) -> tuple:
    """(standing, week) breakpoint TTLs for a call: "5m", "1h" or None.
    `static_calls` — how many of the generation's planned calls share the
    standing instructions (every one); `week_calls` — how many share this
    call's week block (its own department's date slices); `call_seconds`
    — the planned length of one call. A one-hour entry must come before a
    five-minute one in the prompt, so a week block on "1h" puts the standing
    block on "1h" too."""
    t0 = _cheapest_ttl(int(static_calls or 0), float(call_seconds or 0), float(read_rate))
    t1 = _cheapest_ttl(int(week_calls or 0), float(call_seconds or 0), float(read_rate))
    if t1 == "1h" and t0 == "5m":
        t0 = "1h"
    return t0, t1


def prompt_text(content) -> str:
    """A message's content as one text — the blocks joined as the model reads
    them in order; a plain string as it is."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and "messages" in content:
        content = (content.get("messages") or [{}])[0].get("content")
    out = []
    for b in content or []:
        if isinstance(b, dict):
            out.append(str(b.get("text") or ""))
        else:
            out.append(str(getattr(b, "text", "") or ""))
    return "\n\n".join(out)


_REQUEST_DATE = re.compile(r"^- (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2})$", re.M)


def request_dates(content) -> list:
    """The ISO dates THIS REQUEST asks to be written, in order."""
    text = prompt_text(content)
    at = text.rfind("\n" + REQUEST_HEAD)
    return _REQUEST_DATE.findall(text[at:] if at >= 0 else text)
