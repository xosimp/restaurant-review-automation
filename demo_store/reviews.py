"""
demo_store.reviews — Halsted & Rye's Google reviews and the replies posted to them.

Written from a bank of what guests at a Lakeview sports bar actually say —
the smash burger, the beef, the wings on a Bears Sunday, a slow kitchen on
a packed game day, a bartender by name — so the Reviews module, its themes
and its diagnoses have something real-sounding to read. Fictional authors,
fictional staff, the store's own dishes. Seeded: the same review every time.
"""
import random
from datetime import date, datetime, timedelta

from demo_store import calendar as cal
from demo_store import world as w

FIRST = ["Megan", "Josh", "Ashley", "Kevin", "Lauren", "Brian", "Nicole", "Matt", "Rachel", "Tony", "Sam",
         "Jenna", "Mike", "Alyssa", "Carlos", "Priyanka", "Dave", "Erin", "Marcus", "Katie", "Andrew", "Gabby",
         "Steve", "Olivia", "Jon", "Hannah", "Pete", "Claire", "Will T", "Monica", "Ricky", "Danielle", "Ben",
         "Courtney", "Luis", "Tasha", "Greg", "Morgan", "Eddie", "Bridget", "Raj", "Colleen", "Frank", "Yesenia"]
LAST_INITIAL = "ABCDEFGHJKLMNOPRSTVWZ"

SERVERS = ["Kayla", "Tyler", "Sofia", "Nate", "Brianna", "Caleb", "Logan", "Aisha", "Mia", "Paige"]
BARTENDERS = ["Danielle", "Chris", "Jasmine", "Ryan", "Elena", "Derek", "Camille"]

POS_FOOD = [
    ("the smash burger", "food_quality"), ("the Italian beef (get it dipped)", "food_quality"),
    ("the wings", "food_quality"), ("the brisket tacos", "food_quality"), ("the Nashville hot chicken", "food_quality"),
    ("the loaded tots", "food_quality"), ("the fish and chips", "food_quality"), ("the short rib", "food_quality"),
    ("the chicken and waffles at brunch", "food_quality"), ("the pretzel with queso", "food_quality"),
    ("the brisket nachos", "food_quality"), ("the Chicago dog", "food_quality"),
]
GOOD_FOOD = [
    "{dish} is legit — easily top three in Lakeview.", "Had {dish} and it was way better than bar food has any right to be.",
    "{Dish} was perfectly cooked and came out hot.", "Do yourself a favor and order {dish}.",
    "{Dish} was huge and honestly worth every penny.", "Came for the game, stayed for {dish}.",
    "Can't stop thinking about {dish}.", "{Dish} is the move. Fries are crispy too.",
]
GOOD_SERVICE = [
    "{server} took great care of us even though it was slammed.", "Our server {server} was attentive and funny.",
    "{bartender} behind the bar knows the craft — great old fashioned.",
    "Shoutout to {bartender}, best bartender on Halsted.", "{server} checked on us without hovering.",
    "Service was fast and friendly start to finish.", "Staff remembered us from last week, which was a nice touch.",
]
GOOD_VIBE = [
    "Every TV had a game on and the sound was on for the Bears.", "Great spot before a Cubs game — three blocks from Wrigley.",
    "Patio is one of the best in the neighborhood.", "Perfect neighborhood bar: loud on game day, chill on a Tuesday.",
    "Happy hour is a steal ($5 Miller Lites).", "Old Style on draft. Enough said.", "Brunch with bottomless energy and a solid bloody.",
]
CLOSERS = ["We'll be back.", "Already planning our next visit.", "Highly recommend.", "Our new Sunday spot.",
           "10/10 would watch the Bears here again.", "Go Cubs!", ""]
BAD = [
    ("Waited almost 45 minutes for food during the Bears game. Kitchen clearly couldn't keep up.", "wait_time", "high",
     "45-minute wait for food on a Bears Sunday"),
    ("Burger came out well done when I asked for medium. Server was apologetic and took it off the bill.",
     "food_quality", "normal", "Burger overcooked; comped"),
    ("Way too packed on game day, couldn't get a bartender's attention for 20 minutes.", "service", "normal",
     "Slow bar service on game day"),
    ("Wings were soggy and the sauce was barely on them. Not what I remembered.", "food_quality", "normal",
     "Soggy wings, light on sauce"),
    ("Prices have crept up — $9 for a draft is a lot for a sports bar.", "value", "normal", "Drafts feel overpriced"),
    ("Bathrooms were a mess by 9pm on a Saturday.", "cleanliness", "high", "Restrooms not kept up on a Saturday"),
    ("Our order was wrong twice and the manager never came over.", "service", "high", "Order wrong twice, no manager visit"),
    ("Music was so loud we couldn't talk. Fine for the bar, not for dinner.", "ambiance", "normal",
     "Too loud for a dinner table"),
    ("Fries were cold and the beef was dry. Disappointing for the price.", "food_quality", "normal",
     "Cold fries, dry Italian beef"),
    ("Sat on the patio for 15 minutes before anyone came by.", "service", "normal", "Patio table waited 15 minutes"),
    ("Charged for a drink we never got and it took forever to fix the check.", "service", "normal",
     "Billing error took long to fix"),
    ("Hostess told us 20 minutes and it was an hour. Should have just gone to the bar.", "wait_time", "normal",
     "Quoted 20 minutes, waited an hour"),
]
MIXED = [
    "Food was good but the wait on a Sunday was rough.", "Great vibe, drinks a little pricey.",
    "Burger was great, fries were just okay.", "Solid spot, service was a bit slow when it got busy.",
    "Good wings, but they were out of the Daisy Cutter.", "Fun place, just too loud to hang out and talk.",
]
REPLY_GOOD = [
    "Thanks so much, {name}! We'll tell {who} you said hi. See you for the next game. — Will & the Halsted & Rye team",
    "{name}, this made our day. {Dish} is a kitchen favorite too. Come back soon! — Will & the Halsted & Rye team",
    "Appreciate you, {name}! Glad you had a great time — we'll save you a seat for Sunday. — Will & the Halsted & Rye team",
]
REPLY_BAD = [
    "{name}, I'm sorry — that isn't the experience we want for anyone. On big game days we're adding a second expo "
    "and another cook; I'd love to make it right. Please email us at hello@halstedandrye.example. — Will",
    "Thank you for telling us, {name}. I've shared this with our kitchen and floor managers, and we've already "
    "changed how we stage game-day orders. I hope you'll give us another shot. — Will",
]


def _rng(*k):
    return random.Random("|".join(str(x) for x in ("hr-review",) + k))


def _author(rng):
    return f"{rng.choice(FIRST)} {rng.choice(LAST_INITIAL)}."


def generate(start: date, end: date) -> list:
    """[{external_id, author, rating, text, review_date, sentiment, categories, summary, urgency,
    draft_response, response_status, posted_at}] — about a review a day, more after big nights."""
    out = []
    day = start
    n = 0
    while day <= end:
        rng = _rng("day", day.isoformat())
        d = cal.demand(day)
        lam = 0.9 * (1.4 if d["window"] else 1.0) * (1.3 if day.weekday() >= 5 else 1.0)
        k = sum(1 for _ in range(4) if rng.random() < lam / 4)
        for i in range(k):
            n += 1
            r = _rng("r", day.isoformat(), i)
            roll = r.random()
            gameday = bool(d["window"])
            p_bad = 0.055 + (0.035 if gameday else 0.0)
            dish, cat = r.choice(POS_FOOD)
            server, bartender = r.choice(SERVERS), r.choice(BARTENDERS)
            if roll < p_bad:
                text, bcat, urgency, summary = r.choice(BAD)
                rating = 1 if r.random() < 0.55 else 2
                cats, sentiment = [bcat], "negative"
            elif roll < p_bad + 0.07:
                text = r.choice(MIXED) + " " + r.choice(GOOD_FOOD).format(dish=dish, Dish=dish[0].upper() + dish[1:])
                rating, cats, sentiment, urgency = 3, [cat, "service"], "neutral", "normal"
                summary = "Good food, " + ("slow service" if "slow" in text or "wait" in text else "a few misses")
            else:
                parts = [r.choice(GOOD_FOOD).format(dish=dish, Dish=dish[0].upper() + dish[1:])]
                cats = [cat]
                if r.random() < 0.6:
                    parts.append(r.choice(GOOD_SERVICE).format(server=server, bartender=bartender))
                    cats.append("service")
                if r.random() < 0.5 or gameday:
                    parts.append(r.choice(GOOD_VIBE))
                    cats.append("ambiance")
                parts.append(r.choice(CLOSERS))
                text = " ".join(p for p in parts if p)
                rating = 5 if r.random() < 0.82 else 4
                sentiment, urgency = "positive", "normal"
                summary = "Loved " + dish.replace("the ", "", 1) + (" and the service" if "service" in cats else "")
            hour = r.choice([13, 15, 18, 20, 21, 22])
            when = datetime.combine(day + timedelta(days=r.choice([0, 0, 1])), datetime.min.time()) + \
                timedelta(hours=hour, minutes=r.randint(0, 59))
            name = _author(r)
            first = name.split()[0]
            age = (end - day).days
            if age > 6 or (age > 2 and r.random() < 0.6):
                reply = (r.choice(REPLY_BAD) if rating <= 2 else r.choice(REPLY_GOOD)).format(
                    name=first, who=server if "service" in cats else "the kitchen", Dish=dish[0].upper() + dish[1:])
                status, posted = "posted", (when + timedelta(hours=r.uniform(3, 30))).strftime("%Y-%m-%dT%H:%M:%S")
            elif rating >= 4:
                reply = r.choice(REPLY_GOOD).format(name=first, who=server, Dish=dish[0].upper() + dish[1:])
                status, posted = "drafted", None
            else:
                reply, status, posted = None, "pending", None
            out.append({"external_id": f"hr-g-{day.strftime('%Y%m%d')}-{i}", "author": name, "rating": rating,
                        "text": text, "review_date": when.strftime("%Y-%m-%dT%H:%M:%S"), "sentiment": sentiment,
                        "categories": sorted(set(cats)), "summary": summary, "urgency": urgency,
                        "draft_response": reply, "response_status": status, "posted_at": posted})
        day += timedelta(days=1)
    return out
