"""sales_audit_cheatsheet.py — the owner-meeting cheat sheet.

Internal only. Never rendered into the customer report, the share link or
any export. Everything product-specific below was checked against the
codebase (models._MODULE_REGISTRY, pricing.html, scheduler.py, ask_cavnar
tools, privacy.html) — where something could not be verified it says so,
because the whole point is that Will is never confidently wrong.

`build(audit, results)` returns a list of sections customised to the audit's
concept (bar-forward concepts get the bar material emphasised) and to the
numbers already collected (the quick-math section uses real revenue once
it exists).
"""
from sales_audit_engine import BENCHMARKS, PRICING, concept_class


def _money(x):
    return "$" + "{:,.0f}".format(x)


def _first(name):
    return (name or "the owner").split(" ")[0]


def build(audit, results):
    a = audit.get("answers") or {}
    owner = _first(audit.get("owner_name") or a.get("owner_name"))
    rname = audit.get("restaurant_name") or a.get("restaurant_name") or "this restaurant"
    cls = concept_class(a)
    bar_forward = cls in ("sports_bar", "bar")
    fin = (results or {}).get("financials") or {}
    R = (fin.get("annual_revenue") or {}).get("value")
    sections = []

    # ── READ THIS 5 MINUTES BEFORE ─────────────────────────────────────────
    five = {
        "key": "five", "title": "Read this 5 minutes before the meeting", "tag": "start here", "pin": True,
        "blocks": [
            {"h": "5 numbers and concepts to have cold", "items": [
                "Labor % = labor cost ÷ sales. NRA 2025 full-service median 36.5% (profitable operators 34.2%). A sports bar with a heavy bar mix usually runs lower — " + owner + "'s own target beats any benchmark.",
                "Food cost % = food cost ÷ FOOD sales (not total). Full-service median 32%. Always ask: actual or theoretical, food-only or combined.",
                "Pour cost = beverage cost ÷ beverage sales. Blended 18–24%; spirits 18–22, draft ~20–24, bottled 24–28, wine 30–40. Mix changes everything.",
                "Prime cost = labor + all COGS. Under 60–65% of sales is the operator target. It is the one number that says whether the model works.",
                "Variance = what the count says you used minus what the POS says you sold. Under 3–5% is tight; over 10% is systemic.",
            ]},
            {"h": "5 questions to definitely ask", "items": [
                "\"What number catches you by surprise most often?\"",
                "\"Where does money leave the building without you having great visibility into it?\"",
                "\"When you count the bar, how much product is missing versus what the POS says you sold?\"",
                "\"On a big game night, how many parties do you think walk because of the wait?\"",
                "\"What do you usually find out too late?\"",
            ]},
            {"h": "5 likely gotchas", "items": [
                "\"How do you know my labor should be lower?\" → We don't assume it should be. We compare against your own sales patterns, your target and the benchmark, and only count the gap above target, at 30–70% recoverable.",
                "\"Are these savings guaranteed?\" → No. They are identified opportunity ranges from your numbers. Cavnar AI helps identify, monitor and act on them; how much is recovered depends on what is driving the variance.",
                "\"My POS already has reports.\" → It does, and we read from it. The difference is interpretation: one daily brief across labor, food, reviews and marketing, alerts before payroll or month-end, and the ability to ask questions in plain English.",
                "\"Does the bar module exist?\" → Not yet. Bar & Alcohol and Waitlist are upcoming. Today's live modules are Reviews, Labor, Food Cost and Marketing. Say that plainly.",
                "\"Why not ChatGPT?\" → ChatGPT doesn't have your reviews, your shifts or your invoices. Cavnar AI is connected to them and runs every day without being asked.",
            ]},
            {"h": "3 things NOT to promise", "items": [
                "A specific dollar saving. Say \"roughly $X–$Y of identified opportunity\" and \"how much is recoverable depends on the cause\".",
                "Features that aren't live: bar/pour-cost tracking, waitlist, automatic actions, anything that \"knows exactly why\" something happened.",
                "Security or contract specifics you haven't verified (SOC 2, encryption at rest, refund terms). Say \"let me confirm that and send it to you\".",
            ]},
            {"h": "Cavnar AI in 30 seconds", "items": [
                "\"Cavnar AI connects to the systems you already have — your Google reviews, your POS, your social accounts — and every morning it tells you what matters: labor against your target, food cost drift, which reviews need a reply and drafts them in your voice, and what to post this week. It emails you a weekly digest, pushes alerts when something's off, and you can ask it questions about your own numbers in plain English. Four modules today — Reviews, Labor, Food Cost, Marketing — from $349 a month per module or $1,199 for everything.\"",
            ]},
            {"h": "%s-specific areas to explore" % rname, "items": (
                ["Bar mix and pour cost: what share of sales is the bar, does he know true pour cost, how often is it counted, jiggers or free pour, comp policy.",
                 "Game-day economics: staffing for events, guests camping at tables through a game, turn times on Sundays, walkaways during peaks.",
                 "Labor by shift: which shifts run hot, whether managers cut mid-shift, overtime at payroll.",
                 "Food cost on a wings-and-shareables menu: portion consistency, vendor price creep on proteins and oil, theoretical vs actual.",
                 "Promotions: happy hour and game-day specials measured on profit, not sales.",
                 "Reviews: rating vs nearest competitor, who answers, recurring complaints about waits or service on busy nights."]
                if bar_forward else
                ["Labor by shift and overtime at payroll.", "Food cost: actual vs theoretical, inventory cadence, vendor price creep.",
                 "Reviews: rating, response habits, recurring complaints.", "Marketing: what spend actually produces, slow-day tactics.",
                 "Visibility: what he sees daily, what he learns at month-end."])},
        ],
    }
    sections.append(five)

    # ── Special cases ──────────────────────────────────────────────────────
    months_open = (fin.get("months_open") or {}).get("value")
    sections.append({"key": "special", "title": "Two situations that change the pitch", "tag": "well-run / new", "blocks": [
        {"h": "If Erik already runs it tight (numbers at or below target)", "items": [
            "Say it first, out loud: 'These numbers are good. I'm not going to invent a problem.' The report will show 'Performing well' on those lines and a high health score — that is the credibility you spend later.",
            "The audit will size little or nothing in cost savings and the ROI line may be under 1×. Do not fight it. Switch the case to: time (his hours on reports × 52), early warning (drift caught the day it starts, not at month-end), and consolidation (fewer dashboards).",
            "Ask: 'How do you keep it this tight — and what happens on the weeks you're not here?' Well-run operators usually own the numbers personally. That is the gap: the operation depends on him doing the reading.",
            "Ask: 'When did you last catch a supplier price creep the week it happened?' and 'Who answers reviews when you're off?' Even tight operators have one or two of these.",
            "Offer the smaller commitment: one Starter module on the line he cares about most, not the Full System. Over-selling a good operator costs the referral.",
        ]},
        {"h": "If the bar opened only a few months ago" + (" (Simple EJ's: about %d months)" % months_open if months_open else ""), "items": [
            "There is no annual revenue yet — enter monthly, and enter years in business as a fraction (3 months = 0.25). The tool then annualizes, labels it 'estimated', drops every confidence to low, and adds an opening-period note to every line and to the report.",
            "Opening-period numbers are not a run rate: labor runs high while the team trains, sales ramp for 6–12 months, waste and comps are high while recipes and staff settle. Say so before he does — 'nothing in your first quarter is representative, and I'm not going to pretend it is.'",
            "Do not quote savings as recoverable. Say 'baseline to watch'. The report is written that way automatically.",
            "The pitch becomes: set targets now, connect the systems now, and let the daily brief show the drift as things settle — the owners who do this in month three know their real numbers by month six instead of month twelve.",
            "Ask: 'What did you budget labor and food at when you built the model?' Those budget numbers become his targets in the tool and replace every benchmark.",
            "Ask: 'What has surprised you most since opening?' and 'Which number are you watching hardest right now?' — that is the module to lead with.",
            "Offer to re-run the audit at six months with real numbers. Put the date in.",
        ]}]})

    # ── PART 1 — numbers ───────────────────────────────────────────────────
    metrics = [
        ("Labor %", "Labor cost ÷ sales × 100", "Largest controllable cost. Every point is real money every week.",
         "Full-service: NRA 2025 median 36.5%, profitable operators 34.2%. Sports bar / bar-forward: often 27–32% because beverage carries little labor. Counter service: 25–30%.",
         "Ask whether it's fully loaded (taxes, benefits) or wages only, and whether it includes management salaries. Wage markets differ by city."),
        ("Food cost %", "Food cost ÷ food sales × 100", "Second biggest cost; drifts silently.",
         "Full-service median 32% (NRA 2025, food + non-alcohol bev). Wings/shareables 28–33; steak-heavy higher; pizza far lower.",
         "Divide by FOOD sales, not total sales. Actual (from counts) vs theoretical (from recipes) are different numbers."),
        ("Beverage cost %", "Beverage cost ÷ beverage sales × 100", "The bar's margin engine.",
         "Blended 18–24% for a full bar program.", "Depends entirely on mix. A wine-heavy list legitimately runs higher."),
        ("Pour cost", "Same as beverage cost, usually said per category", "How bartenders and vendors talk about it.",
         "Spirits 18–22%, draft ~20–24%, bottled/canned 24–28%, wine 30–40%.", "\"Pour cost\" and \"beverage cost\" are the same idea; don't treat them as two numbers."),
        ("Prime cost", "(Labor + food + beverage cost) ÷ sales × 100", "The one number that says whether the model works.",
         "Under 60–65% of sales.", "Above 65–70% there is usually no profit left after occupancy."),
        ("Gross margin", "(Sales − COGS) ÷ sales × 100", "What's left to pay labor and everything else.", "Full-service ~65–72%; bars higher.", "COGS excludes labor."),
        ("Net margin", "Net profit ÷ sales × 100", "What the owner keeps.", "Full-service 3–6% is typical; bar-forward concepts can run 10–15%.", "Owners often quote pre-tax or before their own salary. Ask."),
        ("Average check", "Sales ÷ guests (covers)", "Drives revenue per table and per turn.", "No universal benchmark — depends on concept.", "Per guest vs per table — say which."),
        ("Covers", "Number of guests served", "Volume. Covers × average check = sales.", "—", "A bar stool guest with three beers is a cover too; some POS count transactions instead."),
        ("Sales per labor hour", "Sales ÷ total labor hours", "Best way to compare shifts and days.", "Full-service commonly $50–90/hr; varies a lot.", "Only meaningful compared with the restaurant's own shifts."),
        ("Inventory variance", "(Actual usage − theoretical usage) ÷ theoretical usage × 100", "Measures what disappears: over-pours, spills, comps, theft, counting error.", "Bar: under 3–5% tight; over 5% investigate; over 10% systemic (vendor-published guidance — directional).", "Needs counts against POS sales; a monthly count hides a lot."),
        ("Theoretical vs actual food cost", "Theoretical = what recipes say it should cost; actual = what inventory says it did", "The gap is waste, portioning, theft and pricing errors.", "A gap of 1–2 points is normal; 3+ is money.", "Theoretical needs costed recipes; without them there is no theoretical."),
        ("Theoretical vs actual beverage cost", "Same idea behind the bar", "Over-pouring is invisible without it.", "A gap of 2–3 points is common in free-pour bars.", "Bartender comps that aren't rung up show up as variance, not comps."),
        ("Contribution margin", "Item price − item cost", "The dollars an item adds, regardless of its %.", "—", "A 40% cost item at $18 contributes more than a 20% cost item at $6. Sell the dollars, not the percentage."),
        ("Table turn time", "Minutes from seating to reset", "More turns = more covers on the same seats.", "Casual full-service 45–75 min; game-day sports bar can be 2–3 hours.", "Long stays on game days are often good — bar spend — as long as the table is spending."),
        ("Marketing ROI", "(Revenue attributed − spend) ÷ spend", "Whether marketing is buying demand or discounting it.", "—", "Attribution in restaurants is weak; ask what they can actually trace."),
        ("Customer acquisition cost", "Spend ÷ new guests acquired", "Only matters if new guests are trackable.", "—", "Most independents can't measure it. Don't pretend."),
        ("Review rating / sentiment trend", "Average star rating; sentiment = share of positive vs negative themes over time", "Independents' revenue moves with rating (HBS/Luca: +1 star ≈ +5–9% revenue).", "4.5+ strong; below 4.2 costs covers.", "A full star is a huge move; the audit only assumes 0.1–0.3."),
    ]
    sections.append({"key": "numbers", "title": "Part 1 — Numbers to know cold", "tag": "formulas",
                     "table": {"cols": ["Metric", "Formula", "Why the owner cares", "Healthy range (with caveats)", "Watch out"],
                               "rows": [list(m) for m in metrics]}})

    # ── PART 2 — quick math ────────────────────────────────────────────────
    base = R or 2000000
    label = ("%s's stated revenue" % owner) if R else "a $2M example (no revenue entered yet)"
    qm = [
        "1 percentage point of any cost line on %s = %s/yr. Half a point = %s. Two points = %s." % (label, _money(base * 0.01), _money(base * 0.005), _money(base * 0.02)),
        "Monthly ↔ annual: monthly × 12. Weekly × 52. A $10k/month problem is $120k/yr; a $500/week leak is $26k/yr.",
        "Labor: 2 points over target on %s = %s/yr gap; the audit counts 30–70%% of it (%s–%s)." % (_money(base), _money(base * 0.02), _money(base * 0.006), _money(base * 0.014)),
        "Food cost: works on FOOD sales. If the bar is 40%% of sales, food sales are %s and 1 food-cost point = %s, not %s." % (_money(base * 0.6), _money(base * 0.006), _money(base * 0.01)),
        "Pour cost: works on BAR sales. 40%% bar mix = %s bar sales; 1 pour-cost point = %s." % (_money(base * 0.4), _money(base * 0.004)),
        "Overtime: only the premium counts. $900/week of OT pay = $46,800/yr, of which the 0.5× premium is ~$15,600. That premium is the avoidable part.",
        "Waste: $500/week in the bin = $26k/yr. Realistically 25–50% avoidable = $6.5k–$13k.",
        "Marketing: $2,500/month untracked = $30k/yr. 10–25% redirectable = $3k–$7.5k. Small — say so.",
        "Software: add the monthly stack, × 12. Only tools the owner would actually drop count.",
        "Comps and voids: $3,000/month unmonitored = $36k/yr; 15–35% avoidable = $5k–$13k.",
    ]
    sections.append({"key": "math", "title": "Part 2 — Quick math", "tag": "mental math", "blocks": [
        {"h": "Shortcuts", "items": qm},
        {"h": "When the math is valid — and when it isn't", "items": [
            "Valid when the base is right: labor % on total sales, food % on food sales, pour cost on bar sales. Mixing bases is the most common way to be wrong.",
            "Valid when the number is the owner's actual, not a guess. A guessed labor % makes every downstream number a guess.",
            "Not valid as a promise. A gap to benchmark is an upper bound on opportunity; the audit brackets 30–70% of it for a reason.",
            "Not valid if the benchmark doesn't fit the concept. A late-night bar and a lunch café have different labor shapes.",
        ]}]})

    # ── PART 3 — sports bar economics ──────────────────────────────────────
    sections.append({"key": "sportsbar", "title": "Part 3 — Sports bar economics", "tag": "bar", "blocks": [
        {"h": "The mix", "items": [
            "Sports bars often run 35–55% of sales through the bar. Beverage gross margin (75–82%) beats food (65–70%), so the bar mix is the profit story.",
            "Liquor carries the best margin per pour; draft beer is next but wastes more (foam, line cleaning, keg tails); wine is the thinnest in % terms but fine in dollars.",
            "Premium spirits: higher cost % but much higher contribution dollars. A $14 premium old fashioned at 24% cost beats a $9 well at 18%.",
        ]},
        {"h": "Why controls matter", "items": [
            "A quarter-ounce over-pour on a 1.5 oz drink is 17% more product for the same money. Across hundreds of pours a week it is the largest single loss source in most bars.",
            "Bartender comps that aren't rung are invisible: they show up as variance, not as comps. Untracked comps are the second thing to ask about.",
            "Spills and breakage: a 1–2% allowance is normal. Draft variance from foam and line cleaning: a few % of keg volume is normal; 10%+ means a line or pressure problem.",
            "Theft: vendor studies attribute a third or more of bar shrinkage to internal theft. Never lead with this — say \"variance\" and let him name causes.",
            "Recipe consistency: cocktails without specs can't have a theoretical cost, so variance can't be measured. Ask about specs before asking about variance.",
        ]},
        {"h": "Game-day demand", "items": [
            "Event days compress the week: a Sunday can be two normal days of sales with three days of labor if the schedule is built by habit.",
            "Guests stay longer during a game — turns drop, but bar spend per table rises. Turn time alone is a bad KPI on game day; use sales per seat hour.",
            "Walkaways during pre-game rushes are real lost covers. Quoted wait accuracy and texting are the levers.",
            "Staffing around events: the failure mode is over-staffing the non-event days to be safe, not under-staffing the big ones.",
            "Happy hour and game-day promos need a profit read, not a sales read. Discounting a full house is a gift.",
        ]},
        {"h": "Where simple assumptions get you in trouble", "items": [
            "Comparing his pour cost with an 18% \"golden rule\" when his list is wine- or craft-beer-heavy.",
            "Treating a long table stay on a game night as inefficiency.",
            "Using total sales as the base for food cost.",
            "Quoting a labor benchmark for a full-service restaurant to a concept that does half its sales over the bar.",
            "Saying \"you're losing 20% of your inventory\" — that's a vendor statistic, not his number.",
        ]}]})

    # ── PART 4 — gotchas ───────────────────────────────────────────────────
    gotchas = [
        ("How are you calculating these savings?", "From your numbers, against your target where you gave one, otherwise a published benchmark. We only count the gap above target, and we show 30–70% of that gap as the range because none of it recovers on its own. Every line has a 'how this was calculated' I can open right now."),
        ("Are these savings guaranteed?", "No. They're identified opportunity, not a promise. Cavnar AI is designed to help you see and act on them daily; what you recover depends on what's actually causing the variance."),
        ("What's the benchmark based on?", "Labor and food cost: the National Restaurant Association's 2025 operations data. Pour cost and variance: bar-industry consensus, which I'd call directional. Your own target always replaces the benchmark."),
        ("My restaurant isn't like other restaurants.", "Agreed — that's why the audit is built on your numbers and your targets, and why it says 'insufficient data' rather than guessing where you didn't give me a figure."),
        ("How does AI know what's happening in my business?", "It reads the systems you connect: Google reviews, your POS shifts and sales, your inventory and invoices, your social accounts. It doesn't guess about your business; it reads it."),
        ("What if the AI is wrong?", "Then you see it and don't act on it. Nothing posts, publishes or changes a schedule without you approving it. It drafts; you decide."),
        ("Does Cavnar AI make decisions automatically?", "No. It drafts review replies you approve with one click, generates schedules you publish, writes posts you schedule, and sends alerts. It never acts on its own."),
        ("Will this tell me to cut staff?", "It tells you where labor is running against your own target by day, shift and role, and it builds schedules from your sales patterns. What you do with that is yours."),
        ("How does it know my food cost?", "From your inventory and invoices — uploaded or synced — plus your menu and recipes if you have them costed. Without those inputs it tells you it doesn't know, rather than inventing a number."),
        ("How does it know if bartenders are overpouring?", "Today it doesn't — bar variance tracking is an upcoming module. What I can size today is the gap from the counts you already do."),
        ("Actual vs theoretical food cost?", "Theoretical is what your recipes say it should have cost for what you sold. Actual is what your inventory says you used. The gap is waste, portioning, theft and pricing mistakes."),
        ("Beverage cost vs pour cost?", "Same thing — beverage cost ÷ beverage sales. 'Pour cost' is just how bar people say it, usually per category."),
        ("How accurate is the review analysis?", "It reads every review and groups the themes; you see the actual reviews behind each theme. It's a faster reader, not an oracle — you're one click from the source every time."),
        ("Couldn't I just get this from my POS?", "The raw numbers, yes — we read them from there. What the POS doesn't do is watch every day, compare to your target, flag the drift before payroll, and put reviews, labor, food and marketing in one morning brief."),
        ("Why can't I just use ChatGPT?", "ChatGPT doesn't have your data. Cavnar AI is connected to it, runs every day without being asked, and knows what a labor target is."),
        ("What does Perplexity do?", "It's a search engine with AI answers. We use it for one thing: checking how your restaurant shows up when people ask AI tools for recommendations — 'AI visibility'."),
        ("What does Claude do?", "Claude is the AI model Cavnar AI uses for the writing and reasoning: drafting replies in your voice, the weekly digest, and answering your questions about your own numbers."),
        ("What restaurant data do you store?", "Reviews and drafted replies, shift and sales data from your POS, inventory and invoice data you provide, marketing content and performance, account details. It stays for the life of the account plus 30 days."),
        ("Who can see my data?", "You and the people you give logins to, and me as the operator. It's never sold, shared with brokers, or aggregated into industry reports without written consent — that's in the privacy policy."),
        ("Is my data used to train AI?", "No. The privacy policy says it explicitly: your data is not used to train AI models for other customers."),
        ("How secure is this?", "Encrypted in transit, hashed passwords, two-factor login available, login alerts, and error monitoring with personal data stripped. If you want specifics beyond that — certifications, encryption at rest — let me confirm and send them rather than guess. [Do not claim SOC 2 or similar without verifying.]"),
        ("What if one of my integrations goes down?", "The dashboard shows the connection state and I get alerted. Reviews keep fetching on a schedule; POS data re-syncs nightly once it's back."),
        ("How frequently is data updated?", "Reviews four times a day. POS shifts and sales sync nightly. Social performance nightly. The weekly digest lands on the day you choose. Ask Cavnar reads live."),
        ("Does it work with multiple locations?", "Yes — each location has its own numbers and you can switch between them or see a consolidated view; every location is its own record, never averaged away."),
        ("What if I add another location?", "We add it to your account; pricing for additional locations isn't on the public page — let me confirm it. [Do not quote a per-location price without verifying.]"),
        ("How much work does this create for my managers?", "Approving drafted replies takes seconds. Schedules are generated, not typed. The rest is reading a morning brief instead of six reports."),
        ("How long does setup take?", "Connecting Google and your POS is minutes; the setup fee covers me doing the onboarding with you. Labor insights need a couple of weeks of shift data to be meaningful; reviews and the brief work from day one."),
        ("How quickly will I see value?", "Reviews and the daily brief immediately. Labor and food cost as soon as there's enough data to compare against your target — usually within the first few weeks."),
        ("How much does it cost?", "$349 a month per module with a $750 setup, billed annually at $3,490. All four modules: $3,000 setup and $11,990 a year — $1,199 a month equivalent."),
        ("Why is it worth that price?", "Because a single point of labor or food cost on your sales is worth more than the subscription, and the audit is showing more than a point in play. And the alternative is you doing the reading."),
        ("What happens if I cancel?", "It's an annual agreement. Your data is deleted within 30 days of cancellation. For refund or early-exit terms, I'll point you to the agreement rather than paraphrase it. [Verify the contract wording before answering in detail.]"),
        ("What results can you guarantee?", "None, honestly. I can guarantee you'll see your numbers every morning against your targets, and that nothing will happen in your business without your say-so."),
        ("What does Cavnar AI actually do that I can't already do myself?", "You could do all of it — if you read every review, checked labor against target every day, compared every invoice, and wrote every post. It does that reading every day so you spend your time on decisions."),
    ]
    sections.append({"key": "gotchas", "title": "Part 4 — Questions that could trip me up", "tag": "gotchas", "qa": gotchas})

    # ── PART 5 — questions to ask ──────────────────────────────────────────
    ask = [
        "What number catches you by surprise most often?",
        "Where does money leave the building without you having great visibility into it?",
        "What do you usually find out too late?",
        "If you disappeared for a week, what part of the restaurant would you worry about most?",
        "What do your managers know every day that you usually don't know until later?",
        "When did you last change something because of a pattern in reviews?",
        "How do you find out that a Friday ran heavy on labor — and when?",
        "When a vendor raises a price, how long until it shows up in your menu?",
        "What's the last report you stopped reading because it took too long to make?",
        "What's a decision you made on gut this month that you wish you'd had a number for?",
        "Which shift would you least like to run the numbers on?",
        "How much of your week is spent building or reading reports instead of running the place?",
    ] + (["When you count the bar, how much is missing versus what the POS says you sold?",
          "On a big game night, how many parties walk because of the wait?",
          "Do you know your pour cost by category — or one blended number?",
          "How do bartender comps get rung — and do you believe the number?",
          "Which of your promotions would you keep if you could only see the profit, not the sales?"]
         if bar_forward else
         ["Which menu items would you drop if you knew their real margin?",
          "How often does the schedule match what the shift actually needed?",
          "Which of your promotions would you keep if you could only see the profit, not the sales?"])
    sections.append({"key": "ask", "title": "Part 5 — Questions I should ask %s" % owner, "tag": "questions", "blocks": [{"h": "Powerful questions", "items": ask}]})

    # ── PART 6 — follow-ups ────────────────────────────────────────────────
    follow = [
        ("\"My labor is around 35%.\"", ["Fully loaded or wages only?", "Includes payroll taxes and benefits?", "Includes management salaries?", "Average across the week, or do you see it by day?", "Which shifts push it highest?", "What's your target?", "Has it been rising?"]),
        ("\"Food cost is 30%.\"", ["Actual or theoretical?", "Food only, or combined with beverage?", "On food sales or total sales?", "How often do you inventory?", "Does it include waste and comps?", "What's your historical target?"]),
        ("\"Pour cost is about 20%.\"", ["Blended, or by liquor / beer / wine?", "From counts or from purchases ÷ sales?", "How often is the bar counted?", "What variance do you see when you count?", "Do comps get rung as comps?"]),
        ("\"We do about $X a year.\"", ["Net of sales tax?", "Including catering / events / third-party delivery?", "Roughly what share is the bar?", "How does it split across the week — how big is Sunday?"]),
        ("\"Overtime isn't a big problem.\"", ["How would you know before payroll?", "Which positions get it when it happens?", "Is it scheduled OT or drift from early clock-ins and late clock-outs?"]),
        ("\"Our reviews are fine.\"", ["What's the Google rating and how many reviews?", "Who responds and how fast?", "What's the nearest competitor at?", "What are the three complaints that keep coming back?"]),
        ("\"Marketing is about $X a month.\"", ["Agency included?", "Do you know which channel brought anyone in?", "When do promos go out — slow days, or whenever?", "Do you measure profit after the discount?"]),
        ("\"We take inventory monthly.\"", ["Full count or key items?", "Who counts, and against what — purchases or POS sales?", "What did the last count show as variance?"]),
    ]
    sections.append({"key": "followups", "title": "Part 6 — Follow-up questions", "tag": "follow-ups", "qa": [(q, "  •  ".join(f)) for q, f in follow]})

    # ── PART 7 — terms not to confuse ──────────────────────────────────────
    terms = [
        ("Revenue vs profit", "Revenue is sales in the door; profit is what's left after every cost."),
        ("Gross profit vs net profit", "Gross = sales − COGS. Net = gross − labor − occupancy − everything else."),
        ("Food cost vs COGS", "Food cost is food only; COGS includes beverage (and sometimes paper/packaging)."),
        ("Prime cost vs operating expenses", "Prime = labor + COGS (controllable). Operating expenses = rent, utilities, insurance, marketing, software."),
        ("Beverage cost vs pour cost", "The same ratio. Pour cost is the bar's word, usually said per category."),
        ("Markup vs margin", "Markup is on cost (a $5 pour at 3× markup is $15). Margin is on price (that drink is 67% margin, 33% cost)."),
        ("Labor dollars vs labor %", "Dollars are payroll; % is payroll ÷ sales. A busy week can raise dollars and lower %."),
        ("Sales vs covers", "Sales are dollars; covers are guests. Average check bridges them."),
        ("Actual vs theoretical cost", "Actual is what you used (counts). Theoretical is what you should have used (recipes × sales)."),
        ("Comps vs voids vs discounts", "Comp: item served, not charged. Void: item removed before payment. Discount: charged less. All three leak differently."),
        ("Turnover (staff) vs turns (tables)", "Staff turnover is people leaving; table turns are seatings per table per shift."),
    ]
    sections.append({"key": "terms", "title": "Part 7 — Terms not to confuse", "tag": "terms", "qa": terms})

    # ── PART 8 — things not to say ─────────────────────────────────────────
    sections.append({"key": "notsay", "title": "Part 8 — Things not to say", "tag": "warning", "table": {
        "cols": ["Don't say", "Say instead"], "rows": [
            ["\"Your labor should definitely be 25%.\"", "\"Against your own target and the full-service benchmark, labor looks a couple of points high — worth seeing by shift before we conclude anything.\""],
            ["\"We can save you $50,000.\"", "\"Based on the numbers you've given me, we're identifying roughly $35,000–$50,000 of potential annual opportunity. How much is realistically recoverable depends on what's actually driving the variance.\""],
            ["\"AI will know exactly why this happened.\"", "\"It will show you where and when it happened and what changed — the why is usually a two-minute conversation with the manager on that shift.\""],
            ["\"This will eliminate waste.\"", "\"Tracking waste usually cuts it meaningfully — a quarter to half is a fair expectation once people know it's being counted.\""],
            ["\"Cavnar AI guarantees you'll improve margins.\"", "\"Cavnar AI is designed to make the drift visible early. Whether margins move depends on acting on it, and that part is yours.\""],
            ["\"You're losing 20% of your bar inventory.\"", "\"Vendor studies say bars lose a lot to variance; what matters is your number, which is why I asked what the last count showed.\""],
            ["\"The bar module tracks pour cost.\"", "\"Bar & Alcohol is an upcoming module. Today I can size the opportunity from your counts; the tracking comes when it ships.\""],
            ["\"It's SOC 2 / encrypted at rest / bank-grade.\"", "\"Encrypted in transit, hashed passwords, 2FA, login alerts. For anything beyond that let me send you the specifics.\""],
        ]}})

    # ── PART 9 — savings defense ───────────────────────────────────────────
    lb = BENCHMARKS["labor"]
    fb = BENCHMARKS["food"]
    defense = [
        ("Labor", "gap = labor % − target (points). Recover 30% / 50% / 70% of the gap, CAPPED at 1 / 2 / 3 points of revenue. $ = points × revenue. If overtime $ given: premium = OT ÷ 3, range 40% / 70% / 100% of premium. Final = the LARGER of the two, never the sum.",
         "Inputs: revenue, labor %, owner target (else band top: full-service 34, sports bar 32). Source: NRA 2025 Ops Data Abstract. Wrong when: owner's % is wages-only (gap understated), wage market is high, or the number is a guess. Overlap: overtime is inside labor dollars — max, not sum. Strength: solid when both inputs are the owner's own."),
        ("Food Cost", "gap = food % − target (points); recover 30/50/70% of it, CAPPED at 1 / 2 / 3 points of FOOD sales. Waste: $/week × 52 × 25/40/50%. Final = LARGER of gap and waste.",
         "Inputs: food %, food sales (or revenue − alcohol sales), target (else band top 32–33). Source: NRA 2025. Wrong when: % is on total sales, is theoretical, or combines beverage (then a blended band is used and the bar is not sized separately). Overlap: waste is inside food cost — max, not sum; comps/discounts are Operations only. Strength: moderate; high only with an actual figure from counts."),
        ("Bar & Alcohol", "gap = pour cost − target (points); recover 30/50/70% of it, CAPPED at 1 / 2 / 4 points of ALCOHOL sales. Variance: bev COGS × (variance − 3%) × 40/60/80%. Final = LARGER of the two.",
         "Inputs: alcohol sales or bar % of sales, pour cost, variance %. Source: bar-industry consensus (vendor-published; directional). Wrong when: wine-heavy mix, pour cost guessed, variance counted against purchases rather than POS. Overlap: variance is inside pour cost — max; bar comps are Operations only. Strength: moderate at best; say 'directional' if asked. Module is UPCOMING."),
        ("Reviews", "ADDED REVENUE, not savings: revenue × 0.25% / 0.5% / 1.0% when rating < 4.3; × 0.15 / 0.3 / 0.6% between 4.3 and 4.6 with a weak response process; × 0.1 / 0.2 / 0.4% at 4.6+ with a weak process; nothing at 4.6+ with a good one.",
         "Inputs: Google rating, revenue, response rate/time. Source: HBS (Luca) +1 star ≈ +5–9% revenue for independents; audit assumes only 0.1–0.3 star. Wrong when: rating is already high, or reviews aren't where his guests come from. Overlap: none (revenue, not cost). Strength: WEAK and speculative — it is labelled low confidence on purpose. Lead with the practice gap, not the dollars."),
        ("Marketing", "untracked ad spend × 10/15/25% + agency fee × (Yes: 50/75/100%, Maybe: 0/25/50%).",
         "Inputs: monthly spend, agency cost, ROI tracked?, would-replace-agency? Source: operator assumption — no authoritative benchmark exists. Wrong when: spend is actually working and just untracked. Overlap: agency counted here only; marketing software counted in Technology only if marked replaceable. Strength: WEAK — keep it small and honest."),
        ("Waitlist", "ADDED REVENUE, not savings: walkaway parties/week × party size (default 2.5) × avg check × 52 × 25/40/50%.",
         "Inputs: owner's walkaway estimate, avg check, party size. Source: operator assumption. Wrong when: the estimate is a guess (it always is) — and it is REVENUE, not profit (contribution ~60–70%). Overlap: none. Strength: WEAK; module is UPCOMING. Present as 'what a busy night might be leaving on the table'."),
        ("Operations", "comps + voids + discounts per month × 12 × 15/25/35%, only when not reviewed daily by person. Owner reporting hours shown as time, not dollars.",
         "Inputs: monthly comps/voids/discounts, monitoring cadence. Source: operator assumption. Wrong when: comps are deliberate hospitality. Overlap: waste (food) and overtime (labor) explicitly excluded. Strength: moderate — the POS has the real number, ask for it."),
        ("Technology", "sum of tools the owner marked replaceable × 12 × 50/75/100%. POS, payroll and accounting never count.",
         "Inputs: the software table. Source: his answers only. Wrong when: he marks something replaceable that he'd actually keep. Overlap: agency fees excluded (Marketing). Strength: strong but usually small."),
    ]
    sections.append({"key": "defense", "title": "Part 9 — Savings defense guide", "tag": "defend the number",
                     "blocks": [{"h": "How the headline is built", "items": [
                         "Cost savings (labor, food, bar, marketing, operations, technology) and added revenue (reviews, guest flow) are summed separately and shown separately — the report headline is the combined range with both lines underneath. If Erik pushes back on the combined number, retreat to cost savings alone; that is the defensible core.",
                         "Every gap-based line is capped at a few points of its base. In plain words: 'we never assume you'll move labor or food cost more than about three points, however far over you are.'",
                         "Overlaps are never summed: overtime sits inside labor, waste inside food, variance inside pour cost, comps only in operations, agency fees only in marketing.",
                         "Realistic scale for a $2.4M sports bar: a well-run one shows $0; an average one lands around 1–3% of sales; a badly run one can show 4–10% because it is genuinely leaking that much, and the report will also show a low health score to match.",
                     ]}],
                     "table": {
        "cols": ["Category", "Formula", "Inputs · benchmark · what breaks it · overlap · honest strength"], "rows": [list(d) for d in defense]}})

    # ── PART 10 — product ──────────────────────────────────────────────────
    sections.append({"key": "product", "title": "Part 10 — Cavnar AI product cheat sheet", "tag": "product", "blocks": [
        {"h": "What it does today", "items": [
            "Reviews (live): fetches Google reviews four times a day, drafts a personalised reply in the restaurant's voice for one-click approval and posting, urgent-review alerts, sentiment and topic trends, competitor rating monitoring, weekly review summary.",
            "Labor (live): daily labor % vs the restaurant's target, by day / shift / role; overstaffed, understaffed and overtime tables; AI-generated schedules from sales patterns and staff availability, published to staff by email; Toast shift sync nightly (CSV upload otherwise).",
            "Food Cost (live): ingredient cost history, supplier price-creep alerts, food cost % vs target, waste rate vs benchmark, AI-suggested order quantities and a supplier order email, menu margin reads, weekly food cost digest.",
            "Marketing (live): social / SMS / email content written in the restaurant's voice and scheduled from the dashboard, a guest text club with QR opt-in, Meta post performance synced nightly, content calendar.",
            "Platform: a daily Home brief across every active module, a weekly AI digest email, iOS app with push alerts, Ask Cavnar (plain-English questions answered from the restaurant's own data, with proposed actions that always need confirmation), competitor intelligence, AI-search visibility check.",
        ]},
        {"h": "What it does NOT do", "items": [
            "Not a POS, payroll or accounting system — it reads from them.",
            "No bar / pour-cost / variance tracking yet (Bar & Alcohol is upcoming). No waitlist (upcoming).",
            "Doesn't act on its own: no auto-posted replies, no auto-published schedules, no auto-sent campaigns.",
            "Doesn't invent numbers: with no inventory data it says so rather than estimating food cost.",
        ]},
        {"h": "How the AI is used", "items": [
            "Claude (Anthropic) writes and reasons: review replies, weekly digest, labor / food / marketing insights, Ask Cavnar. Ask Cavnar can read the restaurant's reviews, menu margins, schedule, shifts, food cost, competitors, marketing posts and text club; anything that changes something becomes a confirmation card.",
            "Perplexity is used only for the AI-visibility check — how the restaurant appears when people ask AI tools for recommendations.",
            "Every AI call is logged with cost and status in the admin console; failures are visible, not silent.",
        ]},
        {"h": "Data, notifications, emails, multi-location, setup", "items": [
            "Data needed: Google Business Profile connection (reviews), POS connection (Toast live; Square and Clover connectors exist — confirm state before claiming) or CSV uploads of shifts, inventory CSV / menu, social accounts for marketing.",
            "Notifications: iOS push, SMS and email alerts — urgent reviews, labor alerts, price changes, digests; quiet hours and a daily alert cap exist.",
            "Emails: weekly digest on the client's chosen day, monthly summary, urgent review alerts, staff schedule emails, supplier order emails, login and security emails.",
            "Multi-location: a brand can hold several locations; switch between them or view all locations together; each location is its own record.",
            "Setup: contract via DocuSign, payment via Stripe, credentials and connections done in onboarding; the setup fee covers that work.",
        ]},
        {"h": "Pricing (pricing.html)", "items": [
            "%s" % PRICING["starter"]["note"],
            "%s" % PRICING["full"]["note"],
            "Free AI audit (this meeting) with a written report. Two-module and extra-location pricing are not published — confirm before quoting.",
        ]},
        {"h": "15 / 30 / 60 seconds", "items": [
            "15s: \"Cavnar AI plugs into your reviews, your POS and your marketing, and every morning tells you what needs attention — labor, food cost, reviews, marketing — in one brief, with the replies and schedules already drafted.\"",
            "30s: see the top section.",
            "60s: add — \"It's four modules you can take separately or together. Reviews drafts every Google reply in your voice for one-click approval and tracks what people keep complaining about. Labor compares every day against your target by shift and role, flags overtime before payroll, and builds schedules from your sales patterns. Food Cost watches supplier prices and your food cost against target and tells you what to order. Marketing writes and schedules posts, texts and emails and runs a guest text club. Around all of it: a weekly digest, push alerts, and Ask Cavnar, where you ask a question about your own numbers and get an answer, not a report to read. Nothing happens without your approval.\"",
        ]}]})

    # ── PART 11 — competitive positioning ──────────────────────────────────
    sections.append({"key": "positioning", "title": "Part 11 — Competitive positioning", "tag": "positioning", "qa": [
        ("\"My POS already does this.\"", "\"It has the data — we read it from there. What it doesn't do is watch it every day against your target, compare it with your reviews and your marketing, and tell you what changed. We're the reader, not another data source.\""),
        ("\"Toast already gives me reports.\"", "\"Good ones. When did you last open the labor one? Cavnar turns the report into a three-line brief every morning and an alert when it drifts — so it gets read.\""),
        ("\"I already have an accountant.\"", "\"Keep them. They tell you what happened last month; this tells you what's happening this week, while you can still do something about it.\""),
        ("\"My managers tell me this stuff.\"", "\"Then you'll be checking their read against the numbers, which makes them better managers. And it covers the days they're off.\""),
        ("\"I already use ChatGPT.\"", "\"For writing, it's great. It doesn't have your reviews or your shifts, and it doesn't wake up on its own. Cavnar is connected and runs daily.\""),
        ("\"I have five different restaurant tools doing this.\"", "\"That's part of the audit — which of the five you'd keep. Cavnar's job is to pull what they produce into one place and interpret it, and to replace the ones you only kept for one feature.\""),
    ]})

    # ── PART 12 — objections ───────────────────────────────────────────────
    sections.append({"key": "objections", "title": "Part 12 — Objection handling", "tag": "objections", "qa": [
        ("Too expensive", "\"Compared with what — the subscription, or the point of labor we just sized? Which line in the audit do you think is least real? Let's stress-test that one.\""),
        ("I need to think about it", "\"Of course. What's the part you want to think through — whether the numbers are right, or whether you'd act on them?\""),
        ("I need to ask my partner", "\"Makes sense. Would it help if I sent the report so you're both looking at the same numbers? What will they push back on first?\""),
        ("I'm already paying for too much software", "\"Agreed — so let's use the audit's software table. Which of those would you drop if one place did it?\""),
        ("I don't want another dashboard", "\"Neither do I. The point is the morning brief and the alerts on your phone. The dashboard is there when you want to dig.\""),
        ("I don't trust AI", "\"Reasonable. Nothing here acts without you — it drafts, you approve. Where would you want to see it be right for a month before you'd trust it?\""),
        ("I'm too busy to set this up", "\"That's what the setup fee is for — I do it with you. What you need to give me is about an hour.\""),
        ("My managers handle this", "\"Then this gives them the numbers every morning instead of you asking. Which manager would you want to see labor by shift first?\""),
        ("My margins are already good", "\"Then the audit should show that, and it does in [category]. The question is whether you want to see it every day or find out at month-end.\""),
        ("I don't want to give you my financial data", "\"Understood. We can size the audit on what you're comfortable with — the report marks anything estimated. For the product, the data stays in your account, isn't sold or used to train models, and is deleted within 30 days if you leave.\""),
        ("Let me try it later", "\"Sure. What would be different later? If it's a season thing, let's book the follow-up for the week before it starts.\""),
        ("Call me in a few months", "\"Happy to. Can I send you the report now so you have the numbers, and put a date in for the call?\""),
    ]})

    # ── PART 13 / 14 — red flags and positive signals ──────────────────────
    sections.append({"key": "flags", "title": "Part 13 — Red flags during the audit", "tag": "red flags", "qa": [
        ("Doesn't know labor %", "Labor is being managed by feel. The opportunity is visibility first; don't assume it's high."),
        ("Inventories monthly or less", "Food and pour cost are unmeasurable between counts; variance hides for weeks."),
        ("Can't explain theoretical food cost", "Recipes probably aren't costed, so drift can't be diagnosed — only noticed."),
        ("No process for overtime", "OT shows up at payroll; the premium is the avoidable part."),
        ("Doesn't know beverage cost", "For a bar-forward concept this is the biggest blind spot in the building."),
        ("Doesn't track review themes", "Recurring complaints are operational data nobody is reading."),
        ("Multiple disconnected tools", "The owner is the integration layer; that's the hours question."),
        ("Relies on spreadsheets", "Someone is rebuilding the same report weekly; it's late by construction."),
        ("Reports arrive days or weeks late", "Decisions are being made on last month's numbers."),
        ("Free pour, untracked comps", "Variance has nowhere to show up except pour cost."),
        ("", "None of these means a badly run restaurant. Many excellent operators run on instinct — the pitch is that instinct plus daily numbers beats instinct alone."),
    ]})
    sections.append({"key": "positive", "title": "Part 14 — Positive signals (don't insult a good operator)", "tag": "positive", "qa": [
        ("Knows actual AND theoretical cost", "Sophisticated. Talk about the size of the gap and how fast they see it, not whether they measure."),
        ("Daily flash reporting", "They already have visibility; sell speed, breadth and the writing (replies, schedules, posts)."),
        ("Labor forecast from sales", "Compliment it. Ask how it handles game days and call-offs."),
        ("Clear variance targets", "Ask what happens when they miss — that's the alerting conversation."),
        ("Weekly inventory discipline", "Rare. Say so. The audit will show food/bar as strong and that's credible."),
        ("Recipes costed and current", "Menu margin conversation, not food-cost-drift conversation."),
        ("Strong manager accountability", "Position Cavnar as the managers' tool, not the owner's watchtower."),
        ("Reviews answered same day, personally", "Reviews module is a time saving, not a reputation rescue. Say that."),
    ]})
    return sections
