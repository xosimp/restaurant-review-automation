# Cavnar AI — Production Edge Case & Failure Scenario Audit (final, merged)

Repository `/Users/simp/review_automation` at commit `f705755`. This report merges six read-only area audits: SEC (input validation, auth, security, recovery), DATA (data layer, jobs, scheduler, backup, settings), AI (model calls and network), MOD (modules, performance, UX), SCHED (the schedule pipeline) and CLIENT (browser, iOS, client state). Finding IDs are the source reports' IDs. Where several reports describe the same defect, the merged finding lists every source ID, keeps the strongest evidence, and takes the highest severity the evidence justifies.

**How the merge was checked.** Fourteen of the 17 merged P0 claims were re-read in the code at `f705755`, and each was confirmed: `_SESSION_USER_SQL` (auth.py:2177-2190), the unconditional `last_active` write (auth.py:2349-2356), `get_client` with no timeout (ai_utils.py:424-439), `_claim_stripe_event` treating every INSERT error as a duplicate (webhook_routes.py:36-44), Toast `page_limit = 20` (toast.py:213, 334), Toast `entry_date = in_dt.date()` on a UTC timestamp (toast.py:490-491), the Places response read without its `status` (fetcher.py:12-27), the sample-data fallback (labor.py:106-112), `critical_low[:4]` / `reorder_soon[:6]` feeding the order builder (inventory.py:512-513, 1312-1313), the media join with no tenant predicate (marketing_publish.py:306, 413; marketing_drafts.py:74), the unsigned numeric OAuth `state` (social_routes.py:117-125), `_newest_published ... LIMIT 1` (staff_schedule.py:26-40) and the RootView-owned `homeViewModel` that sign-in does not reset (RootView.swift:40, 335-346), and the "wrote no shifts ... twice" failure driven by `_missing_dates` (schedule_engine.py:413-482). The documented restore steps are at docs/ops/RECOVERY.md:113-115 (DATA-2 cited 92-102). Two open questions in the source reports were settled from code; they are listed in section 22.

---

## 1. Executive Summary

**Verdict.** Cavnar AI is not ready for many restaurants at once. The core logic inside each module is careful, and many past failures are already pinned by tests. The failures now sit at the edges. First, the tenant and role boundary is resolved per session and is not rechecked when memberships, groups or roles change. That produces two confirmed cross-tenant paths (SEC-1, SEC-2) and three more on marketing media, Meta OAuth and a shared iPhone (MOD-MKT-1, MOD-MKT-5, CLIENT-2). Second, side-effecting sends (guest SMS, staff schedules, supplier orders, newsletters, social posts, review approvals) have no idempotency. A double tap, a client timeout or a restart repeats them; for guest SMS the probe showed 40 guests receiving 80 texts. Third, the whole platform is one gunicorn process with 4 threads, one SQLite writer and one serial scheduler thread. Every authenticated request writes to the database, and every model call can hold a thread for up to 600 s. A full or locked volume therefore takes down all logged-in reads, and four slow Ask calls can take down the whole app. Fourth, several surfaces show confident but wrong numbers: sample labor money shown to new signups, POS dinner shifts dated to the next day, truncated Toast syncs, Places key failures read as "no new reviews", and purchase orders cut to 10 lines. Recovery depends on hand-run SQL, and the documented restore can leave an empty database that passes `integrity_check`.

**Totals.**

| | P0 | P1 | P2 | P3 | Total |
|---|---|---|---|---|---|
| Raw, as reported by the six audits | 18 | 105 | 176 | 87 | **386** |
| Deduplicated (this report) | 17 | 84 | 140 | 73 | **314** |

120 raw findings describe 48 underlying defects. Each merged finding takes the highest severity the evidence supports, so the P0 count falls by only one: MOD-MKT-6 and CLIENT-1 are the same guest-SMS defect. Raw counts by area: SEC 40, DATA 62, AI 33, MOD 148, SCHED 42, CLIENT 61.

**The five things to fix this week** (all small or medium changes):

1. **Close the tenant boundary.**
   - SEC-1: give PIN identities `users.role='employee'`, carry the restaurant on staff sessions, and fail closed when no active membership resolves. Also rewrite `tests/test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role`, which currently pins the unsafe fallback.
   - SEC-2: recheck `active_restaurant_id` against the location group on each request.
   - MOD-MKT-1: add `AND m.restaurant_id = s.restaurant_id` to the three media joins and validate `media_id`.
   - MOD-MKT-5: sign the web OAuth `state`.
   - CLIENT-2: reset `homeViewModel` and the navigation paths on sign-in.
2. **Make guest messaging send exactly once and respect opt-outs** (MOD-MKT-6 / CLIENT-1 / AI-7 / DATA-13, MOD-MKT-8, SEC-17 / MOD-MKT-9):
   - Claim each recipient row before calling Twilio.
   - Normalise STOP messages (punctuation, full-width and zero-width characters, "opt out").
   - Never clear `unsubscribed` from the public opt-in form.
3. **Keep the platform up when the database or the model is slow:**
   - Make the `last_active` write best-effort and throttled (DATA-1).
   - Build the Anthropic client with a 45–90 s read timeout and `max_retries=0` (AI-1, AI-13).
   - Add `timeout=` to the 12 `_req` calls and teach the timeout lint about import aliases (DATA-16 / MOD-MKT-2).
   - Until DATA-2 is fixed, do not run the documented restore against a live container.
4. **Stop losing money events and sending wrong orders:**
   - Treat only `IntegrityError` as a duplicate Stripe or DocuSign event, and return 5xx when the handler fails (AI-2 / SEC-27 / MOD-BIL-3 / MOD-BIL-4).
   - Build purchase orders from the untruncated lists (MOD-FC-1).
5. **Stop showing wrong numbers and wrong schedules:**
   - Gate every sample-data fallback on `is_live` (MOD-LAB-16, MOD-EMP-1).
   - Date POS shifts in the restaurant's timezone (MOD-LAB-3).
   - Treat a Places `status` other than OK/ZERO_RESULTS as a failure (MOD-REV-1 / AI-6).
   - Flag a truncated Toast sync as partial (AI-3 / MOD-LAB-4).
   - Have the staff portal read every published week that overlaps the next 7 days (SCHED-2).

   SCHED-1 (restaurants closed one weekday cannot generate a schedule) is the next P0 and needs a medium-sized change.

---

## 2. Highest Risk Failure Scenarios

**1. A fired employee reads and administers the owner console of the restaurant that fired them (SEC-1; related SEC-2, SEC-5, SEC-6, SEC-14, CLIENT-2).**
- **How it happens.**
  - A staff identity is created with `users.role` left at its default of `'client'` (auth.py:276, 1703-1720).
  - `get_session_user` joins the membership at the identity's home restaurant, not at the restaurant the PIN session was opened for.
  - If that membership is inactive, the join returns nothing and the code keeps `users.role` (auth.py:2177-2190).
- **Probe result.** Jordan works at A and B. A unlinks Jordan, and Jordan PIN-signs in at B. The session resolves to `restaurant_id=1 role=client`, and A's `/mobile/api/account/staff`, `/team`, `/food-cost/cogs` and `/billing` all return 200. A test pins the unsafe fallback.
- **Related paths.**
  - An owner session keeps acting at a location after the location leaves the group, for up to 30 days on iOS (SEC-2).
  - A manager can reach the owner role in two requests (SEC-5, SEC-6).
  - A promoted staff member keeps a 4-digit PIN into the console (SEC-14).
  - A shared iPhone shows the previous account's Home (CLIENT-2).

**2. Every guest on the list is texted twice, including guests who texted STOP (MOD-MKT-6, CLIENT-1, AI-7, DATA-13; related MOD-MKT-7, MOD-MKT-8, SEC-17 / MOD-MKT-9, MOD-MKT-11).**
- **Why it happens.**
  - `send_campaign` texts each guest inside the HTTP request, with a 10 s Twilio timeout per text.
  - It writes `last_campaign_at`, the campaign row and the recipient rows only after the loop finishes (guest_marketing.py:640-700).
  - iOS gives up after 20 s and re-enables Send without a confirmation step.
- **Probe results.**
  - Two overlapping sends texted 20 of 20 guests twice. In the client probe, 40 guests received 80 texts.
  - A crash after 10 texts left no campaign row. The retry then re-texted those 10 guests.
- **Opt-out and consent gaps.**
  - "Stop.", "STOP!" and "opt out" do not unsubscribe (MOD-MKT-8).
  - The public join form re-subscribes a number that texted STOP (SEC-17 / MOD-MKT-9).
- **Impact.** Per-message TCPA exposure. Carrier filtering of the shared A2P campaign, which also carries owner alerts (MOD-MKT-11).

**3. A full or locked volume takes down every logged-in screen, and the documented restore can leave an empty database (DATA-1, DATA-2; related DATA-11, DATA-33, DATA-18 / MOD-PERF-7).**
- **Why every screen fails.** Every authenticated request runs `UPDATE sessions SET last_active=...` and commits, with no try/except (auth.py:2349-2356). When SQLite refuses the write, `get_session_user` raises, so every page returns 500. RECOVERY.md says "writes fail, reads succeed", and `/health` (`SELECT 1`) stays 200.
- **Why the restore fails.** The documented restore runs `mv`, `rm` and `cp` over `railway ssh` against the live container (RECOVERY.md:113-115). Probe p5 showed that a connection opened between those steps leaves a database holding only `scheduler_lease`, and `integrity_check` still reports `ok`.
- **Failures that stay hidden.**
  - Boot migrations swallow "database is locked" (DATA-11).
  - `/health` passes on an empty database (DATA-33).
  - The only off-volume backup is a redacted email attachment built in memory (DATA-18).

**4. Four slow model calls make the whole app unresponsive (AI-1, AI-13; related MOD-INT-5, MOD-MKT-7, MOD-EML-3, SCHED-29, CLIENT-37).**
- **Why a call can last so long.** `get_client()` builds `anthropic.Anthropic(api_key=key)` with the SDK defaults: a 600 s read timeout and 2 retries (ai_utils.py:424-439). `create_with_retry` then wraps those retries in 2 more, so one logical call can make up to 9 HTTP attempts.
- **Why it takes the app down.**
  - Ask streams, on-page insights, AI visibility (8 Perplexity queries behind a 1.3 s gate), guest SMS fan-outs and newsletter fan-outs all run on the 4 gunicorn request threads.
  - While those threads are busy, login, the staff portal, webhooks and `/health` all queue behind them.
  - The timeout lint cannot see SDK clients.

**5. New restaurants and POS-connected restaurants are shown wrong labor money (MOD-LAB-16, MOD-EMP-1, MOD-LAB-3, AI-3 / MOD-LAB-4; related AI-23 / MOD-LAB-5, MOD-LAB-7).**
- **Sample data shown as real.** With no shifts uploaded, `load_shifts_for_restaurant` returns the bundled sample week (labor.py:106-112). The weekly email, mobile Home and `/api/labor-gap` do not check `is_live`. A fresh restaurant is told it is at 36.8% and that "the gap is costing around $12,630/mo". The Team screen lists eight fictional employees.
- **Shifts dated a day late.** Toast and Square date each shift by its UTC clock-in (toast.py:490-491; square.py:181). Every US dinner shift therefore lands on the next day.
- **Truncated Toast syncs.** The sync stops at 20 pages, or 2,000 entries (toast.py:213). It then saves the truncated CSV over the complete one and reports `ok`.

**6. Review ingestion stops for the whole Places-backed fleet while health checks stay green (MOD-REV-1 / AI-6; related MOD-REV-11, AI-4, MOD-REV-7, MOD-INT-2).**
- **Failures read as success.** `fetch_google` checks only the HTTP status (fetcher.py:12-27). Places returns REQUEST_DENIED, OVER_QUERY_LIMIT and NOT_FOUND as HTTP 200 with no `result`. The fetch therefore returns `[]`, stamps `last_fetched_at` and meters a successful $0.017 call.
- **Reviews are lost for good.** Places returns only the newest 5 reviews, so anything past those during the outage is never recovered (MOD-REV-11).
- **Unanalysed reviews are also dropped.** An AI budget stop or provider outage uses up each review's 5 analysis attempts in about 1.25 days, after which the review is never analysed or drafted (AI-4).

**7. Scheduling fails for common restaurants, and staff are shown the wrong week (SCHED-1, SCHED-2, SCHED-4, DATA-17 / SCHED-5, SCHED-6, SCHED-9).**
- **Closed days block generation.** No code can mark a day closed. A restaurant closed on Mondays therefore fails generation after 3 paid model calls ("The model wrote no shifts for Monday twice").
- **The portal shows the wrong week.** It reads only the newest published week (staff_schedule.py:26-40). After the Friday auto-publish of next week, this week's Friday to Sunday show as "off", and a double shift shows only its later leg.
- **Open-shift claims are unsafe.** Two concurrent claims both answer "covered" and one cover is lost. A minor claimed a 6pm–1am bartender shift.
- **Publishing depends on email.** A publish needs at least one successful email, so a portal-only restaurant can never publish.

**8. Supplier purchase orders are short, sent twice, or sized from a wrong stock count (MOD-FC-1, MOD-FC-2, DATA-15 / MOD-FC-11, MOD-FC-4, MOD-FC-8, MOD-FC-9 / DATA-41, MOD-FC-12).**
- **Items are dropped.** The order is built from lists sliced for display, `critical_low[:4]` and `reorder_soon[:6]`. At most 10 lines ever reach a supplier.
- **Duplicates and repeats.**
  - A second ingredient with the same name is dropped.
  - The re-send guard is a 60-second, process-local dict, so a restart or a wait re-sends the PO.
  - The iPhone skips the undo window.
  - The "send exactly what the owner approved" hash is never sent by either client.
- **Stock counts go wrong.** A nightly re-sync after a recount subtracts two days of sales twice.

**9. Payment events are dropped, and billing state goes wrong (AI-2 / SEC-27 / MOD-BIL-3 / MOD-BIL-4, DATA-54 / MOD-BIL-9, MOD-BIL-1, MOD-BIL-2, SEC-11 / MOD-BIL-7).**
- **Events that are lost.**
  - The Stripe claim INSERT treats any error, including "database is locked", as a duplicate and answers 200 `duplicate` (webhook_routes.py:36-44).
  - A handler that fails after claiming the event is never retried.
  - Provisioning is three separate commits made after the claim.
- **State that goes wrong.**
  - A delayed `invoice.paid` reactivates a cancelled customer.
  - A $5 partial refund pauses every location in the group.
  - Without its secret, the DocuSign webhook accepts a forged "completed" event and resets the owner's password.

**10. At scale the scheduler cannot keep up, and it runs inside the web process (DATA-3 / MOD-PERF-1, DATA-6 / MOD-PERF-3, MOD-PERF-2, AI-9 / MOD-INT-1 / MOD-FC-19 / MOD-LAB-9 / MOD-NOT-11 / SCHED-27, DATA-7, DATA-4, MOD-NOT-2).**
- **Everything runs in one serial loop.** Every job runs in one thread inside the web process. The review fetch alone can hold that thread for 3 hours, four times a day. Undo-window sends, held alerts, scheduled posts and the 4pm pulse all wait behind it.
- **Measured costs at 100k restaurants:**

  | Operation | Cost |
  |---|---|
  | `get_all_restaurants()`, called more than 30 times an hour | ~52 s of CPU and ~1 GB per call |
  | `claim_period`, which runs an unindexed DELETE on every call | 37–103 ms per claim at 1–2M rows |
  | Weekly AI-visibility sweep | about 12 days per pass |

- **Consequences.**
  - The lease is renewed only at the start of each tick, so a second process can take it mid-pass and run the jobs again.
  - One restaurant's backlog of held alerts can starve every other restaurant's until they are dropped.

---

## 3. Input Validation

Uploads validate their headers, not their contents. The analysis code then reads raw keys and raw values, so a file the server accepts can still break a module.

**Spreadsheet uploads (shift and inventory CSVs)**
- **Shift CSVs** (SEC-16 / MOD-LAB-10 / MOD-LAB-11 / MOD-LAB-13 / MOD-LAB-14; client_api.py:2782-2900, labor.py:474, 488; models.py:5389-5402). These inputs are accepted with "rows loaded successfully" and then break Labor:
  - Excel's default M/D/YYYY dates raise `ValueError` at labor.py:474.
  - Title-case headers raise `KeyError('day')`.
  - `nan`, `inf` and negative hours zero a day's labor or emit `Infinity`, which is not valid JSON.
  - One "8h" cell makes `compute_blended_rate` raise.

  Each upload also replaces `client_data.shifts_csv` wholesale, with no previous version kept.
- **Inventory CSVs** (MOD-FC-5 / SEC-26; client_api.py:2854-2863, inventory.py:210-221, models.py:4942-4950). These inputs make every Food Cost read raise:
  - A file with only the required columns (`KeyError 'avg_daily_usage'`).
  - A blank cell, a `$1.80` value, a title-case header, or `unit_cost=nan`.

  The auto-migration failure is only printed.
- **Encodings** (SEC-26, MOD-LAB-11). An Excel UTF-8 file carries a BOM, and the upload rejects it with a false "missing required columns: date". A Latin-1 or cp1252 file is refused. The TripAdvisor importer already uses `utf-8-sig` (client_api.py:689).
- **Sales columns** (MOD-LAB-12; labor.py:413-416). A `revenue` column and "$4,200"-style values pass validation and are then ignored. The owner is told there are no sales.
- **Recipe CSV** (MOD-FC-24; recipes.py:394-434). A dish that does not match a POS item is created unlinked and never depletes stock. Rows past 2,000 are dropped silently.

**Numeric input on JSON routes**
- **NaN** (MOD-FC-6; invoices.py:368-379, admin_routes.py:360-367). `NaN` passes the `cost <= 0 or cost > 100000` guard, is stored as NULL, and makes the restaurant's Food Cost raise `TypeError` on every read.
- **Negative receiving and unit mismatches** (MOD-FC-12; inventory_ledger.py:226-242, 346-353). Negative receiving is accepted, and a recipe in ounces against an ingredient in pounds over-depletes 16 times. Stock goes to -485, and the suggested order becomes 498 units.
- **Count-sheet dates** (MOD-FC-15; strategy_routes.py:538). The date is not validated. "9/21/26", the product's own display format, sorts after every ISO date, so it sits inside every future 7-day window.
- **Non-object JSON bodies** (SEC-32). 170 distinct POST routes return 500 on a body that is a string or an array (342 `AttributeError`s in a 1,180-request fuzz), including `/mobile/api/login` and `/mobile/api/verify-2fa`.
- **Alert settings** (MOD-NOT-5; client_api.py:2699, 2707-2709).
  - Quiet hours stored as "9pm" are silently treated as off.
  - A non-numeric cap makes the whole save return 500.
  - An invalid `digest_day` silently stops the digest.

**Scheduling input**
- `week_start` accepts past weeks (SCHED-37; client_api.py:2461-2463).
- A `labor_target_pct` of 0 becomes 30%, and admin input has no bounds (SCHED-36; schedule_engine.py:116, admin_routes.py:686).
- Employee availability notes reach the model prompt as a "hard constraint" instruction (SCHED-12; labor.py:1822-1843).

**Text rendered as HTML or JavaScript**
- Restaurant, supplier, employee and item names are interpolated raw into third-party emails, and the restaurant name is used raw in the From header (MOD-EML-2; emails.py:1263-1319). The overtime alert email interpolates employee names the same way (MOD-LAB-17; client_api.py:2926-2980).
- In the web dashboard:
  - A location name containing an apostrophe breaks the inline `onclick` (CLIENT-17; dashboard.html:1188-1191).
  - A reply template containing `"` injects attributes into the template picker (CLIENT-18; dashboard.html:15716-15737).
  - A client-supplied X-Forwarded-For value is rendered as HTML in the Sessions list (CLIENT-40; auth_routes.py:24-27, dashboard.html:14688-14708).

**Opt-out and phone input**
- STOP variants are missed (MOD-MKT-8; guest_marketing.py:300-302, 405-418). The code reads only the first word and strips no punctuation, so "Stop.", "STOP!", "Please stop", "opt out", zero-width-prefixed "STOP" and full-width "ＳＴＯＰ" all leave the guest subscribed.
- The Ask Cavnar contact tool stores an unvalidated phone number (the probe stored `<script>`) and erases the stored email when only a phone is given (MOD-EMP-4; models.py:7760-7780).

**Files**
- A decompression bomb gets through (MOD-MKT-13; marketing_media.py:52-81). A 20 KB, 13000×13000 PNG costs about 580 MB of RAM in the only web process.
- The 12 MB upload promise can never be met (MOD-MKT-14; hosted_dashboard.py:70). A 5 MB body cap applies first and returns an HTML 413. HEIC is advertised but cannot be decoded.
- On iOS, a comma-decimal price is sent as typed and silently dropped (CLIENT-31).

## 4. Workflow Edge Cases

There is no request idempotency key anywhere in the codebase (DATA summary). Every double-submit, client timeout, retry or second tab repeats the side effect.

**Double-submit and retry**
- **Guest SMS campaigns** are texted twice (MOD-MKT-6 / CLIENT-1 / AI-7 / DATA-13). The iOS view has no re-entry guard and no confirmation step. Both the web and the Ask Confirm button re-enable after a network error (CLIENT-19; dashboard.html:7208-7232).
- **Schedule publish** re-emails every staff member (DATA-12 / SCHED-28 / SCHED-29 / CLIENT-36; client_api.py:5663-5815, delayed.py:137-141). This happens on a double click, from a second device, and when the 11am auto-publish runs after a manual publish. iOS "Send to staff" stays enabled after success.
- **Supplier orders** re-send after 60 s or after any restart (DATA-15 / MOD-FC-11; client_api.py:5373-5385). The iPhone skips the undo window (MOD-FC-9 / DATA-41; mobile_api.py:1794-1880).
- **Newsletters** have no send record, so a second press mails everyone again. Concurrent sends also break unsubscribe tokens (DATA-14 / MOD-EML-3; guest_email.py:138-226).
- **Review approve** re-runs the Google PUT, the webhook and the alert (DATA-26 / MOD-REV-4 / MOD-REV-5 / CLIENT-16; client_api.py:96-189). A probe produced 6 PUTs for 3 reviews. On iOS, approve posts the pre-edit reply when saving the edit failed or was queued (CLIENT-6).
- **Social posts** can go out twice on iOS (CLIENT-10 / DATA-25). An ambiguous Meta 5xx is also retried as if it were a definite refusal (MOD-MKT-4).
- **Other double-submits:**
  - A second quick count rotates away last week's price baseline (DATA-27).
  - A double-clicked alert Save leaves 4 contacts, so every SMS goes out twice (DATA-24).
  - Creating a staff member twice makes two people (DATA-37).
  - Time-off and shift requests are duplicated (DATA-35 / CLIENT-46 / MOD-EMP-9).

**Tabs, devices and concurrent editors**
- Location context is per session, not per tab. A second tab therefore saves Location 1's form into Location 2 (DATA-10; auth.py:2397-2405).
- Whole-form settings saves are last-write-wins. `update_restaurant(expected_version=)` exists, but no route passes it (DATA-28).
- Schedule saves (SCHED-19 / DATA-57 / CLIENT-29):
  - The stale-save check is optional and not atomic.
  - iOS weeks restored from its cache bypass the check.
  - Two quick overrides on iOS raise a false conflict and overwrite each other.

**Long-running actions**
- **Generate** joins a dead job for 10 minutes after a deploy (DATA-9 / SCHED-25; ops.py:228-270), and two presses can start two paid generations (DATA-23).
- **Web generate** discards unsaved edits (SCHED-41), gives up on the first failed poll, and shows a Python traceback (CLIENT-14 / CLIENT-41 / DATA-46 / SCHED-41).
- **Leaving the iOS Labor screen** during generation orphans the job (CLIENT-27).
- **Ask Cavnar on iOS** re-runs the whole question when a stream fails part-way (CLIENT-28).
- **iOS request limits.** Every request is capped at 45 s, so a 90 s generation never completes (CLIENT-20), and nothing keeps an upload alive when the app is backgrounded (CLIENT-21).

**Session expiry**
- On the web, only Home reacts to `session_expired`. Every other module fails silently, and the pollers keep sending requests that get 401s (CLIENT-13; dashboard.html:2760).
- The staff portal offers no path back to sign-in (CLIENT-47).
- A late 401 carrying a superseded iOS token signs out the new session (CLIENT-23).

## 5. Database Edge Cases

**Lock and write-failure behaviour**
- Every authenticated request writes `sessions.last_active`. A read-only, full or long-locked database therefore returns 500 on every request (DATA-1; auth.py:2349-2356).
- Long write transactions queue those session writes behind them, and each waits up to the 30 s busy timeout:
  - The review-request follow-up job holds a write transaction across its whole Twilio loop (DATA-52 / MOD-MKT-10; guest_marketing.py:1017-1080). Probe: another writer got "database is locked" after 0.58 s.
  - `claim_period` runs an unindexed full-table DELETE on every call (DATA-6 / MOD-PERF-3; ops.py:117-176). That is 103 ms per claim at 2M rows.
  - `prune_ledgers` deletes by retention columns that have no index (DATA-40; ops.py:542-575).

**Migrations and health**
- Every boot migration is `try/except: pass`, so "database is locked" is swallowed like "duplicate column". The app then serves on a drifted schema (DATA-11; models.py:1029, 2312-2316).
- The 17 `init_*` calls share one try block, so one failure skips all the ones after it.
- `/health` passes on an empty or unmigrated database (DATA-33; status_manager.py:257-300).
- `init_db(db_path)` runs `ensure_columns()` against the default database, not the one it was given (DATA-44; models.py:2329).

**Constraints**
- TripAdvisor, DoorDash and UberEats imports violate the table's `CHECK(platform IN (...))`, store nothing, and report `imported: N` (DATA-21; models.py:117, client_api.py:674-754). Their dedupe key uses Python's per-process salted `hash()`.
- Deleting a generated schedule fails on a foreign key, because every generation writes a `schedule_versions` row (SCHED-16 / DATA-55; models.py:5325-5338). The existing test builds no version row.
- Account deletion is a flag that nothing acts on (DATA-61). 72 child tables reference `restaurants` with `NO ACTION`, and 23 tables have no foreign key at all.

**Partial writes (several commits where one transaction is needed)**
- Stripe provisioning (DATA-54 / MOD-BIL-9; provisioning.py:50-70).
- Team invite: the login is created with the primary-login role, then narrowed in a separate commit (DATA-56; auth.py:1836-1852).
- Staff name claim: six separate commits (DATA-58).
- Signup, create-client and shift-CSV follow-on writes (DATA-62).
- Schedule edit plus its version row (SCHED-19 / DATA-57).
- Shift claim: the CSV is rewritten, then the status is updated (DATA-17 / SCHED-5).

**Races**
- Open-shift claims (DATA-17 / SCHED-5) and shift-request decisions (DATA-36).
- Concurrent recounts double the inferred waste in 30 of 30 trials (MOD-FC-14).
- PO numbering (MOD-FC-7; models.py:7662-7690). `COUNT(*)+1` wedges permanently after a PO that is not the newest is voided.
- Recipe accept (MOD-FC-27) and reset-token reuse (DATA-50).
- Re-syncing depletion after a recount orders events by row id rather than by event date, so two days are subtracted twice (MOD-FC-4; inventory_ledger.py:54-77, 310-358).

**Stale, duplicate and deleted rows**
- The same review arriving through Places and through GBP is stored twice (MOD-REV-3; fetcher.py:52-66, gmb.py:329-339).
- Soft-deleted reviews still alert and still reach the model. The 48-hour no-response alert also ignores drafted reviews (DATA-60 / MOD-NOT-9; notify.py:1733-1786).
- A restaurant row that fails to hydrate silently disappears from every scheduled job (MOD-PERF-5 / DATA-43; models.py:5625-5633).
- A week published twice keeps both versions live (SCHED-10).

**Unbounded growth**
- `inventory_history` stores the full item list daily with no pruning. That is 2.5 MB a day per 5,000-item restaurant, and it is fully parsed on the request path (MOD-FC-18).
- `alert_holds` and `notification_opens` are never pruned (MOD-NOT-14).
- `schedule_versions` stores a full CSV on every edit (SCHED-27).
- A recommendation event is written on every rescore (SCHED-26).
- Hot per-restaurant tables have no index on `restaurant_id` (MOD-PERF-6 / MOD-NOT-13).

## 6. AI Edge Cases

**Timeouts, retries and threads**
- One model call can hold a request thread for minutes (AI-1; ai_utils.py:424-508). The SDK default is 600 s, and the SDK's retries stack with the wrapper's for up to 9 attempts, which also makes provider incidents worse (AI-13).
- Only `inventory.py:1125` sets a timeout.
- `_ASK_SLOTS = 8` is higher than the 4 request threads, so it never limits anything.

**Budget, outage and model configuration**
- Budget stops and breaker-open errors each use up one of a review's 5 attempts, so after about 1.25 days of outage a review is never analysed or drafted again, urgent ones included (AI-4; analyser.py:293-329, models.py:3127-3175).
- Only Ask and invoice scan show the "paused" message. Every other surface says "check back shortly" indefinitely (AI-11).
- The ledger prices Sonnet 5 at $3/$15 instead of $2/$10, so budgets trip 50% early. The global backstop grows by $200 per paying client (AI-12; ai_utils.py:605-615).
- `thinking: disabled` is forced on every call. One env override to a model that rejects it breaks all AI without opening the breaker (AI-14).
- The over-budget guard in the two diagnosis sweeps reads an attribute that does not exist (AI-25; scheduler.py:1837, 1897).

**Wrong or unverified content**
- `verify_figures` passes invented dollar figures that land near any number in the context, including the year "2026" (AI-5; ai_guard.py:176-262). "$2.4k" and "$1.2M" are never checked.
- Guest-written text reaches four prompts unfenced (AI-15): drafter examples, reviewer names, review-insight excerpts and the marketing best quote.
- Ask's direct-action tools run with no confirmation (AI-16). `remember` writes permanent "owner said" memory.
- The weekly plan runs Ask unattended with action tools enabled and ignores its unverified figures (AI-17).
- "Value delivered" counts overlapping trackers on the same metric twice (AI-18).
- Invoice auto-apply accepts any price when the current cost is 0 (AI-19).

**Malformed or empty responses**
- Ask's final call after a tool round drops `tools` while the history still holds tool blocks. That is a likely 400 on every confirm-card turn (AI-8; ask_cavnar.py:1043-1167). Unable to verify against the live API.
- A refusal becomes an empty answer, or an empty draft marked `drafted` (AI-24).
- JSON output is parsed from free text instead of structured output (AI-26).
- `max_tokens=300` cannot fit an 80–100-word urgent reply in CJK and similar languages, so those reviews are never drafted (AI-20).

**Schedule generator**
- Closed days cannot be expressed, and generation fails after 3 paid calls (SCHED-1).
- The CSV fallback drops `week_start`, the revenue override and the slice context (SCHED-3; labor.py:2090-2108).
- Rosters of 400–500 people exceed the per-call row budget (SCHED-24).
- Rows in 24-hour time skip the close cap (SCHED-38).
- Rows with fewer than 6 columns pass the missing-day check and are then all dropped, which saves an empty week (SCHED-42).
- Availability notes act as a prompt-injection channel (SCHED-12).

**Scale**
- The AI sweeps are serial, unbounded and have no cursor (AI-9 / MOD-INT-1 / MOD-FC-19 / MOD-LAB-9 / MOD-NOT-11 / SCHED-27). The visibility sweep alone is about 12 days per pass at 100k restaurants.
- Duplicate spend comes from iOS retries (CLIENT-20, CLIENT-21, CLIENT-28, CLIENT-56) and from competitor refreshes (SEC-31 / DATA-29).

## 7. Module Edge Cases

**Reviews**
- Places key or quota failures read as "no new reviews" (MOD-REV-1 / AI-6).
- The same review is stored twice across Places and GBP (MOD-REV-3).
- The first GBP connect alerts on every historical 1-star review and uses up the 50-per-day ceiling (MOD-REV-6).
- GBP backfill never goes past the newest 1,000 reviews (MOD-REV-7), and Places returns at most 5 per fetch (MOD-REV-11).
- GBP dates are stored in UTC while Places dates are local (MOD-REV-9).
- Auto-published replies that fail to post are silent (MOD-REV-10).
- The auto-approve daily cap resets at 7pm CT if the server runs in UTC (MOD-REV-13).
- Cancelled restaurants are still fetched, alerted and auto-published (DATA-51 / MOD-REV-2).

**Labor**
- Sample data is shown as live figures (MOD-LAB-16) and as the roster (MOD-EMP-1).
- POS shifts are dated by UTC (MOD-LAB-3).
- Toast syncs are truncated at 2,000 entries (AI-3 / MOD-LAB-4).
- Toast "First L." names merge two people (AI-23 / MOD-LAB-5), and clock-in names never match schedule names (MOD-LAB-2).
- The no-show coverage job crashes on every restaurant with a schedule (MOD-LAB-1).
- Square roles come from the location-assignment enum (MOD-LAB-6), and a failed Square orders page silently truncates the day's sales (MOD-LAB-7).
- Square and Clover never write daily history (MOD-LAB-8).
- Case and whitespace variants of a name split one person's overtime (MOD-LAB-15).
- A POS sync overwrites a longer hand-uploaded history (MOD-LAB-18).
- An open clock-in costs $0 and counts as a no-show (MOD-LAB-21).

**Food Cost**
- Orders are truncated to 10 lines (MOD-FC-1), and same-name ingredients are dropped (MOD-FC-2).
- Re-syncing after a recount double-subtracts sales (MOD-FC-4).
- The draft hash is inert (MOD-FC-8), and a voided queued order is never reported (MOD-FC-10).
- Negative stock is not clamped (MOD-FC-12).
- An archive with a hole reads food cost about 11 times too high (MOD-FC-16).
- Every food-cost % load calls the live POS (MOD-FC-17).
- The web order screen hides unassigned items (MOD-FC-20), and the mobile totals are sums of truncated lists (MOD-FC-21).
- Price Watch keys on the display name (MOD-FC-22), and COGS prices every delivery at today's cost (MOD-FC-23).
- Usage freezes when a dish stops selling (MOD-FC-13).

**Marketing**
- Media is readable across tenants (MOD-MKT-1), and the Meta OAuth state is unsigned (MOD-MKT-5).
- Guest SMS is sent twice (MOD-MKT-6 cluster).
- The SMS fan-out runs inside the request and crosses the 9pm quiet-hours line (MOD-MKT-7).
- STOP variants are missed (MOD-MKT-8), and the public opt-in re-subscribes guests (SEC-17 / MOD-MKT-9).
- Guest promos are sent on the owner-alert A2P campaign (MOD-MKT-11), and a STOP to one restaurant does not stop invites from another (MOD-MKT-12).
- The metrics sync zeroes reach when an insights call fails (MOD-MKT-15).
- Attribution mixes a UTC timestamp with local dates (MOD-MKT-16).
- Draft approval is advisory only (MOD-MKT-17).

**Intel**
- Places errors are reported as "No nearby competitors" (MOD-INT-2).
- An unrated competitor produces a +4.6 "significant" rating jump (MOD-INT-3).
- No city means a recorded visibility score of 0 (MOD-INT-4).
- A failed custom-competitor lookup is reported as the competitor being "gone" (MOD-INT-8).

**Scheduling**
- Closed days, the portal week, doubles, claim races and claim legality (SCHED-1, SCHED-2, SCHED-4, SCHED-5, SCHED-6).
- Overtime is priced across the wrong payroll week (SCHED-7).
- A silent 7-server cap removes shifts (SCHED-8).
- A portal-only restaurant cannot publish (SCHED-9).
- A republished week is double-counted (SCHED-10).
- Close times after midnight (SCHED-13).
- Stale blockers (SCHED-17, SCHED-18).
- No swap consent and no notifications (SCHED-21).
- Trim removes a person's only shift (SCHED-23).
- An unsatisfiable leader rule caps the shift forever (SCHED-30).

**Employees**
- Exact-case name matching (MOD-EMP-2).
- Roster deactivation and portal revoke are separate switches (DATA-59 / MOD-EMP-3).
- The iOS checklist uses the device date against the server's UTC date (MOD-EMP-5).
- Time off cannot be withdrawn (MOD-EMP-7) or checked against the published week (MOD-EMP-8 / SCHED-22).
- 5–8 digit PINs cannot be entered (CLIENT-3).

**Notifications**
- Held alerts bypass the 50-per-day ceiling (MOD-NOT-1).
- One restaurant starves the others' holds (MOD-NOT-2).
- The combined batch is gated by the wrong toggle (MOD-NOT-3).
- Quiet hours use Chicago time for everyone (MOD-NOT-4).
- Push-only owners get no daily alerts (MOD-NOT-8).
- The badge counts rows the viewer cannot see (MOD-NOT-10).
- A removed teammate's phone keeps receiving pushes (DATA-53 / MOD-NOT-6).
- One device token maps to one restaurant (MOD-NOT-7 / CLIENT-8).

**Emails**
- An open relay (SEC-15 / MOD-EML-1).
- Unescaped templates (MOD-EML-2).
- 17 direct Resend send sites (MOD-EML-4).
- GET unsubscribes (MOD-EML-5).
- No postal address (MOD-EML-6).
- A global suppression list (MOD-EML-7).

**Admin and Settings**
- A partial admin save resets other fields (SEC-30).
- `LIMIT 1` picks an arbitrary login (SEC-28).
- The status admin endpoints are outside `admin_required` (SEC-23).
- Settings are last-write-wins (DATA-28).

**Billing**
- Claimed events are dropped (AI-2 cluster).
- Out-of-order events are applied (MOD-BIL-1).
- A partial refund causes a lockout (MOD-BIL-2).
- Payment links expire, and both plans can be paid (DATA-38 / MOD-BIL-5).
- Re-sending a contract re-bills setup and resets the owner's password (DATA-30 / MOD-BIL-6).

**Empty, huge and closed restaurants**
- **New, no data:** sample figures (MOD-LAB-16), a fictional roster (MOD-EMP-1), and attribution that falls back to sample data (MOD-MKT-16).
- **Closed days:** SCHED-1.
- **500 employees:** Toast truncation (AI-3), large-roster generation (SCHED-24), the fix pass taking 17.5 s (SCHED-39), and >7 servers trimmed (SCHED-8).
- **Everything imported:** the GBP first connect alerts (MOD-REV-6) and 1,000 analyses in one pass.

## 8. Network Failures

**Anthropic**
- No timeout, and retries stack to 9 attempts (AI-1, AI-13).
- A budget stop or open breaker shows "check back shortly" (AI-11) and uses up review attempts (AI-4).
- Graceful parts:
  - Ask shows the budget message.
  - The health keyword fallback still alerts (notify `_is_health_alert`).
  - A partial visibility run is shown but not recorded (tested).

**Google**
- A Places non-OK status is treated as success (MOD-REV-1 / AI-6), and the competitor lookup does the same (MOD-INT-2).
- A transient token-refresh failure is reported as "reconnect Google", and a truly revoked token is retried forever (AI-22 / MOD-REV-12; gmb.py:130-163).
- The Places API key leaks into `job_failures`, Sentry and admin JSON through exception strings (MOD-REV-8).

**Meta**
- Twelve calls have no timeout. One of them runs in the scheduler thread daily at 7am, where a black-holed connection stops every job (DATA-16 / MOD-MKT-2).
- A 5xx or 429 is classified as a definite refusal and retried (MOD-MKT-4).

**Twilio**
- There is no retry on a 429 or 5xx, so one failure loses an alert SMS (AI-27; notify.py:96-142).

**Resend**
- Retries go out without an `Idempotency-Key`, so a slow success is sent twice. Worst case is about 46 s inline per email, and a digest claimed before sending is lost for the week (AI-21 / MOD-EML-8).
- An outage leaves a schedule half-published with "Nobody has an email address" (SCHED-9).
- Newsletter fan-out meets the per-team rate limit with no resume (MOD-EML-3).

**Stripe**
- Claimed events are dropped (AI-2 / SEC-27 / MOD-BIL-3 / MOD-BIL-4).
- Events are applied in arrival order (MOD-BIL-1).
- Billing-page calls use the SDK default timeout of about 80 s, sequentially, inside a request thread (AI-33).

**NWS and weather**
- NWS failures are never cached (2 × 10 s per call during an outage), and one geocode blip disables weather for 7 days (AI-28 / MOD-INT-7).

**APNs**
- A 500-deep process-wide queue drops pushes once full, and the alert history still says sent (MOD-NOT-12 / AI-29).
- A push tap from a closed app is lost (CLIENT-7).

**POS**
- Toast truncates at 2,000 entries (AI-3 / MOD-LAB-4).
- A Square non-200 page silently ends the fetch (MOD-LAB-7).
- The live POS is queried on every food-cost load, against a vendor that asked for archive-not-query (MOD-FC-17).
- The mobile Toast connect saves credentials before testing them (SEC-25).

**Railway, deploys and restarts**
- A 5xx, 429 or decode failure on `/me` at launch signs iOS users out and wipes the offline queue (CLIENT-4; SessionStore.swift:106-126). This happens during every deploy window.
- Generations are orphaned (DATA-9 / SCHED-25).
- Delayed actions are stuck in `running` forever (DATA-19).
- Web errors read as "check your connection" (MOD-UX-1 / CLIENT-12).
- Certificate-pin failures look like cancellations on iOS (CLIENT-24).

## 9. Authentication

**Cross-tenant and role escalation**
- A staff identity unlinked at its home restaurant gets that restaurant's owner console (SEC-1, P0).
- An owner session keeps its location after a sale (SEC-2, P0).
- The 2FA pending token is unsigned, so its user id can be swapped (SEC-5; auth_routes.py:249-256).
- Any manager can mint backup codes and disable 2FA (SEC-6; mobile_api.py:4550-4573). With SEC-5, a manager becomes the owner in two requests.
- A promoted staff member keeps a PIN into the console (SEC-14).
- The support role can write through view-as (SEC-12).
- Any login's email change rewrites `owner_email`. That redirects 2FA and alerts, and detaches the location from its group (SEC-13).

**Brute force and throttling**
- The throttles key on the client's first `X-Forwarded-For` value (SEC-3; auth_routes.py:24-27). Probe: 0 of 40 guesses were blocked with a rotating header.
- The mobile 6-digit reset code (SEC-4; mobile_api.py:275-370):
  - Has no per-account cap.
  - Is stored in plaintext.
  - Is generated with `random`, and `random` is also used for the other codes (SEC-33).
- Forgot-password can be used to email-bomb an owner (SEC-3).
- The staff PIN lockout does not escalate and can lock the whole roster (SEC-19).
- The portal's per-IP limit counts successful sign-ins, so the 16th employee on one Wi-Fi is refused (SEC-18).
- Username existence leaks through timing (SEC-35).

**Resets and sessions**
- `must_reset_password` is never cleared, so freeze and "This wasn't me" lock the account permanently (SEC-7; auth.py:2952).
- A reset or password change does not revoke other sessions (SEC-8).
- The mobile reset token can be consumed twice (DATA-50).
- Removed teammates keep their pushes (DATA-53 / MOD-NOT-6), and removed staff keep portal access and share links (DATA-59 / MOD-EMP-3).
- The iOS Keychain items are not device-bound (CLIENT-25).
- iOS logs out on a 5xx at launch (CLIENT-4) and on a late 401 from an old token (CLIENT-23).
- 5–8 digit PINs cannot be entered (CLIENT-3).

**2FA**
- There is one pending slot per restaurant. Concurrent logins clobber each other, codes go to the owner, and OTPs are stored in plaintext (SEC-20).
- The 2FA send-test route has no limiter (DATA-42).

**Other**
- Open redirect via `next` (SEC-22).
- CSRF-exempt JSON routes on `auth_bp` (SEC-21).
- Side-effecting GETs, including not-me (SEC-34).
- SSO accepts an unverified provider email (SEC-38).
- The PIN token is returned in the JSON body (SEC-36).
- Login-report tokens are stored in plaintext (SEC-37).

## 10. Performance

**One process.** Gunicorn runs `--workers 1 --threads 4`, one SQLite writer serves everything, and the scheduler thread runs inside the web process.

**Request path**
- Every authenticated request writes to the database (DATA-1).
- Model calls hold request threads (AI-1).
- Other synchronous fan-outs also hold a request thread:
  - Guest SMS (MOD-MKT-7).
  - Newsletter (MOD-EML-3).
  - Staff schedule emails, one Resend call per employee (SCHED-29).
  - AI visibility, a 10–40 s request behind a process-wide 1.3 s gate (MOD-INT-5).
  - Instagram publish, which sleeps up to 20 s (MOD-MKT-3).
- The Home cache miss re-parses the whole shifts CSV: 0.63 s for 44k rows (MOD-HOME-2). Labor re-parses up to 25,000 rows on every read: 1.36 s and 91 MB for 100k rows (MOD-LAB-18).
- `load_waste_history(limit=None)` takes 2.44 s for one large restaurant (MOD-FC-18).
- The fix pass takes 17.5 s for 99 hard violations (SCHED-39).
- A 20 KB image costs about 580 MB (MOD-MKT-13).

**Scheduler (measured or computed at 100k restaurants)**
- `get_all_restaurants()` takes about 52 s of CPU and about 1 GB, and runs more than 30 times an hour. It is O(columns²) per row because `Row.keys()` is called about 190 times per row (MOD-PERF-2; models.py:5609-5633, 2666-2760).
- `claim_period` takes 37–103 ms per claim, about 62–172 minutes per pass (DATA-6 / MOD-PERF-3).
- The review fetch covers at most 12,960–64,800 restaurants per pass, so each restaurant is reached every 2 days or less often (DATA-7).
- One serial loop blocks every per-tick duty (DATA-3 / MOD-PERF-1).
- Restaurant-iterating sweeps are unbounded (AI-9 cluster).
- Auto-draft reaches 12–30 restaurants per Thursday (DATA-8 / SCHED-11).
- `note_ai_spend` takes 0.54 s per AI call at 3M cache keys (AI-10).

**Drain rates that fall behind**
- `delayed.run_due` handles 20 actions per tick (MOD-PERF-4).
- Alert holds release 200 rows per tick (MOD-NOT-2).
- The push queue holds 500 (MOD-NOT-12).
- `run_due_posts` takes the 200 earliest across time zones (DATA-31).

**Indexes**
- `job_period_claims.claimed_at` has none (DATA-6).
- Retention columns have none (DATA-40).
- `push_deliveries`, `device_tokens`, `ai_visibility_runs`, `review_requests`, `marketing_attribution` and `weekly_reports` have no index on `restaurant_id` (MOD-PERF-6 / MOD-NOT-13).

**Memory**
- Process-local caches never evict (DATA-32 / AI-10 / MOD-HOME-3).
- The nightly backup reads the whole database about 3 times over in memory (DATA-18 / MOD-PERF-7).

**Client**
- The dashboard is 1.27 MB uncompressed and uncacheable, and posts `/api/theme` on every load (CLIENT-38).
- Pollers ignore `document.hidden`. One background Reviews tab makes about 7,900 requests a day (CLIENT-37).
- iOS decodes 48 MP invoice photos on the main thread (CLIENT-39).

## 11. Browser Compatibility

No cross-browser run was made. The dashboard was rendered and viewed only in the Chromium-based Browser pane at 320 and 375 px, so Safari, Firefox and Edge behaviour: Unable to verify from implementation. What the code and that render show:

- **Layout.**
  - At 320 and 375 px the page is 416 px wide, and Sign out and Account sit off-screen (CLIENT-15; dashboard.html:333, 340).
  - A long restaurant name wraps to five lines inside the fixed 56 px header.
- **JavaScript.**
  - The ES5 rule is enforced only on dashboard.html. admin.html uses 126 `const`, 189 arrow functions, 50 `async`, `?.` and `??`, and the audit and client-data pages also use modern syntax. These pages would throw a SyntaxError on Safari before 13.1/14 (CLIENT-44; tests/test_frontend_rules.py:15-38).
  - The BANNED list itself omits `?.`, `??`, spread, `class` and `for…of`.
- **Inline handlers.** Apostrophes in location names break the inline handlers (CLIENT-17), and quotes in templates break the template picker (CLIENT-18).
- **Error handling.** About 200 of the 213 `fetch` sites call `r.json()` without checking the status (MOD-UX-1 / CLIENT-12). `/api/*` returns HTML for 404, 413 and 500, and 55 `.catch(function(){})` handlers swallow the error.
- **Dialogs.** Ten `alert()` calls are used for errors, and iOS Safari can suppress repeated dialogs (CLIENT-48).
- **Accessibility.** There are 44 `outline:none` rules against 8 `:focus-visible` rules, and `div onclick` elements carry no role or tabindex (CLIENT-59). Zoom is not disabled (verified by grep; no test).
- **Status indicator.** The status dot defaults to green when `/api/status` cannot be parsed (CLIENT-43).
- **Dark mode** is forced, so the light-theme CSS branch is dead code (CLIENT matrix).
- **Dates** do not follow the M/D/YY rule in several templates (CLIENT-45).

## 12. Mobile Edge Cases (iOS)

- **Account switching.**
  - Home keeps the previous account's summary after sign-out and a new sign-in (CLIENT-2, P0; RootView.swift:33-41, 335-346).
  - After a location switch, a failed reload shows the old location's numbers (CLIENT-26).
- **Launch and deploys.** A 5xx, 429 or decode failure on `/me` signs the user out and wipes the offline queue (CLIENT-4).
- **Offline queue** (CLIENT-5; PendingWriteQueue.swift:95-125).
  - The queue can crash on `removeFirst()` when a sign-out or location switch runs during replay.
  - One rejected write jams every write behind it until all expire after 24 h.
  - No screen shows the queue's state.
  - A location switch deletes the queue file.
- **Push.**
  - A tap from a closed app is lost because the delegate is set after unlock (CLIENT-7).
  - A signed-out phone still receives pushes, and a token is bound to one restaurant (CLIENT-8 / MOD-NOT-7).
  - Deep-link edge cases (CLIENT-51).
  - A removed teammate still receives pushes (DATA-53 / MOD-NOT-6).
- **Timeouts and background.**
  - The 45 s resource cap overrides the 90 s generation timeout (CLIENT-20).
  - There is no background task for uploads (CLIENT-21).
  - Leaving Labor during a generation orphans it (CLIENT-27), and the poll gives up on the first failure (CLIENT-41).
  - The Ask stream fallback runs the question twice (CLIENT-28).
- **Duplicate actions.**
  - The guest blast has no re-entry guard (CLIENT-1).
  - A failed segments load shows "0 guests" while Send goes to everyone (CLIENT-9).
  - Social publish can post twice (CLIENT-10).
  - Approve can post the old reply (CLIENT-6).
  - Send to staff stays enabled after success (CLIENT-36).
  - Retry-post is dimmed but not disabled (CLIENT-55).
  - "Send it again" OTP resend is not disabled (CLIENT-57).
- **Time zones and locales.**
  - Scheduled posts use the phone's time zone (CLIENT-33).
  - "Today" uses the device's time zone (CLIENT-61).
  - The checklist uses the device date while the server reads UTC (MOD-EMP-5).
  - A comma-decimal price drops the row (CLIENT-31).
- **Errors and state.**
  - 402 and 403 are shown as generic errors behind cached numbers (CLIENT-22).
  - Certificate-pin failures look like cancellations (CLIENT-24).
  - Cancelled loads show as errors in 21 view models (CLIENT-49).
  - Raw system error text is shown (CLIENT-50).
  - Failed loads look like genuine empty states (CLIENT-58).
- **Memory and accessibility.**
  - 48 MP invoice decode on the main thread (CLIENT-39).
  - Two Labor lists clip rows at large Dynamic Type sizes (CLIENT-35).
- **Tests.** The iOS suite passes (114 tests, 0 failures), but none of these cases is tested. There is no UI test target.

## 13. State Management

**Impossible or conflicting state**
- A publish where every email failed leaves a share row, so the portal shows the week as published while `published_at` is NULL (SCHED-9).
- A week published twice keeps both versions live (SCHED-10).
- The shift is changed while its request is still `open` (DATA-17 / SCHED-5).
- Approving with an illegal replacement leaves the shift open to everyone and returns an error (SCHED-20).
- The availability row contradicts itself after an email-link save (CLIENT-11).
- An approve with no draft leaves a review "approved" that will never be drafted (MOD-REV-4).
- A review draft saved in a second tab un-posts a live reply (DATA-26).

**Stale indicators**
- The NEEDS REVIEW blocker never clears (SCHED-18).
- The over-budget blocker uses generation-time hours (SCHED-17).
- An unsatisfiable leader rule shows "weak week" forever (SCHED-30).
- The badge counts rows the viewer cannot see (MOD-NOT-10).
- The status dot shows green on unknown (CLIENT-43).
- `/health` is green on an empty database (DATA-33).
- The scheduler shows a false "outage" during every long pass (DATA-3).
- Freshness is green while Places fails (MOD-REV-1).

**Stale caches**
- Home and Ask use a 60 s snapshot that is not invalidated by uploads, settings or publish (DATA-39).
- iOS Home keeps the previous account's data (CLIENT-2) and the previous location's data (CLIENT-26).
- A location switch clears every tenant's Home cache (MOD-HOME-3).

**Infinite loading and wrong empty states**
- Panels stay on "Loading…" forever on an HTML error (CLIENT-12).
- A job that died in a restart polls `pending` forever (DATA-9 / SCHED-25).
- Failed loads are shown as genuine empty states (CLIENT-58).
- iOS "No urgent reviews" searches only the first 50 reviews (CLIENT-30).

**Duplicate requests and partial updates**
- See section 4.
- The offline queue can jam, crash or be deleted (CLIENT-5).
- The version log drifts from the stored week (SCHED-19 / DATA-57).

## 14. Security Edge Cases

**Cross-tenant (P0)**
- SEC-1 staff identity escalation. SEC-2 location kept after sale.
- MOD-MKT-1: another tenant's media is readable and publishable by integer `media_id` (marketing_publish.py:306, 413).
- MOD-MKT-5: an unsigned OAuth `state` binds an attacker's Meta page to a victim restaurant (social_routes.py:117-125).
- CLIENT-2: a shared iPhone shows the previous account.

**Within-tenant escalation**
- 2FA user-id swap (SEC-5) and backup codes (SEC-6).
- Support writes through view-as (SEC-12).
- An email change rewrites `owner_email` (SEC-13).
- A promoted staff member keeps a PIN (SEC-14).
- The team invite window (DATA-56).
- Module-table gaps let a manager upload inventory, register outbound webhooks, overwrite POS credentials, set review retention and turn on auto-approve (SEC-24).
- Marketing approval is advisory (MOD-MKT-17).
- A view-only labor role can approve time off (SCHED-22).
- Home leaks module alerts the role cannot see (MOD-HOME-1).

**Injection**
- Reflected XSS in `/auth/google/callback` (SEC-9) and `/docusign/callback` (SEC-10), under a CSP that allows `'unsafe-inline'`.
- Stored XSS through a template quote (CLIENT-18) and through the session IP (CLIENT-40).
- HTML injection into emails from names (MOD-EML-2, MOD-LAB-17) and the referral note (SEC-15 / MOD-EML-1).
- Prompt injection through guest text (AI-15, AI-16) and availability notes (SCHED-12).

**Webhooks**
- DocuSign fails open without its secret (SEC-11 / MOD-BIL-7).
- The Svix timestamp is not checked, and the secret is frozen at import (MOD-EML-9).
- The Stripe route has no test (SEC-40).

**Abuse**
- The referral route is an unthrottled relay from will@cavnar.ai (SEC-15 / MOD-EML-1).
- The public opt-in has no verification and a spoofable throttle (SEC-17 / MOD-MKT-9). Legacy bare-id join tokens are accepted, and `tests/test_security.py:277-280` pins that acceptance.
- Competitor refresh starts a thread per press (SEC-31 / DATA-29).
- The 2FA send-test route has no limiter (DATA-42).

**Leaks**
- The Places API key reaches `job_failures`, Sentry and admin JSON (MOD-REV-8).
- A traceback reaches the client (DATA-46 cluster), and raw exception text reaches owners (MOD-UX-2 cluster).
- Competitor menu fetches follow redirects to any host, an SSRF risk (AI-30).
- Session tokens are returned in the JSON body (SEC-36).

**Admin**
- The status endpoints bypass `admin_required` (SEC-23).
- View-as and reset pick an arbitrary login (SEC-28).
- A partial admin save overwrites other fields (SEC-30).
- SECURITY.md contradicts the code (SEC-39).

## 15. Recovery Assessment

**Can the system recover by itself?** Mostly not. Most scheduled work is "claim before work" (ops.py:117-187), so a crash after a claim loses that period's run, and there is no automatic re-run. ops.py:581-587 says so for `daily_alerts`: "a whole day with no alerts at all" (DATA-20). The same applies to the following:

| What is lost | Source |
|---|---|
| Weekly visibility and competitor runs, until next Monday | MOD-INT-1 |
| The weekly plan | AI-17 |
| The 10am batch, which is held in memory | MOD-NOT-16 |
| The digest when a Resend send fails | MOD-EML-8 |
| The Thursday auto-draft cursor | DATA-8 |

Items stuck in a non-final state have no reaper:
- Delayed actions stuck in `running` (DATA-19). `marketing_publish.reap_stuck_publishes` is the pattern to copy.
- Async jobs from a previous process (DATA-9 / SCHED-25).
- Reviews that have used up their AI attempts because of outages (AI-4).

**Can the user recover?** Often not without support:

| Situation | What recovery takes | Source |
|---|---|---|
| Frozen or "not me" account | A DB edit | SEC-7 |
| Wedged PO numbering | A DB edit | MOD-FC-7 |
| CSV upload that replaced good data | Only possible if the owner kept the old file | SEC-16 |
| Hand history overwritten by a POS sync | Only possible if the owner kept the old file | MOD-LAB-18 |
| Paid checkout that never activated | Stripe's retry is dropped | AI-2 cluster, DATA-54 |
| Draft that will not delete | Cannot be done in the app | SCHED-16 / DATA-55 |
| Time-off request that cannot be withdrawn | Cannot be done in the app | MOD-EMP-7 |
| Offline iOS writes | Lost silently | CLIENT-4, CLIENT-5 |

**Data loss and rollback.**
- Backups live only on the same volume. The one off-volume copy is a redacted email attachment built in memory, which stops being possible as the database grows (DATA-18 / MOD-PERF-7).
- A failed snapshot is left behind as "the newest" (DATA-34).
- The documented restore can produce an empty database that passes `integrity_check` (DATA-2).
- After a restore:
  - Claims made after the snapshot are gone, so digests and briefs re-send (DATA-20).
  - `init_db` seeds admin and demo accounts into an empty file (DATA-2).
- Nothing records a version of `shifts_csv` or `inventory_csv`, so there is no rollback for uploads.

**Duplicate work after recovery**
- Guest campaigns and follow-ups leave no record after a crash, so a retry re-texts guests (MOD-MKT-6 cluster, DATA-52 / MOD-MKT-10).
- A held alert whose mark-sent write fails is re-delivered every 5 minutes (DATA-5).
- Two schedulers double-deliver held alerts (DATA-4).
- A restore re-sends digests (DATA-20).

**Manual intervention needed**
- RECOVERY.md relies on hand-typed SQL for claims (DATA-20).
- Setting `must_reset_password` back (SEC-7).
- Account deletion (DATA-61).
- Stripe reconciliation (MOD-BIL-1, MOD-BIL-2).

**What works.**
- WAL mode.
- Consistent snapshots with `integrity_check` (tested).
- A restore drill that proves the newest snapshot (tested; DATA-44 shows it also migrates production).
- The publish claim for scheduled posts (tests/test_publish_claim.py).
- `invoices.apply`, which applies once.
- Idempotent inventory re-imports.

## 16. Missing Tests

Ranked by production risk. Each test is described by what it asserts, the file to put it in, and its type.

1. **Guest SMS campaign sends exactly once.** Two overlapping `send_campaign` calls, and a crashed-then-retried call, each text every guest once (integration, tests/test_guest_marketing.py; MOD-MKT-6 / CLIENT-1 / AI-7 / DATA-13).
2. **Staff identity cannot escalate across restaurants.** A staff identity unlinked at A and signed in by PIN at B is refused every console route and acts at B (integration, tests/test_staff_signup.py; SEC-1). Replace `tests/test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role`.
3. **Owner session stops acting at a removed location** once that location leaves the group (integration, tests/test_location_group_scoping.py; SEC-2).
4. **Authenticated reads survive a write-refusing database.** A GET `/api/home/brief` with a `query_only` connection is not a 500 (integration, tests/test_chaos_recovery.py; DATA-1).
5. **Stripe events survive a failed claim or a failed handler.** A locked claim INSERT is not treated as a duplicate, and a handler that raises after the claim is processed on retry (integration, tests/test_entitlement_and_stripe.py; AI-2 / SEC-27 / MOD-BIL-3 / MOD-BIL-4).
6. **Supplier orders carry every due item.** 30 below-par items produce a 30-line PO, and same-name ingredients at two suppliers produce two groups (unit, tests/test_food_cost_audit_fixes.py; MOD-FC-1, MOD-FC-2).
7. **No-data restaurants show no labor figures and no roster.** Test with `_cached_shifts` not monkeypatched (integration, tests/test_labor_surfaces.py, tests/test_staff_settings_signals_versions.py; MOD-LAB-16, MOD-EMP-1).
8. **Toast 00:30Z keeps the local business date** for a Chicago restaurant (unit, new tests/test_toast.py; MOD-LAB-3).
9. **The staff portal spans published weeks.** With W and W+1 published, Thursday of W still shows Friday to Sunday of W (integration, tests/test_schedule_publish_gate.py; SCHED-2).
10. **A configured closed Monday** saves a 6-day week in one model call (integration, tests/test_schedule_third_audit.py; SCHED-1).
11. **Places non-OK responses fail the fetch.** REQUEST_DENIED and NOT_FOUND raise, and `last_fetched_at` is not stamped (unit and integration, tests/test_chaos_recovery.py; MOD-REV-1 / AI-6).
12. **Toast pagination beyond 20 pages** is either fetched in full or marked partial, and never reported as `ok` (unit, tests/test_toast_sync.py; AI-3 / MOD-LAB-4).
13. **The Anthropic client is bounded**: read timeout of 90 s or less and `max_retries == 0`. The timeout lint flags an unbounded SDK client and `requests` aliases (unit, tests/test_resiliency.py; AI-1, AI-13, DATA-16 / MOD-MKT-2).
14. **Cross-tenant media and OAuth are refused.** A foreign `media_id` is refused, and an unsigned `state` writes no tokens (unit and integration, tests/test_marketing_features.py, tests/test_meta_api.py; MOD-MKT-1, MOD-MKT-5).
15. **2FA and backup codes resist escalation.** An edited pending uid is refused, and a manager is refused backup codes and 2FA disable (integration, tests/test_auth_routes.py, tests/test_permissions.py; SEC-5, SEC-6).
16. **Freeze, then reset, then sign in works, and other sessions are revoked** (integration, tests/test_security.py; SEC-7, SEC-8).
17. **Publishing emails staff once.** Publishing twice, or an auto-publish after a manual publish, emails each staff member once (integration, tests/test_schedule_publish_gate.py; DATA-12 cluster).
18. **Held alerts are delivered once and fairly:**
    - Once under a failed mark-sent write.
    - Once with two runners.
    - At or under the ceiling after a rush.
    - B's hold is released despite A's 300-hold backlog.

    (unit and integration, tests/test_alert_holds.py; DATA-4, DATA-5, MOD-NOT-1, MOD-NOT-2)
19. **Churned or paused restaurants are skipped by every scheduled job.** Iterate `admin_ops.RUNNABLE_JOBS` with every sender stubbed (unit, tests/test_retention.py; DATA-51 / MOD-REV-2).
20. **Real-world spreadsheets are normalised or refused with a row-level message**, never accepted and then broken. Cover M/D/YYYY, title case, BOM, cp1252, NaN, "8h", "$1.80" and required-only columns (integration, new tests/test_client_upload.py; SEC-16 cluster, MOD-FC-5 / SEC-26).
21. **Concurrent open-shift claims have exactly one winner**, and claims refuse a minor past `minor_latest_end`, a missing certification and a wrong role (threaded integration, tests/test_schedule_publish_gate.py; DATA-17 / SCHED-5, SCHED-6).
22. **An async job left by a previous process** is failed at boot whatever its age, and two concurrent starts give one job (unit, tests/test_async_job_store.py; DATA-9 / SCHED-25, DATA-23).
23. **iOS resilience** (iOS unit tests):
    - A 5xx or 429 at launch keeps the session.
    - Sign-out clears Home.
    - `clear()` during a drain does not crash.
    - Approve aborts when the save failed.

    (SessionStoreTests, HomeViewModelTests, PendingWriteQueueTests, ReviewDetailViewModelTests; CLIENT-2, CLIENT-4, CLIENT-5, CLIENT-6)
24. **Web `/api` errors are JSON** for 404, 413 and 500, and every poller checks `document.hidden` and `session_expired` (integration and source-scan, tests/test_frontend_rules.py; MOD-UX-1 / CLIENT-12, CLIENT-13, CLIENT-37).
25. **Quiet hours use the restaurant's timezone** (unit, tests/test_alert_amplification.py; MOD-NOT-4).
26. **A generated schedule with versions can be deleted** (integration, tests/test_schedule_reaudit_fixes.py; SCHED-16 / DATA-55).
27. **The restore swap happens before any connection opens**, and `/health` fails on a database with no `restaurants` table (integration, tests/test_chaos_recovery.py, tests/test_resiliency.py; DATA-2, DATA-33).
28. **A locked database fails the boot migration loudly** (unit, tests/test_models.py; DATA-11).
29. **A review is one row across sources.** Places and GBP copies of the same review produce one row (integration, tests/test_reviews_ai_and_identity.py; MOD-REV-3).
30. **Budget and outage errors do not use up review attempts.** Six passes under `AIBudgetExceeded` leave `analysis_attempts == 0` (unit, tests/test_reviews_audit_remediation.py; AI-4).

**Existing tests that pin the wrong behaviour or miss the case because of their fixture** (must change when the fix lands):
- `tests/test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role` pins SEC-1.
- `tests/test_security.py:277-280` pins acceptance of the legacy bare-id join token (MOD-MKT-9).
- `tests/test_mobile_api.py:885`, and the delete test in test_schedule_reaudit_fixes, build no version row (SCHED-16 / DATA-55).
- `test_a_no_show_becomes_the_managers_issue_once` monkeypatches `coverage_gaps` without the `scheduled` key (MOD-LAB-1).
- `test_attribution_says_nothing_when_there_is_no_pos_data` monkeypatches `daily_sales`, so the real sample fallback is never exercised (MOD-MKT-16).
- `test_roster_is_history_plus_hand_added_minus_deactivated` monkeypatches `_cached_shifts` (MOD-EMP-1).
- `test_a_full_queue_drops_rather_than_exhausting_the_container` asserts the push drop (MOD-NOT-12).
- `test_a_malformed_row_does_not_cost_the_whole_list` pins the silent skip (DATA-43).
- `test_retract_surfaces_the_google_api_error...` asserts raw error text (MOD-REV-15).
- `test_a_closed_day_does_not_break_archive_coverage` must keep passing when MOD-FC-16 is fixed.
- The Stripe webhook route has no test at all, and `stripe` is not installed in the dev interpreter (SEC-40).

## 17. Chaos Testing

Each STEP 17 scenario is below, with a verdict on whether the app fails gracefully and the evidence for it.

**Restaurant opens during an outage** (Railway or database unavailable at open). **Verdict: No.**
- **iOS.** Users who open the app get a 502 on `/me`, are signed out, and lose their offline queue (CLIENT-4).
- **Web.** Users see "check your connection" or panels stuck on "Loading…" (MOD-UX-1 / CLIENT-12).
- **Staff portal.** It cannot load shifts and offers no retry path.
- **Scheduler.** The brief, daily alerts and 10am batch that were claimed before the crash are not re-run for the day (DATA-20, MOD-NOT-16). Held alerts that pass 12 hours late are dropped with only a `print` (MOD-NOT-2). The status page may report a scheduler outage or stay green depending on `/health` (DATA-3, DATA-33).

**A manager edits the schedule while AI generates.** **Verdict: No.** Edits can be silently lost or published half-done.
- Regenerating selected days pins the rows read at job start, so edits saved during the job are not in the new draft (SCHED answers; schedule_engine.py:1877-1893; Medium confidence, not probed).
- A full web Generate discards unsaved edits without asking (SCHED-41).
- Saves are not atomic with their version rows, and the conflict check is optional (SCHED-19 / DATA-57).
- The 11am auto-publish sends whatever was saved during the undo window, with no hash check (DATA-12 / SCHED-28).

**Five employees call off.** **Verdict: No.** Nothing notifies anyone, and concurrent covers corrupt the schedule.
- Drops and open shifts send no push or email to anyone (SCHED-21).
- Concurrent claims lose covers, or give one shift to two people (DATA-17 / SCHED-5).
- Claims skip the minor, certification and role rules (SCHED-6).
- The no-show coverage job crashes (MOD-LAB-1).
- Coverage suggestions ignore role and time off (SCHED-15), and the replacement picker ignores time off (SCHED-14).
- Anyone with any owner note on file is refused every claim, so they can only be moved by a manager (SCHED-35).
- After Friday's publish, current-week shifts cannot be dropped at all (SCHED-2).

**The database slows down.** **Verdict: No.**
- Every request waits on the `last_active` write, up to the 30 s busy timeout, so 4 slow requests saturate the server (DATA-1).
- Long lock holders make it worse: the follow-up job across Twilio calls (DATA-52 / MOD-MKT-10), `claim_period` deletes (DATA-6 / MOD-PERF-3) and `prune_ledgers` (DATA-40).
- Under the lock:
  - Stripe claims read as duplicates and drop payment events (AI-2 cluster).
  - `claim_period` write failures silently stop every scheduled job (DATA-22).
  - Held-alert mark-sent failures re-deliver the same alert every 5 minutes (DATA-5).
  - `finish_async_job` failures leave jobs pending forever (DATA-9).

**Notifications are delayed.** **Verdict: Partly.** The hold mechanism exists and is tested, but losses are silent.
- Held alerts wait behind long scheduler passes (DATA-3 / MOD-PERF-1).
- One restaurant's backlog starves the others until they are dropped as stale after 12 hours, with only a `print` (MOD-NOT-2).
- Pushes beyond a queue of 500 are dropped while the history says sent (MOD-NOT-12 / AI-29).
- `delayed.run_due` handles 20 actions per tick (MOD-PERF-4).
- Twilio failures are not retried (AI-27).

**The Stripe webhook is delayed.** **Verdict: No.**
- A delayed `invoice.paid` after `subscription.deleted` reactivates the customer, and a stale `subscription.updated` restores all four modules (MOD-BIL-1; probe p7).
- A retry after a handler crash is skipped as a duplicate (AI-2 cluster).
- Emailed checkout links expire after Stripe's default lifetime with no way to regenerate them (DATA-38 / MOD-BIL-5).

**AI is partly unavailable.** **Verdict: Mixed.**
- **Graceful:**
  - Ask shows the budget message.
  - Health keyword alerts still fire.
  - A partial visibility run is not recorded.
  - The breaker opens.
- **Not graceful:**
  - Threads are held for up to 600 s per attempt (AI-1).
  - Retries stack to 9 (AI-13).
  - Every non-Ask surface says "check back shortly" (AI-11).
  - Reviews use up their 5 attempts and drop out for good (AI-4).
  - Schedule generation fails after paid calls.
  - The weekly plan is lost for the week (AI-17).

**A CSV import is interrupted.** **Verdict: Partly.**
- The upload is a single request, and `save_client_data` replaces the whole column in one commit.
- The follow-on writes (tenure, `labor_daily_history`, snapshot) are separate commits whose errors are only printed, so an interruption leaves a new CSV with stale history (DATA-62).
- A completed but bad upload has no undo (SEC-16).
- A Toast sync that stops early saves a truncated CSV over the complete one (AI-3 / MOD-LAB-4, MOD-LAB-18).

**Power outage (process killed).** **Verdict: No.** Committed SQLite data survives (WAL), but in-flight work is lost or repeated.
- The scheduler thread is a daemon, so work mid-run is lost:
  - Delayed actions stay in `running` forever (DATA-19).
  - Async jobs stay `pending` (DATA-9 / SCHED-25).
  - A guest campaign mid-send leaves no record and is re-sent on retry (MOD-MKT-6 cluster).
  - The follow-up job re-texts (DATA-52 / MOD-MKT-10).
  - The 10am batch is lost with its claims spent (MOD-NOT-16).
  - A claimed daily job does not run again until its next period (DATA-20).
- A schedule publish mid-loop leaves some staff emailed and no `published_at` (DATA-12).

**Server restart or deploy.** **Verdict: No.**
- iOS users are logged out (CLIENT-4).
- Generations are orphaned and later presses join the dead job (DATA-9 / SCHED-25). The web poll gives up on the first 502 (CLIENT-14 cluster).
- Process-local guards reset:
  - The supplier-order 60 s cooldown (DATA-15).
  - The guest campaign and newsletter limiters (MOD-MKT-6, MOD-EML-3).
  - The AI visibility cache, which is re-bought after every deploy (MOD-INT-5).
- The auto-draft cursor is lost mid-pass (DATA-8).
- A boot migration run under a lock is skipped silently (DATA-11).
- An overlapping container can take the lease mid-pass and double-run the tick (DATA-4).

**High traffic.** **Verdict: No.**
- 4 threads are held by Ask streams and insights (AI-1) and by synchronous fan-outs (MOD-MKT-7, MOD-EML-3, SCHED-29).
- Background pollers add load from every open tab (CLIENT-37), about 2,500 requests/s at 100k open tabs (CLIENT answer 6).
- Every page load moves 1.27 MB uncompressed and performs a write (CLIENT-38).
- Every request writes to the database (DATA-1).
- The scheduler holds the GIL for `get_all_restaurants` (MOD-PERF-2).
- A few decompression-bomb uploads can exhaust memory (MOD-MKT-13).
- A large restaurant's shift change hits the staff-portal per-IP limit (SEC-18).

## 18. Top 100 Edge Cases

**Scoring.**
- Production risk = severity weight × likelihood.
  - Severity weights: P0 = 8, P1 = 4, P2 = 2, P3 = 1.
  - Likelihood: High = 3, Medium = 2, Low = 1. A likelihood given as "Low–Medium" counts as Low.
- Ties are broken in this order:
  1. Customer impact, High before Medium.
  2. Lower effort first, S before M before L.
  3. Higher severity.
- Severities double from one tier to the next so that a Low-likelihood P0 (score 8) ranks with a Medium-likelihood P1, not below the P2s.
- For "at scale" likelihoods the value at 100,000 restaurants is used, as the brief requires.

| Rank | ID(s) | Edge case | Severity | Likelihood | Business impact | Customer impact | Effort | Production risk |
|---|---|---|---|---|---|---|---|---|
| 1 | SCHED-2 | Staff portal reads only the newest published week, so after Friday's publish this week's Fri–Sun shows "off" | P0 | High | No-shows on the busiest days at every portal restaurant | High: staff miss shifts | S | 24 |
| 2 | MOD-LAB-16 | A new restaurant is shown the bundled SAMPLE labor week ("$12,630/mo over target") in the email, Home and gap banner | P0 | High | Fabricated money in customer-facing email | High: first impression is false | S | 24 |
| 3 | MOD-FC-1 | Supplier PO built from display slices: at most 4 critical + 6 reorder lines reach the supplier | P0 | High | Real orders are short | High: kitchen runs out | S | 24 |
| 4 | AI-3, MOD-LAB-4 | Toast sync stops at 20 pages (2,000 entries) silently and overwrites the full CSV | P0 | High | Wrong labor %/OT for the largest clients | High: wrong advice, no caveat | S | 24 |
| 5 | MOD-MKT-6, CLIENT-1, AI-7, DATA-13 | Guest SMS campaign has no claim: a double tap, timeout-retry or crash texts every guest twice | P0 | High | TCPA exposure, A2P filtering, 2× Twilio | High: guests spammed | M | 24 |
| 6 | MOD-LAB-3 | Toast/Square shifts dated by UTC clock-in, so every US dinner shift lands on the next day | P0 | High | Day-level labor advice wrong for every POS client | High: wrong day flagged | M | 24 |
| 7 | SCHED-1 | A restaurant closed on any weekday or holiday can never generate a schedule (fails after 3 paid calls) | P0 | High | Flagship feature unusable for a large segment | High: "Try again" forever | M | 24 |
| 8 | DATA-1 | A full, read-only or locked DB returns 500 on every logged-in request (`last_active` write) | P0 | Medium | Total outage during a "partial" failure | High: every screen fails | S | 16 |
| 9 | MOD-REV-1, AI-6 | Places REQUEST_DENIED/OVER_QUERY_LIMIT read as success; freshness stays green | P0 | Medium | Fleet-wide silent review outage | High: 1-star reviews never alert | S | 16 |
| 10 | SEC-2 | Owner session keeps acting at a location after it leaves the group (sale) | P0 | Medium | Cross-tenant exposure | High: seller reads buyer's data | S | 16 |
| 11 | SEC-1 | Staff identity removed at home restaurant gets its owner console via a PIN sign-in elsewhere | P0 | Medium | Cross-tenant, role-escalated | High: fired staff administers the account | M | 16 |
| 12 | AI-1 | Anthropic client has no timeout (600 s × retries); 4 slow calls take every route down | P0 | Medium | Platform outage | High: whole app hangs | M | 16 |
| 13 | SCHED-4 | Portal hides the first leg of every double shift | P1 | High | Product-caused no-shows | High | S | 12 |
| 14 | SCHED-9 | Publish needs one successful email; portal-only restaurants cannot publish; an email outage half-publishes | P1 | High | Portal and requests unusable | High: misleading error | S | 12 |
| 15 | CLIENT-4 | iOS signs the user out and wipes the offline queue on a 5xx/429/decode error at launch (every deploy) | P1 | High | Churn, lost offline work | High | S | 12 |
| 16 | MOD-EMP-1 | Team roster falls back to sample shifts: eight fictional employees on day one | P1 | High | Trust hit; fixture names in scheduling | High | S | 12 |
| 17 | MOD-FC-5, SEC-26 | Inventory CSV accepted then bricks every Food Cost surface; BOM/cp1252 files rejected | P1 | High | Onboarding blocked, support load | High: "uploaded" then broken | S | 12 |
| 18 | DATA-12, SCHED-28, SCHED-29, CLIENT-36 | Schedule publish not idempotent; auto-publish after manual publish re-emails staff; edits in undo window go out | P1 | High | Staff confusion, mail reputation | High: two schedules | S | 12 |
| 19 | MOD-NOT-4 | Quiet hours evaluated in America/Chicago for every restaurant | P1 | High | Owners disable channels | High: 5am texts | S | 12 |
| 20 | AI-4 | Budget stop/outage uses up each review's 5 AI attempts; reviews never analysed again | P1 | High | Silent data loss in analytics/replies | High: reviews undrafted | S | 12 |
| 21 | MOD-REV-6 | First GBP connect alerts on every historical 1-star and uses up the 50/day ceiling | P1 | High | SMS cost; real alerts suppressed | High: alert flood | S | 12 |
| 22 | MOD-NOT-1 | Alerts held through a rush bypass the 50/day ceiling and the owner cap | P1 | High | Uncapped SMS spend | High: drip of alerts | S | 12 |
| 23 | DATA-21 | TripAdvisor/DoorDash/UberEats imports store nothing (CHECK constraint) but report success | P1 | High | Feature silently loses all data | High | S | 12 |
| 24 | DATA-53, MOD-NOT-6 | A removed teammate's phone keeps receiving the restaurant's pushes | P1 | High | Data exposure after offboarding | High | S | 12 |
| 25 | SEC-8 | Reset and password change never revoke existing sessions (iOS 30-day tokens) | P1 | High | Reset does not end an intrusion | High | S | 12 |
| 26 | SEC-3 | Login/2FA/reset throttles key on client-supplied X-Forwarded-For | P1 | High | Brute force and email bombing | High | S | 12 |
| 27 | MOD-FC-9, DATA-41 | iOS supplier order and schedule publish ignore the send-delay (undo) window | P1 | High | Irreversible sends from the phone | High | S | 12 |
| 28 | MOD-FC-8 | `draft_hash` guard never sent by either client; what is emailed can differ from what was approved | P1 | High | Wrong quantities with owner "approval" | High | S | 12 |
| 29 | CLIENT-11 | Email-link availability save erases the saved note and accepts 7/7 blocked days | P1 | High | Schedules violate stated limits | High | S | 12 |
| 30 | SEC-16, MOD-LAB-10, MOD-LAB-11, MOD-LAB-13, MOD-LAB-14 | Shift CSV with US dates, title-case headers, NaN/inf/negatives or "8h" is accepted, replaces the old data, then breaks Labor | P1 | High | Module down, data replaced | High | M | 12 |
| 31 | DATA-51, MOD-REV-2 | Churned/paused restaurants still fetched, alerted, auto-published and their guests texted | P1 | High | Spend and liability after cancellation | High | M | 12 |
| 32 | AI-5 | `verify_figures` passes invented $ near any context number ("2026"); `$2.4k`/`$1.2M` unchecked | P1 | High | Guard against invented money is weak | High: "$2,000/mo", high confidence | M | 12 |
| 33 | MOD-REV-3 | The same review stored twice via Places and GBP | P1 | High | Double-counted metrics and alerts | High | M | 12 |
| 34 | MOD-FC-4 | Nightly depletion re-sync after a recount subtracts 2 days twice | P1 | High | Over-ordering; waste masked | High | M | 12 |
| 35 | DATA-15, MOD-FC-11 | Supplier-order guard is 60 s, process-local and spent before validation: re-sends after restart; blocks the 2nd supplier | P1 | High | Duplicate deliveries | High | M | 12 |
| 36 | MOD-NOT-7, CLIENT-8 | A device token maps to one restaurant; switching location or lock-screen sign-out misroutes or leaks pushes | P1 | High | Multi-location tier misses alerts | High | M | 12 |
| 37 | DATA-8, SCHED-11 | Thursday auto-draft is one 40-min serial pass; most opted-in restaurants get no draft, silently | P1 | High | Feature silently absent at scale | High | M | 12 |
| 38 | DATA-3, MOD-PERF-1 | One serial scheduler thread: the 3 h fetch stalls undo sends, holds, posts; false "outage" status | P1 | High | Time-critical features late | High | M | 12 |
| 39 | DATA-7 | Review fetch cannot cover 100k restaurants; each is reached every few days | P1 | High | Review SLA fails silently | High | L | 12 |
| 40 | DATA-9, SCHED-25 | Generation killed by a deploy stays "pending" forever; Generate joins the dead job | P1 | High | Wasted paid calls, tickets | Medium | S | 12 |
| 41 | MOD-LAB-1 | No-show coverage job crashes on every restaurant with a schedule (TypeError) | P1 | High | Marketed feature never fired | Medium | S | 12 |
| 42 | AI-8 | Ask's final call drops `tools` while history holds tool blocks (likely 400 on confirm cards) | P1 | High | Action feature may fail | Medium | S | 12 |
| 43 | CLIENT-7 | Push tap that launches the app from closed is dropped | P1 | High | Urgent workflows start with a hunt | Medium | S | 12 |
| 44 | DATA-6, MOD-PERF-3 | `claim_period` runs an unindexed full-table DELETE per call (37–103 ms at 1–2M rows) | P1 | High | Scheduler and write-lock collapse at scale | Medium | S | 12 |
| 45 | MOD-PERF-2 | `get_all_restaurants()` costs ~52 s CPU and ~1 GB at 100k, >30×/hour, holding the GIL | P1 | High | Web latency dominated by scheduler | Medium | S | 12 |
| 46 | MOD-LAB-2 | Toast clock-in names never match schedule names; every employee would read as a no-show | P1 | High | Alert spam once LAB-1 is fixed | Medium | S | 12 |
| 47 | AI-9, MOD-INT-1, MOD-FC-19, MOD-LAB-9, MOD-NOT-11, SCHED-27 | Restaurant-iterating sweeps are serial, unbounded and cursor-less on the scheduler thread | P1 | High | Jobs never finish at scale | Medium | M | 12 |
| 48 | MOD-FC-18 | `inventory_history` grows ~2.5 MB/day per large restaurant, fully parsed on requests | P1 | High | Volume exhaustion; slow Food Cost | Medium | M | 12 |
| 49 | AI-2, SEC-27, MOD-BIL-3, MOD-BIL-4 | Stripe/DocuSign claim treats any DB error as duplicate and returns 200; handler failures after claim never retried | P0 | Low | Paid checkout never activates | High: paid but locked out | S | 8 |
| 50 | MOD-MKT-1 | Any restaurant can publish and read another's photos by integer `media_id` | P0 | Low | Cross-tenant exposure | High | S | 8 |
| 51 | MOD-MKT-5 | Meta OAuth callback trusts unsigned `state=<restaurant_id>` | P0 | Low | Cross-tenant write | High: posts go to a stranger's page | S | 8 |
| 52 | CLIENT-2 | iOS Home shows the previous account's dashboard after sign-out and new sign-in | P0 | Low | Cross-tenant exposure on shared phones | High | S | 8 |
| 53 | SEC-5 | 2FA pending token unsigned; user id swappable within the restaurant | P1 | Medium | Role model bypass | High | S | 8 |
| 54 | SEC-6 | Any console role mints backup codes / disables 2FA; manager to owner in two requests | P1 | Medium | Full in-tenant escalation | High | S | 8 |
| 55 | SEC-9 | Reflected XSS in `/auth/google/callback` under `unsafe-inline` CSP | P1 | Medium | Account takeover primitive | High | S | 8 |
| 56 | SEC-10 | Reflected XSS in public `/docusign/callback` | P1 | Medium | Account takeover primitive | High | S | 8 |
| 57 | SEC-13 | Any login's email change rewrites `owner_email` (2FA channel, groups, billing siblings) | P1 | Medium | Security mail re-routed; location lost | High | S | 8 |
| 58 | SEC-14 | Promoted staff keeps a 4-digit PIN into the console, bypassing 2FA | P1 | Medium | Weak console auth | High | S | 8 |
| 59 | SEC-7 | `must_reset_password` never cleared: freeze / "not me" lock the account permanently | P1 | Medium | DB edit per incident | High: reset loop | S | 8 |
| 60 | SEC-4 | Mobile reset code: no per-account cap, plaintext, `random` | P1 | Medium | Account takeover | High | S | 8 |
| 61 | MOD-MKT-8 | "Stop.", "STOP!", "opt out", full-width/zero-width STOP leave guests subscribed | P1 | Medium | FCC/TCPA exposure | High | S | 8 |
| 62 | CLIENT-6 | iOS approve posts the pre-edit reply when saving the edit failed or was queued | P1 | Medium | Wrong public reply under the brand | High | S | 8 |
| 63 | CLIENT-9 | iOS shows "Goes to 0 guests" after a failed load, but Send goes to everyone | P1 | Medium | Unintended blast | High | S | 8 |
| 64 | CLIENT-3 | 5–8 digit staff PINs can never be entered (pad submits at 4) and failures count to lockout | P1 | Medium | Staff lockouts, tickets | High | S | 8 |
| 65 | SCHED-6 | Claims and swaps skip minor, certification, role and time-window rules | P1 | Medium | Child-labor and alcohol-service breaches | High | S | 8 |
| 66 | DATA-17, SCHED-5 | Two concurrent open-shift claims both "covered"; covers lost | P1 | Medium | Double-booked or empty shifts | High | S | 8 |
| 67 | SCHED-7 | Overtime priced across a Mon–Sun draft, not the payroll week (13× in probe) | P1 | Medium | Wrong money advice | High | S | 8 |
| 68 | SCHED-8 | Hidden 7-server cap removes shifts when no section count is set | P1 | Medium | Understaffed Saturdays | High | S | 8 |
| 69 | MOD-FC-2 | Second same-name ingredient silently dropped from the order | P1 | Medium | Short orders on proteins | High | S | 8 |
| 70 | MOD-FC-10 | Queued/auto supplier order voided at send time is never reported | P1 | Medium | Promised order never sent | High | S | 8 |
| 71 | MOD-FC-16 | Food cost % reads an archive with holes as complete (~11× too high) | P1 | Medium | Confidently wrong headline | High | S | 8 |
| 72 | MOD-BIL-2 | Any refund (even $5 partial) pauses every location in the group | P1 | Medium | Paying customers locked out | High | S | 8 |
| 73 | MOD-NOT-2 | One restaurant's hold backlog starves all others' held alerts until dropped | P1 | Medium | Cross-tenant alert loss | High | S | 8 |
| 74 | MOD-NOT-3 | Combined morning batch gated by the "unresponded" toggles; logged as sent | P1 | Medium | Alerts silently not delivered | High | S | 8 |
| 75 | DATA-5 | Held alert whose mark-sent write fails is re-delivered every 5 min | P1 | Medium | SMS storm on a full volume | High | S | 8 |
| 76 | DATA-16, MOD-MKT-2 | 12 Meta calls without timeout (lint misses `_req`); one hang stops every job | P1 | Medium | Platform-wide job stall | High | S | 8 |
| 77 | DATA-2 | Documented restore on the live container can leave an empty DB that passes `integrity_check` | P0 | Low | Last-resort recovery produces data loss | High | M | 8 |
| 78 | SEC-17, MOD-MKT-9 | Public opt-in: no verification, spoofable throttle, re-subscribes STOPped numbers; bare-id tokens accepted | P1 | Medium | TCPA exposure | High | M | 8 |
| 79 | DATA-18, MOD-PERF-7 | Backups on the same volume; off-site copy redacted, built in memory, will outgrow email | P1 | Medium | Volume loss equals total loss | High | M | 8 |
| 80 | DATA-10 | Location context per session, not tab; a second tab writes into the other location | P1 | Medium | Cross-location corruption | High | M | 8 |
| 81 | MOD-BIL-1 | Stripe events applied in arrival order; delayed events reactivate churned accounts | P1 | Medium | Unpaid access, wrong MRR | High | M | 8 |
| 82 | MOD-MKT-7 | SMS fan-out in-request; quiet hours checked once, sends cross 9pm | P1 | Medium | Thread exhaustion; TCPA window | High | M | 8 |
| 83 | CLIENT-5 | iOS offline queue can crash on replay, jams behind one rejected write, invisible | P1 | Medium | Lost offline work, crashes | High | M | 8 |
| 84 | MOD-FC-12 | Unit mismatch / negative receiving drive stock negative; order grows with the error | P1 | Medium | Absurd orders, COGS inflated | High | M | 8 |
| 85 | SEC-15, MOD-EML-1 | `/api/send-referral` unthrottled relay as Will with raw HTML | P1 | Medium | Domain reputation (2FA mail) | Medium | S | 8 |
| 86 | DATA-52, MOD-MKT-10 | Review-request job holds the write lock across Twilio calls; a crash re-texts | P1 | Medium | Hourly "database is locked" | Medium | S | 8 |
| 87 | CLIENT-10, DATA-25 | Social posts can be published twice (no idempotency; iOS no re-entry guard) | P1 | Medium | Duplicate public posts | Medium | S | 8 |
| 88 | SEC-12 | Read-only support role writes through view-as | P1 | Medium | Unattributable changes | Medium | S | 8 |
| 89 | DATA-4 | Lease renewed only at tick start; second process runs the tick concurrently | P1 | Medium | Duplicate alerts, double AI spend | Medium | S | 8 |
| 90 | DATA-19 | Delayed action killed mid-handler stays `running` forever | P1 | Medium | Silent partial automation | Medium | S | 8 |
| 91 | SCHED-10 | A week published twice keeps both versions live; false hard violations | P1 | Medium | Legal shifts reassigned | Medium | S | 8 |
| 92 | DATA-14, MOD-EML-3 | Newsletter in-request, no send record; retries mail everyone again, unsubscribe tokens clobbered | P1 | Medium | CAN-SPAM exposure | Medium | M | 8 |
| 93 | DATA-20 | Job claims survive a restore and killed runs keep their claim | P1 | Medium | Whole-day gaps or duplicate digests | Medium | M | 8 |
| 94 | SCHED-12 | Employee availability notes enter the prompt as hard-constraint instructions, never checked | P1 | Medium | Prompt injection over hours | Medium | M | 8 |
| 95 | MOD-UX-1, CLIENT-12 | Web `/api/*` errors are HTML; `.json()` throws; "check your connection" / infinite Loading | P2 | High | Incidents misdiagnosed | High | S | 6 |
| 96 | SEC-18 | Staff-portal per-IP throttle counts successes: 16th employee on shared Wi-Fi refused | P2 | High | Shift-start lockouts at large clients | High | S | 6 |
| 97 | MOD-MKT-11 | Guest promos sent on the owner-alert A2P campaign; manual review-request SMS ignores STOP | P2 | High | Owner alerts filtered by carriers | High | S | 6 |
| 98 | DATA-60, MOD-NOT-9 | 48 h no-response alert ignores drafted reviews (the normal case) and fires for soft-deleted ones | P2 | High | Nudge never fires | High | S | 6 |
| 99 | MOD-FC-20 | Web order screen hides unassigned items whenever any supplier group exists | P2 | High | Items silently not ordered | High | S | 6 |
| 100 | CLIENT-17 | Location switcher broken for names with an apostrophe (e.g. "Simple EJ's") | P2 | High | Multi-location web switching dead | High | S | 6 |

**Just below the cut (P1, Low likelihood, score 4):**
- SEC-11 / MOD-BIL-7: DocuSign webhook fails open.
- DATA-54 / MOD-BIL-9: non-atomic provisioning.
- MOD-FC-7: PO numbering wedges.
- MOD-FC-6: NaN in ingredients.
- DATA-11: boot migrations skipped silently.
- MOD-EML-2: unescaped third-party email templates.
- SCHED-3: CSV fallback loses the week.

**Next P2-High items:**
- SCHED-18: NEEDS REVIEW never clears.
- MOD-LAB-6: Square role enum.
- SCHED-17: stale `hours_scheduled`.
- SCHED-21: no swap consent or notifications.
- CLIENT-30: iOS urgent filter covers only the first 50 reviews.

## 19. Findings by priority

P0 and P1 findings are listed in full, in the Top-100 rank order. "Confidence" is the source auditor's confidence. Where this merge confirmed a P0 in code at `f705755`, that is noted.

### P0 Must Fix Before Launch (17)

#### P0-1. The staff portal shows only the newest published week
- **IDs:** SCHED-2 · Severity P0 · Confidence High (confirmed in code) · Likelihood High
- **Files / functions:**
  - staff_schedule.py:26-40 (`_newest_published`), 61-99 (`shifts_for_employee`)
  - shift_requests.py:23-27 (`_published`)
  - scheduler.py:2040-2108 (Friday auto-publish)
- **Evidence:**
  - `_newest_published` selects `... ORDER BY h.generated_at DESC, h.id DESC LIMIT 1`, and `today` plus the 7-day `week` are built only from that row's CSV.
  - Probe p3_db: with W and W+1 published and today being Thursday of W, the result was `today=None` and Thu–Sun all "off". Dropping a W shift was refused with "not on your published schedule".
- **Business impact:** Every restaurant using the portal shows staff "off" on Friday–Sunday every week once next week is published. This is the exact failure the portal exists to prevent.
- **Mitigation:** Merge every published week that overlaps [today, today+7], taking the newest version per `week_start`. `_published` should find the published row that contains the requested date.

#### P0-2. New restaurants are shown the bundled SAMPLE labor week as their own figures
- **IDs:** MOD-LAB-16 · P0 · Confidence High (confirmed) · Likelihood High
- **Files / functions:**
  - labor.py:106-112 (`load_shifts_for_restaurant` falls back to the sample)
  - reporter.py:785-803, 105-116
  - client_api.py:2386-2396 (`/api/labor-gap`)
  - mobile_api.py:814-837
  - dashboard.html:9811-9826
  - marketing_signals.py:40-60
- **Evidence:**
  - These callers skip the `is_live` check: the weekly email's Labor block, the report AI context, `/api/labor-gap`, the mobile Home labor KPI and `daily_sales`.
  - Probe p06_sample on a fresh restaurant: `is_live False pct 36.8`, `monthly_gap 12630.0`. The web banner then reads "that gap is costing around $12,630/mo".
- **Business impact:** Fabricated money figures in customer email and on Home for every new labor signup.
- **Mitigation:** Make `load_shifts_for_restaurant` return `[]` and let the demo path opt in to the sample explicitly, or gate each of these callers on `is_live`. See also P1 MOD-EMP-1.

#### P0-3. Supplier POs are built from display-truncated lists
- **IDs:** MOD-FC-1 · P0 · Confidence High (confirmed) · Likelihood High
- **Files / functions:**
  - inventory.py:510-514 (`critical_low[:4]`, `reorder_soon[:6]`), 1311-1334 (`build_supplier_orders`)
  - client_api.py:5472; mobile_api.py:1823; delayed.py:149-159; ordering.py:84-88
- **Evidence:** The order builder iterates the two sliced display lists. Probe p1_orders: 30 below-par items for one supplier give an order `item_count: 4`, and nothing in the payload says lines were omitted. The trusted auto-order inherits the same truncation.
- **Business impact:** Real purchase orders are short for any restaurant with more than about 10 items to reorder, and the kitchen runs out of items the product "ordered".
- **Mitigation:** Keep the full lists (e.g. `critical_low_all`), slice only in the view, and add `truncated` counts to the display payload.

#### P0-4. The Toast sync silently truncates at 20 pages and overwrites complete data
- **IDs:** AI-3, MOD-LAB-4 · P0 (AI) / P1 (MOD); merged at P0 · Confidence High (confirmed `page_limit = 20` at toast.py:213, 334) · Likelihood High for large restaurants
- **Files / functions:**
  - toast.py:195-239 (`fetch_time_entries`), 303-362 (`fetch_order_selections`), 533-600 (`build_shifts_csv`, `sync_to_db`)
- **Evidence:**
  - 20 pages × 100 = 2,000 entries for a 60-day window. At 500 employees that runs out in 8–13 days.
  - On hitting the cap the loop exits with no flag. `sync_to_db` saves the partial CSV as the whole `shifts_csv`, clears `toast_sync_error` and archives history from it.
  - Order selections have the same cap.
  - Which days are lost depends on Toast's sort order: Unable to verify from implementation.
- **Business impact:** Labor %, overtime and schedule inputs for the largest clients are computed from a fraction of the data and presented as complete. The error also persists into YoY history.
- **Mitigation:** Page per day or per week. When the cap is hit, mark the sync partial and never overwrite a complete prior CSV.

#### P0-5. The guest SMS campaign can text every guest twice
- **IDs:** MOD-MKT-6, CLIENT-1, AI-7, DATA-13 · P0 (MOD, CLIENT) / P1 (AI, DATA) · Confidence High (confirmed) · Likelihood High
- **Files / functions:**
  - guest_marketing.py:622-700 (`send_campaign`)
  - mobile_api.py:2873-2907; client_api.py:4504-4508
  - ai_utils.py:787-797 (process-local limiter)
  - iOS GuestTextClubViewModel.swift:336-360; APIClient.swift:89-90
  - dashboard.html:15295-15336, 7208-7232
- **Evidence:**
  - Eligibility is filtered on `last_campaign_at`. The campaign row, the recipient rows and the `last_campaign_at` stamps are all written in one commit after the loop, while each text is a synchronous Twilio POST with `timeout=10`.
  - The only guard is `ai_rate_limited(..., 3, 300)`, which is in memory and allows 3 sends.
  - iOS times out at 20/45 s, has no re-entry guard and no confirmation. The web and Ask Confirm re-enable after a network error.
  - Probes:
    - Two overlapping sends: 20 of 20 guests texted twice.
    - Client probe: 40 guests received 80 texts.
    - Crash after 10 texts: 0 campaign rows, and the retry re-texted those 10.
- **Business impact:** Per-message TCPA exposure, carrier filtering of the shared A2P campaign (which also carries owner alerts, see MOD-MKT-11), doubled Twilio spend, and no audit record of a crashed send.
- **Mitigation:**
  - Insert the campaign row as `sending` with a client idempotency key (UNIQUE).
  - Insert all recipient rows first, then claim each with `UPDATE ... SET sent_at=? WHERE id=? AND sent_at IS NULL` before `send_sms`.
  - Move the fan-out to a background job with a cursor.
  - On the client, add a confirmation that shows the count, add a re-entry guard, and treat a timeout as "check campaign history".

#### P0-6. Toast and Square shifts are dated by the UTC clock-in, not the business date
- **IDs:** MOD-LAB-3 · P0 · Confidence High (confirmed `entry_date = in_dt.date()` on a UTC-aware datetime, toast.py:490-491) · Likelihood High
- **Files / functions:**
  - toast.py:454-461, 486-507 (`normalise_entries`)
  - square.py:142, 176-183
- **Evidence:**
  - A 7pm CDT clock-in is 00:00 UTC the next day, so the shift is dated tomorrow with a start of "00:00".
  - Toast sales use the local business date, so evening labor is priced against the wrong day. Square also dates orders by `created_at[:10]` in UTC.
  - Affected downstream: day-level labor %, `dow_summary`, overstaffed/understaffed days, payroll-week OT buckets, `daypart_of` and `labor_daily_history`, the forecast input.
- **Business impact:** Systematically wrong day-level labor advice for every POS-connected customer.
- **Mitigation:** Convert to the restaurant's timezone (time_utils) before taking the date and hour, and prefer the POS business date.

#### P0-7. A restaurant closed on any weekday or holiday can never generate a schedule
- **IDs:** SCHED-1 · P0 · Confidence High (confirmed `_missing_dates` and the "wrote no shifts ... twice" raise, schedule_engine.py:413-482) · Likelihood High
- **Files / functions:**
  - schedule_engine.py:411-425, 465-483, 511-522 (`_generate_in_parts`, `_missing_dates`, `_rows_by_date`)
  - labor.py:1910-1920
- **Evidence:**
  - Every date must carry model-written rows. Nothing in `schedule_rules`, `get_close_times` or `demand_signals` can mark a day closed, and the retry prompt tells the model to staff the missing day.
  - Probe p1_pure: `FAILED -> The model wrote no shifts for Monday twice ... model calls: 3`.
  - The same wall applies to one-person rosters open 7 days, to holiday closures and to Thursday–Sunday concepts.
  - If the model instead obeys the retry, it staffs the closed day, and `_ensure_role_floors` adds rows too.
- **Business impact:** The Labor module's main feature is unusable for Monday-closed and brunch-only restaurants and for every holiday week. Each attempt costs 3 or more paid calls, and the Thursday auto-draft fails for these restaurants every week.
- **Mitigation:** Add owner-set closed days and closed dates, remove them from `all_dates` and from the slices, tell the prompt, and skip them in `_ensure_role_floors` / `_top_up_hours_gap`. Treat a missing date as a failure only when history shows that weekday normally trades.

#### P0-8. A full, read-only or locked database fails every logged-in request
- **IDs:** DATA-1 · P0 · Confidence High (confirmed auth.py:2349-2356) · Likelihood Medium
- **Files / functions:**
  - auth.py:2297-2356 (`get_session_user`), 2702-2732 (`login_required`, `mobile_login_required`)
  - docs/ops/RECOVERY.md:159; status_manager.py:257-300
- **Evidence:**
  - Every authenticated request runs `UPDATE sessions SET last_active=...` and `conn.commit()` with no try/except.
  - Probe p8 (`PRAGMA query_only`): `get_session_user RAISES: OperationalError attempt to write a readonly database`.
  - A lock held past the 30 s busy timeout behaves the same way, after a 30 s wait per request, and 4 such requests saturate the server.
  - RECOVERY.md says "Writes fail, reads succeed", and `/health` (`SELECT 1`) stays 200.
- **Business impact:** A total outage during the one failure the runbook calls partial, with the operator misled about the cause.
- **Mitigation:** Make the update best-effort (try/except) and throttle it to once per N minutes per session, which also removes a write from every GET. Correct RECOVERY.md "Volume full".

#### P0-9. Places non-OK status is recorded as a successful fetch
- **IDs:** MOD-REV-1, AI-6 · P0 (MOD) / P1 (AI); merged at P0 because it is a fleet-wide silent ingestion outage with health green · Confidence High (confirmed fetcher.py:12-27) · Likelihood Medium
- **Files / functions:**
  - fetcher.py:12-27 (`fetch_google`)
  - scheduler.py:404-424 (`run_daily_fetch._process_restaurant`)
  - models.py:5648-5656 (`update_last_fetched`); status_manager.py:413; ai_utils.py:815-825
- **Evidence:**
  - Only `raise_for_status()` is checked. REQUEST_DENIED, OVER_QUERY_LIMIT, INVALID_REQUEST and NOT_FOUND come back as HTTP 200 with no `result`, so the fetch returns `[]`.
  - `fetched_ok = True` and `last_fetched_at` is stamped. The call is metered as `status 'ok', cost 0.017`, and nothing is written to `job_failures`.
  - Probes: p1_places_status (MOD) and p_places (AI).
- **Business impact:** One GCP billing or quota problem silently stops review ingestion for every restaurant without GBP OAuth, while the 25-hour staleness check stays green. Places returns only the newest 5 reviews, so the gap is permanent (MOD-REV-11).
- **Mitigation:** Treat any status other than OK/ZERO_RESULTS as a failure (raise with `error_message`, never the URL), meter it as `error`, and show NOT_FOUND to the owner as "your Google listing ID needs updating". Alert ops once per status per day.

#### P0-10. An owner session keeps acting at a location after that location leaves the group
- **IDs:** SEC-2 · P0 · Confidence High · Likelihood Medium
- **Files / functions:**
  - auth.py:2177-2190, 2356-2367, 2397-2405 (`get_session_user`, `switch_active_restaurant`)
  - client_api.py:4706-4728 (`_do_switch_location`); models.py:5827-5870
- **Evidence:**
  - Group membership is checked once, at switch time. `_SESSION_USER_SQL` uses `s.active_restaurant_id` for any `u.role='owner'` on every request without rechecking it.
  - Probe test_probe_group: with L2 moved to another owner, the result was `existing session acts at: 2 role owner`, and `/api/account/team` at L2 returned 200.
  - iOS sessions are exempt from the idle timeout, so this lasts 30 days.
- **Business impact:** Cross-tenant exposure and writes to a sold location's data.
- **Mitigation:** Revalidate `active_restaurant_id` against `get_location_group(...)` per request (cached per request), and clear it on every session when admin edits `location_group` or `owner_email`.

#### P0-11. A staff identity removed from its home restaurant gets that restaurant's owner console
- **IDs:** SEC-1 · P0 · Confidence High (confirmed `_SESSION_USER_SQL` joins at `u.restaurant_id` for non-owners) · Likelihood Medium
- **Files / functions:**
  - auth.py:276 (`users.role DEFAULT 'client'`), 1573-1582 (`claim_staff_name`), 1703-1720 (`create_user`), 2177-2190, 2375-2392 (`get_session_user`)
  - mobile_api.py:4844-4852; staff_routes.py:138-203
  - tests/test_memberships.py:185-193
- **Evidence:**
  - PIN identities keep `users.role='client'`. The membership join is made at the identity's home restaurant, not the one the session was created for, and an inactive home membership falls back to `users.role`.
  - Probe test_probe_staff_escalation:
    - After A unlinks Jordan, a PIN sign-in at B resolves to `restaurant_id=1 role=client`.
    - `/mobile/api/account/staff`, `/team`, `/food-cost/cogs` and `/billing` all return 200.
    - Even while active at both restaurants, a sign-in at B resolves to A.
  - A test pins the fallback.
- **Business impact:** A fired employee reads and administers the restaurant that fired them. Every two-job employee sees the wrong restaurant's schedule.
- **Mitigation:** Set `users.role='employee'` for PIN identities (with a backfill), store the restaurant on staff_pin sessions and join memberships on it, fail closed when no active membership resolves, and rewrite the pinning test. This also fixes SEC-29.

#### P0-12. The Anthropic client has no timeout, so model calls can hold all four request threads
- **IDs:** AI-1 · P0 · Confidence High (confirmed ai_utils.py:424-439) · Likelihood Medium (High at scale)
- **Files / functions:**
  - ai_utils.py:424-508 (`get_client`, `create_with_retry`); inventory.py:1125
  - client_api.py:40-46, 1443-1662 (Ask and insights)
  - railway.json; scripts/check_timeouts.py:3-30
- **Evidence:**
  - The client inherits `Timeout(read=600)` and `max_retries=2`. `create_with_retry` retries timeouts twice more, and only `inventory.py:1125` passes a timeout.
  - The Ask stream's `generate()` holds a request thread across up to 7 model calls, and `_ASK_SLOTS = 8` is higher than the 4 threads.
  - The timeout lint checks only `requests` and `httpx`.
- **Business impact:** During a slow-provider period, or with 4 concurrent Ask questions, the process serves nothing else: login, portal, webhooks and `/health` all stop.
- **Mitigation:** Build the shared client with `Timeout(connect=5, read=45–90)` and `max_retries=0`, add per-route wall-clock budgets, move Ask and insights to the async-job pattern (or a separate bounded pool), and extend the lint to SDK clients (see also AI-13).

#### P0-13. Stripe and DocuSign claims treat any DB error as a duplicate, and handler failures after the claim are never retried
- **IDs:** AI-2, SEC-27, MOD-BIL-3, MOD-BIL-4 · P0 (AI) / P2 (SEC, MOD); merged at P0 (money) · Confidence High (confirmed webhook_routes.py:36-44) · Likelihood Low
- **Files / functions:**
  - webhook_routes.py:22-81 (`_claim_stripe_event`, `_claim_docusign_event`), 271-356, 684-696, 756-760, 852-855
- **Evidence:**
  - Every error from the INSERT, including "database is locked", is caught by `except Exception: claimed = False  # duplicate PK`, so the route returns 200 `{"duplicate": true}`.
  - The claim is committed before processing. Activation (`update_restaurant`, `_apply_module_entitlement`) errors are printed and the route still returns 200, and the DocuSign outer `except` always returns 200.
  - Probes: p_stripe_claim (AI); p7_stripe (c) and p8_claim (MOD).
- **Business impact:** A paid checkout never activates, or a cancellation never revokes access, and nothing alerts anyone.
- **Mitigation:** Treat only `sqlite3.IntegrityError` as a duplicate. Claim, process and mark done in order (or delete the claim on failure), return 5xx so the provider retries, and `ops.capture` every swallowed error.

#### P0-14. Any restaurant can publish and read another restaurant's photos by integer `media_id`
- **IDs:** MOD-MKT-1 · P0 · Confidence High (confirmed joins at marketing_publish.py:306, 413; marketing_drafts.py:74) · Likelihood Low
- **Files / functions:**
  - marketing_publish.py:268-282, 302-314, 409-415 (`schedule_post`, `list_scheduled`, `run_due_posts`)
  - marketing_drafts.py:29-82; mobile_api.py:3077-3082
  - marketing_media.py:119-132 (unused `get_media_token`)
- **Evidence:** `media_id` is stored from the request body unchecked, and the joins `LEFT JOIN marketing_media m ON m.id = s.media_id` have no tenant predicate and return `m.token`. Probes p01 and p02: A scheduled a post with B's media, received B's token, and `run_due_posts` published B's image.
- **Business impact:** Cross-tenant exposure of unpublished marketing photos, enumerable by sequential id.
- **Mitigation:** Validate with `get_media_token(media_id, rid)` in `schedule_post` and `save_draft`, and add `AND m.restaurant_id = s.restaurant_id` to all three joins.

#### P0-15. The Meta OAuth callback trusts an unsigned `state=<restaurant_id>`
- **IDs:** MOD-MKT-5 · P0 · Confidence High (confirmed social_routes.py:117-125) · Likelihood Low
- **Files / functions:** social_routes.py:27-45, 47-152 (`instagram_connect`, `instagram_callback`)
- **Evidence:** The web branch runs `rid = int(state) if state.isdigit()` and then writes `ig_token`, `ig_user_id`, `fb_page_token` and `fb_page_id` to that restaurant. Only the mobile branch verifies a signature, and the callback does not require login.
- **Business impact:** A cross-tenant write. The victim's scheduled posts and metrics sync go to the attacker's page while the owner sees "connected".
- **Mitigation:** Sign web state the same way as mobile (`gmb.sign_mobile_state`), or bind it to the session with a nonce.

#### P0-16. iOS Home shows the previous account's dashboard after sign-out and a new sign-in
- **IDs:** CLIENT-2 (CLIENT-42 folded in) · P0 · Confidence High (confirmed RootView.swift:40, 335-346) · Likelihood Low–Medium
- **Files / functions:**
  - ios/CavnarAI/CavnarAI/RootView.swift:33-41, 335-346, 379
  - HomeViewModel.swift:18, 55-80; SessionStore.swift:392-416
- **Evidence:**
  - `homeViewModel`, `homePath` and `modulesPath` are RootView `@State` and survive sign-out. Sign-in resets only `selectedTab` and Ask.
  - `load()` shows an error only `if summary == nil`, so user B sees A's summary until B's fetch lands, and indefinitely if it fails. `lastLoadedAt` is still A's.
  - An in-flight load may re-save A's summary under the unscoped `home.summary` key after `purgeAll()` (Medium confidence).
- **Business impact:** One tenant's revenue, reviews and labor figures shown to another tenant's user on a shared phone.
- **Mitigation:** Reset the view model and the paths on an `isAuthenticated` change, scope cache keys by user and restaurant, and check a session generation before `cache.save`.

#### P0-17. The documented restore can replace the restored snapshot with an empty database
- **IDs:** DATA-2 · P0 · Confidence High (mechanism reproduced), Medium (production timing) · Likelihood Low
- **Files / functions:**
  - docs/ops/RECOVERY.md:113-115 (restore step 4)
  - models.py:610-615 (`get_conn`); ops.py:446-458; hosted_dashboard.py:835-857
- **Evidence:**
  - The step runs three `railway ssh` commands (`mv`, `rm -f` of `-wal`/`-shm`, `cp`) against the live service. The web threads and the scheduler keep opening `get_conn` throughout.
  - Probe p5, with one connection opened in the gap: after the copy, the database had `tables: ['scheduler_lease']` and `integrity_check: ok`. Boot then seeds admin and demo accounts, and `/health` returns 200.
- **Business impact:** The last-resort restore can silently produce an empty platform that looks healthy, and a second attempt after new writes compounds the loss.
- **Mitigation:** Make the restore a boot-time operation (`RESTORE_FROM=` swapped before the first `get_conn`, scheduler held until the variable is cleared). Add a post-restore assertion that the restaurant count is at least the snapshot's, and make `/health` check the schema and row count (DATA-33).

### P1 High Priority (84)

#### P1-1. The portal hides the first leg of every double shift
- **IDs:** SCHED-4 · Confidence High · Likelihood High
- **Files / functions:** staff_schedule.py:70-99 (`shifts_for_employee`); templates/staff_portal.html:219-236
- **Evidence:** `by_day[d] = s` keeps only the later row for a date, and the portal renders only `today` and `week`. Probe p8_double: a 10:30am–2:30pm plus 5:00pm–10:00pm day showed `today` as 5:00pm only.
- **Business impact:** No-shows on the lunch leg of doubles, caused by the product itself.
- **Mitigation:** Make `by_day` a list per date and render every shift.

#### P1-2. Publishing requires at least one successful email; an email outage leaves a half-published week
- **IDs:** SCHED-9 · Confidence High · Likelihood High
- **Files / functions:** client_api.py:5723-5773 (`_publish_schedule`); staff_schedule.py:26-40; shift_requests.py:23-27
- **Evidence:**
  - `published_at` is stamped only `if sent:`. With no contacts the result is `publish: 200 False Nobody has an email address`, and the portal shows nothing.
  - `create_schedule_share` runs before the send, so when Resend returns 503 a share row exists, the portal shows the week, and `published_at` stays NULL.
  - The shift requests, the payroll tail, outcomes and auto-publish all read `published_at`.
- **Business impact:** A PIN-only staff portal cannot work without email, and an email incident leaves unexplained inconsistent state.
- **Mitigation:** Stamp `published_at` on the owner's publish and treat email as one channel. Create the share only after a send, and return distinct errors for "no contacts" and "all sends failed".

#### P1-3. iOS signs the user out and wipes the offline queue when `/me` returns 5xx, 429 or an undecodable body at launch
- **IDs:** CLIENT-4 · Confidence High · Likelihood High (every deploy window)
- **Files / functions:** ios/.../Core/SessionStore.swift:106-126, 392-416 (`validateStoredSession`, `clearLocalSession`); APIClient.swift:56, 241-253
- **Evidence:** `isRetryable` is only `.offline || .timedOut`. `finish()` maps every status ≥400 to `.server` and decode failures to `.decoding`, and both call `handleSessionExpired()`. That clears the passcode, SecureCache and `PendingWriteQueue`.
- **Business impact:** Every iPhone user who opens the app during a deploy is logged out, and their queued offline writes are lost.
- **Mitigation:** Clear the session only on a 401 carrying `session_expired`, or a 403 `staff_account`. Treat 5xx, 429 and decode failures as retryable.

#### P1-4. The team roster falls back to the bundled sample shifts
- **IDs:** MOD-EMP-1 · Confidence High · Likelihood High
- **Files / functions:**
  - staff_settings.py:195-236, 305-335 (`roster`, `reliability`)
  - models.py:4655-4700, 4830-4848 (`_cached_shifts`, `get_employee_tenure`)
  - strategy_routes.py:820-851
- **Evidence:** Probe p03_roster on a fresh restaurant returned eight fictional names with reliability and tenure, while the signup pool correctly returned `[]`. staff_roster.py's own docstring forbids this. The only test monkeypatches `_cached_shifts`.
- **Business impact:** Fictional people on day one. Owners can rate and deactivate fixtures, and the scheduler sees a roster that is not theirs.
- **Mitigation:** `_cached_shifts` returns `[]` when the data is not live, and the sample is used only for explicit demo accounts.

#### P1-5. Inventory CSVs that pass validation brick Food Cost, and Excel UTF-8 and cp1252 files are rejected
- **IDs:** MOD-FC-5, SEC-26 · Confidence High · Likelihood High
- **Files / functions:**
  - client_api.py:2800-2806, 2848-2863 (`client_upload_data`)
  - models.py:4942-4955 (`save_client_data`)
  - inventory.py:210-221, 1190-1193; inventory_ledger.py:643-656
- **Evidence:**
  - Only the five headers are checked, after lower-casing. `load_inventory` then does `float(r["avg_daily_usage"])` with exact keys.
  - The auto-migration error is only printed, so every read re-parses the CSV and raises.
  - Probe p3_csv raised on each of these: required-only columns (`KeyError 'avg_daily_usage'`), a blank cell, `$1.80`, title-case headers, and `unit_cost=nan` (`TypeError`).
  - A BOM file is rejected with "missing required columns: date", and Latin-1 with "Could not read file".
- **Business impact:** A new Food Cost customer's first action can brick the module under a green "uploaded" message.
- **Mitigation:** Parse with `load_inventory` before saving and reject with a row and column message. Default optional columns to 0, strip `$` and `,`, normalise headers, reject non-finite values, and decode `utf-8-sig` then cp1252.

#### P1-6. Schedule publish is not idempotent
- **IDs:** DATA-12, SCHED-28, SCHED-29, CLIENT-36 · Confidence High · Likelihood High
- **Files / functions:**
  - client_api.py:5663-5815 (`_publish_schedule`)
  - delayed.py:137-141 (`_run_schedule_publish`)
  - scheduler.py:2040-2110 (`run_auto_publish_schedules`)
  - mobile_api.py:1637; iOS PublishScheduleSheet.swift:218, 382-401
- **Evidence:**
  - The send loop always emails, and `published_at` is never checked before sending.
  - The 11am delayed publish neither rechecks that the week is unpublished and unedited nor hashes the CSV. Its `_run_order_send` twin does.
  - Auto-publish can pick a regenerated draft of a week that is already published.
  - Web saves with a delay insert another `delayed_actions` row per click.
  - iOS "Send to staff" stays enabled after success.
  - A kill mid-loop leaves some staff emailed and no `published_at`.
- **Business impact:** Staff receive two, possibly different, schedules for one week.
- **Mitigation:** Claim the publish with `UPDATE ... SET publishing_at=... WHERE id=? AND published_at IS NULL`, record each recipient, and resume rather than re-send. The delayed handler rechecks the edit and publish state and a CSV hash, and skips weeks that are already published.

#### P1-7. Quiet hours are evaluated in America/Chicago for every restaurant
- **IDs:** MOD-NOT-4 · Confidence High · Likelihood High
- **Files / functions:** models.py:6487-6510 (`is_in_quiet_hours`); notify.py:489-491, 1422-1425; issues.py:213, 473; home_brief.py:1038
- **Evidence:** `now = _dt.now(ZoneInfo("America/Chicago"))`, although `count_alerts_today` already uses the restaurant's timezone. Probe (D): an LA restaurant at local 10:41 inside a 10:00–11:00 window returned `False`.
- **Business impact:** Pacific owners are texted from 05:00 to 07:00 despite quiet hours. This is the top reason owners disable a channel.
- **Mitigation:** Use `time_utils.restaurant_now(...)`.

#### P1-8. A budget stop or provider outage uses up every pending review's 5 AI attempts
- **IDs:** AI-4 · Confidence High · Likelihood High
- **Files / functions:** analyser.py:293-329; drafter.py:244-278; scheduler.py:442-511; models.py:3127-3175 (`record_ai_attempt`, `get_pending_analysis`, `get_pending_drafts`)
- **Evidence:** `AIBudgetExceeded` and `AIProviderDown`, which are raised before any API call, still count an attempt. Nothing resets the counter. Probe p_attempts: pending on passes 1–5, then `pending=0` and `unanalysed: 1` even after the budget cleared.
- **Business impact:** After about 1.25 days of outage or budget pause, reviews are never analysed or drafted again, and a DB edit is the only fix.
- **Mitigation:** Do not count retryable, budget or breaker errors, and add an admin "retry stalled" action.

#### P1-9. A history import alerts on every old review
- **IDs:** MOD-REV-6 (MOD-NOT-15 folded in) · Confidence High · Likelihood High
- **Files / functions:** scheduler.py:428-477; gmb.py:271-280; notify.py:1353-1530, 448-475 (`fire_review_alerts`, `_over_alert_ceiling`)
- **Evidence:** Every newly inserted row reaches `fire_review_alerts` with no `review_date` age filter, and one `review.received` webhook fires per review. Probe p9: 300 reviews dated 2019 produced 300 alerts. In production the ceiling stops at 50 and then suppresses genuine alerts that day.
- **Business impact:** The worst first impression on connect day: SMS spend, 1,000 analyses in one pass, and real 1-star alerts suppressed.
- **Mitigation:** Alert and fire webhooks only for reviews less than N days old, treat the first import as a silent backfill, and cap analysis per pass.

#### P1-10. Alerts held through a rush bypass the 50/day ceiling and the owner's cap
- **IDs:** MOD-NOT-1 · Confidence High · Likelihood High
- **Files / functions:** notify.py:457-476, 953-1103 (`blast`, `hold_alert`, `release_due_alerts`, `deliver_alert`); models.py:6569-6586
- **Evidence:** The ceiling counts `alert_log` rows, and held alerts write none until release, when `deliver_alert` "deliberately does NOT re-check" the cap. Probe p1: 120 held, 0 counted, 120 delivered against a ceiling of 50.
- **Business impact:** The backstop added after "one fetch became 180 notifications" fails for half the fetch slots.
- **Mitigation:** Count unsent holds in `count_alerts_today`, or recheck the ceiling at release.

#### P1-11. TripAdvisor, DoorDash and UberEats imports store nothing but report success
- **IDs:** DATA-21 · Confidence High · Likelihood High
- **Files / functions:** models.py:117 (`CHECK(platform IN (...))`), 3023-3110 (`save_reviews`); client_api.py:674-754
- **Evidence:** A `Review(platform="tripadvisor")` violates the CHECK, `save_reviews` returns `(0, [])`, and the route returns `ok=True, imported=N`. Probe p13: 0 rows stored. The dedupe key uses the per-process salted `hash()`.
- **Business impact:** Every use of the feature silently loses the customer's data.
- **Mitigation:** Rebuild the table with the new platforms, error when `new == 0` and rows were rejected, and use a sha256 `external_id`.

#### P1-12. A removed teammate's phone keeps receiving the restaurant's pushes
- **IDs:** DATA-53, MOD-NOT-6 · Confidence High · Likelihood High
- **Files / functions:**
  - auth.py:1939-1977 (`revoke_team_member`); admin_routes.py:204-209
  - push.py:126-140, 685-700 (`get_device_tokens`, `fire_push`)
- **Evidence:** Revocation sets `is_active=0` and deletes sessions but never touches `device_tokens`. `get_device_tokens` has no join on `users.is_active`, and `fire_push(user_ids=None)` fans out to every token. iOS unregisters only on its own sign-out. No test covers this.
- **Business impact:** Data exposure (reviews, health alerts) to people the owner deliberately removed.
- **Mitigation:** Delete or park the user's tokens on revoke and deactivate, and join `users.is_active=1` in `get_device_tokens`.

#### P1-13. Password reset and password change never revoke existing sessions
- **IDs:** SEC-8 · Confidence High · Likelihood High
- **Files / functions:**
  - models.py:5027-5040 (`consume_reset_token`)
  - mobile_api.py:356-363, 4204-4225; auth_routes.py:540-560; admin_routes.py:803-852
  - auth.py:2152-2168 (iOS exempt from the idle timeout)
- **Evidence:** None of these paths touches `sessions`. Probe: `attacker ios session alive after web reset: True`.
- **Business impact:** A reset does not end an intrusion, and an attacker keeps a 30-day bearer token.
- **Mitigation:** Delete all sessions and trusted devices on reset, and all but the current one on a password change, and notify the owner.

#### P1-14. Throttles key on the client-controlled first X-Forwarded-For value
- **IDs:** SEC-3 · Confidence High (code), Medium (depends on Railway appending) · Likelihood High
- **Files / functions:** auth_routes.py:24-27 (`_get_client_ip`), 304, 411; mobile_api.py:208-228, 275-285, 314-325, 473-477; admin_routes.py:1867; client_api.py:4600, 5065
- **Evidence:** The throttles use `request.headers["X-Forwarded-For"].split(",")[0]` instead of the ProxyFix-set `remote_addr`. Probe: 40 wrong reset codes gave 36 × 429 normally and 0 × 429 with a rotating header. `_clear_attempts` on any success also clears the IP key.
- **Business impact:** No per-IP brute-force protection on the 2FA and reset codes, and forgot-password becomes an email-bombing tool against Resend reputation.
- **Mitigation:** Use `remote_addr`, add per-account keys, and stop clearing the IP key on success.

#### P1-15. The iOS supplier-order and schedule-publish twins skip the send-delay (undo) window
- **IDs:** MOD-FC-9, DATA-41 · Confidence High · Likelihood High
- **Files / functions:** client_api.py:5490-5503, 5806-5810; mobile_api.py:1637, 1794-1880; strategy_routes.py:665-679
- **Evidence:** The web queues through `delayed.schedule` when `send_delay_minutes > 0`, but `send_delay_minutes` does not appear in mobile_api.py. The mobile copy also returns raw `str(e)` and skips `outcomes.observe`. Only the web path is tested.
- **Business impact:** The undo guarantee fails on the surface most prone to mis-taps.
- **Mitigation:** Route both twins through one shared body that includes the delay branch.

#### P1-16. The `draft_hash` guard is inert because neither client sends it
- **IDs:** MOD-FC-8 · Confidence High · Likelihood High
- **Files / functions:** client_api.py:5474-5481; mobile_api.py:1825-1829; dashboard.html:5244-5246; iOS SupplierOrderViewModel.swift:74-90
- **Evidence:** The check runs only `if expected`. The web body is `{supplier_email}` and the iOS `SendBody` is the same. The draft is rebuilt at send time.
- **Business impact:** The quantities emailed can differ from what the owner reviewed, while the audit trail says the owner approved them.
- **Mitigation:** Send the hash from both clients and require it (409 when absent).

#### P1-17. Saving availability from the email link erases the note and accepts 7 of 7 days blocked
- **IDs:** CLIENT-11 · Confidence High · Likelihood High
- **Files / functions:** templates/staff_schedule.html:69-85; client_api.py:5053-5085; models.py:4880-4898; staff_routes.py:414-436
- **Evidence:** The note input is not pre-filled, and the upsert writes `notes=excluded.notes`. Probe: after an email-link save, `notes: None`, with a self-contradictory `available_days` and `unavailable_days`. The portal refuses 7/7 with a 400; the link accepts it.
- **Business impact:** Stated limits disappear and the next schedule violates them.
- **Mitigation:** Pre-fill and preserve the note, and share one validation function between the two surfaces.

#### P1-18. Shift CSVs pass validation, replace the previous dataset, then break Labor
- **IDs:** SEC-16, MOD-LAB-10, MOD-LAB-11, MOD-LAB-13, MOD-LAB-14 · Confidence High · Likelihood High
- **Files / functions:**
  - client_api.py:2782-2900 (`client_upload_data`); admin_routes.py:577-605
  - models.py:4909-4930, 5389-5402 (`save_client_data`, `compute_blended_rate`)
  - labor.py:13-18, 305-324, 410-421, 474, 488, 519
- **Evidence:**
  - Validation lowercases the headers only for its own check; the analysis reads raw keys and parses `%Y-%m-%d`. Probes:
    - `9/14/2026` raises `ValueError`.
    - `Date,Employee,...` raises `KeyError('day')`.
    - `nan`/`inf` produce `labor_pct inf`, which serialises to invalid JSON `Infinity`.
    - An 8h row plus a −8h row produce 0% labor, "on target".
    - `"8h"` makes `compute_blended_rate` raise.
  - The post-save analysis is inside `except: pass`, so the upload says "success".
  - Each upload replaces `shifts_csv` wholesale, and formula-leading cells are stored verbatim.
- **Business impact:** One Excel export destroys the good dataset and breaks Labor, and recovery needs the old file.
- **Mitigation:**
  - Normalise the headers (BOM, case) once and pass normalised rows downstream.
  - Parse every row before saving: several date formats; finite hours between 0 and 24; finite, non-negative sales.
  - Reject what analysis cannot read, and keep the previous version with an undo.

#### P1-19. Churned and paused restaurants are still fetched, analysed, alerted and auto-published, and their guests are still texted
- **IDs:** DATA-51, MOD-REV-2 (MOD-BIL-8 folded in) · Confidence High · Likelihood High
- **Files / functions:**
  - webhook_routes.py:542-561; scheduler.py:288-291, 487-491, 1669-1713, 2569-2627
  - models.py:5636-5646; notify.py:1739-1823, 2033-2040
  - guest_marketing.py:926, 1017-1028; marketing_publish.py:409-415; delayed.py:89-100; pos.py:85
- **Evidence:**
  - Cancellation only sets `billing_status='churned'`.
  - The fetch selector, auto-approve, weekly Intel, `notify` (which has no `billing_status` reference at all), the opt-in invites, review-request follow-ups (confirmed: the query at guest_marketing.py:1018-1028 has no billing predicate), posts, delayed actions and POS sync all ignore billing status. Probes p2_churned and p6_churn.
  - The owner is 402-blocked, so they cannot turn any of this off.
- **Business impact:** Paid API and SMS spend on non-customers, replies published to a former customer's Google listing with no contract in force, and texts to their guests.
- **Mitigation:** One `active_restaurant_ids()` used by every job. Cancel pending delayed actions and posts on churn.

#### P1-20. `verify_figures` passes invented dollar figures and never checks K/M suffixes
- **IDs:** AI-5 · Confidence High · Likelihood High
- **Files / functions:** ai_guard.py:176-262 (`_numbers`, `unsupported_figures`, `verify_figures`); ask_cavnar.py:62, 1037, 1233-1246; reporter.py:516-529
- **Evidence:** The corpus includes every bare numeral (the year 2026, ids, times) with a ±2% tolerance, and `_MONEY_RE` stops at the digits. Probe p_verify: "$2,000 a month", "$1,990", "$2.4k", "$1.2M" and "2,400 dollars" all passed. Guest text also feeds the corpus.
- **Business impact:** The only automated defence against invented money, used by the digest email, Ask confidence and UNVERIFIED markers, is weakest exactly where saving claims land.
- **Mitigation:** Build the corpus from labelled figures only, parse k/K/M/"thousand", tighten the tolerance, and exclude guest text.

#### P1-21. The same review is stored twice when it arrives through both Places and GBP
- **IDs:** MOD-REV-3 · Confidence High · Likelihood High
- **Files / functions:** fetcher.py:52-66; gmb.py:329-339; models.py:114-148, 3061-3105; scheduler.py:393-404
- **Evidence:** The two sources use different `external_id` schemes on the same platform, and the only dedupe is the UNIQUE key. The GBP-failure fallback repeats this on every outage day. Probe p3: two rows for the same guest review.
- **Business impact:** Double-counted metrics, double analysis and double alerts. The Places copy cannot be posted to Google.
- **Mitigation:** Use a source-independent identity (author, createTime to the second, rating, text hash), and have the GBP insert adopt an existing Places row.

#### P1-22. A nightly re-sync after a recount subtracts about 2 days of sales twice
- **IDs:** MOD-FC-4 · Confidence High · Likelihood High
- **Files / functions:** scheduler.py:895-901; inventory_ledger.py:54-77, 174-223, 310-358 (`compute_daily_depletion`, `_compute_current_stock`, `record_recount`)
- **Evidence:** The re-sync deletes and re-inserts depletion rows for 3 days, giving them new ids, and current stock subtracts events with `id > recount.id`. Probe p2: a true count of 80 read as 60, and the next recount inferred 0 waste.
- **Business impact:** Understated stock after every count leads to over-ordering, and the real shrink is masked.
- **Mitigation:** Order by `event_date` relative to the recount, or upsert with stable ids.

#### P1-23. The supplier-order re-send guard is 60 s, process-local, and spent before validation
- **IDs:** DATA-15, MOD-FC-11 · Confidence High · Likelihood High
- **Files / functions:** client_api.py:5372-5430, 5466-5505 (`_order_send_allowed`, `_send_supplier_orders`); mobile_api.py:1794-1880; ordering.py:29-55
- **Evidence:**
  - `_order_send_last = {}` is keyed by restaurant and is lost on every deploy, so a retry after 61 s or a restart re-sends. Duplicate POs also count toward `supplier_trust`.
  - The cooldown is recorded before the draft is built, so:
    - The second supplier's Send is blocked for 60 s.
    - A 400 or 409 still burns the cooldown.
    - The check is not atomic across threads (probe: 1 in 2000).
- **Business impact:** Duplicate deliveries, and multi-supplier restaurants blocked from sending their week's orders.
- **Mitigation:** A durable claim, one open PO per (supplier, draft hash), set only after a successful send, with an explicit "send again".

#### P1-24. A device token belongs to one restaurant, and sign-out or a location switch leaks or misroutes pushes
- **IDs:** MOD-NOT-7, CLIENT-8 · Confidence Medium–High · Likelihood High (multi-location owners)
- **Files / functions:**
  - push.py:65-76, 98-116, 440-443; mobile_api.py:2071-2104; client_api.py:4706-4728
  - iOS PushManager.swift:76, 213-238, 274; SessionStore.swift:379-387
- **Evidence:**
  - `apns_token` is UNIQUE with a single `restaurant_id`, and a location switch never re-registers.
  - The lock-screen "Sign out" sends `apns_token: nil`.
  - Unregister is scoped to the current restaurant, so after a switch it returns a 404 that is swallowed.
  - The payload carries no `restaurant_id`.
- **Business impact:** Multi-location owners get one location's alerts, and a signed-out phone keeps receiving operational alerts.
- **Mitigation:** Key tokens by (token, restaurant) or fan out group-wide, persist the token in the Keychain and always send it on logout, unregister by token and user, and include `restaurant_id` in the payload.

#### P1-25. The Thursday auto-draft is one 40-minute serial pass, so most opted-in restaurants silently get no draft
- **IDs:** DATA-8, SCHED-11 · Confidence High · Likelihood High (at scale)
- **Files / functions:** strategy_jobs.py:413-506 (`run_auto_draft_schedules`); scheduler.py:2312-2318, 2040-2110; ops.py:421
- **Evidence:** Claimed once per Thursday, with `_run_schedule_job` running inline. Measured generations took 80–196 s, so a pass drafts about 12–30 restaurants, and the cursor resumes next Thursday (and is lost on a mid-pass deploy). Friday auto-publish skips silently, and the 40-minute pass exceeds the 1,800 s lease window.
- **Business impact:** "Draft next week for me" does nothing for most opted-in owners, and Thursday stalls every other job.
- **Mitigation:** Use a bounded worker pool off the loop thread with per-restaurant claims, re-run hourly through Thursday, and push "couldn't draft" to anyone not reached.

#### P1-26. One serial scheduler thread lets long passes stall every minute-level duty
- **IDs:** DATA-3, MOD-PERF-1 · Confidence High · Likelihood High (at scale)
- **Files / functions:** scheduler.py:176-180, 2196-2531 (`scheduler_loop`, `FETCH_MAX_SECONDS=3*3600`); strategy_jobs.py:413-472, 573-585; status_manager.py:26, 167-177; morning_brief.py:34, 637-661
- **Evidence:**
  - The fetch (up to 3 h, four times a day) and auto-draft (40 min) run inline. `release_due_alerts`, `issues.tick`, `morning_brief.run_due`, `delayed.run_due` and `run_due_posts` run after them, and the heartbeat comes last.
  - Consequences:
    - Posts publish hours late or fail after 6 h.
    - The 16:00–18:00 pulse window never fires for Central restaurants.
    - The status page shows "outage" after 20 minutes.
- **Business impact:** Time-critical features silently degrade as the customer base grows.
- **Mitigation:** Separate the minute-level duties onto their own thread with its own lease, run the fetch in a pool outside the tick with a heartbeat, and track liveness by tick start.

#### P1-27. The review fetch cannot cover 100k restaurants
- **IDs:** DATA-7 · Confidence Medium (per-restaurant time not measured in code) · Likelihood High (at scale)
- **Files / functions:** scheduler.py:176-180, 279-600, 2348-2353 (`run_daily_fetch`, `bounded_map`, `_fetch_order`)
- **Evidence:** Throughput is 6 workers × 10,800 s / t. At t ≥ 1 s that is at most 64,800 per pass, and at t = 5 s it is 12,960, so each restaurant is reached every ~2 days. The only signal is an ops digest line.
- **Business impact:** The core review-response SLA fails silently, and 1-star alerts arrive days late.
- **Mitigation:** Decouple fetch from AI work (a queue), scale workers to the backlog, and alert on fetch age past the SLA.

#### P1-28. A generation killed by a restart stays "pending" forever, and Generate joins the dead job
- **IDs:** DATA-9, SCHED-25 · Confidence High · Likelihood High
- **Files / functions:** ops.py:228-355 (`sweep_stale_jobs`, `active_job`, `read_async_job`, `finish_async_job`); hosted_dashboard.py:818-824; client_api.py:2455-2471; dashboard.html:10980-10990
- **Evidence:** The boot sweep fails only jobs older than 10 minutes, so a job killed 2 minutes in survives. Probe p3: `boot sweep swept: 0`, and `active_job` returned the zombie at 9 minutes. The web then waits 15 minutes and promises "It will appear in Schedule History", which is false.
- **Business impact:** Wasted paid generations and "schedule never came" tickets after every deploy.
- **Mitigation:** Store a boot or process id on the job, fail every pending job from a previous boot, and have `read_async_job` fail jobs older than the maximum runtime.

#### P1-29. The no-show coverage job crashes on every restaurant with a schedule
- **IDs:** MOD-LAB-1 · Confidence High · Likelihood High
- **Files / functions:** strategy_jobs.py:632-636 (`run_coverage_check`); intraday.py:193-194 (`coverage_gaps`); tests/test_intraday.py:172-176
- **Evidence:** `coverage_gaps` returns `"scheduled": len(...)`, an int, and the job iterates it, raising `TypeError: 'int' object is not iterable`, which is swallowed. Probe p01: `{'opened': 0}`. The only test's fixture omits the key.
- **Business impact:** A shipped, marketed feature has never fired, and the ops log fills with TypeErrors.
- **Mitigation:** Return the rows, and test through the real `coverage_gaps`. Fix MOD-LAB-2 first or at the same time.

#### P1-30. Ask Cavnar's final call after a tool round drops `tools`
- **IDs:** AI-8 · Confidence Medium (the live API response is unverified) · Likelihood High
- **Files / functions:** ask_cavnar.py:1043-1053, 1143-1167 (`ask_with_tools`); strategy_jobs.py:256-257
- **Evidence:** The proposal branch and the rounds-exhausted branch call `create_with_retry` without `tools` while `messages` still holds `tool_use` and `tool_result` blocks. Probe p_ask_tools: `call 2: tools passed=False ... history_has_tool_blocks=True`.
- **Business impact:** Confirm-card turns may 400 ("Couldn't get an answer"). At minimum, each such turn misses the prompt cache.
- **Mitigation:** Pass the same `tools` with `tool_choice={"type":"none"}`.

#### P1-31. A push tap that launches the app from closed is lost
- **IDs:** CLIENT-7 · Confidence High · Likelihood High
- **Files / functions:** ios/.../Push/PushManager.swift:76-78, 293-313; RootView.swift:419-422
- **Evidence:** `center.delegate = self` is set only inside `requestAuthorizationAndRegister`, after sign-in and unlock. `didFinishLaunchingWithOptions` does not set it.
- **Business impact:** Urgent alerts do not take the owner to the item.
- **Mitigation:** Set the delegate and categories in `didFinishLaunching`, and hold the route in DeepLinkRouter until unlock.

#### P1-32. `claim_period` runs an unindexed full-table DELETE on every call
- **IDs:** DATA-6, MOD-PERF-3 · Confidence High · Likelihood High (at scale)
- **Files / functions:** ops.py:117-176; morning_brief.py:653; scheduler.py:1022-1048; strategy_jobs.py:560; notify.py:1727
- **Evidence:**
  - Every call runs `CREATE TABLE IF NOT EXISTS`, the INSERT, and `DELETE ... WHERE claimed_at < -45 days`. The only index is the PK.
  - Probes: 37 ms per claim at 1M rows (about 62 minutes per 100k pass), and 103 ms at 2M rows (about 172 minutes).
  - The write lock is held during the scan.
- **Business impact:** Scheduler throughput collapses and request writes queue behind it.
- **Mitigation:** Index `claimed_at`, prune once a day in `prune_ledgers`, and create the table at boot.

#### P1-33. `get_all_restaurants()` costs about 52 s of CPU and 1 GB at 100k restaurants and runs more than 30 times an hour
- **IDs:** MOD-PERF-2 · Confidence High · Likelihood High (at scale)
- **Files / functions:** models.py:5609-5633, 2666-2760 (`_restaurant_from_row`); morning_brief.py:644; strategy_jobs.py:36-38; scheduler.py:2455-2471, 1961
- **Evidence:** Probe: `N=100000 load=51.66s peak_mem=996MB`. `Row.keys()` is called about 190 times per row, and `SELECT *` pulls every blob. It runs every tick plus six intraday passes every 20 minutes, inside the web process and under the GIL.
- **Business impact:** Web latency is dominated by the scheduler, with OOM risk.
- **Mitigation:** Compute `keys = set(row.keys())` once per row, select only the needed columns and rows, and page with a cursor.

#### P1-34. Toast clock-in names never match schedule names
- **IDs:** MOD-LAB-2 · Confidence High · Likelihood High (once MOD-LAB-1 is fixed)
- **Files / functions:** toast.py:471-474, 701-716 (`normalise_entries`, `fetch_clock_ins_today`); intraday.py:169-188
- **Evidence:** History uses "Maria G.", the live feed uses "Maria Garcia", and the match is exact. The clock-in window is the UTC day.
- **Business impact:** Daily false "hasn't clocked in" issues and SMS for every Toast employee.
- **Mitigation:** One shared name formatter or the Toast GUID, and a local-day window.

#### P1-35. Restaurant-iterating sweeps are serial, unbounded and cursor-less on the scheduler thread
- **IDs:** AI-9, MOD-INT-1, MOD-FC-19, MOD-LAB-9, MOD-NOT-11, SCHED-27 · Confidence High · Likelihood High (at scale)
- **Files / functions:**
  - scheduler.py:830-915, 1669-1900, 1717-1810, 2238-2280 (visibility, competitor, depletion, snapshots, diagnoses, stale check, POS sync, daily alerts)
  - strategy_jobs.py:237-285, 509-521 (weekly plan, recipe drafts, outcomes)
  - ops.py:374-391 (`_pplx_wait_turn`)
- **Evidence:**
  - Each job is `for r in get_all_restaurants()` with no pool, time bound or `job_cursors` cursor, against CLAUDE.md's "bounded and resumable" rule. `ops.run_job` runs inline.
  - Estimates:
    - Visibility is 8 queries per restaurant at 1.3 s pacing, about 12 days per pass at 100k.
    - Diagnoses are one Sonnet call per restaurant per day.
    - Outcomes re-read 12 weeks per restaurant, about 10^7 queries.
  - Claims are taken first, so a restart loses the rest of the period.
  - Several of these jobs also skip no churned accounts.
- **Business impact:** Jobs never finish at scale, and Monday and 5am chains block fetches, alerts and briefs.
- **Mitigation:** The `run_daily_fetch` shape (pool, deadline, cursor), spread weekly work by `id % 7`, and give interactive checks priority on the Perplexity gate.

#### P1-36. `inventory_history` grows without bound and is fully parsed on the request path
- **IDs:** MOD-FC-18 · Confidence High · Likelihood High
- **Files / functions:** food_cost_intelligence.py:94-148; scheduler.py:1769-1810; waste_trend.py:142-218; cogs.py:223; inventory.py:855-866
- **Evidence:** One row per restaurant per day carries the full `items_json`, and nothing prunes it. Probe p9 (5,000 items): 2.5 MB per day, `load_waste_history(limit=None)` 2.44 s, `build_food_cost_pct` 1.91 s, and a 306 MB database after 4 months. That is about 2.7 TB a year at 100k restaurants.
- **Business impact:** Volume exhaustion, backup blow-ups, and the slowest Food Cost for the largest customers.
- **Mitigation:** Store a slim `items_json`, prune to weekly rows after 8 weeks, and pass `limit` or date ranges.

#### P1-37. The 2FA pending token is unsigned and its user id can be swapped
- **IDs:** SEC-5 · Confidence High · Likelihood Medium
- **Files / functions:** auth_routes.py:249-256, 309-322, 369-378; mobile_api.py:263-266, 488-535
- **Evidence:** The token is base64 `rid:uid:secret`, with the secret stored per restaurant. Verification checks only that the named user is active at the same restaurant, and `must_reset_password` is not rechecked. Probe: `verify with swapped uid -> True session user: owner`.
- **Business impact:** Any login that can complete 2FA becomes any other login at the restaurant, including a frozen one.
- **Mitigation:** Store `hash(uid:secret)` or sign the token, and recheck `must_reset_password`.

#### P1-38. Any console role can mint backup codes and enable or disable 2FA
- **IDs:** SEC-6 · Confidence High · Likelihood Medium
- **Files / functions:** mobile_api.py:4550-4573; client_api.py:5986-6001; auth_routes.py:530-537; models.py:3407-3440
- **Evidence:** There are no permission checks, and codes are per restaurant. Probe: a manager minted 10 codes, swapped the pending uid, and got an owner session. Regenerating silently invalidates the owner's codes. The web toggle is not logged.
- **Business impact:** Full within-tenant escalation available to every manager.
- **Mitigation:** Require TEAM_INVITE plus a fresh password, bind codes to a user, and log every change.

#### P1-39. Reflected XSS in `/auth/google/callback`
- **IDs:** SEC-9 · Confidence High · Likelihood Medium
- **Files / functions:** auth_routes.py:759-767 (`gmb_callback`); security_headers.py:4-12
- **Evidence:** `error` is concatenated into `postMessage({...msg:'" + msg + "'})`. Probe payload `x'});alert(document.domain);//` executes, and the CSP allows `'unsafe-inline'`. Script can then read `csrf_js`.
- **Business impact:** An account-takeover primitive from one link: turn off 2FA, invite a login, add a webhook.
- **Mitigation:** `json.dumps` the value, or drop it.

#### P1-40. Reflected XSS in the public `/docusign/callback`
- **IDs:** SEC-10 · Confidence High · Likelihood Medium
- **Files / functions:** webhook_routes.py:698-712 (`docusign_callback`)
- **Evidence:** `<p>Error: {error}</p>` is built from `request.args`, with no authentication, on the dashboard origin. Probe: the script is present in the response.
- **Business impact:** As P1-39, without needing the victim on a specific page.
- **Mitigation:** Escape the value, or return a static page.

#### P1-41. Any login's email change rewrites the restaurant's `owner_email`
- **IDs:** SEC-13 · Confidence High · Likelihood Medium
- **Files / functions:** auth_routes.py:563-600; mobile_api.py:4230-4270; models.py:5827-5870; webhook_routes.py:204-251
- **Evidence:** Both twins update `restaurants.owner_email` regardless of role. Probes: a manager became `owner_email`, and the owner's group shrank to `[L1]`, so switching to L2 returned "Location not in your group". Stripe sibling propagation splits the same way.
- **Business impact:** 2FA codes and alerts re-routed to the manager, a location lost, and billing no longer propagating.
- **Mitigation:** Update only when the caller is the principal whose email equals the current `owner_email`, notify the old address, and key groups on `organization_id`.

#### P1-42. A staff member promoted to manager keeps a PIN that opens the console
- **IDs:** SEC-14 · Confidence High · Likelihood Medium
- **Files / functions:** mobile_api.py:4749-4808; auth.py:536-591, 974-1054; staff_routes.py:138-176
- **Evidence:** After promotion the member disappears from the staff list, but a PIN login still returns 200, the Bearer token reaches `/mobile/api/labor/team`, and the role resolves to manager. The session lasts 14 h with no 2FA.
- **Business impact:** Console access protected by a 4-digit PIN, invisible to the owner.
- **Mitigation:** Clear `pin_hash` on promotion, and refuse PIN sign-in for any role other than employee.

#### P1-43. `must_reset_password` is never cleared, and "not me" is a GET
- **IDs:** SEC-7 · Confidence High · Likelihood Medium
- **Files / functions:** security.py:196-205; auth.py:2923-2956 (`clear_must_reset_password` has no production caller); models.py:5027-5040, 4966-4974; auth_routes.py:1086-1087
- **Evidence:** Probe: freeze, then a completed reset, then mobile login returns 403 "needs a password reset". Admin reset does not clear the flag either. `/auth/not-me/<token>` is a side-effecting GET inside login emails, which link-scanning gateways may trigger (which gateways do: Unable to verify from implementation).
- **Business impact:** The documented takeover response locks every login permanently, and recovery is a DB edit.
- **Mitigation:** Clear the flag on every reset path, make not-me a POST confirmation, and add an admin "unfreeze".

#### P1-44. The in-app 6-digit reset code has no per-account attempt cap
- **IDs:** SEC-4 · Confidence High · Likelihood Medium
- **Files / functions:** mobile_api.py:275-370 (`mobile_forgot_password`, `mobile_reset_password`)
- **Evidence:**
  - `random.randint`, stored in plaintext (probe printed the code), valid for 1 h, and never burned by wrong guesses.
  - Failures are recorded by IP only.
  - HIBP is called before the code check, so every guess also uses a request thread.
- **Business impact:** Takeover of any owner whose email is known, limited only by throughput.
- **Mitigation:** Hash with `secrets`, burn the code after 5 misses per email, and check the code before HIBP.

#### P1-45. STOP handling misses common revocation phrasings
- **IDs:** MOD-MKT-8 · Confidence High · Likelihood Medium
- **Files / functions:** guest_marketing.py:300-302, 405-418 (`handle_inbound_sms`)
- **Evidence:** Only the first word is read, with no punctuation stripping. Probe p05: "Stop.", "STOP!", "Please stop", "opt out", "remove me", zero-width and full-width STOP all left `unsubscribed=0`, with no reply sent. Whether Twilio Advanced Opt-Out catches these: Unable to verify from implementation.
- **Business impact:** Texts continue after a revocation (the FCC 2024 order lists "opt out"), with per-message damages.
- **Mitigation:** NFKC-normalise, strip zero-width characters and punctuation, match keywords anywhere in the message, and send a confirmation.

#### P1-46. iOS approve publishes the old reply when saving the edit failed or was queued
- **IDs:** CLIENT-6 · Confidence High · Likelihood Medium
- **Files / functions:** ios/.../Features/Reviews/ReviewDetailViewModel.swift:112-150, 212-252; client_api.py:96-189; models.py:3262
- **Evidence:** `approve()` calls `saveDraft()` and then posts the approve unconditionally, so the server posts the stored old draft. A timed-out approve is queued and replayed, and `_do_approve` is not idempotent (see the P2 approve cluster).
- **Business impact:** The wrong public reply is posted under the restaurant's name.
- **Mitigation:** Abort or queue the approve behind a successful save, and add a server posted-state guard.

#### P1-47. iOS shows "Goes to 0 guests" when segments fail to load, but Send goes to everyone
- **IDs:** CLIENT-9 · Confidence High · Likelihood Medium
- **Files / functions:** ios/.../GuestTextClubViewModel.swift:77, 97-115; GuestTextClubView.swift:147-199; guest_marketing.py:507-552
- **Evidence:** A failed segments load silently leaves `selectedSegmentCount ?? 0` with segment "all", and an unknown segment falls back to everyone. Send is disabled only while sending, and there is no confirmation, unlike the web.
- **Business impact:** An unintended blast to the whole list.
- **Mitigation:** Disable Send on a load failure or a zero count, show Retry, and add a confirmation with the count.

#### P1-48. Staff with 5–8 digit PINs can never sign in
- **IDs:** CLIENT-3 · Confidence High · Likelihood Medium
- **Files / functions:** templates/staff_login.html:236-255, 480-486; iOS StaffLoginView.swift:179-181; auth.py:647-648, 764-780; staff_routes.py:672-692
- **Evidence:** The pad auto-submits at 4 digits while the server accepts 4–8. Probe: a 6-digit PIN change returned 200, the 4-digit pad submit returned 401, and each failure counts toward the 15-minute lockout.
- **Business impact:** Recurring staff lockouts that are hard to diagnose.
- **Mitigation:** Add a submit key, or enforce exactly 4 digits everywhere.

#### P1-49. Open-shift claims and swaps skip the minor, certification, role and time-window rules
- **IDs:** SCHED-6 · Confidence High · Likelihood Medium
- **Files / functions:** schedule_engine.py:2469-2489 (`replacement_is_legal`); shift_quality.py:2130-2225; shift_requests.py:195-203, 252-254
- **Evidence:** Only `can_work` and the swap index are checked. Probe: a minor claimed a Friday 6pm–1am Bartender shift, and a later sweep flags `minor_late` but nothing runs that sweep after a claim. Open shifts are listed to every role.
- **Business impact:** Child-labor and alcohol-certification breaches created through Cavnar's own portal.
- **Mitigation:** Run `schedule_rules.violations` on the trial rows and require the role or a cross-trained role.

#### P1-50. Two staff can claim the same open shift
- **IDs:** DATA-17, SCHED-5 · Confidence High · Likelihood Medium
- **Files / functions:** shift_requests.py:185-269 (`claim`, `approve_swap`, `_execute_swap`); models.py:5159-5191; staff_routes.py:576-595
- **Evidence:** A SELECT for open requests, a whole-CSV rewrite, then `UPDATE ... SET status='covered' WHERE id=?` with no guard. Barrier probes: two claimants of one shift both got "covered" and the row shows one of them; claims on two different shifts made one cover vanish. A kill between the two writes leaves the request stuck.
- **Business impact:** Two people turn up for one shift, or nobody does.
- **Mitigation:** Claim atomically with `WHERE status='open'` and a rowcount check, compare-and-swap the CSV, or use `BEGIN IMMEDIATE`.

#### P1-51. Overtime is priced across the Monday–Sunday draft instead of the payroll week
- **IDs:** SCHED-7 · Confidence High · Likelihood Medium
- **Files / functions:** schedule_engine.py:2086-2093; schedule_economics.py:60-88 (`priced_cost`); schedule_rules.py:528-581
- **Evidence:** `base_hours` sums every bucket into one 40 h ceiling. Probe with a Wednesday payroll: the engine reported 26 h and a $260 premium; the truth is 2 h. Daily overtime (`daily_ot_hours`) is never priced.
- **Business impact:** Phantom overtime and an inflated "over budget" push managers to cut staff.
- **Mitigation:** Price per (person, payroll bucket) from `c.base_hours`.

#### P1-52. A hidden 7-server cap removes shifts when no section count is configured
- **IDs:** SCHED-8 · Confidence High · Likelihood Medium
- **Files / functions:** schedule_engine.py:1254-1383, 2019-2026 (`_trim_server_overlap_cap`)
- **Evidence:** `_SERVER_MAX_OVERLAP = 7` is the fallback, and the model is never told about it. Probe p9: 14 Saturday servers became 7, and `backstop_trimmed_dates` is rendered nowhere.
- **Business impact:** Thin Saturday nights for large dining rooms, with no trace.
- **Mitigation:** Cap only when `section_count` is set, and report every trim.

#### P1-53. A second ingredient with the same name is dropped from the order
- **IDs:** MOD-FC-2 · Confidence High · Likelihood Medium
- **Files / functions:** inventory.py:1311-1319; food_cost_intelligence.py:281-301; models.py:1586-1609
- **Evidence:** The order deduplicates by display name, although `supplier_comparison` models dual-sourcing. Probe: two "Chicken Breast" rows at two suppliers produced 1 line.
- **Business impact:** Silent short orders on dual-sourced proteins.
- **Mitigation:** Deduplicate by ingredient id.

#### P1-54. A queued or auto supplier order voided at send time is never reported
- **IDs:** MOD-FC-10 · Confidence High · Likelihood Medium
- **Files / functions:** delayed.py:113-159; strategy_jobs.py:906-933; ordering.py:80-106; inventory.py:1365-1371
- **Evidence:** A hash mismatch returns `ok: False` and is stored as `failed` with no push, feed item or capture. The hash covers every supplier group, so any count or date change voids them all. The owner was already told "goes out in an hour".
- **Business impact:** A delivery that never arrives.
- **Mitigation:** Hash per supplier group, and notify the owner through the same channel that announced the order.

#### P1-55. Food cost % treats a sales archive with holes as complete
- **IDs:** MOD-FC-16 · Confidence High · Likelihood Medium
- **Files / functions:** cogs.py:89-173, 292 (`_archived_net_sales`, `build_food_cost_pct`); models.py:5405-5433
- **Evidence:** Coverage is judged only by the first and last archive dates. Probe p7: net sales 2,000 against a true ~23,000, labelled "Over target" (about 11× too high).
- **Business impact:** A confidently wrong headline number feeding diagnoses and the brief.
- **Mitigation:** Require a row count close to the open-day count (allowing a regular closed weekday).

#### P1-56. Any refund, even a $5 partial one, pauses every location in the group
- **IDs:** MOD-BIL-2 · Confidence High · Likelihood Medium
- **Files / functions:** webhook_routes.py:456-513; models.py:5665-5666; auth.py:2464-2476
- **Evidence:** `charge.refunded` sets `paused` with no amount check. Probe: a $5 refund on $750 paused both locations. `invoice.payment_failed` then lifts a chargeback pause to `past_due`.
- **Business impact:** A goodwill credit locks a paying multi-location customer out.
- **Mitigation:** Pause only on disputes or full refunds, and never move a paused or churned account to `past_due`.

#### P1-57. One restaurant's hold backlog starves every other restaurant's held alerts
- **IDs:** MOD-NOT-2 · Confidence High · Likelihood Medium
- **Files / functions:** notify.py:985-1030 (`release_due_alerts`)
- **Evidence:** The release uses `ORDER BY id LIMIT 200` and skips rows over the per-restaurant cap, but those rows still consume the window. Probe p5: B's single hold was reached at about 12.2 h and dropped. Stale drops are only printed.
- **Business impact:** A cross-tenant alert loss that grows with volume.
- **Mitigation:** Select per restaurant (row_number) or page by cursor, and record dropped holds.

#### P1-58. A combined morning batch is gated by the "unresponded review" toggles
- **IDs:** MOD-NOT-3 · Confidence High · Likelihood Medium
- **Files / functions:** notify.py:1119-1311 (`_deliver_combined`, `deliver_alert`)
- **Evidence:** `daily_briefing` is missing from `type_map`, so it falls back to `al_unres_*`. Probe: with those toggles off there was no push and no email, while every item was logged as sent.
- **Business impact:** The more that is wrong, the less the owner hears, and the 7-day suppression then hides the repeat.
- **Mitigation:** Add `"daily_briefing": (None, None, None)`, and log only what was delivered.

#### P1-59. A held alert whose mark-sent write fails is re-delivered every 5 minutes
- **IDs:** DATA-5 · Confidence High · Likelihood Medium
- **Files / functions:** notify.py:992-1040 (`release_due_alerts`, `_mark_sent`)
- **Evidence:** `_mark_sent` runs after delivery with no guard. Probe p11: 3 deliveries of one alert over 3 ticks. The 50/day ceiling cannot help because `alert_log` also fails to write.
- **Business impact:** An SMS storm on a full volume.
- **Mitigation:** Claim with a conditional UPDATE before delivering, and do not deliver if the claim fails.

#### P1-60. Twelve outbound calls have no timeout, and the lint misses the `_req` alias
- **IDs:** DATA-16, MOD-MKT-2 · Confidence High · Likelihood Medium
- **Files / functions:** scripts/check_timeouts.py:41; scheduler.py:941-956 (`refresh_expiring_tokens`); social_routes.py:68, 83, 90, 97, 181, 198, 206, 419; admin_routes.py:1757, 1773
- **Evidence:** An AST scan with alias resolution finds 12 calls, and the lint prints "OK". `refresh_expiring_tokens` runs daily in the scheduler thread, and `_do_post_to_instagram` is reached from the scheduler through `run_due_posts`.
- **Business impact:** One black-holed Graph connection stops every background job platform-wide, and four hung posts take the web worker.
- **Mitigation:** Add `timeout=` to all 12 calls, and resolve `import requests as X` in the lint.

#### P1-61. The public guest opt-in has no verification and re-subscribes numbers that texted STOP
- **IDs:** SEC-17, MOD-MKT-9 · Confidence High · Likelihood Medium
- **Files / functions:** client_api.py:4593-4630; guest_links.py:23-36; guest_marketing.py:187-245, 419-425 (`_upsert_contact`)
- **Evidence:**
  - Anyone can submit any phone with `consent: true`, and `_upsert_contact` sets `unsubscribed=0`. Probe: a number that had texted STOP was re-subscribed.
  - The throttle is process-local and keyed on the left-most XFF value.
  - `ALLOW_LEGACY_JOIN_LINKS` defaults to 1, so `/api/public/guest-optin/<id>` reaches every restaurant. tests/test_security.py:277-280 pins this.
- **Business impact:** TCPA exposure, and A2P suspension that would also stop OTP and alert texts.
- **Mitigation:** Double opt-in (YES reply), never clear `unsubscribed` from the web form, a durable throttle on `remote_addr` and the phone, and turn legacy links off.

#### P1-62. Backups live only on the same volume, and the off-site copy is redacted and built in memory
- **IDs:** DATA-18, MOD-PERF-7 · Confidence High (code), Medium (provider attachment limit) · Likelihood Medium
- **Files / functions:** scheduler.py:1094-1283 (`backup_db`, `_write_consistent_snapshot`, `_redact_snapshot`, `_prune_old_backups`); docs/ops/RECOVERY.md:67-90
- **Evidence:**
  - Local snapshots sit in `dirname(DB_PATH)/backups`.
  - The emailed copy nulls every credential.
  - `f.read()`, then Fernet, then base64 holds about 2.8–4× the database size in RAM in the web process at 2am.
  - Past the attachment limit the off-site copy stops existing.
- **Business impact:** Losing the volume means losing all data. Before that, it means losing every integration.
- **Mitigation:** Stream encrypted, unredacted snapshots to object storage, and alert when the off-site copy is skipped.

#### P1-63. Location context is per session, not per tab
- **IDs:** DATA-10 · Confidence High · Likelihood Medium
- **Files / functions:** auth.py:2357-2366, 2397-2405; client_api.py:4706-4730
- **Evidence:** A switch updates `sessions.active_restaurant_id`, and every tab shares the cookie. No write carries the restaurant the page was rendered for. A poll started in the other tab returns 404.
- **Business impact:** Cross-location corruption of settings, uploads and schedules.
- **Mitigation:** Send the rendered `restaurant_id` with writes and return 409 on a mismatch.

#### P1-64. Stripe events are applied in arrival order
- **IDs:** MOD-BIL-1 · Confidence High · Likelihood Medium
- **Files / functions:** webhook_routes.py:399-454, 542-625
- **Evidence:** No handler compares `event["created"]` or re-reads the subscription. Probe p7: after `subscription.deleted`, a delayed `invoice.paid` set the account active, and a stale `subscription.updated` restored all modules. The "first payment" email also fires.
- **Business impact:** Churned customers regain the full product, and MRR is overstated.
- **Mitigation:** Store the last-applied `created` per restaurant, or re-read the subscription from Stripe on each event.

#### P1-65. Campaign fan-out runs inside the HTTP request, and quiet hours are checked only once
- **IDs:** MOD-MKT-7 · Confidence High · Likelihood Medium
- **Files / functions:** guest_marketing.py:129-135, 652-665; notify.py:129-135; mobile_api.py:2898
- **Evidence:** One Twilio POST (10 s timeout) per guest inside the request. `guest_sms_allowed_now` is evaluated before the loop, and every contact is loaded into memory.
- **Business impact:** A large list pins 25% of platform capacity, and sends cross the 9pm TCPA window.
- **Mitigation:** Enqueue the campaign, send in bounded batches with a cursor, and recheck quiet hours per batch.

#### P1-66. The iOS offline write queue can crash on replay, jams behind one rejected write, and shows no state
- **IDs:** CLIENT-5 · Confidence High (structure), Medium (crash timing) · Likelihood Medium
- **Files / functions:** ios/.../Core/PendingWriteQueue.swift:66-125 (`drain`, `clear`, `dropWrites`); SessionStore.swift:354, 364, 405-408
- **Evidence:**
  - Actor reentrancy lets `clear()` run mid-drain, and then `removeFirst()` on an empty array is fatal.
  - Any error, including a permanent 4xx, leaves the entry at the head, and everything expires after 24 h.
  - `pendingCount` has no readers, and `purgeAll()` deletes the queue file.
- **Business impact:** Lost offline work and crashes on sign-out.
- **Mitigation:** Remove by id, drop and report permanent 4xx, surface the queue state, and exclude the queue file from `purgeAll`.

#### P1-67. Unit mismatch and negative receiving drive stock negative, and nothing clamps it
- **IDs:** MOD-FC-12 · Confidence High · Likelihood Medium
- **Files / functions:** inventory_ledger.py:226-242, 346-353, 866-904; admin_routes.py:474-487; inventory.py:155-171, 287-304
- **Evidence:** Probes: receiving −500 gave stock −485, then a suggested order of 498 and a stock value of −2,425, which feeds COGS.
- **Business impact:** Absurd real orders and an inflated food cost %.
- **Mitigation:** Clamp at 0 for ordering and valuation, flag the ingredient, refuse non-positive receiving, and add a depletion sanity check.

#### P1-68. `/api/send-referral` is an unthrottled open relay as Will, with raw HTML
- **IDs:** SEC-15, MOD-EML-1 · Confidence High · Likelihood Medium
- **Files / functions:** admin_routes.py:1862-1909 (`send_referral`); mobile_api.py:5590-5594
- **Evidence:** The "10 per hour" limit is only a comment (`ip` is unused). The route is `@login_required` only, `note`, `owner_name` and `referrer` are interpolated raw, and it sends through the direct Resend SDK, bypassing suppression.
- **Business impact:** Phishing from will@cavnar.ai. Complaints land on the domain that carries 2FA and reset mail.
- **Mitigation:** Escape the fields, validate the address, a DB-backed limit per user, principal-only access, and route through `emails.deliver`.

#### P1-69. The review-request job holds the SQLite write lock across its whole Twilio loop
- **IDs:** DATA-52, MOD-MKT-10 · Confidence High · Likelihood Medium
- **Files / functions:** guest_marketing.py:1003-1081 (`run_review_request_followups`); models.py:611
- **Evidence:** The first UPDATE opens a transaction, and the single commit comes at the end. Probe p06: another writer got "database is locked" after 0.58 s. A crash rolls back every stamp, so guests are re-texted. Failures are also stamped as sent.
- **Business impact:** Platform-wide write failures on the hour, and duplicate guest texts.
- **Mitigation:** Claim and commit per guest before sending, and bound each run with a cursor.

#### P1-70. Social posts can be published twice
- **IDs:** CLIENT-10, DATA-25 · Confidence Medium–High · Likelihood Medium
- **Files / functions:** ios/.../MarketingViewModel.swift:475-491; MarketingView.swift:489-537; social_routes.py:154-215, 401-425; gmb.py:598-660; marketing_publish.py:246-280
- **Evidence:** iOS `publish()` has no re-entry guard and stays enabled after posting. The immediate publish routes have no claim, and Instagram takes 20+ s. `schedule_post` has no UNIQUE constraint.
- **Business impact:** Duplicate public posts.
- **Mitigation:** A client `request_id` with a UNIQUE index, disable after posting, and "check your page" wording on timeout.

#### P1-71. The read-only support role can write through view-as
- **IDs:** SEC-12 · Confidence High · Likelihood Medium
- **Files / functions:** auth.py:2737, 2349-2356; admin_routes.py:1248-1290
- **Evidence:** View-as mints an ordinary client session. Probe: support turned 2FA off through it (200). SECURITY.md promises "every admin write returns 403".
- **Business impact:** Unattributable client changes by support staff.
- **Mitigation:** Record the opener's role, and refuse non-GET requests for support view-as sessions.

#### P1-72. The scheduler lease is renewed only at tick start, so a second process runs the same tick
- **IDs:** DATA-4 · Confidence High · Likelihood Medium (with worker.py or an overlapping container)
- **Files / functions:** ops.py:421-464; scheduler.py:2210; docs/ops/RAILWAY_SCHEDULER_SPLIT.md:42-58; hosted_dashboard.py:860-872; notify.py:992-1030
- **Evidence:** A pass longer than 1,800 s leaves the heartbeat stale, and the standby takes over. Probe p11: two runners made 2 deliveries of one held alert. The docs call the two-process setup "safe".
- **Business impact:** Duplicate SMS, email and push, and double AI spend, in the recommended configuration.
- **Mitigation:** Heartbeat from inside long passes, and claim each hold before delivering.

#### P1-73. A delayed action that dies mid-handler stays `running` forever
- **IDs:** DATA-19 · Confidence High · Likelihood Medium
- **Files / functions:** delayed.py:63-132 (`run_due`, `pending`); marketing_publish.py:402-407, 514
- **Evidence:** The scheduler is a daemon thread, and nothing re-selects `running` rows. `pending()` hides them from the feed. `marketing_publish` has the reaper this lacks.
- **Business impact:** Silent partial automation, such as half the staff emailed.
- **Mitigation:** `reap_stuck_actions` to fail and alert `running` rows older than 15 minutes, run first.

#### P1-74. A week published twice keeps both versions live
- **IDs:** SCHED-10 · Confidence High · Likelihood Medium
- **Files / functions:** schedule_rules.py:528-581 (`_published_tail`); client_api.py:5750-5768; schedule_intel.py:47-60, 200-248
- **Evidence:** An older published row is never cleared. Probe p5: Ana's superseded shifts fed `base_hours` and produced false `over_max_hours` and `rest_gap` violations, which the fix pass then acts on. Outcomes and the fairness ledger count the week twice.
- **Business impact:** Legal shifts reassigned and false publish blockers.
- **Mitigation:** Supersede older rows on publish, and read only the newest per `week_start`.

#### P1-75. The newsletter has no send record, and concurrent sends break unsubscribe links
- **IDs:** DATA-14, MOD-EML-3 · Confidence High · Likelihood Medium
- **Files / functions:** guest_email.py:89, 138-226 (`send_newsletter`); mobile_api.py:3197-3214; emails.py:313-420
- **Evidence:** A synchronous `deliver` per subscriber inside the request, with a process-local limit of 2 per 10 minutes and no per-recipient marker. Concurrent sends overwrite `email_token`, so the other batch's `/e/<token>` links die. Resend 429s are retried only twice.
- **Business impact:** Duplicate newsletters, broken unsubscribe links (CAN-SPAM), and partial sends with no resume.
- **Mitigation:** Persist the newsletter and its recipients, send from a background job in batches with per-recipient claims, and write tokens with `WHERE email_token IS NULL`.

#### P1-76. Job claims survive a restore, and a run killed after its claim loses its period
- **IDs:** DATA-20 · Confidence High · Likelihood Medium
- **Files / functions:** ops.py:123-187, 578-607 (`claim_period`, `stuck_jobs`); docs/ops/RECOVERY.md:125-139
- **Evidence:** Claim-before-work, as the code itself notes: "a whole day with no alerts at all". `stuck_jobs` only reports. After a restore, claims made after the snapshot are gone, so sent digests re-send, and RECOVERY offers one hand-typed INSERT.
- **Business impact:** Whole-day alert gaps, or duplicate client email, fixed with SQL under pressure.
- **Mitigation:** Leased claims with `finished_at`, and a restore hook that rebuilds today's claims from `job_runs`, `email_log` and `alert_log`.

#### P1-77. Employee availability notes reach the prompt as a hard-constraint instruction and are never checked
- **IDs:** SCHED-12 · Confidence High · Likelihood Medium
- **Files / functions:** staff_routes.py:414-438; labor.py:1822-1843; schedule_rules.py:474-489
- **Evidence:** A 300-character note is appended verbatim under "This is a hard constraint". `c.notes` is never read by code.
- **Business impact:** An employee can steer hours through the model, while real stated limits are silently broken.
- **Mitigation:** Quote the note as untrusted data, and move structured limits to `time_windows`.

#### P1-78. The DocuSign webhook fails open without its secret, and a forged "completed" replaces the owner's password
- **IDs:** SEC-11, MOD-BIL-7 · Confidence High (code); whether the secret is set in production: Unable to verify from implementation · Likelihood Low
- **Files / functions:** webhook_routes.py:720-736, 776-840; docs/ops/SECURITY.md:14
- **Evidence:** `if ds_secret:` wraps the HMAC check. Probe with the secret unset: an unsigned completed event returned 200, the contract was marked signed, and the owner password was replaced. The Stripe route passes an empty secret straight to `construct_event` with no check.
- **Business impact:** A contract marked signed without a signature, and the owner locked out.
- **Mitigation:** Fail closed when the secret is unset, add a boot check, and reset the password only for never-logged-in users.

#### P1-79. Stripe checkout provisioning is non-atomic and runs after the event is claimed
- **IDs:** DATA-54, MOD-BIL-9 · Confidence High · Likelihood Low
- **Files / functions:** provisioning.py:37-81 (`provision_from_checkout`); webhook_routes.py:297, 369-397; auth.py:18-30
- **Evidence:** `create_restaurant`, `create_user` and `update_restaurant` are separate commits. Probe (e): `create_user` raising left an orphan `trial` restaurant with 0 users, and Stripe's retry is dropped as a duplicate. An existing owner buying a second location is never provisioned.
- **Business impact:** "I paid and never got my login", which the automatic path cannot repair.
- **Mitigation:** One transaction, and mark the event seen only after commit.

#### P1-80. Purchase-order numbering can wedge permanently after a void
- **IDs:** MOD-FC-7 · Confidence High · Likelihood Low
- **Files / functions:** models.py:7641-7695 (`record_purchase_order`, `void_purchase_order`); client_api.py:5396-5430; delayed.py:144-159
- **Evidence:** The number is `COUNT(*)+1`, and a failed email deletes its PO. Voiding a PO that is not the newest makes every later allocation collide five times and raise. Probe p5 confirmed. The route blames "the supplier addresses".
- **Business impact:** Ordering is dead for that restaurant until an engineer edits the table.
- **Mitigation:** Allocate from `MAX(...)+1`, or mark voided POs instead of deleting them.

#### P1-81. NaN reaches `ingredients` through owner-facing routes and crashes analysis
- **IDs:** MOD-FC-6 · Confidence High · Likelihood Low
- **Files / functions:** invoices.py:368-379 (`apply`); strategy_routes.py:356-361; admin_routes.py:360-367; inventory.py:275, 1171-1181
- **Evidence:** NaN passes both comparisons and is stored as NULL, so `waste * unit_cost` raises `TypeError` on every analysis. `applied_json` returns invalid JSON (`NaN`).
- **Business impact:** One value takes down a restaurant's Food Cost and its jobs until someone edits the row by hand.
- **Mitigation:** Check `math.isfinite` on every numeric write, and coerce NULL to 0 on read.

#### P1-82. A lock at boot silently skips migrations, and the app serves on the drifted schema
- **IDs:** DATA-11 · Confidence High · Likelihood Low
- **Files / functions:** models.py:868-874, 1029-1031, 2312-2316; hosted_dashboard.py:785-835; docs/ops/RECOVERY.md:46-47
- **Evidence:** Each migration is `try: ... except Exception: pass`. Probe p6: an 8 s lock made `init_db` return normally with the column missing. The boot block wraps 17 inits in one try that only prints, and RECOVERY claims a failed migration "will loop".
- **Business impact:** Routes return 500 until the next deploy, looking like an application bug.
- **Mitigation:** Swallow only "duplicate column", use a 30 s timeout, exit non-zero on init failure, and add a schema check to `/health`.

#### P1-83. Names are interpolated unescaped into third-party emails and used raw in the From header
- **IDs:** MOD-EML-2 · Confidence High · Likelihood Low–Medium
- **Files / functions:** emails.py:949-975, 1210-1365 (supplier order, staff schedule, team invite); client_api.py:3346-3378; guest_email.py:205; mobile_api.py:401-470
- **Evidence:** Probe p07: an anchor in the restaurant name renders as a live link, and `getaddresses` parses the From header as `[('', '')]`. A benign "Mama's, Kitchen" parses as two mailboxes. Whether Resend rejects that: Unable to verify from implementation.
- **Business impact:** Cavnar-signed phishing to suppliers, staff and guests, or broken newsletters.
- **Mitigation:** `html.escape` every field, and build From with `formataddr` after sanitising.

#### P1-84. The CSV fallback for a refused structured-output call drops the week, the revenue override and the slice
- **IDs:** SCHED-3 · Confidence High · Likelihood Low
- **Files / functions:** labor.py:2090-2108 (`generate_optimized_schedule`)
- **Evidence:** The recursion omits `week_start`, `projected_revenue_override` and `prior_rows`. Probe p2: the request was for 10-12, the fallback generated 9-28 with budget None, and a slice produced `dates: []`.
- **Business impact:** The wrong week, with no hours ceiling, or up to 6 paid calls ending in an error.
- **Mitigation:** Recurse with every keyword argument, and narrow the error match.

### P2 Medium Priority (140)

Merged findings are listed first, then single-source findings grouped by area.

- SCHED-16, DATA-55: No generated draft can be deleted; the `schedule_versions` foreign key makes DELETE fail with a 500. models.py:5325-5338
- MOD-PERF-5, DATA-43: `get_all_restaurants` swallows hydration errors, so a malformed row silently drops out of every job. models.py:5625-5633
- AI-21, MOD-EML-8: Resend retries without an `Idempotency-Key` (a slow success is sent twice); up to ~46 s inline per email; a claimed digest is lost on failure. emails.py:294-398; scheduler.py:603-700
- DATA-26, MOD-REV-4, MOD-REV-5, CLIENT-16: Review approve has no status gate and no claim. It re-runs the Google PUT, webhook and alert; double clicks post twice; a phantom id returns ok. client_api.py:96-189; dashboard.html:8839-8895
- DATA-32, AI-10, MOD-HOME-3: Process-local caches never evict; `note_ai_spend` takes 0.54 s per call at 3M keys; a location switch clears every tenant's Home cache. home_brief.py:25-26; ai_utils.py:126-178; client_api.py:31-55
- SEC-31, DATA-29: Competitor refresh starts an unbounded thread of paid Places and Claude calls per press. admin_routes.py:1817-1845; mobile_api.py:3872-3892
- DATA-30, MOD-BIL-6: Re-sending a contract overwrites the envelope id; the superseded envelope is a dead end; completion re-bills setup and resets an active owner's password. admin_routes.py:889-926; webhook_routes.py:756-852
- DATA-38, MOD-BIL-5: Payment links are raw Checkout URLs that expire and cannot be regenerated; both plans can be paid; resend-welcome kills the first password. emails.py:981-1060; admin_routes.py:931-955, 1641-1680
- DATA-35, CLIENT-46, MOD-EMP-9: Time-off, drop and swap requests and staff-settings upserts are check-then-write with no UNIQUE, so double taps duplicate rows (probe: 3 rows). time_off.py:44-80; shift_requests.py:52-110; staff_settings.py:152-184
- MOD-EMP-8, SCHED-22: Approving time off never checks the published week, and a LABOR_VIEW-only login can approve. strategy_routes.py:762-776; time_off.py:104-122
- DATA-59, MOD-EMP-3: Removing or deactivating an employee leaves the PIN login, sessions and 60-day share links; roster deactivation and portal revoke are unrelated switches. models.py:4285-4295; strategy_routes.py:853-875; auth.py:608-627
- DATA-60, MOD-NOT-9: The 48 h no-response alert counts only `pending` (misses the normal `drafted` case) and fires for soft-deleted reviews; AI queues ignore `deleted_at`. notify.py:1733-1786; models.py:3133-3155
- AI-23, MOD-LAB-5: The Toast "First L." identity merges two staff into one person with phantom overtime. toast.py:470-477
- AI-22, MOD-REV-12: A transient Google token-refresh failure raises the owner "reconnect" alert; a revoked token is retried forever. gmb.py:130-163; scheduler.py:311-339
- MOD-NOT-12, AI-29: The global push queue cap of 500 drops pushes while history says sent. push.py:615-636, 685-717
- CLIENT-14, CLIENT-41, DATA-46, SCHED-41: Schedule generation polling gives up on the first failure (web and iOS); owners see a Python traceback; web Generate discards unsaved edits. dashboard.html:10920-11020; schedule_engine.py:2410-2413; LaborViewModel.swift:2034-2076
- MOD-UX-1, CLIENT-12: Web `/api/*` errors are HTML; about 200 `r.json()` sites throw; 55 empty catches; "check your connection" and infinite Loading. hosted_dashboard.py:681-790; dashboard.html
- SCHED-19, DATA-57, CLIENT-29: The stale-save check is optional and not atomic; the version-row write can fail silently; iOS cached weeks and quick overrides overwrite edits. mobile_api.py:5741-5782; schedule_versions.py:133-150; LaborViewModel.swift:1640-1693
- MOD-PERF-6, MOD-NOT-13: Hot per-restaurant tables (`ai_visibility_runs`, `review_requests`, `marketing_attribution`, `weekly_reports`, `push_deliveries`, `device_tokens`) have no index on `restaurant_id`. models.py:1232; push.py:65-88

**SEC**
- SEC-18: The staff-portal per-IP throttle counts successful sign-ins; the 16th employee on shared Wi-Fi is refused. auth.py:1196-1255
- SEC-19: The PIN lockout does not escalate; a roster-wide lockout DoS is possible, and slow online brute force works. auth.py:647-650, 857-880
- SEC-20: 2FA has one slot per restaurant; concurrent logins clobber each other; codes go to the owner; OTPs are plaintext. auth_routes.py:208-247
- SEC-21: `auth_bp` JSON endpoints are outside CSRF; the security toggles have no permission check or audit. hosted_dashboard.py:195-213; auth_routes.py:530-537
- SEC-22: Open redirect via `next` after login and 2FA. auth_routes.py:197, 280-283
- SEC-23: The status-page admin endpoints bypass `admin_required`, the 2FA gate, audit and CSRF. status_routes.py:67-122
- SEC-24: Unmapped routes let managers upload inventory, read `inv-trend`, register webhooks, overwrite POS credentials and set retention or auto-approve. auth.py:2501-2550
- SEC-25: Mobile Toast connect saves before testing and keeps the cached token. mobile_api.py:4330-4356
- SEC-28: Admin view-as and reset-by-restaurant pick an arbitrary login (`LIMIT 1`). admin_routes.py:855-866, 1253-1258
- SEC-29: Staff identities show as "Owner" in the Team list and count toward the last-owner guard. auth.py:1806-1811, 1928-1936
- SEC-30: The admin client-settings save is a full overwrite that defaults every missing key. admin_routes.py:626-740

**DATA**
- DATA-22: `claim_period` fails closed and silently on write errors, and fails open with a non-stopping memo on open errors. ops.py:123-187
- DATA-23: Generate is check-then-insert; two presses start two paid generations; jobs older than 10 minutes are treated as "not running". ops.py:256-291
- DATA-24: Saving alert settings deletes and re-creates contacts; a double click doubles SMS; the consent timestamp is erased. client_api.py:2659-2712
- DATA-27: A double-submitted quick count destroys last week's price baseline. client_api.py:3082-3160
- DATA-28: Whole-form settings saves are last-write-wins; `expected_version` is unused. models.py:2449-2551
- DATA-31: `run_due_posts` takes the 200 earliest local-time posts across time zones, so due posts starve. marketing_publish.py:409-436
- DATA-33: `/health` passes on an empty or unmigrated database. status_manager.py:257-300
- DATA-34: A failed snapshot is left on disk as the "newest" backup. scheduler.py:1094-1120
- DATA-36: Shift-request decide is an unguarded UPDATE; approve and deny from two tabs end last-writer-wins. shift_requests.py:152-170
- DATA-37: Creating a staff account twice makes two people. mobile_api.py:4811-4845
- DATA-39: Home and Ask 60 s caches are not invalidated by uploads, settings or publish. home_brief.py:272-292; ask_cavnar.py:594-605
- DATA-40: `prune_ledgers` deletes by unindexed retention columns under the write lock. ops.py:542-575
- DATA-42: The 2FA send-test route has no limiter; each call is a paid SMS and invalidates the previous code. mobile_api.py:4477-4495
- DATA-56: A team invite commits the login with the principal role first, then narrows it in a second commit. auth.py:1836-1852
- DATA-58: The staff name claim is six separate commits; a partial failure locks the employee out of their name. auth.py:1575-1597
- DATA-61: Account deletion is a flag nothing acts on; the schema blocks deletion. models.py:2584-2598

**AI**
- AI-11: Budget and provider-down messages reach only Ask and invoice scan; the rest say "check back shortly". ai_utils.py:353-371; client_api.py:1225-1245
- AI-12: The ledger prices Sonnet 5 at $3/$15 (list $2/$10); the global backstop grows $200 per client. ai_utils.py:605-615, 678-684
- AI-13: Retries stack to 9 HTTP attempts per call; the breaker probe is not single-flight. ai_utils.py:23-28, 472-502
- AI-14: `thinking: disabled` is forced on every model; an env override to Opus 5.5 or Fable breaks all AI. ai_utils.py:391-417
- AI-15: Guest text reaches four prompts unfenced and seeds the figure-verification corpus. drafter.py:155-162; client_api.py:983-991; marketing.py:406-419
- AI-16: Ask direct-action tools run without confirmation; `remember` writes permanent memory. ask_cavnar_tools.py:32-58, 290-310
- AI-17: The weekly plan runs unattended with action tools, never checks its figures, and claims before work. strategy_jobs.py:204-265
- AI-18: "Value delivered" double-counts overlapping trackers on the same metric. outcomes.py:104-128, 289-356
- AI-19: Invoice auto-apply accepts any price when the current cost is 0 or the line cannot be checked. ordering.py:139-164
- AI-20: `max_tokens=300` cannot fit urgent CJK replies, so those reviews are never drafted. drafter.py:179-216

**MOD: Reviews and Intel**
- MOD-REV-7: GBP backfill never passes the newest 1,000 reviews and re-downloads them every cycle. gmb.py:271-311
- MOD-REV-8: The Places API key leaks into `job_failures`, Sentry, logs and admin JSON. fetcher.py:14-23; ops.py:36-57
- MOD-REV-9: GBP review dates are stored in UTC while Places dates are local, so day bucketing disagrees. gmb.py:325-338
- MOD-REV-10: Failed auto-publish replies are silent, stay `approved` forever, and count as responded. scheduler.py:2618-2624
- MOD-REV-11: Places returns at most 5 reviews per fetch; anything beyond is lost permanently. fetcher.py:12-16
- MOD-REV-13: The auto-approve cap is computed on the server's date against Chicago stamps, so it resets at 7pm CT. models.py:7359-7367
- MOD-INT-2: The competitor job counts failures as "analysed"; Places errors are shown as "No nearby competitors". scheduler.py:1673-1683; competitor.py:384-399
- MOD-INT-3: An unrated competitor is stored as 0 and reported as a "significant" +4.6 jump. competitor.py:486, 868
- MOD-INT-4: No city is recorded as a 0 visibility score; a failed lookup is negative-cached for 24 h. client_api.py:3569-3652
- MOD-INT-5: AI visibility is a 10–40 s synchronous request behind a process-wide 1.3 s gate. client_api.py:3495-3556

**MOD: Labor and Employees**
- MOD-LAB-6: Square role comes from `assigned_locations.assignment_type`, not the job. square.py:71-74
- MOD-LAB-7: Square daily sales stop silently on any non-200 page. square.py:137-148
- MOD-LAB-8: Square and Clover never write `labor_daily_history`. square.py:211-230; clover.py:210-229
- MOD-LAB-12: A `revenue` column and "$4,200" sales are accepted and then ignored. labor.py:413-416
- MOD-LAB-15: Name case and whitespace variants split one person, so overtime disappears. labor.py:421-462
- MOD-LAB-17: The overtime email fires for every historical week, says "this week", and uses unescaped names. client_api.py:2926-2980
- MOD-LAB-18: A POS sync overwrites a longer hand-uploaded history; the full CSV is re-parsed per request. models.py:4909-4927
- MOD-LAB-21: An open clock-in (0 h) costs $0 and counts as a no-show. toast.py:493-500; staff_settings.py:305-335
- MOD-EMP-2: Exact-case name matching across roster, settings, ratings and contacts; deactivation does not stick. staff_settings.py:206-236
- MOD-EMP-4: Ask's `set_staff_contact` with only a phone erases the email; the phone is unvalidated. ask_cavnar_tools.py:882-898
- MOD-EMP-5: The iOS checklist completes on the device date but re-reads the server's UTC date. StaffPortalView.swift:241; models.py:3826-3831
- MOD-EMP-7: Time off cannot be withdrawn or corrected; the overlap rule blocks the fix. time_off.py:26-58

**MOD: Food Cost**
- MOD-FC-3: The supplier grouping key is case-sensitive, so one supplier gets two POs. inventory.py:1332-1349
- MOD-FC-13: `avg_daily_usage` freezes when a dish stops selling, so the item keeps being reordered. inventory_ledger.py:317-358
- MOD-FC-14: Concurrent recounts each infer the full gap as waste. inventory_ledger.py:182-221
- MOD-FC-15: The count-sheet date is unvalidated; "9/21/26" sits in every future window. strategy_routes.py:538
- MOD-FC-17: The archive-first sales read misses for windows ending today, so every load calls the live POS. cogs.py:126-215
- MOD-FC-20: The web order screen hides unassigned items when any supplier group exists. dashboard.html:5219-5241
- MOD-FC-21: Mobile `overstock_total` and `waste_items_total` sum truncated lists. mobile_api.py:1966-1976
- MOD-FC-22: Price Watch keys on the display name; dual-sourced rises are masked; a rename erases history. inventory.py:532-594
- MOD-FC-23: COGS prices every delivery in the window at today's unit cost. cogs.py:46-65
- MOD-FC-24: Recipe CSV creates POS-unlinked dishes, truncates at 2,000 rows, and cannot correct quantities. recipes.py:394-434

**MOD: Marketing and Email**
- MOD-MKT-3: Instagram publish sleeps up to 20 s in the request and in the scheduler tick; 200 posts serialise. social_routes.py:194-203
- MOD-MKT-4: A Meta 5xx or 429 is treated as a definite refusal and retried, so a post can go out twice. social_routes.py:187-213; marketing_publish.py:98-101
- MOD-MKT-11: Guest marketing SMS uses the owner-alert A2P campaign; the manual review-request SMS ignores STOP and quiet hours. guest_marketing.py:657; client_api.py:3305-3323
- MOD-MKT-12: A STOP to one restaurant does not stop another's Toast invite; invites never reach western restaurants; churned restaurants keep inviting. guest_marketing.py:405-411, 911-975
- MOD-MKT-13: A 20 KB decompression-bomb image costs about 580 MB of RAM. marketing_media.py:52-81
- MOD-MKT-14: Photo upload dead-ends: a 5 MB cap against the promised 12 MB, a 413 returned as HTML, HEIC undecodable, and iOS sends full resolution. hosted_dashboard.py:70; marketing_media.py:37-63
- MOD-MKT-15: Meta metrics sync is unbounded and overwrites stored reach and impressions with 0 when a call fails. scheduler.py:971-998; social_routes.py:331-388
- MOD-MKT-16: Attribution mixes a UTC post time with local business dates, double-credits same-day posts, and falls back to sample sales. marketing_publish.py:148-152; marketing_signals.py:40-254
- MOD-MKT-17: Draft approval is advisory; members can publish or schedule directly, and the direct routes skip the module gate. marketing_drafts.py:86-117; social_routes.py:154-162
- MOD-EML-4: About 17 direct `Resend.Emails.send` sites bypass suppression, `email_log`, the flood guard and retry. client_api.py:3380; admin_routes.py:1891
- MOD-EML-5: Unsubscribe endpoints change state on GET. client_api.py:4564-4573, 4688-4705
- MOD-EML-6: Commercial email lacks a postal address; the guest review-request email has no opt-out. guest_email.py:195-203; emails.py:707-723

**MOD: Notifications, Home and Billing**
- MOD-NOT-5: Quiet hours of "9pm" silently turn quiet hours off; a non-numeric cap makes the whole save 500; `digest_day` is unvalidated. client_api.py:2699-2709
- MOD-NOT-8: Push-only owners never receive labor, trend or threshold alerts. notify.py:1813-1823
- MOD-NOT-10: The unread badge counts rows the viewer cannot see; a new login is badged with the full history. models.py:6636-6650
- MOD-HOME-1: Home "Alerts" and "changes" leak module alerts a role may not see. home_brief.py:414-420, 852-868
- MOD-PERF-4: `delayed.run_due` runs at most 20 actions per 5-minute tick. delayed.py:89-120

**SCHED**
- SCHED-13: Close times and windows after midnight flag every evening shift, while overnight shifts escape the window check. schedule_engine.py:793-831; schedule_rules.py:319-340
- SCHED-14: The replacement picker ignores time off, deactivation, minors, dayparts and rest. mobile_api.py:5884-5943
- SCHED-15: Call-off coverage suggestions ignore role, time off and hours. labor_replacements.py:14-40
- SCHED-17: A saved edit never updates `hours_scheduled`, so the budget blocker goes stale. models.py:5159-5191
- SCHED-18: "NEEDS REVIEW" text stays in the notes after a fix, so the blocker never clears. schedule_engine.py:2285-2296; client_api.py:5629-5631
- SCHED-20: Approving a drop with an illegal named replacement errors but leaves the shift open to all. shift_requests.py:152-176
- SCHED-21: Swaps need no consent from the colleague; nothing in the request flow notifies anyone. shift_requests.py:69-269
- SCHED-23: Trim-to-budget runs before the rule sweep and can remove someone's only shift. schedule_engine.py:2044-2129; schedule_economics.py:399-492
- SCHED-24: Rosters of 400–500 people exceed the per-call row budget and outlive the one-job guard. schedule_engine.py:26-27, 396-455
- SCHED-26: Every manager rescore counts as a "showing", so recommendation kinds are suppressed forever. schedule_intel.py:425-451
- SCHED-30: A leader rule nobody can meet caps the shift every week and blocks publish. shift_quality.py:476-590, 1731-1736

**CLIENT**
- CLIENT-13: Web session expiry is handled only on Home; pollers keep sending requests that get 401s. dashboard.html:2760, 7110-7124
- CLIENT-15: The dashboard scrolls sideways at 320 and 375 px; long names overflow the 56 px header. dashboard.html:333, 340
- CLIENT-17: The location switcher breaks on apostrophes in names. dashboard.html:1188-1191
- CLIENT-18: A reply template with `"` breaks the picker and injects attributes (stored, within the tenant). dashboard.html:15716-15737
- CLIENT-19: Ask Confirm silently re-enables after a network error (web and iOS). dashboard.html:7208-7232; AskCavnarViewModel.swift:446-468
- CLIENT-20: iOS caps every request at 45 s regardless of the requested timeout. APIClient.swift:89-90
- CLIENT-21: No background task keeps iOS uploads and generations alive. InvoiceScanSheet.swift:135
- CLIENT-22: iOS treats 402 and 403 as generic errors hidden behind cached Home numbers. APIClient.swift:241-245; HomeViewModel.swift:29, 73
- CLIENT-23: A late 401 from a superseded token signs out the new iOS session. APIClient.swift:231-236
- CLIENT-24: Certificate-pin and captive-portal failures look like "cancelled", so screens fail blank. PinnedSessionDelegate.swift:72-104
- CLIENT-25: iOS Keychain items are not device-bound. Keychain.swift:19-20
- CLIENT-26: After an iOS location switch, a failed reload shows the old location's numbers. SessionStore.swift:353-366
- CLIENT-27: Leaving iOS Labor during generation orphans the job. LaborView.swift:14, 322
- CLIENT-28: The iOS Ask stream fallback re-runs the whole question after partial progress. AskCavnarViewModel.swift:544-612
- CLIENT-30: iOS review filters search only the first 50 reviews; load errors are never shown. ReviewsListViewModel.swift:37-146
- CLIENT-31: iOS quick count sends the typed price, so comma-decimal locales drop rows. FoodCostQuickEntryViewModel.swift:93-145
- CLIENT-32: iOS roster rollback writes to a stale index. ScheduleSetupViewModel.swift:489-525
- CLIENT-33: iOS scheduled posts use the phone's time zone and calendar. MarketingComposeViewModel.swift:154-161
- CLIENT-34: iOS guest-contact delete is one tap and hard-deletes the consent and STOP record. GuestTextClubView.swift:429-434; guest_marketing.py:248-254
- CLIENT-35: Two iOS Labor lists clip rows at large Dynamic Type. RosterSection.swift:236-253
- CLIENT-37: Web pollers ignore `document.hidden`. dashboard.html:7123-7124, 8705-8710
- CLIENT-38: The 1.27 MB uncompressed dashboard posts `/api/theme` (a DB write) on every load. dashboard.html:8193, 8398-8402
- CLIENT-39: iOS decodes invoice photos at full size on the main thread. InvoiceScanSheet.swift:74, 191-201
- CLIENT-40: A forged X-Forwarded-For plants HTML in the owner's Sessions list. auth_routes.py:24-27; dashboard.html:14688-14708

### P3 Low Priority (73)

- AI-28, MOD-INT-7: NWS failures are never cached (2 × 10 s per call); one geocode blip disables weather for 7 days. weather.py:76-177
- MOD-UX-2, AI-31, MOD-REV-15, MOD-FC-25, MOD-EMP-10, CLIENT-50: Raw exception text and API bodies reach owners, staff and iOS users. client_api.py:597, 1227-1245; gmb.py:385-392; schedule_engine.py:2488-2489
- SEC-32: 170 POST routes return 500 on non-object JSON bodies. e.g. auth_routes.py:543; mobile_api.py:211
- SEC-33: Security codes use `random` instead of `secrets`. auth_routes.py:211, 432
- SEC-34: Side-effecting GET endpoints (logout, view-as, not-me). auth_routes.py:449-456, 1086
- SEC-35: Username enumeration by timing in `verify_password`. auth.py:1747-1760
- SEC-36: The staff PIN sign-in returns the session token in the JSON body. staff_routes.py:188-203
- SEC-37: Login-report tokens are plaintext; the not-me page interpolates the email unescaped. auth.py:2912-2921; auth_routes.py:1108-1111
- SEC-38: SSO trusts an unverified provider email; the status page writes on every public GET. auth_routes.py:1003-1016; status_routes.py:17
- SEC-39: SECURITY.md contradicts the code on OTP hashing, webhook rejection and support writes. docs/ops/SECURITY.md
- SEC-40: The Stripe webhook route has no test and `stripe` is not installed in the dev interpreter. webhook_routes.py:272-696
- DATA-44: `init_db(db_path)` runs `ensure_columns()` against the default database. models.py:2329
- DATA-45: `_claim_fallback.pop()` drops an arbitrary key, not the oldest. ops.py:178-184
- DATA-47: A stale comment says the async result is deleted on first poll. schedule_engine.py (~2320)
- DATA-48: `CREATE TABLE IF NOT EXISTS` runs on hot paths, against the CLAUDE.md rule (measured harmless for locking). ops.py:160-162, 220-225
- DATA-49: The IG/FB token refresh is one daily pass with no per-restaurant claim. scheduler.py:918-970
- DATA-50: Password reset tokens can be consumed twice concurrently. models.py:5025-5040
- DATA-62: Self-signup, admin create-client and shift-CSV follow-on writes can half-apply. mobile_api.py:438-456; admin_routes.py:108-131
- AI-24: A refusal becomes an empty answer, or an empty draft marked drafted. ask_cavnar.py:1053-1057; drafter.py:216-236
- AI-25: The diagnosis sweeps' over-budget guard reads a non-existent attribute. scheduler.py:1837, 1897
- AI-26: JSON-producing calls parse free text instead of using structured output. analyser.py:276-279
- AI-27: Twilio `send_sms` never retries a 429 or 5xx. notify.py:96-142
- AI-30: The competitor menu fetch follows redirects to any host (SSRF); its spend is unattributed. competitor.py:67-191
- AI-32: The guest SMS length is not enforced (1,200 characters accepted). guest_marketing.py:595-620
- AI-33: Billing-page Stripe calls use the SDK default timeout, sequentially. client_api.py:2532-2590
- MOD-REV-14: Reviews removed upstream are never removed locally. models.py:3023-3120
- MOD-REV-16: Held auto-approve candidates are re-captured as failures every cycle. scheduler.py:2597-2617
- MOD-REV-17: The weekly digest omits unanalysed reviews and prints non-M/D/YY dates. reporter.py:995-1024
- MOD-REV-18: `run_daily_fetch` runs a per-restaurant "urgent" query whose result is discarded. Candidate for future cleanup after additional verification (the CLAUDE.md ten-point trace was not run). scheduler.py:524-534
- MOD-INT-6: Competitor Places usage is metered wrongly. competitor.py:836-839
- MOD-INT-8: A failed custom-competitor lookup is reported as "gone" (labelled measured). competitor.py:846-881
- MOD-LAB-19: The `labor.updated` webhook always sends nulls. client_api.py:2896-2900
- MOD-LAB-20: The Labor caveat says "the larger was used" while the code drops those days. dashboard.html:3805; labor.py:429-438
- MOD-EMP-6: Checklist role matching is case-sensitive. models.py:3816-3840
- MOD-FC-26: `check_stale_inventory`: one bad timestamp aborts the report, and it compares UTC to Chicago time. scheduler.py:716-790
- MOD-FC-27: `recipes.accept` is not claim-first; a double accept marks the recipe "rejected". recipes.py:347-380
- MOD-FC-28: Reprice "monthly" is a 28-day figure labelled 30 days. menu_intelligence.py:212-240
- MOD-MKT-18: Grouped marketing defects: ISO date in the failure alert, raw Meta errors, click counting of HEAD requests and bots, delete of media in use. The `/api/debug-insights` and `/api/meta-review-test` routes are a candidate for future cleanup after additional verification. marketing_publish.py:366-370; social_routes.py:242-256, 437-498
- MOD-EML-7: One global suppression list across tenants; suppressed addresses still count as subscribers. webhook_routes.py:942-983; models.py:7898-7925
- MOD-EML-9: Grouped email defects: non-M/D/YY CT dates, no Svix timestamp check, the frozen webhook secret, and the unsubscribe secret fallback. emails.py:218, 2333; webhook_routes.py:908-940; models.py:8031
- MOD-NOT-14: `alert_holds` and `notification_opens` are never pruned. ops.py:513-529
- MOD-NOT-16: The 10am batch is held in memory, so a restart loses it with its claims spent. notify.py:1228-1266
- MOD-HOME-2: A Home cache miss re-parses the whole shifts CSV; `?fresh=1` bypasses the cache. home_brief.py:354-381
- MOD-BIL-10: Checkout creation makes 10 synchronous Stripe calls inside the DocuSign webhook and a new Price every time. emails.py:1425-1453
- SCHED-31: Working all seven days raises no violation when `max_consecutive_days` is above 6. schedule_rules.py:741-783
- SCHED-32: Rest gaps across a DST change are off by an hour. schedule_rules.py:199-211
- SCHED-33: New Year's week loses holiday context; the events block asserts a fixed 20–40% lift. schedule_engine.py:143-187
- SCHED-34: Past-dated open shifts stay claimable; same-date doubles cannot be claimed. shift_requests.py:141-149
- SCHED-35: Any owner note on a person blocks them from every claim. shift_quality.py:2090-2134
- SCHED-36: A labor target of 0 becomes 30%; admin input is unbounded. schedule_engine.py:116; admin_routes.py:686
- SCHED-37: Generation accepts any week, including past ones, and those weeks can be published. client_api.py:2461-2463
- SCHED-38: 24-hour model times skip the close cap, arrival and hours reconciliation. schedule_engine.py:665, 713-831
- SCHED-39: The fix pass costs about 0.18 s per hard violation, uncapped, on the web process. shift_quality.py:2442-2466
- SCHED-40: Denied drop requests are learned as "avoids". schedule_intel.py:272-305
- SCHED-42: CSV-fallback rows with fewer than 6 columns pass the day check, then are all dropped, and an empty week is saved. schedule_engine.py:511-517, 1920-1928
- CLIENT-43: The web status dot shows green when `/api/status` cannot be parsed. dashboard.html:14727-14743
- CLIENT-44: The ES5 rule covers only dashboard.html; admin and audit pages use ES2020 syntax. tests/test_frontend_rules.py:15-38; admin.html:251-823
- CLIENT-45: Staff- and owner-facing dates break the M/D/YY rule (web and iOS). staff_schedule.html:59, 64; staff_portal.html:402, 466
- CLIENT-47: The staff portal gives no sign-in path when the 14-hour session ends. staff_portal.html:249-252
- CLIENT-48: Errors are shown through `alert()` in key flows. dashboard.html:9914, 10251
- CLIENT-49: iOS shows cancelled loads as errors in 21 view models. ReviewsListViewModel.swift:122-124
- CLIENT-51: iOS push deep-link edge cases (beyond the first page, string ids, stacked paths). ReviewsListView.swift:244-249; DeepLinkRouter.swift:86
- CLIENT-52: Two iOS NetworkMonitor instances; the one used for wording starts out "online". RootView.swift:10; APIClient.swift:175
- CLIENT-53: The iOS Ask stream treats any 401 as expiry and drops refusal messages. APIClient.swift:341-362
- CLIENT-54: The sentiment chart plots a zero-review week as 0.0★. SentimentRiverChart.swift:34, 92-104
- CLIENT-55: iOS "Retry posting" and "Write a reply" are dimmed but not disabled. ReviewDetailView.swift:313-320, 399-410
- CLIENT-56: Reopening a review mid-draft offers to draft again (a second paid call). ReviewsListView.swift:71
- CLIENT-57: The iOS staff-signup "Send it again" button is not disabled while loading, so it sends extra OTP SMS. StaffSignupView.swift:171
- CLIENT-58: Failed loads render as genuine empty states. GuestTextClubViewModel.swift:131-144; dashboard.html:15043-15050
- CLIENT-59: Keyboard and focus gaps: `div onclick` without a role; 44 `outline:none` rules against 8 `:focus-visible`. dashboard.html:3074, 15730
- CLIENT-60: Invoice "Apply" stays after success; the rules sheet overwrites edits on reload. InvoiceScanSheet.swift:307-327; ScheduleRulesSheet.swift:108-111
- CLIENT-61: "Today" and staleness use the device time zone; the portal tasks date is browser-local. LaborViewModel.swift:1167-1168, 1963; staff_portal.html:166-169
- CLIENT-62: A full swipe deletes a history draft with no confirmation; a 4 s sleep in the rescore keeps Save disabled. ScheduleHistoryView.swift:98-104; LaborViewModel.swift:1700

## 20. Final Questions

### (1) The ten most likely production failures

These are ranked by how often they will happen at today's customer base, not by severity. All ten are High likelihood.

| # | Failure | Where it happens | IDs |
|---|---|---|---|
| 1 | A new labor signup is shown sample money ("$12,630/mo over target") and eight fictional employees | labor.py:106-112 | MOD-LAB-16, MOD-EMP-1 |
| 2 | Every POS-connected restaurant's dinner shifts are dated to the next day | toast.py:490-491 | MOD-LAB-3 |
| 3 | An Excel spreadsheet upload says "success" and then breaks Labor or Food Cost | M/D/YYYY dates, capitalised headers, BOM, "$" values, optional columns missing | SEC-16 cluster; MOD-FC-5 / SEC-26 |
| 4 | The staff portal shows Friday to Sunday as "off" every week after Friday's publish, and hides the lunch leg of doubles | staff_schedule.py:26-40 | SCHED-2, SCHED-4 |
| 5 | Restaurants closed on any weekday can never generate a schedule | schedule_engine.py:413-482 | SCHED-1 |
| 6 | iOS users are signed out on every deploy, and generations started before a deploy never finish | SessionStore.swift:106-126; ops.py:228-270 | CLIENT-4, DATA-9 / SCHED-25 |
| 7 | Guest SMS lists of about 60–100 or more are sent twice when the 20 s iOS timeout fires and the owner retries | guest_marketing.py:640-700 | MOD-MKT-6 / CLIENT-1 / AI-7 / DATA-13 |
| 8 | Supplier orders leave out everything past 10 lines, and the second supplier's Send is blocked for 60 s | inventory.py:512-513; client_api.py:5467 | MOD-FC-1, MOD-FC-11 |
| 9 | Quiet hours are wrong for every non-Central restaurant, and the "no reply in 48h" nudge never fires for drafted reviews | models.py:6487-6510; notify.py:1733-1786 | MOD-NOT-4, MOD-NOT-9 |
| 10 | Any web server error reads as "check your connection" or leaves "Loading…" forever | hosted_dashboard.py:681-790 | MOD-UX-1 / CLIENT-12 |

Close behind:
- Places key or quota failures silently stopping review sync (MOD-REV-1 / AI-6).
- Toast truncation at large restaurants (AI-3 / MOD-LAB-4).
- Cancelled restaurants still being processed and texted (DATA-51 / MOD-REV-2).

### (2) Failures that could cause customer data loss

**Whole-database loss**
- The documented restore can leave an empty database that passes `integrity_check` (DATA-2).
- Backups exist only on the same volume. The redacted email copy will stop working as the database grows (DATA-18 / MOD-PERF-7).
- A failed snapshot shadows the good one (DATA-34).

**Imports and syncs that silently lose or replace data**
- Third-party review imports store nothing (DATA-21).
- A bad CSV upload replaces the previous dataset, and no version is kept (SEC-16 cluster).
- A POS sync overwrites a longer hand-uploaded history (MOD-LAB-18).
- A Toast sync truncated at 2,000 entries overwrites a complete CSV (AI-3 / MOD-LAB-4).

**Reviews never captured or never processed**
- Places failures are treated as success (MOD-REV-1 / AI-6).
- Places returns at most the newest 5 reviews per fetch (MOD-REV-11).
- GBP imports stop at the newest 1,000 (MOD-REV-7).
- Reviews that use up their attempts during an outage are never analysed (AI-4).

**Edits and records overwritten**
- Concurrent claims lose covers (DATA-17 / SCHED-5).
- Stale or concurrent saves overwrite schedule edits (SCHED-19 / DATA-57 / CLIENT-29).
- Saving from the email link erases the availability note (CLIENT-11).
- Ask erases a staff email when only a phone is given (MOD-EMP-4).
- Alert-settings saves erase the SMS consent timestamps (DATA-24).
- Guest contact delete is a hard delete of the consent and STOP record (CLIENT-34).
- A double quick count destroys last week's baseline (DATA-27).
- The metrics sync overwrites reach and impressions with 0 (MOD-MKT-15).
- An iOS rollback writes to the wrong employee (CLIENT-32).

**Offline and in-flight work**
- iOS offline writes are wiped by a launch 5xx (CLIENT-4), jammed or expired (CLIENT-5), or deleted by a location switch (CLIENT-5).

**Account and billing state**
- Stripe and DocuSign events are dropped after being claimed (AI-2 cluster).
- A re-signed contract overwrites an active owner's password (DATA-30 / MOD-BIL-6).
- A teammate can set review retention to 6 months (SEC-24).
- A crashed campaign leaves no record of the texts that were really sent (MOD-MKT-6 cluster).

### (3) Failures that most damage trust

**Another tenant's data**
- A fired employee administers the restaurant that fired them (SEC-1).
- A seller keeps access to a location they sold (SEC-2).
- A shared phone shows the previous account (CLIENT-2).
- Another tenant's photos, or an attacker's Meta page, end up on a restaurant's feed (MOD-MKT-1, MOD-MKT-5).

**Guests**
- Guests are texted twice (MOD-MKT-6 cluster).
- Guests keep getting texts after saying "Stop." or "opt out" (MOD-MKT-8), or after being re-subscribed by a stranger (SEC-17 / MOD-MKT-9).

**Confident but false numbers**
- Sample money and fictional staff on day one (MOD-LAB-16, MOD-EMP-1).
- Invented "$2,000/mo" passing verification at high confidence (AI-5).
- Food cost read about 11× too high (MOD-FC-16).
- Phantom overtime (SCHED-7).
- An inflated "value delivered" (AI-18).

**Staff and schedules**
- Staff shown "off" on days they work (SCHED-2).
- Two people told they have the same shift (DATA-17 / SCHED-5).
- A 16-year-old allowed to claim a 1am close (SCHED-6).
- A colleague's shift traded away without being asked (SCHED-21).

**Replies, alerts and access**
- The pre-edit AI reply posted publicly (CLIENT-6).
- Replies auto-published to a cancelled customer's Google listing (DATA-51 / MOD-REV-2).
- A fired manager still receiving alerts (DATA-53 / MOD-NOT-6).
- A $5 courtesy refund locking out a paying group (MOD-BIL-2).
- A password reset that does not end an intrusion (SEC-8).

### (4) The most expensive support tickets

**Tickets that need a database edit or manual reconciliation**

| Ticket | IDs |
|---|---|
| "Reset my password and I still can't sign in" after a freeze or "not me" | SEC-7 |
| "I can't send any orders" when PO numbering wedges; the owner is told to check supplier addresses | MOD-FC-7 |
| "Half my reviews have no replies or sentiment since the outage" | AI-4 |
| "I paid and never got my login", or paid but modules locked | AI-2 cluster, DATA-54 / MOD-BIL-9 |
| Re-activated churned accounts, refund lockouts, double setup charges from two open checkout sessions | MOD-BIL-1, MOD-BIL-2, DATA-38 / MOD-BIL-5 |
| Duplicate digests and severed integrations after a restore | DATA-20, DATA-18 |

**Legal or forensic tickets**
- "My guests got the text twice" or "after they said stop". This needs Twilio log forensics and carries TCPA exposure (MOD-MKT-6 cluster, MOD-MKT-8, SEC-17 / MOD-MKT-9).

**Tickets that give no clue in the UI**
- "It said uploaded and now Labor or Food Cost is broken" (SEC-16 cluster, MOD-FC-5 / SEC-26, MOD-FC-6).
- "Why won't it generate?" Every attempt costs 3 or more model calls (SCHED-1).
- "Why is my schedule missing servers?" (SCHED-8).
- "I changed my PIN and now it never works" (CLIENT-3).
- "I lost a location after my manager changed their email" (SEC-13).
- "The supplier delivered twice" or "the order was missing items" (DATA-15 / MOD-FC-11, MOD-FC-1, MOD-FC-2).
- "My staff didn't show up Saturday" (SCHED-2, SCHED-4).

### (5) Five edge cases to convert to regression tests immediately

Each of these pins a P0, is cheap to write, and would fail today.

1. **A guest SMS campaign sends exactly once.** Two overlapping `send_campaign` calls, and one crashed-then-retried call, text each guest once (tests/test_guest_marketing.py; MOD-MKT-6 / CLIENT-1 / AI-7 / DATA-13).
2. **A staff identity cannot reach another restaurant's console.** A staff identity unlinked at A and signed in by PIN at B is refused every `/api` and `/mobile/api` console route, and its session acts at B. This replaces the test that pins the fallback (tests/test_staff_signup.py; SEC-1).
3. **Authenticated reads survive a database that refuses writes.** With `query_only` connections, a GET of `/api/home/brief` is not a 500 (tests/test_chaos_recovery.py; DATA-1).
4. **A supplier order carries every due item.** Thirty below-par items for one supplier produce a 30-line purchase order (tests/test_food_cost_audit_fixes.py; MOD-FC-1).
5. **The portal shows the current week after the next week is published.** With W and W+1 published, `shifts_for_employee` on Thursday of W still shows Friday–Sunday of W (tests/test_schedule_publish_gate.py; SCHED-2).

Next in line:
- A no-data restaurant shows no labor figures and no roster (MOD-LAB-16, MOD-EMP-1).
- A locked Stripe claim or a handler crash is processed on retry (AI-2 cluster).
- A configured closed Monday generates a schedule (SCHED-1).
- A Toast shift at 00:30Z keeps its local date (MOD-LAB-3).
- A Places REQUEST_DENIED fails the fetch (MOD-REV-1 / AI-6).

### (6) What breaks first at one million users

The six audits agree on the order. What breaks first is the single process: gunicorn `--workers 1 --threads 4`, one SQLite writer, and the scheduler thread, all inside the same process.

1. **Request threads and the SQLite writer.**
   - Every authenticated request writes `sessions.last_active`, which puts one write-lock acquisition on every page view (DATA-1).
   - Four slow model calls or Ask streams occupy all four threads (AI-1).
   - CLIENT estimates about 2,500 requests/s from background pollers alone at 10% of 1M users with a tab open. Those pollers ignore `document.hidden` and never stop after the session expires (CLIENT-37, CLIENT-13).
   - Lock holders make each request wait out the 30 s busy timeout:
     - `claim_period`'s unindexed DELETE (DATA-6 / MOD-PERF-3).
     - `prune_ledgers` (DATA-40).
     - The follow-up job holding its transaction across Twilio calls (DATA-52 / MOD-MKT-10).
2. **The scheduler thread, which shares the process and the GIL.**
   - `get_all_restaurants()` takes about 52 s of CPU and 1 GB per call and runs more than 30 times an hour (MOD-PERF-2).
   - The review fetch covers each restaurant only every few days (DATA-7).
   - The serial sweeps take days per pass; AI visibility alone is about 12 days (AI-9 cluster).
   - Auto-draft reaches a few dozen restaurants per week (DATA-8 / SCHED-11).
   - Minute-level duties wait hours (DATA-3 / MOD-PERF-1).
3. **Drain rates.** These drop or delay work silently:
   - 200 holds per tick (MOD-NOT-2).
   - 20 delayed actions per tick (MOD-PERF-4).
   - A 500-deep push queue (MOD-NOT-12 / AI-29).
   - 200 due posts selected across time zones (DATA-31).
4. **Storage and memory.**
   - `inventory_history` grows about 2.7 TB a year at 100k restaurants (MOD-FC-18).
   - `schedule_versions` stores a full CSV on every edit (SCHED-27).
   - Per-restaurant queries scan whole tables because of missing indexes (MOD-PERF-6 / MOD-NOT-13).
   - Unbounded in-process caches grow until the one worker is OOM-killed (DATA-32 / AI-10).
   - The emailed backup can no longer carry the database (DATA-18).
5. **External limits.** The org-level Anthropic rate limit is hit sooner because stacked retries send up to 9 attempts per call (AI-13). The global AI budget backstop grows with client count, so it stops protecting (AI-12). A single Perplexity 50 RPM key paces every visibility check (MOD-INT-5, AI-9).

---

## 21. Edge Case Test Matrix (before new tests)

**Columns.**
- The rows are every area's matrix rows. The source area is shown in brackets.
- "Edge Cases Covered" is covered/identified, where covered means an existing test drives the case (each area confirmed this by grep).

**Rows are not deduplicated.** Areas sometimes enumerated the same feature from a different angle, for example Staff Portal from SEC and from CLIENT, or the Admin Console and the Admin UI. Those rows are kept separate because their edge-case lists differ. Some underlying cases therefore appear in two rows.

| Feature | Happy Path Tested | Edge Cases Covered | Missing | Risk |
|---|---|---|---|---|
| Marketing incl. guest SMS & newsletter (MOD) | ⚠️ partial (no Meta OAuth or direct publish) | 66/135 | 69 | Critical |
| Food Cost (MOD) | ✅ | 88/144 | 56 | Critical |
| Reviews (MOD) | ✅ | 44/75 | 31 | Critical |
| Labor Analytics (MOD) | ⚠️ partial (no CSV upload or POS shift sync) | 20/50 | 30 | Critical |
| iOS App Core: APIClient, auth, caching, push (CLIENT) | ✅ | 7/26 | 19 | Critical |
| Notifications & Alerts (MOD) | ✅ | 27/44 | 17 | Critical |
| iOS Reviews/Marketing/Food Cost/Intel screens (CLIENT) | ⚠️ partial | 3/20 | 17 | Critical |
| Authentication & Sessions (SEC) | ✅ | 10/24 | 14 | Critical |
| Schedule Generator: model + pipeline (SCHED) | ✅ | 11/24 | 13 | Critical |
| Permissions & Roles (SEC) | ✅ | 5/16 | 11 | Critical |
| Schedule Publish & Versions (SCHED) | ✅ | 7/18 | 11 | Critical |
| Shift Requests: drop/swap/claim (SCHED) | ✅ | 4/15 | 11 | Critical |
| Data Layer & Migrations (DATA) | ✅ | 6/16 | 10 | Critical |
| Background Scheduler: loop, lease, cursors (DATA) | ✅ | 9/18 | 9 | Critical |
| Ask Cavnar AI (AI) | ✅ | 15/23 | 8 | Critical |
| External Integrations: email/SMS/push/weather/POS/Google (AI) | ✅ | 11/19 | 8 | Critical |
| Staff Portal: PIN auth (SEC) | ✅ | 11/18 | 7 | Critical |
| Backup & Recovery (DATA) | ✅ | 5/12 | 7 | Critical |
| AI Platform: retry, budgets, ledger (AI) | ✅ | 16/23 | 7 | Critical |
| Employees & Team (MOD) | ✅ (the roster test monkeypatches `_cached_shifts`) | 27/50 | 23 | High |
| Emails (MOD) | ⚠️ partial (none for the ~17 direct Resend sites) | 21/42 | 21 | High |
| Web Dashboard: shell, navigation, polling (CLIENT) | ✅ | 5/24 | 19 | High |
| Intel data (MOD) | ⚠️ partial (`run_competitor_analysis` only ever mocked) | 29/44 | 15 | High |
| Billing & Contracts (MOD) | ⚠️ partial (no Stripe or DocuSign route tests) | 21/36 | 15 | High |
| CSV/File Imports (SEC) | ⚠️ partial | 3/14 | 11 | High |
| Staff Portal UI: portal, PIN login, `/s/` link (CLIENT) | ✅ | 6/16 | 10 | High |
| iOS Labor & Scheduling screens (CLIENT) | ⚠️ partial | 2/12 | 10 | High |
| Async Jobs & Delayed Actions (DATA) | ✅ | 6/15 | 9 | High |
| Settings & Restaurant Config (DATA) | ✅ | 5/14 | 9 | High |
| Webhooks: Stripe/DocuSign/POS (SEC) | ⚠️ partial | 6/13 | 7 | High |
| Admin Console (SEC) | ✅ | 5/12 | 7 | High |
| Review AI: analysis + drafting (AI) | ✅ | 12/19 | 7 | High |
| Schedule Rules & Compliance (SCHED) | ✅ | 13/20 | 7 | High |
| Marketing AI (AI) | ✅ | 7/13 | 6 | High |
| Schedule Economics: trim, cost, stagger (SCHED) | ✅ | 6/12 | 6 | High |
| Time Off & Availability (SCHED) | ✅ | 6/12 | 6 | High |
| Intel AI: competitors + AI visibility (AI) | ✅ | 7/12 | 5 | High |
| Food Cost AI: insight, recipes, invoices (AI) | ✅ | 10/14 | 4 | High |
| Recommendations/CFO/Value (AI) | ✅ | 7/11 | 4 | High |
| Home Brief (MOD) | ✅ | 13/21 | 8 | Medium |
| Accessibility & Theming (CLIENT) | ⚠️ partial | 3/9 | 6 | Medium |
| Schedule Intel: outcomes, ledger, learning (SCHED) | ✅ | 5/10 | 5 | Medium |
| Shift Quality Engine (SCHED) | ✅ | 12/16 | 4 | Medium |
| Admin UI (CLIENT) | ✅ | 2/6 | 4 | Low |
| **Total (44 rows)** | 34 ✅ · 10 ⚠️ · 0 ❌ | **604/1,187 (50.9%)** | **583** | 19 Critical · 20 High · 4 Medium · 1 Low |

By area:

| Area | Covered/identified | Missing |
|---|---|---|
| SEC | 40/97 | 57 |
| DATA | 31/75 | 44 |
| AI | 85/134 | 49 |
| MOD | 356/641 | 285 |
| SCHED | 64/127 | 63 |
| CLIENT | 28/113 | 85 |

No feature has zero happy-path coverage. The gap is edge cases: about half of the 1,187 enumerated cases have no test. CLIENT has the lowest coverage at 25%, and none of its P0 or P1 findings is tested.

---

## 22. Unable to verify from implementation

These are the items the six audits could not settle from the code, merged where they overlap.

**Infrastructure and configuration**
- Whether Railway appends to or replaces a client-supplied `X-Forwarded-For`. This decides how exploitable SEC-3 and CLIENT-40 are. ProxyFix is configured assuming it appends.
- Whether `DOCUSIGN_WEBHOOK_SECRET` is set in production (SEC-11 / MOD-BIL-7).
- The Railway container's timezone. No TZ is set in the repo, so it is UTC by default. This decides MOD-REV-13.
- Whether a second scheduler process can take the lease mid-pass under the current single-process start command (SCHED-11, DATA-4). The mechanism is confirmed. Whether it occurs depends on running worker.py or on overlapping containers.
- Whether roughly 2.3 GB of image decode OOM-kills the Railway container, which depends on the plan (MOD-MKT-13).
- Behaviour on Safari, Firefox and Edge. Only a Chromium-based pane was used (section 11).

**Vendor behaviour**
- Stripe. The `stripe` package is not installed in the audit interpreter, so the following were read but not run (SEC-40, MOD-BIL-7, AI-33):
  - The production signature-verification path.
  - Behaviour with an empty `STRIPE_WEBHOOK_SECRET`.
  - The SDK's default HTTP timeout.
- Whether Stripe prevents a second payment when two Checkout sessions are open for one restaurant (DATA-38 / MOD-BIL-5).
- Resend:
  - The maximum attachment size (DATA-18 / MOD-PERF-7).
  - The account's current per-team rate limit (MOD-EML-3).
  - Whether a malformed or multi-mailbox From header is rejected (MOD-EML-2).
  - Whether duplicate sends are deduplicated without an `Idempotency-Key` (MOD-EML-8).
- Whether Twilio's carrier-level STOP or Advanced Opt-Out blocks delivery for the punctuated or phrase forms, or after a web re-subscribe (MOD-MKT-8, SEC-17).
- Which days a truncated Toast sync loses, because Toast's sort order is unknown (AI-3 / MOD-LAB-4).
- Whether Toast returns `paidMinutes` on an open time entry (MOD-LAB-21).
- Whether Google changes a Places review's `time` field when the review is edited, which would fork the `external_id` (MOD appendix R1 #20).
- Whether the live Messages API rejects `tool_use` / `tool_result` history without `tools`. It historically did (AI-8).
- Whether the model can be steered by guest text into calling `remember` or `edit_review_reply`. The unguarded execution path is certain (AI-16).
- Which mail gateways pre-fetch links, triggering the GET "not me" and unsubscribe endpoints (SEC-7, MOD-EML-5).
- Whether sending the Toast opt-in invite without prior consent is lawful (MOD-MKT-12).

**Measurements not available**
- The per-restaurant fetch time `t` in production, which sets review-fetch coverage (DATA-7).
- How long a `prune_ledgers` DELETE holds the lock at production table sizes (DATA-40).
- Very large rosters were evaluated from constants and local timings, not a live model call (SCHED-24).
- Whether regenerating selected days drops edits saved during the job (SCHED answer 2; schedule_engine.py:1877-1893). Medium confidence, not probed.
- Whether a later view re-validates a published week against newly approved time off (MOD-EMP-8).
- Whether the email and push channels still carry an alert when Twilio fails (AI-27). Not traced end to end.
- Server-side device-row lifetime after an iOS sign-out (CLIENT-8). Medium confidence.
- Whether an in-flight iOS Home load re-saves the previous account's summary after `purgeAll()` (CLIENT-2). Medium confidence.
- CLIENT's P2 and P3 iOS findings were produced by sub-audits and not re-read line by line. They carry the confidence stated in CLIENT.md.

**Settled from code during this merge**
- MOD appendix R6 #5 asked whether `run_review_request_followups` gates on `billing_status`. It does not: the query at guest_marketing.py:1018-1028 has no billing predicate. This confirms DATA-51's claim and is folded into DATA-51 / MOD-REV-2.
- DATA-2 cited RECOVERY.md:92-102 for the restore. The `mv`, `rm` and `cp` commands are at RECOVERY.md:113-115. The claim stands.

**Cleanup candidates**
- MOD-REV-18 (the discarded "urgent" query), and MOD-MKT-18's `/api/debug-insights` and `/api/meta-review-test` routes, did not get the CLAUDE.md ten-point reference trace. They are Candidate for future cleanup after additional verification, not recommended deletions.

## 23. Edge Case Test Matrix (after the new tests)

Every [MISSING] edge case from sections 16 and 21 now has an automated test, apart from nine that need a real browser, device, second process or live third party. A test that exposes a confirmed defect asserts the **correct** behaviour and is a strict expected failure naming the finding (`@pytest.mark.xfail(strict=True, reason="SEC-1: …")`, Swift `XCTExpectFailure(…, strict: true)`). It turns into a hard failure the day the defect is fixed, so the marker is removed with the fix.

| Feature | Happy Path Tested | Edge Cases Covered (before → after) | Missing after | Not automatable | Risk |
|---|---|---|---|---|---|
| Marketing incl. guest SMS & newsletter (MOD) | ✅ (now tested) | 66/135 → **135/135** | 0 | — | Critical |
| Food Cost (MOD) | ✅ | 88/144 → **144/144** | 0 | — | Critical |
| Reviews (MOD) | ✅ | 44/75 → **74/75** | 1 | 1 | Critical |
| Labor Analytics (MOD) | ✅ (now tested) | 20/50 → **50/50** | 0 | — | Critical |
| iOS App Core: APIClient, auth, caching, push (CLIENT) | ✅ | 7/26 → **22/26** | 4 | 4 | Critical |
| Notifications & Alerts (MOD) | ✅ | 27/44 → **44/44** | 0 | — | Critical |
| iOS Reviews/Marketing/Food Cost/Intel screens (CLIENT) | ✅ (now tested) | 3/20 → **20/20** | 0 | — | Critical |
| Authentication & Sessions (SEC) | ✅ | 10/24 → **24/24** | 0 | — | Critical |
| Schedule Generator: model + pipeline (SCHED) | ✅ | 11/24 → **24/24** | 0 | — | Critical |
| Permissions & Roles (SEC) | ✅ | 5/16 → **16/16** | 0 | — | Critical |
| Schedule Publish & Versions (SCHED) | ✅ | 7/18 → **18/18** | 0 | — | Critical |
| Shift Requests: drop/swap/claim (SCHED) | ✅ | 4/15 → **15/15** | 0 | — | Critical |
| Data Layer & Migrations (DATA) | ✅ | 6/16 → **16/16** | 0 | — | Critical |
| Background Scheduler: loop, lease, cursors (DATA) | ✅ | 9/18 → **18/18** | 0 | — | Critical |
| Ask Cavnar AI (AI) | ✅ | 15/23 → **23/23** | 0 | — | Critical |
| External Integrations: email/SMS/push/weather/POS/Google (AI) | ✅ | 11/19 → **19/19** | 0 | — | Critical |
| Staff Portal: PIN auth (SEC) | ✅ | 11/18 → **18/18** | 0 | — | Critical |
| Backup & Recovery (DATA) | ✅ | 5/12 → **10/12** | 2 | 2 | Critical |
| AI Platform: retry, budgets, ledger (AI) | ✅ | 16/23 → **23/23** | 0 | — | Critical |
| Employees & Team (MOD) | ✅ | 27/50 → **50/50** | 0 | — | High |
| Emails (MOD) | ✅ (now tested) | 21/42 → **42/42** | 0 | — | High |
| Web Dashboard: shell, navigation, polling (CLIENT) | ✅ | 5/24 → **23/24** | 1 | 1 | High |
| Intel data (MOD) | ✅ (now tested) | 29/44 → **44/44** | 0 | — | High |
| Billing & Contracts (MOD) | ✅ (now tested) | 21/36 → **36/36** | 0 | — | High |
| CSV/File Imports (SEC) | ✅ (now tested) | 3/14 → **14/14** | 0 | — | High |
| Staff Portal UI: portal, PIN login, `/s/` link (CLIENT) | ✅ | 6/16 → **16/16** | 0 | — | High |
| iOS Labor & Scheduling screens (CLIENT) | ✅ (now tested) | 2/12 → **11/12** | 1 | 1 | High |
| Async Jobs & Delayed Actions (DATA) | ✅ | 6/15 → **15/15** | 0 | — | High |
| Settings & Restaurant Config (DATA) | ✅ | 5/14 → **14/14** | 0 | — | High |
| Webhooks: Stripe/DocuSign/POS (SEC) | ✅ (now tested) | 6/13 → **13/13** | 0 | — | High |
| Admin Console (SEC) | ✅ | 5/12 → **12/12** | 0 | — | High |
| Review AI: analysis + drafting (AI) | ✅ | 12/19 → **19/19** | 0 | — | High |
| Schedule Rules & Compliance (SCHED) | ✅ | 13/20 → **20/20** | 0 | — | High |
| Marketing AI (AI) | ✅ | 7/13 → **13/13** | 0 | — | High |
| Schedule Economics: trim, cost, stagger (SCHED) | ✅ | 6/12 → **12/12** | 0 | — | High |
| Time Off & Availability (SCHED) | ✅ | 6/12 → **12/12** | 0 | — | High |
| Intel AI: competitors + AI visibility (AI) | ✅ | 7/12 → **12/12** | 0 | — | High |
| Food Cost AI: insight, recipes, invoices (AI) | ✅ | 10/14 → **14/14** | 0 | — | High |
| Recommendations/CFO/Value (AI) | ✅ | 7/11 → **11/11** | 0 | — | High |
| Home Brief (MOD) | ✅ | 13/21 → **21/21** | 0 | — | Medium |
| Accessibility & Theming (CLIENT) | ✅ (now tested) | 3/9 → **9/9** | 0 | — | Medium |
| Schedule Intel: outcomes, ledger, learning (SCHED) | ✅ | 5/10 → **10/10** | 0 | — | Medium |
| Shift Quality Engine (SCHED) | ✅ | 12/16 → **16/16** | 0 | — | Medium |
| Admin UI (CLIENT) | ✅ | 2/6 → **6/6** | 0 | — | Low |
| Write-route idempotency & atomicity (DATA, found during test writing) | ✅ | 0/16 → **16/16** | 0 | — | Critical |
| **Total (45 rows)** | all ✅ | **604/1203 (50.2%) → 1194/1203 (99.3%)** | **9** | 9 | |

**The nine not automated here:** Reviews — whether an edited Google review changes its `time` (needs the live Places API). iOS App Core — offline-queue replay ×2 (the queue always sends through the shared client, which cannot take the mock), `currentUser` after an offline launch (no reconnect hook), deep links past the first 50 reviews (no UI test target). Backup & Recovery — an off-volume backup copy (DATA-18: there is no destination to assert against), reconciling job claims after a restore (DATA-20: the record lives in the restored tables). Web Dashboard — sideways scroll at 320 px (needs a layout engine; CSS proxies are tested). iOS Labor — "today" in the restaurant's time zone (the app never receives it).

### What was added

| Area | Files | New tests passing | New tests xfail (known defect) |
|---|---|---|---|
| Security, auth, imports, webhooks, admin | `tests/test_edge_sec_*.py` (7) | 60 | 132 |
| Data layer, jobs, scheduler loop, backups, settings, idempotency | `tests/test_edge_data_*.py` (10) | 29 | 118 |
| AI paths and outside services | `tests/test_edge_ai_*.py` (9) | 85 | 109 |
| Schedule pipeline | `tests/test_edge_sched_*.py` (9) | 41 | 85 |
| Reviews, labor, food cost, home, performance | `tests/test_edge_mod_a_*.py` (15) | 86 | 166 |
| Marketing, intel, team, notifications, email, billing | `tests/test_edge_mod_b_*.py` (11) | 105 | 217 |
| Web dashboard, staff portal, admin, accessibility | `tests/test_edge_client_web_*.py` (6) | 298 | 65 |
| iOS app | `ios/CavnarAI/CavnarAITests/Edge*Tests.swift` (8) | 21 | 92 |
| **Total** | **75 files** | **725** | **984** |

Per-area counts are each writer's own run of its files (parametrized cases counted separately); iOS counts are XCTest's. The whole Python suite reports 890 expected failures, two fewer than the per-area sum after the isolation fixes.

**Suite after the change:** Python 4,279 passed, 5 skipped, 890 xfailed, 0 failed; iOS 227 executed, 1 skipped (the opt-in live TLS test), 0 failures, stable across three runs. The Python suite now runs in parallel (pytest-xdist, `-n auto --dist loadfile`): 3 min 51 s, against 14 min 36 s serially.

### Defects found while writing the tests (not in sections 18-19)
- Web 2FA "Resend code" never works: `/resend-2fa` splits the `rid:uid:secret` token in two, so the secret never matches (auth_routes.py:406-420).
- Admin "reset password by restaurant" returns 500 on every call: it passes `current_user` twice (admin_routes.py:866).
- Imported reviews are never analysed in the background: the import calls `analyser.process_new_reviews`, which does not exist, and the error is swallowed.
- Ask Cavnar returns 502 for the whole question when a history entry's content is a list, number or object (`ask_cavnar._sanitize_history`, ask_cavnar.py:847).
- After the schedule-delete foreign-key failure (DATA-55) the database stays write-locked until the error is fully handled.
- Six more from MOD appendix lines with no finding: churned restaurants' guests still get review-request texts; a review fetched before Google was connected is approved with no reason it cannot post; approve-all refreshes the Google token once per review; a 2019 backlog uses up the auto-approve daily cap; "Tomato" and "tomato " import as two ingredients; an empty below-par item with zero recorded usage never reaches the order.
- A US number typed with an extension is stored as the foreign number +630555012345; the HELP reply doesn't name the restaurant; a campaign has no maximum length.

### The test suite itself was writing to the developer's database (fixed)
The shared `db_path` fixture's `init_db` ran `ensure_columns()` against the default path, and the default was `./reviews.db` in the repository root; the Simple EJ's demo seed also wrote its shift data there because four calls dropped `db_path`. A full run grew the real `reviews.db` by about 550 KB, and eight existing tests passed only because that database happened to contain the right tables and rows. `tests/conftest.py` now points the default at a throwaway volume, pre-created so the legacy-adoption code does not copy the real database into it, and builds the same schema the app builds at boot. `demo_seed.py` passes `db_path` through. Verified: `reviews.db` has the same size and timestamp before and after full serial and parallel runs.

### Older tests that pin a defect and must change with its fix
`test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role` (SEC-1), `test_mobile_connections.py::test_connect_toast_saves_but_reports_error_when_token_fetch_fails` (SEC-25), `test_email_delivery.py`'s GET-unsubscribe and 2023-timestamp webhook tests (MOD-EML-5/9), `test_security.py`'s bare-id join link (MOD-MKT-9), `test_marketing_features.py`'s retry-on-500 (MOD-MKT-4), and the existing queue-full push test (MOD-NOT-12).
