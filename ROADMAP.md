# Roadmap — Cavnar AI

What's open, what's next and what's deliberately on hold. Update it when an
item ships or a new one is committed to. Keep it short enough to read in one
pass: it is not a changelog (git is).

Last refreshed 10/2/26.

## Active client

**Simple EJ's (Erik Baylis)** is the first paying client: production
restaurant 5; restaurant 4 is Will's demo copy.
- **Live.**
  - RPOWER (since 9/28/26), with the nightly Daily Sales Report on a
    4-4-5 fiscal calendar from 12/31/25.
  - Salaried staff (Erik, Jim Heflin, Anthony Abbott, Andrew Marola, Gabe
    Huerta) in the owner's labor %.
  - Task sheets on the staff app.
- **Waiting on Erik.**
  - Confirm the period start dates against Back Office.
  - Send one Back Office export of each kind (items, a count, a recipe, a
    week of invoices).
  - Send the manager schedule with him and Jim on it.
  - Confirm Danielle Benedict.

## Open / in progress

- **Google Business reviews API access.**
  - Applied 9/30/26 (case 0-4962000041049); the quota is 0 until approved.
  - Then: enable the Business Profile API (v4), Erik reconnects, and watch
    the `[GMB]` logs.
- **RPOWER schedule push.** The preview builds and checks the exact body
  without sending. A live push needs Will's go-ahead and RPOWER's answers on
  the open questions.
- **App Store.** Privacy manifest and in-app account-deletion request done
  (the staff app's own Delete my account too, 10/2/26). Still open (Will):
  the App Store Connect listing copy, the reviewer demo login and a
  TestFlight upload (`docs/app-store-submission.md`); once the app is
  listed, set `IOS_APP_STORE_URL` — until then the staff web page and the
  `/s/` schedule pages name the TestFlight invite instead of a store button.
- **Google OAuth verification.** A Cloudflare bot challenge in front of
  `cavnar.ai` blocked Google's crawler. It needs a Cloudflare rule change,
  not code (last checked 9/25/26).
- **Messaging setup.**
  - The staff Twilio A2P campaign is not registered yet; owner alerts and
    sign-in codes are approved. Will: register it with the consent wording
    in `people.STAFF_SMS_CONSENT_TEXT` ("Text me about my schedule and my
    requests: a posted or changed week, swaps, open shifts and time off…" —
    schedule and request notices), then set
    `TWILIO_STAFF_MESSAGING_SERVICE_SID`. Until then `sms_available` is
    false, the staff app hides its texts switch and staff are reached by the
    app and email only.
  - `CAVNAR_POSTAL_ADDRESS` (a PO box for email footers) is unset.
- **iPhone parity** for the web work of 9/28–9/30/26:
  - the Reports tab;
  - per-person pay rates;
  - dining sections;
  - the post-sign-in passkey offer, which needs the
    `webcredentials:dashboard.cavnar.ai` entitlement (the site already
    publishes it).
- **iOS gaps from the 9/3/26 audits** (re-checked 9/30/26):
  - 3.2: `AskCavnarView` computes `userTextWidth` on every render.
  - 4.2: foreground refresh exists only on Home and Labor.
  - 6.4 / 6.5: Reviews, Intel and Marketing have no read cache for offline
    opens, and Labor shows no staleness notice.
  - 7.4: four charts carry no accessibility labels (`LaborRibbonChart`,
    `WeekRadarChart`, `WasteLedgerChart`, `RecoverableGaugeChart`).
  - 7.7: `Color.cavnarInk3(_ contrast:)` is defined and never used.
- **Designs written, not built** (`docs/plans/`):
  - Postgres and more web workers;
  - a materialised restaurant health row;
  - the review fetch's AI work on its own queue;
  - a nonce-based CSP.
- **Repository cleanup.** The Sep 21 tiers 4–6 landed: `config.py`, one
  model registry, `demo_seed.py`, and DDL at boot. Still open:
  - the remaining module-level `from models import ... get_conn` imports
    (34 modules; move each to a call-time `get_conn` as it is touched);
  - the hand-duplicated web/mobile route pairs.
- **Staff app, left open after the 10/2/26 fix round** (each with its reason
  in the round's reports):
  - a separate `staff_account` push type, so owners can mute sign-ins
    without muting PIN locks, sprays and claims (today all ride
    `staff_signin`) — needs a product call;
  - a manager's "took it" on Home does not close that person's live offer
    (it expires when the shift passes) — accepting there would run the cover;
  - no retention entry for `shift_change_requests` (it predates the round;
    the learners read it);
  - B3's minimal change to `schedule_engine.replacement_is_legal` (a gap the
    shift already had is not charged to the cover) awaits the schedule
    owner's review;
  - `shifts_for_employee` and `/staff/api/colleagues` could read the narrow
    `models.get_published_week_csv(with_stations=True)` (PERF-12);
  - not built: ranking cover candidates by a running-late ETA (AI-09);
  - known limits: the staff brief's figure check binds numbers as a set, not
    item by item; spelled-out numbers ("nine reviews") are not caught; a
    house rule that names a rostered teammate makes any answer citing it say
    "Ask your manager"; a Studio edit that changes two people in one slot
    leaves its floor section alone;
  - `task_sheets.store_photo` has no route callers (candidate for future
    cleanup after additional verification);
  - if production already holds two active staff logins under one name, the
    `uq_memberships_active_employee_name` index is not created until an
    owner resolves them (`ops.capture` names them, job
    `memberships_name_index`).
- **Apollo.io upgrade.** No longer held for Gia Mia, who is not becoming a
  client. Decide on its own merits.

## Recently shipped (newest first; trim entries older than ~2 months)

- **10/2 — The employee app, fix round** (from the staff app audit; server
  B1–B8 and S9, iOS I1–I4).
  - Sign-in and identity: one active login per name, a common-PIN denylist,
    attempts counted before the PIN check, owner notices on locks, sprays
    and claims, Change PIN on its own counter, server sign-out, forgot PIN by
    text, delete my account, US-only verification texts with an hourly
    ceiling, a location switch that asks that location's PIN.
  - Notifications: staff phones on their own push tier (an owner alert can
    never reach one), one delivered-only rule (app, consented text, email),
    quiet hours with texts held to 8am, shift and critical-task reminders
    (`staff_reminders`), consent that covers schedule and request notices.
  - Shifts: the restaurant's own date, every leg of a double, manager-posted
    open shifts and one-person offers answered in the app (the coverage ask
    is one), start-time gates, expiry and escalation (`open_shift_watch`),
    voiding, the role checked both ways, who's on with me, approved time off
    called off, reasons and notes both ways; availability by hours and dates
    with one versioned save; floor sections per shift.
  - Tasks: a same-day cover takes the sheet, one round trip per tick,
    photos authorised before they are stored and capped, the critical
    out-of-range alert, last night's note.
  - Running late, announcements with Got it, a thread with the manager on
    duty and the Team inbox under Labor.
  - The employee's own hours and tips, week stats, guest mentions, the
    post-shift pulse, a calendar feed; hashed schedule links that follow the
    live week and die on deactivation.
  - A personal pre-shift brief, a manager-approved rewrite and focus item,
    translation, house rules and docs with cited answers, certifications
    with reminders (`cert_reminders`).
  - The iPhone app: Today, Tasks, Requests and Me tabs, the inbox, offline
    ticks, a Next shift widget.

- **10/1 — Event Intelligence phase 4 (no new APIs).**
  - The Blackhawks, Bulls and Fire seasons and the White Sox postseason, from
    each league's own published schedule, as season files. Since the second
    re-audit: the Fire's cup matches (U.S. Open Cup, Leagues Cup), the Bulls'
    local TV (CHSN), the White Sox 2026 regular season and a Cubs 2026 file.
    Still by hand in the admin's Event calendar: the Sox ALDS games 4 and 5
    (if necessary) need a result or a cancellation once the series ends.
  - A frequent series (40-odd home games) stays context until this restaurant
    measures it to matter: its nights stay in the usual-night baselines, and
    it earns no alert, games-ahead item or game-night line until then. A
    date's games come preseason last, rarest first; each sport names its own
    start (puck drop, tip-off, first pitch).
  - Reviews: service and wait complaints posted around game nights against
    other days, a lean by posted date, as review-diagnosis evidence and in Ask.
  - What games did at other restaurants that follow the team, behind the
    privacy floor (eight restaurants from five owners, the viewer's out,
    rounded to 5%), said as theirs and never planned on — materialised
    nightly, so a request never reads other restaurants' nights. Blind-audited
    the same day: praise no longer counts as strain, quiet games confound
    nothing but another game, an unplayed playoff game is never measured (the
    admin's Event calendar asks for its result), note rules are owner-only and
    read nothing that could mean something else.
  - Re-audited the same day: one clean-nights rule and one same-kind rule for
    every game read (a game at another ground is its own), kickoffs and send
    times on the restaurant's clock, the measured game leads when two share a
    date, one rule for who sees the item mix, and a removed game comes back
    only through Put back (web and iOS), with who removed it.
  - Re-audited a second time the same day: a game's season class (preseason,
    playoff, cup) is part of its label, so no reader pools one class with
    another; one figure per game in the brief and the report, the forecast's
    own once the label's effect applies; removing a past game re-measures its
    night; the follow switch and Put back save at once and re-sync on the
    bounded admin pool; event changes reach Account activity; one rule for
    who reads the guest text and its send time; the big-game heads-up shows
    only to the Labor readers it was pushed to; the follow switch on iOS.
  - Not built (each needs an events or sports API): concerts and festivals,
    road closures, the final whistle, watch-party searches.
- **10/1 — Event Intelligence phase 3.**
  - What games like the next one sold, item by item; prep and a game-week
    ordering bump (through the recipes) once two such games are measured.
  - The guest text's send time from kickoff (a starting rule, said as one) on
    the brief and in Campaign Studio; the Studio goal names what game nights
    sell.
  - The season's measured money on the follow card and in Ask, kept apart
    from value delivered; one push the afternoon before a game measured big.
- **10/1 — Event Intelligence phase 2.**
  - The morning brief names a followed game up to three days out: what games
    like it did here (or the last one, as one night), a staffing plan by role
    and the rush around kickoff once two games are measured (clean nights; a
    jump only where the games jumped), and a game-day campaign to start.
  - The nightly report sets tonight's game against the last one of the same
    side, and the day after says who worked games like tomorrow's.
  - Owners follow or stop following a calendar from the Labor events card; the
    admin console's Event calendar corrects a game (Week 18's date when the
    league sets it); its followers re-sync on the admin job pool, bounded.

- **9/30.**
  - The Reports tab and a figure strip per night.
  - Hourly pay per person: a $0 POS punch is costed at that person's own
    rate.
  - Labor % includes salaries for the owner; headcount is recorded per
    night; covers come from POS guest counts.
  - Passkeys and Sign in with Apple on the web.
  - A WebGL sign-in background with no banding.
  - Evening CI failures fixed at their root (UTC against local dates).
- **9/29.**
  - A ticket-level POS archive with payments, punches and prices.
  - POS job lists mirrored into who can work what.
  - The RPOWER schedule push preview.
  - Memory and learning audits: 86 items, then a 77-item re-audit fix
    round.
- **9/28.**
  - The admin console rebuilt into five areas (Overview, Operations,
    Customers, Engineering, Analytics).
  - Salaried staff; Campaign Studio (text, email and social from one goal).
  - RPOWER live.
  - The DSR's first nights at Simple EJ's.
- **9/26–9/27.**
  - Schedule Studio, a full-page scheduling app; overtime rebalancing.
  - The notifications and messages redesign; owner-chosen draft and order
    days.
- **9/22–9/25.**
  - The edge-case audit fix rounds.
  - Recommendation ROI (the ledger learns, ranks and answers).
  - The Benchmark Engine and peer groups.
  - The Daily Sales Report engine (`dsr/`, phases 1–6).
- **9/21.**
  - Repository audit and cleanup.
  - The Intelligence Engine (`intelligence/`).
  - Recipes from the pasted menu.
  - M/D/YY dates everywhere.

## Deliberately not doing (yet)

- **Self-serve account deletion** for an owner. Service is contract-based
  (DocuSign), so cancelling goes through Will, not a button. (An employee's
  staff login can be deleted in the staff app since 10/2/26.)
- **A second LLM provider** for anything but the AI-visibility checks
  (Perplexity stays scoped to that one job).
- **A schema migration or version-table system.** Additive `ALTER TABLE`
  is enough at this scale; revisit only if a destructive change becomes
  unavoidable.
- **More than one gunicorn worker**, until the process-local state in
  `docs/plans/POSTGRES_AND_WORKERS_PLAN.md` moves into the database.

## Reference

`README.md` is the way in. CLAUDE.md indexes every reference doc.
