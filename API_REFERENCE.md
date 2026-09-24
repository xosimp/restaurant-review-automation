# API Reference — Cavnar AI

This documents **patterns and resource groups**, not every individual route — there are 751 URL rules (680 unique paths) across the app, and the specific handler is always one `grep` away once you know which file and prefix to look in. That lookup is intentionally cheap; re-deriving the whole route table from scratch every session is not. Use this file to answer "which file, which blueprint, which auth" before opening anything.

## Blueprints (registered in `hosted_dashboard.py`)

| Blueprint | File | URL prefix | Auth | Routes |
|---|---|---|---|---|
| `client_bp` | `client_api.py` | `/api/*` (+ page and public-token routes: `/approve/<id>`, `/e/<token>`, `/s/<token>`, `/join/<token>`, `/u/<token>`, `/m/<token>.jpg`) | web session cookie (`auth.login_required`) | 194 |
| `mobile_bp` | `mobile_api.py` | `/mobile/api/*` | Bearer token (`auth.mobile_login_required`) | 205 |
| `strategy_bp` / `strategy_mobile_bp` | `strategy_routes.py` | each `_ROUTES` entry at `/api/…` **and** `/mobile/api/…` | session / bearer | 113 + 113 |
| `issue_link_bp` | `strategy_routes.py` | `/i/<token>` | signed token | 1 |
| `admin_bp` | `admin_routes.py` | `/admin/*`, plus `/privacy`, `/terms`, `/sms-optin-preview`, `/.well-known/security.txt`, `/og-image.png`, `/favicon.*`, `/api/competitor-intel`, `/api/send-referral`, `/api/export-reviews` | `auth.admin_required` (writes need `is_admin`; the `support` role reads) | 96 |
| `audit_bp` | `sales_audit_routes.py` | `/admin/audits/*` (+ a public shared report by token) | admin | 22 |
| `staff_bp` | `staff_routes.py` | `/staff/*` — PIN sign-in, today, availability, time off, pre-shift | staff session (`auth.staff_login_required`) | 21 |
| `auth_bp` | `auth_routes.py` | `/login`, `/logout`, `/forgot-password`, `/reset-password/<token>`, `/verify-2fa`, `/auth/*`, and the account-security `/api/*` routes (sessions, change-password, update-email, 2FA toggles) | none → issues session; the `/api/*` ones need a session | 22 |
| `webhook_bp` | `webhook_routes.py` | `/stripe-webhook`, `/webhooks/resend`, `/docusign/callback`, `/docusign/callback2`, `/docusign/webhook` (inbound) | signature-verified | 6 |
| `social_bp` | `social_routes.py` | Instagram/Facebook OAuth + `/api/post-to-facebook`, `/api/post-insights` | mixed | 9 |
| `toast_bp` / `square_bp` / `clover_bp` / `rpower_bp` | `*_routes.py` | POS connect + `/admin/<pos>/*` sync/bootstrap | mixed | 8 / 7 / 7 / 6 |
| `status_bp` | `status_routes.py` | `/status` public page + `/admin/status/*` | public / admin | 6 |
| app-level | `hosted_dashboard.py` | `/`, `/health`, `/sitemap.xml`, `/robots.txt`, `/og-image-v2.png` | — | 5 |

The outbound-webhook config routes (`GET/POST/DELETE /api/webhook`, `POST /api/webhook/test`) are in `client_bp`, not `webhook_bp`. `audit_app.py` is a separate standalone Flask app (the digital audit scorecard on :9000), not a blueprint. There is no web `/register` and no web `/me`; both exist only under `/mobile/api/` (and `/staff/api/me`). `hosted_dashboard` refuses to boot if two rules share a (path, method).

Two 409s a client may meet on writes. **`location_changed`**: the dashboard names the location a page was rendered for in `X-Cavnar-Restaurant-Id` (sent by `templates/_csrf_fetch.html` from the `cavnar-restaurant-id` meta tag); `auth.login_required` refuses a request whose id is not the session's current location, except `/api/switch-location` (DATA-10). A request without the header is unaffected. **Stale form**: a whole-form settings save (the phone profile save, both alert-settings saves) may carry `expected_version` — the `row_version` it loaded — and gets 409 with `current_version` if the row has moved on (`models.expected_version_from`, DATA-28); without it the save is last-write-wins as before.

## The `_m()` delegation pattern

58 of `client_bp`'s `/api/*` handlers are one line (the rest own their body or share a `_do_*` with mobile — see *Which pattern to use* below):
```python
@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)
```
`_m(name)` looks up `mobile_api.<name>` and unwraps its `@mobile_login_required` decorator (`__wrapped__`), so the web route calls the exact same function body the iOS route calls — just with the already-resolved `current_user` from the web session instead of a decoded bearer token. **When a route's real logic isn't there, look in `mobile_api.py` first.** A genuinely web-only route (no iOS equivalent — e.g. CSV export, a desktop-only settings page) is one of the minority that doesn't delegate.

**Which pattern to use for a new endpoint both surfaces need.** Prefer a `strategy_routes._ROUTES` entry: one body, registered at both prefixes by the loop at the bottom of that file, with `_rid`/`_body`/`_principal` helpers. Second choice: a `_do_*` body in `client_api.py` called from both routes. Last: `_m()`. Never a third: 34 pairs still re-implement the same logic on both sides (885 lines, one of which has already diverged in its error handling), and each is a fix waiting to be missed on the other surface.

## Resource groups

### Account (`/api/account*`, `/mobile/api/account/*` — largest single group, ~36 mobile + ~26 client routes)
Profile, security (2FA setup/verify, backup codes, trusted devices, sessions list + revoke-others), team (invite/list/revoke — owner-role only), sign-in history, notification/alert-channel preferences, data export, billing status, marketing-email opt-out, timezone, connections status.

### Labor (`/api/labor*`, `/mobile/api/labor/*` — ~27 mobile + ~17 client)
Shift CSV upload, labor % + ribbon data, schedule generation/history/sharing, staff availability, staff contacts (for schedule delivery), Operational Score (`team`), scheduling profiles (demand levels), overtime/overstaffed-day detail.

**Schedule generation, review and publish** (both prefixes unless noted; bodies in `strategy_routes._ROUTES` except the first four):
- `GET /api/generate-schedule` / `POST /mobile/api/labor/generate-schedule` → `{job_id}` (needs `SCHEDULE_DRAFT`); returns `joined: true` when a generation for this restaurant is already running (`ops.active_job`) — poll that job. `GET …/schedule-status/<job_id>` → the finished payload: `preview_rows` (rows may carry `needs_review`/`review_reason`), `summary` (deterministic diff lines against the last published week), `narrative` (the model's own note), `quality` (each `shifts[].assignments[]` carries a `why` explanation), `review` `{hard, soft, by_kind, lines, hard_rows, fixes, unfixed}`, `rule_violations`, `pending_time_off`, `history_id`, `generation_seconds`, `chunked`, `roster`, `demand_by_date`, `learned_patterns`.
- `POST labor/schedule/score` `{rows, save?, history_id?}` → `{quality, violations, review}`; a save needs `SCHEDULE_DRAFT`, appends an `edited` version and writes `review_json`.
- `POST labor/schedule/violations` `{rows}` → `{violations, review, pending_time_off}` — re-check after an edit. `POST labor/schedule/apply-fixes` `{rows}` → `{rows, fixes, unfixed, quality, review}` — puts somebody legal on every hard breach; the owner still saves.
- `POST labor/publish-schedule` `{schedule_id?, acknowledge?}` (needs `SCHEDULE_PUBLISH`) → `409 {needs_ack, blockers[]}` until a human acknowledges (`client_api.publish_blockers`: NEEDS REVIEW rows, hard breaches, a weak or low-confidence quality verdict); on send stamps `published_at`/`published_by` and appends a `published` version. Friday auto-publish holds instead and tells the owner (`schedule_publish_held`).
- `GET labor/schedule-history/<id>/versions` → `{versions:[{version, reason: generated|edited|fixes|published, saved_by, changes, lines, score}], draft_vs_published}`.
- Roster and rules: `GET labor/roster` (everyone incl. deactivated, with settings, score, reliability, pairs), `POST labor/staff-settings` (active, employment_type, min/max hours, per-day daypart availability, is_minor), `POST labor/staff-pairs` / `DELETE labor/staff-pairs/<id>` (prefer/avoid), `GET/POST labor/rules` (compliance rules + per-role floors), `GET/POST labor/demand-signals` + `DELETE labor/demand-signals/<id>` (events and reservation counts per date; POST takes rows or a `date,covers` CSV).
- Shift requests (manager): `GET labor/shift-requests` → `{requests, open}` (rows carry `kind` drop|swap and, for swaps, the target shift); `POST labor/shift-requests/<id>/decide` `{decision: approve|deny, replacement?}` — approving a swap moves both shifts after the legality check runs both ways.
- Generation options: `POST generate-schedule` `{week_start?, dates?, history_id?}` — pick the week, or regenerate only some days of a draft with the rest pinned. The finished payload also carries `trimmed`/`hours_trimmed` (the deterministic trim back to the budget), `staggered`, `projected_cost` (overtime at 1.5× past the ceiling), `over_budget_dollars`, `projected_revenue_source`, `hourly_profile_ready`, `demand_data_through`, `reservation_feed`, `holiday_lift`, `could_hold`, `departments`, `regenerated_dates`.
- Saves: `POST labor/schedule/score` takes `version`; a newer version on file returns `409 {conflict, latest_version, saved_by, lines}`. A save to a published week emails only the people whose shifts changed and returns `changed_since_sent`. `POST labor/schedule/violations` with `baseline_rows` adds `cost` (hours and overtime-priced dollars the edit moves).
- Rules (extended): `GET/POST labor/rules` also carry `jurisdiction` + `pack` (compliance_packs), `role_arrivals`, `role_close_mins` (the last of a role stays until N minutes after close), `manager_rule_unusable`, `role_requirements` (certifications by role), `foh_roles`, `patio_roles`, `role_cross_training` (whole percents per role; GET also returns `cross_training_defaults` per scheduled role and the flat `cross_training_default`), `trim_to_budget`, `rules.manager_on_duty`, and the reservation provider (`reservation_provider`, `reservation_api_key`; `POST labor/reservations/sync`).
- Roster (extended): settings carry `time_windows`, `certifications`, `preferred_dayparts`, `desired_hours`; `GET labor/roster` adds `suggested_pairs`.
- `GET labor/learned-patterns` / `POST labor/learned-patterns` `{key, dismissed}` — the owner's say over what the draft learns. `POST labor/schedule/recommendation` `{kind, key, action}` — the accept/dismiss ledger. `GET labor/intel` — outcomes by daypart, the rotation ledger, behaviour-learned preferences, who could hold a station, suggested pairs, sales per labor hour, the revenue projection; `edit_prediction` (ready or why not, and its held-out hit rate on past drafts), `weight_calibration` (per dimension: current, suggested, step, the driving outcome and an `explanation`), `rotation` (the multi-week plan and its `lines`), `splh_objective` (the targets and their basis) and `starting_points` (a borrowed starting headcount for a restaurant with no history, or the reason there is none). A generated schedule also carries `likely_edits` (predicted rows carry `likelihood` and `reason`), `rotation_plan`, `splh_objective`, `splh_report` and `starting_headcount`.

### Reviews (`/mobile/api/reviews/*`, `/mobile/api/review-stats`; web routes are flat — `/api/reviews/page`, `/api/review-stats`, `/api/topic-heatmap`, `/api/sentiment-trend`, `/api/response-performance`, `/api/regenerate-draft/<id>`, `/api/save-draft/<id>`, and the actions at root: `/approve/<id>`, `/skip/<id>`, `/undo/<id>`, `/retract/<id>`)
List/detail/approve/skip/regenerate a draft response, retry a failed Google post (`/reviews/<id>/retry-post`, both surfaces), bulk approve-all, sentiment trend, topic heatmap, response-performance, review-request send, review stats.

### Food Cost (`/mobile/api/food-cost/*`, `/api/food-cost*`)
Inventory CSV upload, custom items, purchase orders, waste/drift insight.

### Marketing (`/mobile/api/marketing/*`; web routes are mostly flat — `/api/mkt-stats`, `/api/mkt-performance`, `/api/mkt-insight`, `/api/content-calendar`, `/api/post-to-google`, `/api/brand-voice`, `/api/generate-content`, plus `/api/marketing/*` for drafts, schedule, links, media, attribution, diagnosis, tags)
Drafts (create/edit/list), media upload, scheduling, attribution per post, the text-club feature (`/api/guest-contacts*`, `/api/guest-campaign/draft|send`, `/api/guest-newsletter`, `/api/guest-qr`, `/api/guest-segments`, `/api/public/guest-optin/<token>`), templates.

### Intel (`/mobile/api/intel/*`, `/api/intel*`, `/api/ai-visibility*`)
Competitor snapshots, AI-visibility run trigger + results.

### Ask Cavnar (`/mobile/api/ask-cavnar/*`, `/api/ask-cavnar*`)
`opening` (deterministic briefing, no model call), `conversations` (list/create), `history` (a conversation's messages), the streaming chat endpoint itself (SSE), and the tool-confirmation route a `write`-kind tool's proposal card posts to. Each proposal in an answer carries `proposal_id`, `details` (`[{label, value}]`), `preview` (the words that would go out) and `at_stake` (dollars, or null); `POST ask-cavnar/action` takes `{action, outcome, proposal_id?, reason?, summary, conversation_id}` — a `proposal_id` not this restaurant's (or naming another action), or proposed to another login when the caller is not an account principal, is a 404 (re-audit B6); the audit line quotes the proposal's own summary (a client summary is capped at 200 characters, a `body` over 4,000 is dropped) and lands in `conversation_id` only when that chat is the caller's own. A turn saved without a conversation lands in the CALLER's current chat, never another login's (B5); `conversations` previews and counts only the turns the caller may read, and `DELETE ask-cavnar/history` clears only the caller's chats.

### Connections (`/mobile/api/connections/*`)
Per-integration connect/disconnect/status: Google Business, Instagram, Toast, Square, Clover.

`GET /mobile/api/account` → `connections` (CA3 F6/F13): `toast`, `square`, `clover` and now `rpower` each `{connected, last_synced, error, sync_state, age_days}` — `sync_state` from `pos_health.provider_state` (current | aging | stale | error | unknown | not_connected); `pos` is `pos_health.pos_sync_state` for the provider in use; `google_business` adds `source` (gbp | places_sampled | none) and `label` ("Google reviews (sampled — Places returns 5 at a time)" for Places-only). All new keys are additive; clients decode them optional.

`POST /mobile/api/connections/toast` and the web twins (`/api/toast/save`, `/admin/toast/save/<id>`) refuse the literal "demo" credentials unless the restaurant is flagged `is_demo` (CA3 F8).

Admin: `/admin/api/client/<id>` → `client.integrations[]` lists `rpower` beside toast/square/clover, each POS row with `sync_state`/`age_days` and an `error` when a sync simply stopped (stale, no error written); `client.freshness.pos_state` is `pos_health.pos_sync_state`; `client.freshness.inventory` is the newest `ingredients.last_recount_at`; `client.setup_completeness` `{score, checks, label: "Setup completeness"}` — `data_completeness` is the same object, kept as an alias until admin.html reads the new key.

### Home (`/api/home/brief`, `/api/home/brief/group`, `/mobile/api/home`, `/mobile/api/home/modules`)
The deterministic Home-tab payload — see `home_brief.py`. `?fresh=1` forces recompute past the 60s per-restaurant cache. `/group` returns every location in an owner's `location_group` side by side.

**Recommendation Confidence — contracts K1/K4** (confidence audit, 9/24/26; `rec_trust.assess`, see `INTELLIGENCE_ENGINE.md` → Recommendation Confidence). Every Home card (`recommendations[]`) and attention item (`attention[]`; mobile `needs_attention[]`) carries `confidence: {pct (0-100 | null), band (low|medium|high, derived), label ("72% confidence" | "Confidence not yet measurable"), reason, score (pct/100, 0.0 when null — iOS decodes it non-optional), caution, dimensions: {evidence: {pct, basis, n, kind}, accuracy: {pct|null, basis, n, improved, source: own|cohort|none, low, high}, freshness: {pct|null, basis, as_of (M/D/YY), as_of_iso, stalest}}, version: 1}`. Cards no longer carry `strength` (clients must tolerate it missing); a card with dollars carries `dollars_basis` (the figure's scope); the labor-over attention item carries `dollars_weekly` + `dollars_basis`; a POS sync failure (any provider, RPOWER included) is the critical attention `pos_sync` with `provider`. `freshness[]` entries are `{module, source, state (current|aging|stale|not_connected|unknown|sample), pct, as_of, as_of_iso, basis, error}` plus the legacy `key/label/at/note`; `brief.data_as_of` is the stalest source's M/D/YY date; `monitoring: {count_live (current sources only), sources, stalest_as_of}`. `/mobile/api/home` carries the same `freshness`, `monitoring`, `data_as_of`. The one-thing hero (`GET /cross-module` → `fix_first`) carries `confidence` (K1), `claim_kind`, `model_written`, and `dollars_basis` when it has dollars; the morning brief's `money` line carries `money: {low, high, label}` so a range is never collapsed to one figure. Where an older client decodes a field named `confidence` as a string — review / food / labor / campaign diagnosis blocks, food cost drivers (and the food brief's `fix_first`), the Reviews read's "Do today" rec — `confidence` stays the band word and the K1 object is `confidence_detail` (drivers also carry `evidence_input`).

`GET /mobile/api/home/modules` (web twin `GET /api/home/modules`) → `{ok, modules}` — the Modules grid alone: the same `modules[]` tiles `/mobile/api/home` carries (`key, label, icon, status, kpi, pulse`), built by `mobile_api._home_module_tiles`. It builds no Home brief and records nothing (re-audit K5): the iOS Modules tab used to fetch all of `/mobile/api/home` for this list, and every open logged Home's cards as shown on a screen that renders none of them. Ask's opening fallback (`GET /mobile/api/ask-cavnar/opening` and its web twin, when the morning brief has no lines) builds Home with `home_brief.build_home_brief(..., present=False)` — answered keys still left out, nothing presented, nothing cached.

**Value — contract K4** (`value_delivered.headline` + `home_block`, one shape on both Homes): web `/api/home/brief` → `value`, and `/mobile/api/home` → `value` (beside the older flat `total_value_delivered`, `value_label`, `value_by_module`, `value_history`, kept for old clients) = `{total, net_monthly, worsened: {count, monthly, priced_count}, cumulative: <outcomes.cumulative>, unpriced_wins: [{title, module, line}], sales_lift: {monthly, net_monthly, wins, basis}, history, per: "month", label, by_module, caveat}`. `total` is the measured improvements per month; a surface shows `net_monthly` when `worsened.count > 0`. All of it is as THIS login may see it (`viewer_scope`); the day's `value_snapshots` point — what `history` draws — is the restaurant-wide **net** figure, and is written only for a login that sees everything.

### The owner's day (`strategy_routes.py`, web + mobile twins)
`actions` (everything still open, filtered by what the reader may see), `actions/snooze` `{key, days}`, `closeout` (GET tonight's + the questions: `fields`, `dsr_fields`, `labels`; POST any of the ten fields — the four quick lines and `equipment`, `vip_guests`, `maintenance`, `shift_notes`, `general_notes`, `influence`; a field left out keeps what was filed, an empty string clears it). `morning-brief` now returns the caller's own brief plus `can_edit`; `morning-brief/settings` also takes `hold_alerts` and `preshift_nudge_hour`. `GET morning-brief?view=home` (K6) is the read Home's "Before service" card makes: the lines it renders — every keyed line but `fix_first` and `money`, which Home's focus card shows — are presented on `home` for this login, once per key a day. Without `view=home` (the settings screens) nothing is recorded.

### The nightly DSR (`strategy_routes.py`, each at `/api/…` and `/mobile/api/…`)
Any console login; employee and support logins get 403. Every payload carries `view` — `owner` for an owner login at the location (owner / client role, or a Cavnar admin), `manager` for every other console login — decided by `dsr.access.view_for`. Both views render from the same stored facts; the manager view drops blocks the role can't read (Food without FOOD_COST_VIEW, …), loss lines (comps / voids / refunds) without LOSS_VIEW, and owner-only financials (`budget*`, `vs_budget*`, `prime_cost*`, `source_checks`), and the narrative items that cite them. Always the session's own restaurant.
- `GET dsr?limit=&before=` — recent nights, latest version each: `{business_date, label (M/D/YY), version, status, provisional, missing, finalized_at, net, lead, lead_missing}` — `net` only when the Sales block is ready (never 0 for unknown), `lead` the summary's opening sentence as this login may read it, `lead_missing` why there is none. The list also carries `enabled` (`dsr_enabled`) and `tonight` (the business date Close day would run). Home's "Last night" card reads `limit=1`.
- `GET dsr/<YYYY-MM-DD>?version=` — the night: `facts` (blocks, `missing`, `withheld`, `fiscal`), `narrative`, `checklist`, `versions` (each with `finalized_at_local` / `created_at_local`, the restaurant's wall clock), `status`, `provisional`, `trigger`. 404 when there is none; 400 on a bad date. The web screen is `/#dsr/YYYY-MM-DD` — the link every DSR email's "View full report" carries.
- `GET dsr/<YYYY-MM-DD>/status` — the progressive checklist: `stages` [{key, label, at, at_local, done, current}], `blocks` [{name, label, status, reason, at, at_local}], `narrative`, `closed_by`, `next_attempt_at`, `missing`; `exists: false` before the night has started.
- `POST dsr/close` `{date?, rerun?}` — Close day: starts the night now (trigger `manual`) on a background thread; 202 `{started: true}`, 200 `{started: false, status: "final"}` when it's already final. `rerun: true` re-runs a finished night as a new version — owner view only (403 otherwise, checked before the date). `date` defaults to tonight's business date; a manager may close only tonight or the night before, an owner any of the last 7 business dates (`DSR_OWNER_DAYS`) — the "generate one if the automation failed" path (400 outside the window); 409 when `dsr_enabled` is off; rate-limited per restaurant.
- `GET dsr/week?date=` / `GET dsr/period?date=` — Erik's weekly grid and the fiscal period (`dsr.rollup` through `access.redact_grid`: budget columns for the owner view only; labor columns only with LABOR_VIEW). Each day row carries `gross_basis` (`items` | `all` — the basis that night was built under, read from the night itself; null with no report). Every totals block (week, period to date, each week of a period, the period) carries `days_measured` (nights with a net), `<column>_days` per summed column — `gross_days` can be fewer than `days_measured` when an everything-rung night had no tax or voids from the POS — `gross_bases` (the sorted set of bases the gross total adds) and `gross_mixed` (true when that set has two). A period's length is its own year's (a listed 53-week year's five-week period is five weeks).
- `GET dsr/week.xlsx?date=` — the week as a real .xlsx (`dsr.xlsx.week_workbook`) in the grid's layout: title row "Simple EJ's · Period 9 · Week 4 · 9/16/26 – 9/22/26", header, a row per day, week total, period to date; `$#,##0` and `0.0%` formats, frozen header; built from the same redacted payload, so a manager's file has no budget. `Content-Disposition: attachment`.
- `POST dsr/budget` `{date, gross, net}` or `{days: [{date, gross, net}, …]}` (≤14) — owner only (403 for every other login). A blank figure clears it; negative or non-numeric is 400.
- `POST dsr/category` `{pos_name, category}` — owner only. Map a POS department to one of the six (matched case-insensitively) or the owner's own label; "Unmapped" is refused. Counts from the next report on.
- `GET dsr/settings` / `POST dsr/settings` — owner only. GET: `settings` {`fiscal_week_start_dow` (0=Mon…6=Sun), `fiscal_year_start`, `fiscal_period_scheme` (`4x13` | `445` | `454` | `544` | the exact period lengths in weeks, comma-separated: 12–13 of 4 or 5 totalling 52 or 53), `dsr_enabled`, `dsr_notify` (email + push the finished report; off by default), `dsr_gross_basis` (`items` | `all`: gross as items only, or everything rung incl. tax and voids), `dsr_deadline_hour`, `calendar_label`, `fiscal_years` — the years that run differently, `[{start, start_label (M/D/YY), lengths, weeks, fiscal_year}]`, `[]` when none}, `categories`, `category_map`, and `unmapped` — the departments the latest report couldn't place, with their dollars (`unmapped_as_of`). POST: a JSON object with any of the eight settings — `fiscal_period_scheme` also as a list of ints, `fiscal_years` as `[{start: "YYYY-MM-DD", lengths: [ints] | "445"…}]` (`[]` or null clears it); Period 1 and every listed year must start on the week's first day, listed years must not overlap (400 otherwise, the reason in M/D/YY). A value of the wrong type — a list or object for `dsr_gross_basis`, a bool for `dsr_deadline_hour`, a body that is not an object — is a 400 with the reason, never a 500.
- `POST dsr/history/import` — **owner view only** (403 otherwise). Last Year from the owner's old DSR workbooks: multipart field `file`, `.xlsx` or `.csv`, at most 2 MB (the app's 5 MB cap answers 413 above that). Reads the template layout today (`dsr.history_import`; header row `Date, Gross, Net, Food, Liquor, Beer, Wine, Retail, NA Beverage`, matched loosely — case, spacing, "Day", "NA Bev", "Net Sales"; every sheet with that header; Excel serial or M/D/YY / ISO dates; `$1,234.50` and `(12.00)` figures). A blank cell is never a zero: a day with neither gross nor net is `skipped`; a bad figure, bad or future date, or a repeated date is an entry in `errors` ("P9 W1 row 6: Gross '$abc' isn't a number.") and that row alone is not imported. 200 `{ok: true, imported, skipped, errors: [...]}` (a re-import of a day replaces it; `dsr.rollup`'s Last Year reads it); 400 `{ok: false, error, imported: 0, skipped: 0, errors: [error]}` when the file can't be read at all (wrong type, damaged, no matching header, empty, too large); rate-limited per restaurant (429). Always the session's restaurant. Erik's own weekly-grid workbook layout is not read yet — it is added as a second layout when his file arrives.
- `GET dsr/history/template.csv` — the import template, header row only (`text/csv`, attachment `dsr-last-year-template.csv`); any login with a DSR view.

### Strategic foundations (`strategy_routes.py`, each at `/api/…` and `/mobile/api/…`)
`issues` (GET list / POST create), `issues/<id>/resolve`, `issues/<id>/reassign`, `issues/<id>/ask-cover` (POST `{name}` — LABOR_VIEW; texts, or emails, one of the coverage issue's suggested covers the request; SMS only to a consented number), `issues/routing` (GET/POST, principal-only), `goals` (GET/POST), `goals/<id>/end`, `outcomes` (GET/POST), `outcomes/<id>/abandon`, `metrics`, `food-cost/dish-scorecard`, `food-cost/reprice` (each suggestion carries `rec_key` `reprice:<dish>`; answered dishes are left out), `food-cost/reprice/apply` (POST `{dish, price?}` — one tap to the suggested price, or the owner's adjusted one; records suggested vs chosen, answers the recommendation and starts the outcome tracker; 409 when the dish has no live suggestion — web `/api` and `/mobile/api` twins in client_api / mobile_api), `food-cost/invoices` (GET list / POST multipart `file`), `food-cost/invoices/<id>`, `food-cost/invoices/<id>/apply`, `food-cost/recipe-drafts` (GET — drafts plus `missing` and `ingredients` counts), `food-cost/recipe-drafts/<id>/accept|reject`, `food-cost/recipes/draft` (POST `{menu}` — the pasted menu, one dish a line with an optional price; empty body drafts the POS dishes with no recipe; at most `RECIPE_DRAFT_LIMIT` model calls a request, `remaining` says how many are left), `food-cost/recipes/import` (POST CSV), `food-cost/recipes/scan` (POST multipart photo — iOS only now), `labor/demand?day=`, `labor/auto-draft` (GET/POST), `loss-signals` (LOSS_VIEW: owners, or a manager granted it), `morning-brief` (the caller's own brief) + `morning-brief/settings` (principal-only). Notifications: `GET notifications` (scoped to what the login may see; marks read per login), `GET notifications/unread-count`, `POST notifications/opened` `{type, alert_id?, rec_key?}`, `GET notifications/engagement` (types sent a lot and never opened — a suggestion, not a change), `POST account/send-test-push` (the caller's own devices only, delivered inline so the response is what APNs said). Team roles: `POST /api/account/team/invite` takes `role` (client | manager | member); `POST /api/account/team/<id>/role` `{role}` (owner-only) and its `/mobile/api` twin. Team access: `POST /api/account/team/<id>/access` and `/mobile/api/account/team/<id>/access` — `{permission, enabled}` and/or `{morning_brief}`, owner-only. Public: `GET/POST /i/<token>` (issue link; GET is side-effect free, POST `action=ack|resolve|ask_cover` — `ask_cover` with `name`, one of the coverage issue's suggested covers). Staff portal: `GET /staff/api/preshift`.

### Outcomes and value — the rec-ROI contracts (`strategy_routes.py`, `client_api.py`, `mobile_api.py`; web and mobile twins share one body)
The outcome engine is `outcomes.py` / `metrics.py` / `value_delivered.py` (MODULE_OVERVIEW.md → Outcomes). Dates in machine fields are ISO; every owner-facing string (`label_text`, `reason`, `attribution_label`, `net_note`) is already M/D/YY.

- **`GET outcomes`** `?status=&ids=` → `{ok, outcomes: [row], caveat}` — newest 50, or with `ids` (comma-separated, ≤200) exactly those trackers of this restaurant however old (the timeline asks for the ones its page links to; a non-number is 400). Only rows this login may see (re-audit A26): the metric (food cost / waste need FOOD_COST_VIEW, `comp_rate` / `void_rate` need LOSS_VIEW) AND the recommendation behind it (`rec_learning.viewer_sees` on the linked episode: owner-only, a loss, a module it lacks) — an unseen row is simply absent. Each row also carries `checkin_key` — the recommendation key a check-in for it is sent against (`POST recs/checkin`), `null` when no recommendation stands behind the tracker (a manual one, a monthly reprice figure), so no surface offers a check-in the server would refuse. **Contract K7:** `summary` for a result the owner said they never made (check-in "no") carries no dollars — "…improved, but it isn't counted: you said the change wasn't made." — and `counts` (false for it) is what a surface colours by. Each row, besides the table's own columns and `metric_label`, `unit`, `summary`, `informational`:
  - `module` — who is credited: the recommendation's own module (`labor`, `inventory`, `reviews`, `marketing`, `intel`, `other`), never inferred from the metric when the recommendation named one.
  - `family` — the metric family (`labor_cost`, `food_cost`, `sales`, `guest_rating`, `reply_speed`, `comps`, `voids`).
  - `baseline_value`, `after_value`, `delta`, `delta_pct` — `delta` is after − baseline; `delta_pct` null when the baseline is 0 or unknown. `baseline_raw` is the before-window reading before any seasonal adjustment.
  - `baseline_kind` — `prior window` | `matched weekdays` | `same weeks last year`.
  - `attribution` — `none` | `associated` | `consistent` | `held` (null while tracking); `attribution_label` — the owner sentence; never claims cause (the `associated` level carries `caveat`'s sentence).
  - `concurrent` — `[{kind, label, date}]`, kind one of `tracker` | `accepted_rec` | `price_change` | `event` | `holiday` | `closure` | `sales_move` (a labor, food-cost, comp or void share whose sales moved past their band in the same weeks, "Sales per day rose 15% in the same weeks") | `trend` (the number was already moving this way before the change, by enough to explain the move — label "Already moving this way before the change"); `concurrent_checked` false for a row evaluated before this existed (its grade is then at most `associated`). The grade is also capped at `associated` by the owner's check-in (conditions changed, or "no") — at evaluation, at the re-check and at the check-in alike.
  - Readings: a window counted in trading days (labor %, sales, one weekday's sales, comps, voids) is `unknown` unless ≥ 70% of its trading days were measured (`outcomes.MIN_COVERAGE`); comps and voids are read only over the days the POS was asked about them. Weekday-sensitive trackers (labor %, food cost %, sales, comps, voids, waste) run whole weeks: a `window_days` of 30 is measured as 28.
  - `recheck_on`, `recheck_verdict` (`held` | `faded` | `reversed` | `unknown` | null), `recheck_value`, `rechecked_at`, `validated` (bool: improved, held at re-check, nothing else changed on its family), `counts` (still counted in Delivered), `result_line` (`"Average rating 4.2★ → 4.4★, improved"`), `window_days`.
  - `status=tracking` rows add `interim` `{value, delta, delta_pct, as_of, days_in, verdict, baseline, band, multiple}` (also `days`, = `days_in`) — a partial reading, labelled as such.
  - Calibration (confidence audit 9/24/26): `baseline_kind` may also be `before the trigger`; `trigger_start` / `trigger_end` / `trigger_value` (the window that fired the recommendation, ISO; null = not triggered); `baseline_overlaps_trigger` (bool — shown, `counts` false, never a win or a loss); `noise_band` (this restaurant's band in the metric's unit, null = the stated band), `noise_sigma`, `false_alarm_rate` (two-sided), `band_basis` (words); `grade_phrase` — the grade as one clause ("held" only for `held`). A check-in with `conditions_changed` now sets `counts` false, as "no" does. Advice not taken (`observed:untaken:*`) is never listed; `observed:supplier_order_sent:*` rows are `informational`.
- **`GET value`** → `{ok, delivered, avoided, opportunity, surfaced, ledger, promise?, rates}` — the four figures still never summed. Everything this login may not see (`value_delivered.viewer_scope`: denied modules, comps/voids without LOSS_VIEW, results whose recommendation it may not see) is dropped BEFORE `total_value`, `best_ever`, `cumulative` and `unpriced_wins` (re-audit A26). `delivered` adds:
  - `worsened` `{count, monthly, priced_count}` and `net_monthly` (= `monthly` − `worsened.monthly`; may be negative, and `net_note` says so), `net_by_module`. `monthly` stays the improvements alone. `count` includes unpriced results (a rating that fell); `priced_count` is how many make up `worsened.monthly` — "less $X from N that got worse" uses `priced_count`.
  - `annual` is `monthly` × 12 and `annual_basis` says so: a projection, not a measurement.
  - **Savings only (re-audit A6).** `monthly`, `worsened`, `net_monthly`, `by_module` and `biggest` are cost savings (labor, food cost, comps, voids, waste, overtime). A sales rise is gross revenue — money through the till, not profit — and is reported apart in `sales_lift` `{monthly, wins, worsened: {count, monthly}, net_monthly, by_module, basis}`, never added to the savings; `sales_pricing` is `"separate"`. A labor/food-cost/comp/void move over the same weeks as a sales move the same way is not counted as a saving (its share of sales fell because sales rose; re-audit A5). Within one family a narrower reading overlapping a broader one (overtime inside labor %, waste inside food cost %, a weekday inside sales, complaints inside the rating) is the broader one's — wins and losses netted per family before anything is summed (A7) — and of the rest, the non-overlapping set of largest dollars counts (A20).
  - Monthly dollars for a share of sales (labor %, food cost %, comps, voids, sales) are priced on the restaurant's own **trading days** (`metrics.days_per_month`: the weekdays it trades × 52/12 — 26 for a restaurant closed Mondays), and accrue only on days actually traded and measured (re-audit A1).
  - `validated_monthly`, `validated` (count), `faded` (wins that no longer held at re-check — no longer in `monthly`).
  - `consistent_monthly` / `consistent` (improvements that were a clear move or held) and `associated_monthly` / `associated` (tied to other changes, or past the band once) — the two parts of `monthly` by grade, quoted apart, never added to anything again; `cumulative.by_grade` `{consistent_or_held, associated}` is the same split of the measured days. The Home `value` headline carries `consistent_monthly` / `associated_monthly` too.
  - `wins_measured` (distinct improvements, priced or not), `wins_by_module` `{module: count}`, `unpriced_wins` `[{title, module, line}]` (rating and reply-time wins: counted, never priced).
  - `cumulative` `{total, gained, lost, since, until, days, measured_days, by_module, sales_lift, basis}` — a sum of measured days (`outcome_value_days`, summed in SQL: one reading per family and day, the broadest that held, then the largest; disowned trackers excluded), net of changes that got worse; `total` is null when no day has been measured. `sales_lift` is the same sum for sales readings (`{total, gained, lost, since, until, days, measured_days, by_module, basis}`, null when none), never added to `total`.
  - `rates` — the same object as the top-level `rates`: `{reply_rate, reply_rate_basis, reply_minutes, agency_monthly, agency_basis, schedule_minutes, invoice_minutes, overtime_multiplier, overtime_threshold_hours, overtime_basis, days_per_month, weeks_per_month, recheck_days, accrual_horizon_days, consistent_multiple}`. A surface pricing replies reads `rates.reply_rate`, never its own 5.
  - `ledger` adds `outcomes_improved` and its line.
- **Tracker-start replies.** Every door that can start a tracker answers with exactly one of:
  - `tracker` `{id, metric, label, evaluate_on, window_days, baseline_kind, module, label_text}` — `label_text` e.g. `"measuring labor % until 10/21/26"`;
  - `tracker_refused` `{code, reason, in_flight_until, in_flight?}` — `code` `in_flight` (with `in_flight` `{id, metric, label, title}` and `reason` e.g. `"Already measuring labor % until 10/21/26. One change per number at a time…"`), `no_metric` (`"There is nothing here Cavnar can measure it against yet"`) or `not_measurable`.
  
  Where: `POST outcomes` (Home Track; `ok: true` with `tracker_refused`, `warning` = `message` = its reason, no `outcome` — the recommendation is still recorded as accepted); `POST recs/event` for `accepted` (always one of the two) and `completed` (only when the recommendation carries a metric — Done without one promises nothing), plus the existing `message` / `tracking`; `POST /api/home/dismiss` + `/mobile/api/home/dismiss` with `kind: done` (keeps `outcome: {id, evaluate_on}` when started); `POST labor/schedule/recommendation` (`kind: hours`, accepted); `POST food-cost/reprice/apply` and `POST food-cost/menu-item-price` (when the price follows a suggestion). A slow-day campaign's tracker starts on a background thread after the sends and has no reply to carry.
  
  Gates: an owner-pressed Track and Ask's `track_outcome` refuse while another tracker measures the **same metric** (keys normalised: `weekday_sales:tuesday` is `weekday_sales:Tuesday`); every automatic start — observed actions (a schedule published, an order sent; at most once per metric per calendar month, whatever became of the first), reprice, campaigns, schedule accepts, `recs/event` Accept/Done and Home Done — refuses while anything in the **same family** is measured (`outcomes.AUTOMATIC_SOURCES`, re-audit A18). An alert-opened tracker never blocks a real one. Two starts at once are serialised (`BEGIN IMMEDIATE`): the second is answered by the first, never a 500. Dates (`started_on`, `evaluate_on`) are the restaurant's own calendar date, not the server's.

  `POST outcomes` with a `source_key` (Track this on a recommendation — contract K2): 404 `{ok: false, error: "Recommendation not found."}` unless the key's latest episode exists, was shown, and this login may see it. What the recommendation carries wins over the body's `metric`: a DSR action by its kind (`dsr_action:reorder:…` answers `tracker_refused` `no_metric`), a slow day by that weekday's sales, an overtime card or move by `overtime_hours`. A tracker already running on the key is returned only when it measures the same number and this login may see it — otherwise 409 `{ok: false, error: "Something else is already being measured for this."}`, its details withheld. A metric the login may not see is 403. Taking it silences the card for max(14 days, the tracker's window). `POST outcomes/<id>/abandon` answers `{ok: true}` whether or not anything changed: a tracker of another restaurant, or one this login may not see, is left alone (re-audit A28).
- **`GET metrics`** also lists `overtime_hours` (hours past 40 per payroll week; dollars at the overtime premium) and `response_hours` (median hours from review to reply; no dollars) — both accepted by `POST goals`, `POST outcomes` and Ask.

### Webhooks (`/api/webhook*` outbound config, `/webhooks/*` inbound)
`GET /api/webhook` (current config or null), `POST /api/webhook` (save + return a secret), `POST /api/webhook/test` (fire a test delivery), `DELETE /api/webhook` (remove). Inbound: Stripe and Twilio webhook receivers, signature-verified, event-id de-duped (`stripe_events_seen`).

### Auth (`auth_routes.py`; `/mobile/api/login`, `/register`, `/apple-signin`, `/verify-2fa`, `/forgot-password`, `/reset-password`, `/logout`, `/me`, `/device-tokens`)
Login (web + mobile variants), mobile register, forgot/reset password, verify-2fa, Apple/Google sign-in, logout, mobile `/me`, APNs token registration.

### Staff portal (`/staff/*`, `staff_routes.py`)
`/staff/r/<token>/login` (PIN), `/staff/api/me`, today's schedule (`/staff/api/shifts` — published weeks only; `published:false` when nothing is), availability, time off requests, `/staff/api/signup/claim`, `/staff/api/pin`, `/staff/api/preshift`, shift requests (`GET/POST /staff/api/shift-requests` — `kind: swap` with a named colleague's shift, `POST …/<id>/withdraw`), `GET /staff/api/colleagues`, `GET/POST /staff/api/preferences` (preferred dayparts, desired hours), and open shifts (`POST /staff/api/open-shifts/<id>/claim` — legality is `schedule_engine.replacement_is_legal`, the same check the owner's pickers use). Each shift carries a `break` window when the meal-break rule applies.

### Other groups with web + mobile twins
`/api/tasks*` (today's checklist), `/api/team/inbox` + `/api/team/messages*`, `/api/changelog*`, `/api/switch-location` + `/api/group-locations`, `/api/home` + `/api/home/dismiss` (hide / done / not_for_us / snooze `days`, `undo`, `restore_kind`; every answer also written to rec_ledger) + `/api/home/assign` (`{key, title, detail, contact_id}` → an issue keyed to the recommendation; owner only), `/api/rpower/status` and the `/admin/rpower/*` bootstrap.

### Admin (`/admin/*`, 96 routes on `admin_bp`; `/admin/status/*`, `/admin/audits*` and `/admin/<pos>/*` live on their own blueprints)
Client health rollup (owner → brand → location), job run history, manual contract send, sales-audit tool (`/admin/audits/*`), changelog authoring, status-incident management.

## Conventions

- **Success**: `jsonify(ok=True, ...)`, 200. **Failure**: `jsonify(ok=False, error=...)`, appropriate 4xx.
- **POST bodies**: `request.get_json() or {}` — never assume the body parses.
- **Side-effect emails/SMS**: wrapped in best-effort `try/except`, routed to `ops.capture()` on failure rather than failing the request the user is waiting on, and logged via `log_email()` / `email_log`.
- **CSRF**: every state-changing web POST needs the CSRF token (`_csrf_fetch.html`'s helper attaches it automatically to `fetch()` calls in the dashboard's own JS).
- **Rate limiting**: Ask Cavnar is capped at 5/min per restaurant **and user** (`ai_utils.ai_rate_limited`, keyed on both); AI budget is also enforced globally and per-restaurant (`ai_budget_exceeded`) before any model call fires.
- **Streaming**: Ask Cavnar's chat endpoint is Server-Sent Events, with named progress labels per tool call (e.g. `"read_alerts": "Checking what needs you"`) sent before the tool result, so the client can show what's happening rather than a bare spinner.

## Finding a specific route fast

```bash
grep -n "'/mobile/api/labor" mobile_api.py      # mobile handler
grep -n '"/api/labor' client_api.py             # web equivalent, if any (often `_m(...)`)
grep -n "def mobile_labor_" mobile_api.py        # by handler name pattern
grep -n '"/labor/' strategy_routes.py            # the _ROUTES table (registered at both prefixes)
python3 scripts/repo_inventory.py                     # every rule, by blueprint, from the live url_map
```

## Schedule experiments (admin only)

- `GET /admin/api/schedule-experiments` — per experiment and arm: weeks, draft
  acceptance (share of rows sent unedited) with a 90% interval, issues and
  labor % from `schedule_outcomes`, the draft's Shift Quality, the verdict
  under the minimum-sample rule, and every pin in force.
- `POST /admin/api/schedule-experiments/pin` `{restaurant_id, experiment, arm}` —
  pin a restaurant to an arm (`"off"` = the control) or unpin (`arm: null`).
- `POST /admin/api/schedule-experiments/promote` `{experiment, arm, note?}` —
  the reviewed step (ROI #46): refused unless the readout's verdict calls
  exactly that arm the winner and nothing is promoted yet; recorded in
  `schedule_experiment_promotions` with who and the verdict's words; every
  restaurant then gets that arm (a pin, `pin_source: "promoted"`), even after
  the experiment is retired in code. `POST …/revert` `{experiment}` ends it.
  The readout's experiments carry `promotion` (in force or null) and
  `promotions` (the trail).
- No owner route returns an arm. The generation payload's `optimizer.solver`
  carries the solver's stats (status, proved optimal, seconds, slots), not the arm.

## Recommendation record (`strategy_routes._ROUTES`: each at `/api/…` and `/mobile/api/…`)

Restaurant-scoped and redacted per viewer (`rec_learning.viewer_sees`): a
login never sees a recommendation about a module it cannot open (food →
FOOD_COST_VIEW — a food kind filed under Home counts as food, and so does a
DSR action on the Food block), a loss recommendation without LOSS_VIEW
(`issues.viewer_sees_loss`), or an owner-only one (a DSR action resting on
budget, prime cost or loss figures) unless it is a principal. Counts,
rates and pages are computed after redaction.

- `GET recs/summary?days=30|90|180` → `{ok, days, since (YYYY-MM-DD, UTC),
  by_module: {<module>: {shown, answered, accepted, completed, implemented,
  dismissed, ignored, n, accept_rate, accept_rate_low, accept_rate_high,
  enough}}, by_tag: [{tag, label, module, modules, measured, improved,
  worsened, no_clear_change, unknown, success_rate, enough}], most_effective:
  {tag, label, module, modules, success_rate, measured, improved} | null,
  min_settled, min_measured}`.
  Episodes first shown in the window; superseded ones are not counted.
  `answered` = accepted + completed + implemented + dismissed (a taken
  episode whose change was made counts as implemented); `n` = answered +
  ignored (expired unanswered — **in the denominator**); `accept_rate` =
  (accepted + completed + implemented) ÷ n with a 90% Wilson interval;
  `enough` = n ≥ 10. `by_tag` is over taken episodes with a result:
  `measured` = improved + worsened + no_clear_change; `unknown` (not
  measurable, or discounted by the owner's check-in) is never in the
  denominator; `success_rate` = improved ÷ measured; `enough` = measured ≥
  5. One `by_tag` row per tag across every module it was recommended under
  (`module` = where most of its results came from; `modules` lists them),
  counting one result per change (one per tracker; on one metric, one per
  non-overlapping after-window); a result that faded or reversed at its
  re-check is `no_clear_change`, one the owner disowned or said conditions
  changed is `unknown` (`rec_learning.learned_verdict`, re-audit B9/B13).
  `most_effective` is the tag with `enough`, success ≥ 0.5 and the best
  lower 90% bound, carrying its own `improved` of `measured` — "most often
  followed by an improvement", not the cause of one. Any other `days` is a
  400.
- `GET recs/timeline?limit=30&before=<next_before>` → `{ok, items: [{key,
  title, module, tags, first_shown_at, surfaces, answer, answered_at,
  reason_code, reason, implemented_at, tracker_id}], next_before}` — newest
  first; `answer` ∈ accepted | completed | implemented | dismissed | snoozed
  | expired | superseded | open; `tracker_id` joins to `GET outcomes` for the
  result; `limit` ≤ 100; `next_before` is null on the last page (`before`
  also takes a plain UTC timestamp; anything else is a 400). Timestamps are
  UTC `YYYY-MM-DD HH:MM:SS`; clients render M/D/YY.
- `GET recs/what-worked?days=90|180` (default 180; any other `days` is a
  400) → `{ok, days, enough, sentences: [str], facts: {since, window,
  acceptance: [{module, label, n, taken, dismissed, ignored, accept_rate,
  accept_rate_low, accept_rate_high, enough}], changes: [{metric, label,
  unit, lower_is_better, family, results, improved, worsened,
  no_clear_change, mean_delta, mean_delta_pct, outcome_ids}], measured:
  <outcomes.cumulative over the window>, most_effective: <as
  recs/summary> | null, minimums: {settled_for_rate, measured_for_tag,
  results_per_metric, measured_days}, caveat}}` — "What worked for you"
  (`owner_report.what_worked`, rec-ROI #28), built without a model. A
  sentence appears only when its own minimum is met: acceptance per module
  with `enough` (ignored in the denominator); the mean stored `delta_pct`
  (points for a % metric) of recommendations the owner took, per metric,
  over ≥ 2 non-overlapping results that still count or showed no clear
  change; measured dollars summed over the window's measured days, net,
  after ≥ 14 of them; the most effective tag. `enough` = at least one
  sentence; with none, `sentences` is `[]` and nothing is estimated. Words
  are "associated with" / "measured", never causal. Redacted exactly like
  `recs/summary` (module permissions, loss without LOSS_VIEW, owner-only);
  the owner's monthly email carries the same sentences (viewer none).
- `POST recs/checkin` **(K1)** `{tracker_id?, key?, did_it: "yes"|"no"|"partly",
  conditions_changed: bool, note?}` → `{ok, recorded, checkin: {did_it,
  conditions_changed, note, tracker_id, attribution: {implemented,
  confounded, discount}, key}}`. With `tracker_id` (an int — clients send
  the result's own id) the episode is the one that tracker measures
  (`rec_instances.tracker_id`) at this restaurant: the `checkin` event, any
  `implemented` ("yes") and `outcomes.apply_checkin` all land on THAT result,
  however many times the key was shown since; a `key` sent beside it must be
  that episode's. Without `tracker_id`: the key's latest episode that has a
  tracker, else its latest. `rec_ledger.checkin_episode` documents the
  resolution and `rec_ledger.checkin` the meta (an outcome evaluation reads
  it through `rec_ledger.latest_checkin(rid, tracker_id)`); `discount` is
  true for "no" or conditions_changed. 404 when there is no such episode or
  this login may not see it (another restaurant's tracker, a food-cost
  result for a manager); 400 for a malformed body (a non-integer
  `tracker_id`, neither key nor tracker_id). No `shown` is required: the
  tracker is what the owner is asked about.
- **Who may answer (K2).** Every answer route answers only a recommendation
  this login was SHOWN and may SEE (`rec_learning.answerable_episode`: the
  key's latest episode exists at this restaurant, has a `shown` event, and
  `viewer_sees` allows it) — else 404 `No such recommendation.` (nothing is
  confirmed either way, and nothing is written: an answer never opens an
  episode — `rec_ledger.record(require_existing=True)`). The routes:
  `POST recs/event` (before any tracker starts), `POST /api/home/dismiss` +
  `/mobile/api/home/dismiss` (Home's setup/health nudges —
  `home_brief.HOME_SETUP_KEYS`: google_not_connected, reviews_stale,
  toast_sync, inventory_stale, post_failed, social_not_connected — are
  Home's own hides, gated by their module's permission and never written to
  the ledger; `undo` and `restore_kind` are not answers), `POST
  actions/snooze` (an `issue:<id>` row is not a recommendation: allowed when
  it is this restaurant's issue and, for a loss issue, the login has
  LOSS_VIEW), `POST outcomes` with any `source_key`, `POST
  guest-winback/<id>/dismiss` (404 as for a draft that is gone) and `POST
  labor/schedule/recommendation` for `accepted`/`dismissed` (the key is
  `schedule_intel.schedule_rec_key(kind, key)`; `restored` brings a kind
  back and is not checked). `POST outcomes/<id>/abandon` is gated by the
  outcome group (A28). An employee login never reaches these (403). Track
  (`POST outcomes`, and a schedule accept that starts a tracker) holds the
  key quiet for the tracker's whole window, `max(14, window_days)` (B1).
- **Structured reasons.** `POST recs/event` and `POST /api/home/dismiss`
  (and `/mobile/api/home/dismiss`) take `reason_code` ∈ already_doing |
  doesnt_fit | too_costly | bad_timing | dont_trust_data | other (plus the
  free `reason`); stored on the answer's meta; any other code is a 400.
  **K3:** so do `POST guest-winback/<id>/dismiss` `{kind?, reason_code?,
  reason?}` and the schedule ✕, `POST labor/schedule/recommendation`
  `{action: "dismissed", kind, key, reason_code?, reason?}` (web and mobile
  twins; the reason is ≤200 characters).
  `recs/event` refuses the events the server writes itself (implemented,
  superseded, checkin, abandoned, outcome, expired, shown). `GET decisions`
  rows carry `reason_code`, and `answer` may be `implemented`; they are
  redacted for the login exactly as the record is (`decisions.history(viewer=)`:
  no food-cost, owner-only or loss decision for a manager, no food-cost or
  comp/void outcome without the permission, and a teammate reads only the Ask
  proposals it answered) — and so are Ask's decisions context and its
  `read_decisions` tool (re-audit B4).
- **Home's `answerable`.** Every Home attention item (web `/api/home/brief`,
  mobile `/mobile/api/home` `needs_attention`) and card carries `answerable`:
  true only when it is dismissable, not a setup/health nudge and a key the
  ledger presents (`home_brief.attention_answerable`, `rec_delivery.answerable`).
  Home records as shown exactly the answerable items it renders (re-audit B7).

Admin only (internal): `GET /admin/api/calibration?days=365` (contract K7) → `{bands: [{range: "70-79", n, predicted_mean, observed_rate, low, high, enough}], brier, n, by_kind: [{kind, n, predicted_mean, observed_rate, enough}], by_dimension: {evidence|accuracy|freshness: {bands, brier, n}}, floor_n: 20, kind_floor_n: 5, distrust: {n, by_kind}}` — the confidence % snapshotted at delivery against taken results read through `learned_verdict`; an observed rate, interval or Brier only at the floor. `/admin/api/recommendations` (acceptance) now groups `by_confidence` by the snapshot's band ("not snapshotted" before it) and its `outcome_rate` is improved ÷ measured (`measured` added). `GET /admin/api/recommendations/calibration?days=365`
(predicted vs measured $ by kind, ROI #43) and `GET
/admin/api/recommendations/missed?days=30` (problems with no recommendation
before them, ROI #44); both take `restaurant_id`.

## Intelligence engine

- `GET /admin/api/intelligence` (admin) — the Intelligence page payload.
- Home brief recommendations carry `confidence: {score, band, caution}`.
- Ask tools `read_restaurant_memory`, `read_platform_intelligence`.
- `POST /mobile/api/account/update-profile` and `POST /admin/api/brand/<id>`
  accept `category` (taxonomy in `intelligence/categories.py`).
- `POST /api/generate-content` and the mobile twin return `tags`
  (`menu_item_id`, `menu_item_name`, `occasion`, `post_kind`, `label`).
- `POST /api/marketing/posts/<id>/tags` (web) / `/mobile/api/marketing/posts/<id>/tags`
  — correct a post's tags.
- `GET /api/marketing/attribution` rows now carry `menu_item_name`, `occasion`,
  `post_kind`, `item_lift_pct`, `reviews_mentioning`, `guest_list_delta`,
  `engagement_rate`; the payload adds `weakest`, `by_kind`, `by_occasion`, `by_dish`.

**Recommendation trust (Reviews, Food Cost, Marketing, Intel).** Every model-written recommendation carries a rec_ledger key and is answered through `POST recs/event` `{key, event: completed|dismissed|accepted, surface, module, kind: not_for_us}`. `GET /api/review-insight` and `/mobile/api/reviews/insight` add `recs: [{key, text, kind}]` and, per diagnosis, `rec_key`, `answered`, `as_of`, `stale_note`. `GET /api/inv-insight` (HTML with controls, plus `recs`) and `/mobile/api/food-cost/analytics` (`insight_rec_keys`, aligned with `insight_recommendations`); `food-cost/cfo`'s `diagnosis` carries `rec_key` / `answered` / `stale_note`. `GET /api/mkt-insight` and `/mobile/api/marketing/insight` likewise (one stored read shared by both). `GET /api/intel/recs` → `{recs: [{key, text, cites: [{ref, competitor, rating, time, text}]}], withheld_recommendations, unverified, nothing_to_act_on}`; `/mobile/api/intel` carries the same as `recommendation_items`. Win-back: `GET guest-winback` → `{available, draft: {id, segment, segment_label, segment_size, message, rec_key, return, max_chars, sms_window}}`, `POST guest-winback/<id>/send` `{message?}` (through the campaign gates: consent, quiet hours, three-day cap, length), `POST guest-winback/<id>/dismiss` — web and mobile twins. `food-cost/auto-order` GET adds `count_freshness` (an automatic order is held on a count older than `ordering.COUNT_FRESH_DAYS`). Marketing attribution posts carry `verdict` (lifted / dropped / no_clear_change) and `noise_band_pct`.

## Recommendation fields — the contract every surface shares

Recommendation ROI audit (9/24/26), coverage items #6, #7, #10, #25, #26, #32, #41, #48. A recommendation is recorded as **shown** (`rec_ledger.present`) when it is DELIVERED to a person — a route that returns it to the screen showing it, an email that went out, a push that was sent — never when it is built (`rec_delivery`). Every payload that carries a presentable recommendation carries, on that item:

| Field | Type | Meaning |
|---|---|---|
| `rec_key` | string | its `rec_ledger` key — what `POST recs/event {key: rec_key, event, surface, module}` answers |
| `answerable` | bool | true when the client should offer Done / Not for us (and Track where `strategy_routes.REC_TRACK_METRICS` has the module). False for keys nothing can answer (`money:*`, `urgent_reviews`, `intraday_pulse:*`, and — re-audit C8 — `pulse_cut:*`, `quiet_night:*`, `digest_move:*`, `monthly_move:*`, shown only in a push or an email that offers no answer; all never presented), for bookkeeping keys (`standby:`, `calibration:` — their own button is the answer), for a queue task (below), and for one already answered |
| `answered` | bool | (where present) the owner already answered it on some surface: a screen that is a record (the DSR) keeps the line and drops the controls |

Where it is on the wire:

- **Labor read** — `GET /api/labor-insight` → `{insight (HTML with controls), rec_items: [{index, key, rec_key, text, rec_id, answered, controls, answerable}], recs: [{key, rec_key, text, answerable}], diagnosis}`; `GET /mobile/api/labor/insight` → the `_insight_json` fields (`insight_rec_keys` aligned with `insight_recommendations`, answered lines left out) plus `rec_items`. Keys `insight_labor:<hash>`, surface `labor`. `diagnosis` (both) gains `rec_key` (`diag_labor:<driver>` — `weekday:<day>`, `role:<role>` or `overtime`, from `labor.diagnose`'s new `driver`), `answered`, `answerable`; its action is `what_would_confirm`. Every `rec_items` list (Food, Marketing, Labor) now carries `rec_key` and `answerable`.
- **Home's one thing and links** — `GET cross-module` (`/api/…`, `/mobile/api/…`): `fix_first.rec_key` / `fix_first.answerable` and, per link, `rec_key` (`link:<kind>:<subject>`) / `answerable`. Both presented on `home`; a link the owner answered is left out.
- **Morning brief** — each keyed line (`GET morning-brief` → `brief.lines[]`) carries `rec_key` and `answerable` beside the old `rec`; a line that stands for several (running low) also carries `rec_keys` — every key it names (at most `morning_brief.STOCK_NAMED`, 3: "and 3 more" names nobody), which a Done / Not for us answers one by one. Presented on `home` by `?view=home` (K6), on `brief_email` when the email went out. The push payload carries `surface: "brief_push"`, `alert_id`, `rec_key`, `answerable`; it presents only the two lines the lock screen shows (`morning_brief.push_lines`), and only once a phone took it.
- **Pushes** — every push the server sends with a recommendation carries `rec_key`, `answerable` and `surface` (`alert_push` or `brief_push`). A push presents what it showed when APNs accepted it on a phone (`push.fire_push(on_delivered=)`, `rec_delivery.when_pushed`), never when it was queued; a push-only job (pulse, quiet night) sends only to recipients with a deliverable device and to nobody when there are none (`strategy_jobs.deliverable_audience`). The pre-dinner pulse's `staffing_move` and the quiet-night push carry `answerable: false` and no `rec_key` (C8). The combined morning alert's text and push present only the lead item they name (`notify.deliver_alert(text_recs=)`); its email presents every item.
- **Queue** — `GET actions` `items[]`: a recommendation carries `rec_key` and `answerable` and is presented on `queue`; a task — `issue:*`, `shift_request:*`, `time_off:*`, `invoice:*` (`action_queue.TASK_KEY_PREFIXES`, finished at its source) — carries `answerable: false`, no `rec_key`, is never presented, and its snooze (`actions/snooze`) stays out of the ledger. Generating a whole week records `schedule:next-week` implemented (`schedule_engine.mark_next_week_built`).
- **Issues (coverage covers)** — `GET issues?status=unresolved` (what Home's open-issues list reads, both clients): each of the first four issues with a suggested cover nobody was asked yet gains `cover_rec_key` (`cover:<date>:<person missing>`) and `cover_answerable`, presented on `home`. The issue page (`/i/<token>`) presents its covers on `issue_sms` when a person POSTs from it — its GET stays side-effect free (link previews). Filing the issue presents nothing.
- **Module reads** — `GET /api/mkt-insight` `recs[]`, `GET /api/inv-insight` `recs[]`, `GET /api/review-insight` + `/mobile/api/reviews/insight` `recs[]`, `GET /api/intel/recs` `recs[]`: each carries `rec_key` and `answerable` beside `key`. `GET /mobile/api/marketing/insight` keeps `rec_items` in its payload. Every diagnosis with a recommended action (`diagnosis`, `diagnoses[]` on Reviews; `diagnosis` on `food-cost/cfo`) carries `rec_key`, `answered`, `answerable`; Reviews presents only `diagnosis` — the one both clients render.
- **Schedule quality** — `quality.recommendation_items[]` (generation status poll, `labor/schedule/score`, `labor/schedule/apply-fixes`, `labor/schedule/optimize`, `labor/schedule-history/<id>`): each `{text, kind, key, rec_key, answerable, answered}`, at most `schedule_engine.SHOWN_RECOMMENDATIONS` (5, what both clients render), presented on `schedule_review` by the response that serves it (`schedule_engine.present_quality`) — never by the scoring itself, which the nightly auto-draft runs with nobody looking.
- **Loss flags** — `GET loss-signals` (LOSS_VIEW only): each `flagged[]` item carries `key` / `rec_key` (`loss:<week start>:<kind>:spike` or `…:<approver>` — the key the concentration issue is filed under) and `answerable`; presented on `home`; answered flags are left out of `flagged`.
- **DSR** — `GET dsr/<day>`: each `narrative.actions_tomorrow[]` gains `rec_key`, `answered`, `answerable`. The view presents on `dsr` only for a night within `strategy_routes.DSR_VIEW_PRESENT_DAYS` (3); an older report is history and presents nothing. The first DSR email presents on `dsr_email` once Resend accepted it.
- **Content calendar** — `GET /api/content-calendar` `ideas[]`, `GET /mobile/api/marketing` `calendar[]`, `POST /mobile/api/marketing/calendar` `calendar[]`: each idea carries `rec_key` (`content_idea:<hash of the angle>`), `answered`, `answerable`. The web grid shows all seven and presents every open one on `marketing`; the phone shows one day's card at a time, so the phone routes present only the day it opens on (today's, else the first — `client_api.focused_calendar_index`), and `POST /mobile/api/marketing/calendar/seen {rec_key}` → `{ok, rec_key, answered, answerable}` presents each other day when the phone turns to it (404 for a key not in this restaurant's cached week). Writing from an idea (`from_calendar`) records it `accepted`.
- **AI-visibility roadmap** — `GET /api/ai-visibility` and `GET /mobile/api/intel/ai-visibility` add `roadmap: [{key, rec_key, title, why, detail, action, impact, module, done, answered, answerable}]` — the four cards `dashboard.html` built in the browser, built on the server (`client_api.ai_visibility_roadmap`), same order and done rules; open cards are presented on `intel`. Ask's `read_ai_visibility` reads the same payload and presents nothing.
- **Schedule drafts** — `GET /api/schedule-status/<job_id>` and `/mobile/api/labor/schedule-status/<job_id>`, once `status` is `done`: each `standby_days[]` with a person gains `rec_key` (`standby:<date>:<name>`, answered by `standby/ask`) and `answerable: false`; each `overtime_forecast[]` with a `candidate` gains `rec_key` (`overtime_move:<employee>:<date>`) and `answerable`. Presented on `schedule_review`. `schedule-intel`'s `weight_calibration`, when ready, gains `rec_key: "calibration:weights"`, `answerable: false` (Apply is its answer).
- **Ask confidence (K5)** — every answer's `confidence` is the band word, now measured from what the tools returned (live reads, sample reads counted 0, figures checked, the freshness of the modules read; any unverified figure caps it low) instead of how many modules were consulted, and `confidence_detail` is the K1 object.
- **Ask** — `POST /api/ask-cavnar`, `/mobile/api/ask-cavnar` and the stream's `answer` event add `message_id` (the answer's id, what feedback rates) and `suggestions: [{text, rec_key, answerable}]` — the answer's own concrete list items that start with an imperative verb and carry no untraced figure (`ask_cavnar.extract_suggestions`, no second model call), keyed `ask_tip:<hash>`, presented on `ask`; answered ones are dropped BEFORE the cap of three, so a fourth unanswered suggestion is offered when the first three were answered. `GET /mobile/api/ask-cavnar/opening` adds `feedback: {rated, helpful, days}`.

**Model output guards (Group H, 9/24/26) — fields clients may render; every one is optional (older servers send none).**
- **Diagnoses** (`diagnosis` / `diagnoses[]` on Reviews, `diagnosis` on `food-cost/cfo`, Ask's diagnosis reads): `operational_evidence[]` entries now each carry `verified: true` — unverified ones never reach a client; `confidence` is the capped band (medium at most with no verified cross-check); `model_confidence` is the model's own band, for comparison only, never rendered as confidence.
- **Reviews read** (`GET /api/review-insight`, `/mobile/api/reviews/insight`): `causes_verified`, `unsupported_causes[]`, `forecast` (`{kind: "review_rating_week", predicted, computed: true}` or null — the "Next week" line is computed). `recs[]` (Do today) carry `advice_signature` and a key `insight_review:<family>:<subject>` when the line names one (else the old hash). Each cluster (`complaint_clusters`, Ask's reads) carries `unclassified` (reviews with no severity tier) and `worst_severity` may be `"unclassified"`.
- **Marketing read** (`GET /api/mkt-insight`, `/mobile/api/marketing/insight`): `causes_verified`, `unsupported_causes[]`, `forecast` (`marketing_reach_week`). A read failing either check offers no controls.
- **Labor read** (`GET /api/labor-insight`): on the stale-cache fallback, `stale: true`, `as_of` (M/D/YY), `as_of_iso`, `age_days`, `stale_note`. The insight text's `UNVERIFIED:` line may now name misattached figures and an unsupported cause as well as figures.
- **Recipe drafts** (`food-cost/recipe-drafts`, `recipes/scan`): each draft carries `is_estimate`, `needs_yield`, `unit_warnings[]` (`{name, note}`), `confidence_levels`; each line `source` (`estimate` | `transcribed`), `per` (`plate` | `batch`), `unit_ok`, `card_qty` / `card_unit`, `unit_note`. `POST food-cost/recipe-drafts/<id>/accept` takes `yield` (plates the card makes); a batch draft without it and without owner-typed lines is refused (409, `needs_yield: true`); the answer carries `unit_skipped[]`.
- **DSR** (`GET dsr/<day>` narrative): `verification.estimated` and `verification.measured` beside `kept` — the footer counts estimate-backed lines apart; each `actions_tomorrow[]` item carries `advice_signature`, `effort_source: "model"`, `urgency_basis` and, when the facts did not carry "Today", `urgency_adjusted: {from, to, why}`.

**`POST ask-cavnar/feedback`** (`/api/…` and `/mobile/api/…`, `strategy_routes._do_ask_feedback`) — `{message_id | turn_id, helpful: bool, note?: string ≤ 500}` → `{ok, feedback: {message_id, conversation_id, helpful, note, rated_at}, summary: {rated, helpful, not_helpful, notes, days}}`. 400 without a numeric id or a boolean `helpful`; 404 when the message is not an assistant answer of THIS restaurant given to THIS login (nothing is written). Rating the same answer again replaces the rating. Table `ask_feedback`, created at boot.

**Keyed links (#32).** Links in the alert email (its button), the alert SMS (its closing `dashboard.cavnar.ai`, keyed at delivery — only when `SMS_TRACKED_LINKS=1`, off by default because the longer address can add a billed segment), the `_reach` email, the weekly digest (the one thing, "What connects"), the monthly email (the one thing), the morning brief email and the DSR email (each action) carry `rec=<key>&src=<surface>` and, where the sender knows it, `rid=<restaurant>`. A key that is never presented is not carried (`rec_delivery.link`). Every load of `/` with `rec`/`src` — with or without a question (`?ask=`) — is recorded server-side (`rec_delivery.record_link_open`, once per key, surface, login and the restaurant's local day; re-audit C14): against the location `rid` names when this login may see it (its own, or a group location it may switch to), and against none otherwise. `dashboard.html`'s `?ask=` handler no longer posts the open. Signing in keeps the query string — the password form through `next`, Google sign-in through `/auth/google-sso?next=` (carried across Google's round trip in a 5-minute cookie, a path on this site only; re-audit C10).

**Shown but deliberately not presented** (decided per source, so no acceptance rate counts them as ignored): the money ranking (`money:*` — where the money is, a module label with a figure; its action is the one thing, which carries `same_as`), the owed replies as the one thing (`urgent_reviews` — discharged by replying; a subject-less "Not for us" would silence every future low-star prompt), the pulse's "X% behind a typical Friday" (`intraday_pulse:*` — news), the pulse's staffing move and the quiet-night heads-up (`pulse_cut:*`, `quiet_night:*` — push only, nothing answers a push), the digest's model-written move and the monthly email's next move (`digest_move:*`, `monthly_move:*` — email only, answerable nowhere), the queue's tasks (`action_queue.TASK_KEY_PREFIXES`), Home's quick actions and readiness next steps (navigation and setup: their completion is observed directly, it is not an owner's judgment), the group brief's attention rows (status per location, no action), predicted schedule edits (a forecast of the manager's own edits, scored by `schedule_learning.edit_prediction_backtest`) and the what-if comparison (the evidence behind "Improve with Cavnar", whose offer is presented as `optimizer:*`). The DSR push carries no `rec_key`: its body is a needs-attention line, not an action; the tap opens the report, whose view presents the actions.

**`POST notifications/opened`** also takes `surface` — `alert_push` or `brief_push`; any other value, or none, falls back to the push type (`morning_brief` → `brief_push`, else `alert_push`). One open per notification (`alert_id`) and login, or per type, login and UTC day when the push has no history row.

## Forecasts, owner wording and cross-module agreement — additive fields (confidence audit, group I, 9/24/26)

Every field below is ADDITIVE: older clients ignore it, and a client must tolerate it missing (older servers, cached payloads). Dates in owner-facing strings are M/D/YY; machine fields stay ISO.

**Forecast accuracy — contract K8.** One record per closed period, from `forecast_log.accuracy(rid, kind)`:
`{available, kind, scored, n_periods, n_weeks | n_months, mean_error_pct, bias_pct, reading ("close" | "roughly right" | "often wide"), withheld, reason}`. `mean_error_pct` is mean |predicted − actual| / actual; `bias_pct` the mean signed (predicted − actual) / actual — one denominator. `withheld: true` means the record reads "often wide" and the forecast of that kind is not shown (it keeps being recorded and scored). Nothing is stated under 2 scored periods (`available: false`, `reason` says how many).
- Food brief (`food_cost_intelligence.executive_brief`, every Food Cost payload that carries `brief`): `forecast_accuracy` (waste, kind `waste_week`) and `prime_cost_accuracy` (the frozen mid-month prime-cost projection, kind `profitability_month`), top level and inside `trust`.
- Demand — `demand.demand_accuracy(rid)`: `{available, n_nights, mean_error_pct, bias_pct (+ = nights ran ABOVE the forecast), inside_range_pct, n_ranged, window_days, basis, reason, claim_kind: "measured"}` from the nightly report's own out-of-sample forecast (`dsr_metrics` `sales.vs_forecast_pct`, `sales.net`, `sales.forecast_low/high`). Nothing under 7 scored nights. Carried by `GET morning-brief` → `brief.demand_accuracy` (logins that may read labor), `GET /mobile/api/labor` → `demand_accuracy` + `week_projection_accuracy` (K8 record for kind `revenue_week`), and Ask `read_schedule` → `demand_accuracy` + `week_projection_accuracy`.
- `demand.forecast_day` (morning brief "today", Ask `read_demand`, `GET labor/demand`, the DSR forecast baseline): `low`/`high` are the 10th–90th percentile of the same weekday's own nights once there are 8 (`range_basis`); `null` before that, with `range_note` ("range not yet measurable — 5 past Fridays, needs 8"). `claim_kind: "forecast"`. The brief's "today" line carries `forecast: true` + `claim_kind: "forecast"`, and the brief email's footer names it as a projection.
- The DSR sales block stores `forecast_low` / `forecast_high` per night beside `forecast_net`.

**Forecast kinds** (`forecast_log.KINDS`, one row per restaurant × kind × period, insert-once): `waste_week` ($, next ISO week's waste — the four-week mean, outlier weeks left out; the owner sees the bias-corrected figure when the record has a consistent lean), `profitability_month` (%), `revenue_week` ($, the week's sales projection frozen when its schedule is published; scored on exactly the days it covered, unscored if any of them has no sales), `labor_week` (% of sales over the ISO week), `marketing_reach_week` (summed reach of posts published that ISO week), `review_rating_week` (★, the ISO week's average rating under metrics' 5-review floor). Scored nightly by the `forecast_scoring` job once the period has closed.

**Rating trend** — `review_intelligence.rating_trend` returns `direction` ∈ `review_intelligence.TREND_DIRECTIONS` = `improving | declining | flat` (never `up`/`down`), and `flat` when the fitted slope and the first-to-last change disagree. The Reviews executive brief's `trend` adds `first, latest, change, weeks, reviews, min_reviews_per_week` — the web header renders this instead of its own rule. The morning brief's trend line and the negative-trend SMS/email read the same scorer (declining at medium/high confidence) and state weeks and reviews.

**AI visibility** — `GET /api/ai-visibility`, `/mobile/api/intel/ai-visibility`: `ai_score_band` (`often | sometimes | rarely | uncertain`), `ai_score_label`, `ai_score_tone` (`good | warn | bad | neutral`) — computed from the 90% range `ai_score_low`–`ai_score_high`, never the point; a range crossing a band edge reads "somewhere between 22% and 86% — too few questions to say more". `presence_label` ("Listing strength" — not GBP completeness), `presence_band_label`, `presence_tone` from one table (`client_api.PRESENCE_BANDS`: 80 strong/good, 60 a few gaps/warn, 40 needs work/warn, else critical gaps/bad). Ask `read_ai_visibility` adds `ai_score_low`, `ai_score_high`, `ai_score_label`, `answered_queries` and a `note` to quote the range. `business_intelligence.gather()['visibility']` adds `ai_score_low`, `ai_score_high`, `answered`. The drop alert and the intel × reviews link fire only when the two runs' ranges do not overlap (`notify.visibility_range`).

**Market average** — `GET /mobile/api/intel` adds `own_vs_market`, `standing` (`ahead | level | behind | null`), `standing_label`, `standing_tone` (`competitor_intel_format.market_standing`: +0.3★ leads, −0.1★ level; compared only against Google's all-time rating, never the imported sample). The web Intel page receives the same object as the template variable `intel_market` (`own_rating`, `own_rating_basis`, `own_rating_count`, `market_rating`, `market_rating_reviews`, `market_rating_n`, plus the standing fields). The welcome email's first look uses the same weighted average (`neighbourhood.reviews`, `basis`).

**Labor vs industry** — web `savings_breakdown` and `/mobile/api/labor` `savings_breakdown` add `labor_industry_pct` (34.5) and `labor_industry_basis`; both dollar figures come from `thresholds.labor_vs_industry_monthly` (0 on estimated hours, missing sales or a sub-week period). Web `sales_lift_yr` is now always 0 (the unsourced 3.1% figure was retired).

**Holiday banner** — web `labor_upcoming[]` and `/mobile/api/labor` `labor_upcoming[]`: `{name, date (ISO), date_str (M/D/YY — was "September 21st"), days_away, lift_pct, based_on, label, claim_kind}`. `label` states what THIS restaurant's sales did on that holiday last year when known ("Last year 38% above a typical Thursday here — …"), else "Holiday — check your own history". Clients render `label` in place of the old generic copy.

**Value naming** — `GET value` (`/api/value`, `/mobile/api/value`) adds `sections: [{key: "measured", heading: "What was measured", figures: ["delivered"]}, {key: "surfaced", heading: "What Cavnar surfaced / still available", figures: ["avoided", "surfaced", "opportunity"]}]`; `delivered.scope = "monthly_rate"`. The Home `value` block (K4) adds `scope: "monthly_rate"` and `cumulative_scope: "measured_days_sum"`; `value_delivered.headline` adds `scope`. Savings milestones carry `data.scope = "all_time_sum"`.

**Inventory** — `analyse_inventory` adds `benchmark_state` (`measured | not_measured | no_data`; a week with nothing logged is "No waste recorded", tone `neutral`, never "Excellent"), `recoverable_kind: "opportunity"` and `annual_recoverable_basis` ("one week's count × 4.33 weeks a month × 12 — a projection of what is still being lost, not money saved"). Clients style recoverable as available, never as a win.

**Monthly review** — `review.prime_cost` is the CLOSED month measured (`{pct, previous, delta, claim_kind: "measured", window, basis}`), or `null` when either half cannot be measured — never this month's projection, so the screen and the email agree.

**Schedule** — likely-edit rows (`likely_edits[]` of kind `predicted`) are withheld until the restaurant's leave-one-out backtest covers 4 held-out drafts, and each carries `backtest_hit_rate`, `backtest_hits`, `backtest_flagged`, `backtest_weeks`, `base_rate`, `calibration_note` (also appended to `text`). `standby_days[]` add `base_rate` and `assumption` ("Assumes no-shows are independent…"); people with no clock-in record count at the base rate; every rate is smoothed toward it. Roster `reliability` entries add `no_shows`, `raw_no_show_rate`, `base_rate`, `no_show_threshold` (= `shift_quality.UNRELIABLE_RATE`, the engine's line — clients colour red at exactly this) and `unreliable`; `no_show_rate` is now the smoothed rate.

**Demand signals** — an event with no covers and no lift is stored with `lift_pct: null`; `by_date` marks it `assumed: true` with `assumed_lift_pct` (25, said as an assumption) and it never raises a demand level. `demand.slow_days()` entries carry `consistency` (share of that weekday's nights under the typical day; "reliably slow" needs 0.75).

**DSR** — the reviews block's `avg_rating` is `null` under 5 reviews, with `detail.rating_note` ("3 reviews — not rated (a rating needs 5)") and `detail.rating_min_reviews`. The food block's `est_food_cost_pct` is `null` under 50% of units costed, with `estimate.coverage_floor_pct` and `estimate.coverage_note`; the label speaks of units, not dishes. The weekly .xlsx marks provisional nights "(provisional)" and adds a footnote.

**Morning brief** — the money line adds `money: {low, high, per: "month", label, claim_kind, basis}` (a point figure has low == high) so no client regex-collapses a range.
