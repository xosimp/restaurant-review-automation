"""
reply_programs.py — the restaurant's memberships and loyalty programs, worked
into review replies (Danny at Simple EJ's, 10/9/26: "it tracks mentions and
inserts a nudge to the loyalty programs" — Charlie's Crew for families,
Founders Club at the bar).

A program is a name, what members get in public-safe words (the perk, never
a price), the words in a review that make it relevant (triggers), an
optional sign-up link, whether it is 21+, and an optional private follow-up
the owner can send when they have a way to reach the guest.

The rules are narrow, because a pitch in a public reply reads as marketing:

  * only a happy guest's reply (4-5 stars, not negative, not urgent, and not
    a star rating with no words) — a 3-star or a complaint never gets one;
  * never on Yelp, which flags promotional replies;
  * ONE program per reply — the one the review names most — in one natural
    sentence, by name and perk, never a price, and left out when it would
    read forced;
  * the link only on Google, written without "https://", and only the
    program's own;
  * a draft that names two programs, names one where none belongs, or puts
    a price beside one is written again, told why (drafter._attempt).

The private follow-up is never posted: the review card shows it, with Copy,
under a reply that names its program.

Stored as restaurants.reply_programs (JSON list). Anyone with the Reviews
module may edit it, as with reply_keywords; each change is in the change log
and an add, an edit or a remove saves on its own. Deterministic; no model
call here.
"""
import json
import re

from models import DB_PATH

MAX_PROGRAMS = 4
MAX_NAME = 40
MAX_PERK = 160
MAX_TRIGGERS = 40
MAX_TRIGGER_LEN = 30
MAX_FOLLOWUP = 600
MIN_STARS = 4

_BARE_LINK_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|us|io|biz|info|restaurant|bar|menu|"
                           r"club|app)(?:/[^\s,)]*)?", re.I)
_PRICE_RE = re.compile(r"\$\s?\d|\b\d+(?:\.\d{2})?\s?(?:dollars|bucks)\b|\d\s?/\s?mo\b", re.I)


def _one_line(s, limit):
    return " ".join(str(s or "").split())[:limit].strip()


def display_link(link) -> str:
    """The link as a reply writes it: no scheme, no trailing slash."""
    s = str(link or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^www\.", "", s, flags=re.I)
    return s.rstrip("/")


def clean_program(raw) -> dict:
    """One program, tidied, or ValueError with the owner's sentence."""
    raw = raw or {}
    name = _one_line(raw.get("name"), MAX_NAME).strip(" .\"'")
    if len(name) < 2:
        raise ValueError("Give the program a name.")
    perk = _one_line(raw.get("perk"), MAX_PERK)
    if len(perk) < 6:
        raise ValueError("Say what members get, in a few words (no price).")
    if _PRICE_RE.search(perk):
        raise ValueError("Leave the price out of what members get — a public reply never states one. "
                         "Put it in the private follow-up instead.")
    trig = raw.get("triggers")
    if isinstance(trig, str):
        trig = re.split(r"[\n,;]+", trig)
    triggers, seen = [], set()
    for t in trig or []:
        w = _one_line(t, MAX_TRIGGER_LEN).lower().strip(" .\"'")
        if len(w) < 2 or w in seen:
            continue
        seen.add(w)
        triggers.append(w)
        if len(triggers) >= MAX_TRIGGERS:
            break
    if not triggers:
        raise ValueError("Add at least one word a guest might use that makes this program relevant.")
    link = str(raw.get("link") or "").strip()
    if link:
        if not re.match(r"^(https?://)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", link, re.I):
            raise ValueError("The link should be a web address, like simpleejs.com/loyalty.")
        if not re.match(r"^https?://", link, re.I):
            link = "https://" + link
    followup = str(raw.get("followup") or "").strip()[:MAX_FOLLOWUP]
    return {"name": name, "perk": perk, "triggers": triggers, "link": link or None,
            "adults_only": bool(raw.get("adults_only")), "followup": followup or None}


def of(restaurant) -> list:
    raw = getattr(restaurant, "reply_programs", None) if restaurant is not None else None
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out = []
    for p in items or []:
        try:
            out.append(clean_program(p))
        except ValueError:
            continue
    return out[:MAX_PROGRAMS]


def _rx(word):
    return re.compile(r"(?<![\w])" + re.escape(word.lower()) + r"(?![\w])")


def hits(text, program) -> int:
    low = str(text or "").lower()
    return sum(len(_rx(t).findall(low)) for t in program.get("triggers") or [])


def allowed(rating, urgency=None, sentiment=None, platform=None, rating_only=False) -> bool:
    """Whether a reply may carry a program at all."""
    try:
        r = int(rating or 0)
    except (TypeError, ValueError):
        r = 0
    if rating_only or r < MIN_STARS:
        return False
    if str(urgency or "").lower() == "high" or str(sentiment or "").lower() == "negative":
        return False
    return str(platform or "google").lower() != "yelp"


def pick(programs, rating, text, urgency=None, sentiment=None, platform=None, rating_only=False):
    """The one program to offer this reply, or None: the one whose words the
    review uses most, list order breaking a tie."""
    if not programs or not allowed(rating, urgency, sentiment, platform, rating_only):
        return None
    best, best_n = None, 0
    for p in programs:
        n = hits(text, p)
        if n > best_n:
            best, best_n = p, n
    return best


def prompt_note(program, platform=None) -> str:
    if not program:
        return ""
    link = display_link(program.get("link")) if (program.get("link") and str(platform or "google").lower() == "google") else ""
    return (f"\nMEMBERSHIP: the guest mentioned something the restaurant's {program['name']} program is for. If — and "
            f"only if — it reads naturally after you have thanked them, add ONE short sentence inviting them to it, "
            f"by name, saying what members get in these words or close to them: \"{program['perk']}\"."
            + (" It is for guests 21 and over." if program.get("adults_only") else "")
            + (f" You may end that sentence with the link {link} written exactly like that." if link else
               " Do not include any link.")
            + " Never state a price, never mention any other program, never pitch it twice. If it would read "
              "forced, leave it out.")


def named_in(text, programs) -> list:
    low = str(text or "").lower()
    return [p for p in programs or [] if _rx(p["name"]).search(low)]


def check(draft, programs, chosen, rating, urgency=None, sentiment=None, platform=None, rating_only=False) -> str:
    """"" when the draft keeps to the rules, else the reason (phrased to
    follow "it ..." in the drafter's retry note)."""
    if not programs:
        return ""
    named = named_in(draft, programs)
    if not named:
        return ""
    if len(named) > 1:
        return "named more than one membership program — name one at most"
    if not allowed(rating, urgency, sentiment, platform, rating_only) or not chosen \
            or named[0]["name"].lower() != chosen["name"].lower():
        return f"pitched {named[0]['name']} where it doesn't belong — leave every membership program out"
    if _PRICE_RE.search(draft):
        return "put a price in a public reply — say what members get, never what it costs"
    own = display_link(chosen.get("link")).lower() if chosen.get("link") else ""
    for m in _BARE_LINK_RE.finditer(draft):
        link = m.group(0).rstrip("./").lower()
        if not own or str(platform or "google").lower() != "google" or link != own:
            return "added a link that isn't the program's own — leave links out"
    return ""


def offer_text(restaurant) -> str:
    """What the owner wrote about their programs — the reply check's
    offer source, so a perk the owner stated ("kids eat free on
    weeknights") is theirs to repeat, never an invented comp."""
    return " ".join(f"{p['name']}: {p['perk']}." for p in of(restaurant))


def program_in(text, programs) -> dict | None:
    hit = named_in(text, programs)
    return hit[0] if hit else None


def usage(restaurant_id, programs, days=90, db_path=DB_PATH) -> dict:
    """{name: replies posted in the last `days` days that name it}."""
    out = {p["name"]: 0 for p in programs or []}
    if not out:
        return out
    from models import get_conn
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT draft_response FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            "AND response_status='posted' AND draft_response IS NOT NULL "
            "AND COALESCE(posted_at, approved_at) >= datetime('now', ?)", (restaurant_id, f"-{int(days)} days")).fetchall()
    finally:
        conn.close()
    for (d,) in rows:
        for p in named_in(d, programs):
            out[p["name"]] += 1
    return out


def save(restaurant_id, programs, user=None, db_path=DB_PATH) -> list:
    """Store the list and log the change; returns what was stored."""
    from models import get_restaurant, update_restaurant
    before = of(get_restaurant(restaurant_id, db_path=db_path))
    update_restaurant(restaurant_id, {"reply_programs": json.dumps(programs) if programs else None}, db_path=db_path)
    try:
        import change_log
        kw = {"user": user} if user else {}
        change_log.record(restaurant_id, "reviews", "reply_programs", [p["name"] for p in before],
                          [p["name"] for p in programs], subject="Membership programs in replies", **kw)
    except Exception:
        pass
    return programs


def api(restaurant_id, method, data=None, user=None, db_path=DB_PATH) -> tuple:
    """The one body of GET/POST /api/reviews/programs and its mobile twin:
    ({"ok", "programs": [... + "used"], "max"}, status). A POST is one
    change — `add` (a program), `update` (a program, with `name` the one it
    replaces) or `remove` (a name) — so an edit never overwrites a list
    someone else just changed."""
    from models import get_restaurant
    progs = of(get_restaurant(restaurant_id, db_path=db_path))
    if method == "POST":
        data = data or {}
        try:
            if data.get("add") is not None:
                p = clean_program(data["add"])
                if any(x["name"].lower() == p["name"].lower() for x in progs):
                    return {"ok": False, "error": f"There's already a program called {p['name']}."}, 400
                if len(progs) >= MAX_PROGRAMS:
                    return {"ok": False, "error": f"Up to {MAX_PROGRAMS} programs."}, 400
                progs = save(restaurant_id, progs + [p], user=user, db_path=db_path)
            elif data.get("update") is not None:
                old = str(data.get("name") or "").strip().lower()
                p = clean_program(data["update"])
                idx = next((i for i, x in enumerate(progs) if x["name"].lower() == old), None)
                if idx is None:
                    return {"ok": False, "error": "That program isn't on the list any more."}, 404
                if any(i != idx and x["name"].lower() == p["name"].lower() for i, x in enumerate(progs)):
                    return {"ok": False, "error": f"There's already a program called {p['name']}."}, 400
                progs = save(restaurant_id, progs[:idx] + [p] + progs[idx + 1:], user=user, db_path=db_path)
            elif data.get("remove"):
                gone = str(data["remove"]).strip().lower()
                progs = save(restaurant_id, [x for x in progs if x["name"].lower() != gone], user=user,
                             db_path=db_path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
    used = usage(restaurant_id, progs, db_path=db_path)
    return {"ok": True, "programs": [dict(p, used=used.get(p["name"], 0)) for p in progs],
            "max": MAX_PROGRAMS}, 200
