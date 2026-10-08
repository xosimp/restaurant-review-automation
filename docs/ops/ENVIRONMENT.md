# Environment variables — Cavnar AI

Every variable the code reads, in one place. Production sets them on the Railway `web` service; a local backend reads the `.env` beside `hosted_dashboard.py` (`.env.example` is the starting point). Never commit a value, never paste one into a doc, and never run a local copy with production's keys and the scheduler on (CLAUDE.md).

`tests/test_environment_doc.py` fails when the code reads a variable this file does not name, so a new one is documented in the commit that adds it. The list is regenerated with `python3 scripts/repo_inventory.py --env` (name, default, readers).

Defaults are the code's own; "—" means none (unset is off or empty).

## Required to run in production

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `SECRET_KEY` | `""` | Flask session and token signing (auth, email links, OAuth state, guest links). **Required in production**; without it the app runs on an ephemeral key (`CAVNAR_SECRET_KEY_EPHEMERAL` marks that) and every restart signs everyone out. | auth.py, emails.py, gmb.py, guest_links.py |
| `CREDENTIAL_KEY` | — | Fernet key for every credential at rest (`credentials.py`: POS tokens, OAuth tokens, TOTP secrets). **Required**; see `SECURITY.md`. | credentials.py, guest_links.py |
| `BASE_URL` | — | The dashboard's public origin (`config.base_url`), the root of every emailed/texted/pushed link. Production: `https://dashboard.cavnar.ai`. | config.py |
| `RAILWAY_VOLUME_MOUNT_PATH` | `"."` | Where the SQLite file and backups live (the Railway volume). Default `.` locally; tests point it at a scratch directory. | models.py, platform_monitor.py, scripts/seed_simple_ejs_reviews.py, status_manager.py |
| `ANTHROPIC_API_KEY` | `""` | Claude, for every model call (`ai_utils`). Empty forces every AI path onto its offline fallback (tests, CI). | ai_utils.py, competitor.py, emails.py, provider_health.py |
| `RESEND_API_KEY` | `""` | Resend, for every email. Empty: nothing is sent (tests, CI). | emails.py, hosted_dashboard.py, models.py, ops.py |
| `FROM_EMAIL` | `DEFAULT_SENDER` | The sender address for transactional mail (`config.from_email`). | config.py |
| `WILL_EMAIL` | `DEFAULT_SENDER` | Where operator mail goes: failure digests, backups, drills, signups (`config.will_email`). | config.py |
| `BACKUP_ENCRYPTION_KEY` | `""` | Fernet key for every off-site backup copy. The backup fails if no off-site copy was made (`RECOVERY.md`). | ops.py, scheduler.py |
| `CAVNAR_PIN_PEPPER` | `""` | Pepper for staff PINs — `docs/ops/PIN_PEPPER_RUNBOOK.md`. | auth.py |

## Runtime and deployment

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `PORT` | `5000` | The port `hosted_dashboard.py` binds when run directly (default 5000; run locally as `PORT=5050`, which the iOS debug build expects). | hosted_dashboard.py |
| `RUN_SCHEDULER_IN_WEB` | `"1"` | 1 (default): the web process runs the scheduler thread. `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`. | hosted_dashboard.py |
| `ALLOW_LOCAL_SCHEDULER` | `""` | 1 lets a non-Railway backend run the scheduler and live sends. Never set it on a machine with production keys (CLAUDE.md). | scheduler.py |
| `RESTORE_FROM` | — | Boot-time database restore source (`db_restore.py`, `docs/ops/RECOVERY.md`). While set, the scheduler and live sends stay off. | admin_routes.py, db_restore.py, scheduler.py |
| `ALLOW_EMPTY_DATABASE` | — | 1 lets the app boot on an empty database where the volume's marker says client data existed (a deliberate fresh start); otherwise that boot is refused and `/health` fails (`status_manager.platform_emptied`). | status_manager.py |
| `CAVNAR_CONN_POOL` | `1` | Reuse SQLite connections per thread (10/3/26): a new connection re-reads the whole schema, ~2.6ms, against ~0.01ms for a pooled one. Each open handle still has a connection of its own, uncommitted work is rolled back on close, and state a caller changed is reset or the connection is never reused (`models.get_conn`). `0` opens and closes every connection for real — the kill switch if anything looks wrong. | models.py |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | SQLite's `synchronous` level on every new connection once WAL is confirmed (AI cost audit 10/7/26 #92). `NORMAL` under WAL never corrupts the file and keeps every commit through an app crash, kill or redeploy; a host power loss or OS crash can lose the commits since the last checkpoint (seconds of writes) — the file reads as it was a moment earlier. `FULL` (or `EXTRA`) fsyncs every commit again, SQLite's default. A database not in WAL always keeps FULL (`models.sqlite_synchronous`, `docs/ops/RECOVERY.md`). | models.py |
| `CAVNAR_FORCE_SECURE_COOKIES` | — | Force the `Secure` cookie flag (and HSTS) off Railway, e.g. behind another TLS proxy. | auth.py |
| `CAVNAR_SECRET_KEY_EPHEMERAL` | — | Set by the app itself when it had to mint a throwaway `SECRET_KEY`, so presence checks (the admin key panel) don't read it as configured (#125). Never set by hand. | hosted_dashboard.py |
| `SENTRY_DSN` | `""` | Sentry error reporting. Empty: off. | hosted_dashboard.py, http_layer.py |
| `LOG_LEVEL` | — | Log level for `logging_setup.py`. | logging_setup.py |
| `HEALTHCHECK_PING_URL` | — | A dead-man's-switch URL the supervisor pings each healthy cycle (`ops.py`). | ops.py |
| `GIT_COMMIT` | — | Build commit for `/health` and the console when Railway's variable is absent. | hosted_dashboard.py, platform_monitor.py |
| `RAILWAY_GIT_COMMIT_SHA` | — | Set by Railway: the deployed commit. | admin_routes.py, hosted_dashboard.py, platform_monitor.py |
| `RAILWAY_DEPLOYMENT_ID` | — | Set by Railway. | platform_monitor.py |
| `RAILWAY_REPLICA_ID` | — | Set by Railway. | platform_monitor.py |
| `RAILWAY_PROJECT_ID` | — | Set by Railway; its presence means "on Railway". | config.py |
| `RAILWAY_ENVIRONMENT` | `"production"` | Set by Railway (default `production`). | config.py, hosted_dashboard.py |

## Sign-in and security

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `ADMIN_USERNAME` | `"will"` | The operator login seeded at boot (default `will`). | admin_routes.py, hosted_dashboard.py |
| `ADMIN_REQUIRE_2FA` | `"0"` | 1 makes the admin console refuse a session with no second factor. | auth.py |
| `HIBP_DISABLED` | — | Skip the breached-password check (it fails open on an outage anyway). | security.py |
| `GOOGLE_SSO_CLIENT_ID` | `""` | Google sign-in OAuth client (the login page's button). | auth_routes.py |
| `GOOGLE_SSO_CLIENT_SECRET` | `""` | Its secret. | auth_routes.py |
| `APPLE_WEB_SERVICES_ID` | — | Sign in with Apple on the web: the Services ID (`ai.cavnar.dashboard`). Empty hides the button. | auth_routes.py |
| `APNS_BUNDLE_ID` | `"ai.cavnar.CavnarAI"` | The app's bundle id (default `ai.cavnar.CavnarAI`); also the audience for the app's Sign in with Apple tokens. | mobile_api.py, push.py |
| `PASSKEY_HOSTS` | — | Extra hosts passkeys may be used on besides `BASE_URL`'s and localhost (a test tunnel), comma-separated. | passkeys.py |
| `ALLOW_PUBLIC_SIGNUP` | `""` | Allows the mobile self-serve register route (off: accounts come from a signed contract). | mobile_api.py |
| `STAFF_OTP_COUNTRY_CODES` | `"1"` | Country calling codes a staff sign-up or forgot-PIN code may be texted to, comma-separated (default +1 only). | auth.py |
| `STAFF_OTP_HOURLY_CAP` | `200` | Platform-wide ceiling on staff verification texts per hour (`auth.STAFF_OTP_HOURLY_CAP_DEFAULT`). | auth.py |
| `STAFF_SIGNUP_DEV_CODE` | — | 1 returns the staff verification code in the response, only while Twilio is not configured (a local backend). | auth.py |

## Email and texts

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `RESEND_WEBHOOK_SECRET` | `""` | Verifies Resend's delivery/bounce webhooks (`webhook_routes.py`) — what turns "sent" into "delivered". | webhook_routes.py |
| `BUG_REPORT_EMAIL` | `"will@cavnar.ai"` | Where in-app bug reports go (default will@cavnar.ai). | emails.py, models.py |
| `SUPPORT_EMAIL` | — | The support address in guest marketing messages; falls back to `WILL_EMAIL`. | guest_marketing.py |
| `CAVNAR_POSTAL_ADDRESS` | `""` | The postal address in marketing email footers (CAN-SPAM). Empty leaves it out. | emails.py |
| `WILL_PHONE` | — | Operator paging by text (`ops.py`). | ops.py |
| `TWILIO_ACCOUNT_SID` | `""` | Twilio account (SMS). | notify.py, provider_health.py |
| `TWILIO_AUTH_TOKEN` | `""` | Twilio auth token. | notify.py, provider_health.py |
| `TWILIO_FROM_NUMBER` | `""` | Fallback sending number for owner texts. | notify.py |
| `TWILIO_GUEST_FROM_NUMBER` | `""` | Fallback sending number for guest texts. | notify.py |
| `TWILIO_MESSAGING_SERVICE_SID` | `""` | A2P messaging service for owner alerts. | notify.py |
| `TWILIO_OTP_MESSAGING_SERVICE_SID` | `""` | A2P messaging service for sign-in codes. | notify.py |
| `TWILIO_GUEST_MESSAGING_SERVICE_SID` | `""` | A2P messaging service for guest marketing texts. | notify.py |
| `TWILIO_STAFF_MESSAGING_SERVICE_SID` | `""` | A2P messaging service for every staff text: a posted or changed week, and request notices (swaps, open shifts, time off), each only for an employee whose own consent covers that purpose (`preferences.staff_sms_scope`). Unset until the staff campaign is registered with the wording in `people.STAFF_SMS_CONSENT_TEXT`; while it is unset `people.staff_sms_ready()` is false, the staff app hides its "text me" switch (`sms_available`) and no staff text goes. | notify.py, people.py |
| `STAFF_SMS_HOLD` | `""` | `1` holds every staff text although the staff campaign is approved and `TWILIO_STAFF_MESSAGING_SERVICE_SID` is set. While it holds, `people.staff_sms_ready()` is false, the "text me" switch stays hidden and a staff send is logged `not_configured` ("on hold"). Set 10/5/26 because employees read their schedule in the staff app, which isn't live for them yet. Remove it to go live. | notify.py, people.py |
| `SMS_TRACKED_LINKS` | `""` | 1 keys the link in alert texts for open tracking (off: the longer link can add a billed segment). | notify.py |
| `PUBLIC_BASE_URL` | — | Origin for guest join links (`/g/<token>`); default `https://dashboard.cavnar.ai`. | guest_marketing.py |
| `ALLOW_LEGACY_JOIN_LINKS` | `"0"` | Accept old unsigned guest join links (default 0). | guest_links.py |
| `IOS_APP_STORE_URL` | — | The App Store link: the onboarding emails' "get the app" step (offered when no owner device is registered; empty leaves the step out), and the App Store button on the staff web page and the `/s/` schedule pages (empty: they name the TestFlight invite instead). | scheduler.py, staff_routes.py (client_api reads it through `staff_routes._app_url`) |

## Push

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `APNS_KEY_ID` | — | Apple Push key id. | provider_health.py, push.py |
| `APNS_TEAM_ID` | — | Apple developer team id. | provider_health.py, push.py |
| `APNS_PRIVATE_KEY` | `""` | Apple Push .p8 key contents. | provider_health.py, push.py |

## Integrations

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `PERPLEXITY_API_KEY` | `""` | Perplexity, for the AI-visibility checks only. | ai_utils.py, client_api.py |
| `AI_VISIBILITY_MODEL` | `"sonar"` | Perplexity model for AI-visibility checks (default `sonar`). | client_api.py |
| `GOOGLE_CLIENT_ID` | `""` | Google Business Profile OAuth client (connecting reviews). | auth_routes.py, gmb.py, mobile_api.py |
| `GOOGLE_CLIENT_SECRET` | `""` | Its secret. | gmb.py |
| `GMB_REDIRECT_URI` | `"https://dashboard.cavnar.ai/auth/google/callback"` | Google Business web OAuth callback. | gmb.py |
| `GMB_MOBILE_REDIRECT_URI` | `"https://dashboard.cavnar.ai/auth/google/mobile-callback"` | Google Business iOS OAuth callback. | gmb.py |
| `GOOGLE_PLACES_API_KEY` | — | Google Places (competitors, ratings). | config.py |
| `GOOGLE_API_KEY` | — | Older name read as a fallback for the Places key (`config.py`). | config.py |
| `META_APP_ID` | `""` | Meta app (Instagram/Facebook connect). | mobile_api.py, scheduler.py, social_routes.py |
| `META_APP_SECRET` | `""` | Its secret. | scheduler.py, social_routes.py |
| `META_REDIRECT_URI` | `"https://dashboard.cavnar.ai/instagram/callback"` | Meta OAuth callback. | mobile_api.py, social_routes.py |
| `GA_SERVICE_ACCOUNT_JSON` | `""` | The Google service account (JSON key, or base64 of it) website analytics reads GA4 and Search Console with — read-only (`analytics.readonly`, `webmasters.readonly`). Unset: website analytics is dormant and the Connections card says so. Railway only, never the repo. | web_analytics.py |
| `WEB_ANALYTICS_MAX_SECONDS` | `900` | The daily website-analytics read's wall-clock bound (resumable). | scheduler.py |
| `RETAIN_WEB_ANALYTICS_DAYS` | `800` | How long the website's daily figures are kept (floor 400). | ops.py |
| `META_LOGIN_CONFIG_ID` | `""` | The Facebook Login for Business configuration the Instagram & Facebook connect dialog names (`config_id`); unset sends the scope list instead (`meta_api.login_params`). | meta_api.py |
| `META_GRAPH_VERSION` | `"v21.0"` | Graph API version (default v21.0). | meta_api.py |
| `TOAST_API_BASE` | `"https://ws-api.toasttab.com"` | Toast API host. | toast.py |
| `TOAST_SANDBOX` | `""` | Use Toast's sandbox. | toast.py |
| `BACKOFFICE_API_BASE` | — | Back Office API host, once one exists (`docs/plans/BACK_OFFICE_INTEGRATION.md`). | backoffice.py |
| `POS_ARCHIVE_READS` | `"1"` | 1 (default): nightly readers read a closed day from the POS ticket archive instead of the POS API. | pos_archive.py |

## Billing and contracts

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `STRIPE_SECRET_KEY` | `""` | Stripe, for checkout, subscriptions and reconcile. | admin_ops.py, admin_routes.py, billing_jobs.py, client_api.py |
| `STRIPE_WEBHOOK_SECRET` | `""` | Verifies Stripe webhooks. | admin_routes.py, hosted_dashboard.py, webhook_routes.py |
| `BILLING_PREVIEW_IDS` | `""` | Restaurant ids that see the billing screen before launch (`client_api.py`). | client_api.py |
| `DOCUSIGN_INTEGRATION_KEY` | `""` | DocuSign app (JWT). | docusign_helper.py |
| `DOCUSIGN_USER_ID` | `""` | DocuSign impersonated user. | docusign_helper.py |
| `DOCUSIGN_ACCOUNT_ID` | `""` | DocuSign account. | docusign_helper.py |
| `DOCUSIGN_PRIVATE_KEY` | `""` | DocuSign RSA key. | docusign_helper.py |
| `DOCUSIGN_BASE_URL` | `"https://demo.docusign.net"` | DocuSign API host (default the demo host; production is na4). | docusign_helper.py |
| `DOCUSIGN_AUTH_HOST` | `""` | DocuSign auth host. | docusign_helper.py |
| `DOCUSIGN_TEMPLATE_ID` | `""` | The service-agreement template. | docusign_helper.py |
| `DOCUSIGN_TEMPLATE_NAME` | `"Cavnar AI Service Agreement"` | Template name used by `scripts/docusign_create_template.py`. | scripts/docusign_create_template.py |
| `DOCUSIGN_REDIRECT_URI` | `"https://dashboard.cavnar.ai/docusign/callback"` | DocuSign consent callback. | docusign_helper.py |
| `DOCUSIGN_WEBHOOK_SECRET` | `""` | Verifies DocuSign Connect webhooks. | webhook_routes.py |

## Backups

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `BACKUP_S3_BUCKET` | — | Off-site backup bucket. | offsite_backup.py |
| `BACKUP_S3_ENDPOINT` | — | S3-compatible endpoint. | offsite_backup.py |
| `BACKUP_S3_REGION` | — | Region. | offsite_backup.py |
| `BACKUP_S3_ACCESS_KEY_ID` | — | Access key. | offsite_backup.py |
| `BACKUP_S3_SECRET_ACCESS_KEY` | — | Secret. | offsite_backup.py |
| `BACKUP_S3_PREFIX` | `"cavnar-backups/"` | Key prefix (default `cavnar-backups/`). | offsite_backup.py |
| `BACKUP_DIR` | — | Where the nightly local snapshot is written; default `backups/` beside the database. | scheduler.py |
| `BACKUP_RETAIN_COUNT` | `"3"` | How many local snapshots (`cavnar_ai_backup_<date>.db.gz`, gzipped since 10/7/26) are kept — the newest N, gzipped and older plain ones counted together. Replaces `BACKUP_RETAIN_DAYS` (7 by default, 14 set in production), which the code no longer reads: remove it from Railway. | scheduler.py |
| `BACKUP_FREE_SPACE_FACTOR` | `"1.5"` | Free space the backup needs before it starts, as a multiple of the database and its WAL: the `VACUUM INTO` copy (at most the database) plus its gzip, with margin. Was 3.5 when the snapshot, the scrubbed copy and the encryption all had to fit up front; the off-site copy now checks its own room (1.4x the snapshot) just before it is made. | scheduler.py |

## Model behaviour

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `RESPONSE_VALIDATION_MODE` | — | JSON `{surface: "shadow"\|"enforce", "*": …}`; default enforce everywhere. A surface in shadow logs the verdict but shows the original text (`response_validation.mode_for`). | response_validation.py |
| `PROVIDER_PROBES_SKIP` | — | Comma-separated provider names whose health probe is skipped (`provider_health._skipped`). | provider_health.py |
| `DEMO_PASSWORD` | — | Password for the seeded demo accounts (`demo_seed.py`). | demo_seed.py |
| `AI_BATCHES_ENABLED` | `"1"` | The Message Batches kill switch (`ai_batches.enabled`, AI cost audit 10/7/26 #19): `0` sends every batchable call synchronously at list price. Batches only ever go where `scheduler.scheduling_allowed()` — never from a local backend, whatever this says. | ai_batches.py |
| `AI_TIER_T1_MODEL` | `claude-haiku-4-5-20251001` | The model behind tier T1 for every workflow whose ladder names it (`ai_workflows`, AI orchestration design 10/7/26). One change moves every T1 workflow; the learner measures it. | ai_workflows.py |
| `AI_TIER_T2_MODEL` | `claude-sonnet-5` | Tier T2's model. | ai_workflows.py |
| `AI_CANARY_RESTAURANTS` | `"4"` | Comma-separated restaurant ids that get a canaried workflow's cheap first rung (`ai_workflows.canary_start`, context re-audit 10/7/26 #3). A policy with `canary` set — `labor_insight`, `draft_response`, `staff_answer`, `task_sheet_starter`, each Haiku-first where the call site ran Sonnet before the registry — starts on T1 only for these; every other restaurant starts one rung up (T2), as before. The default is Simple EJ's Demo (Will's test account). Empty: no canary restaurant; `*`: every restaurant (the canary over for every canaried workflow at once). The console turns `canary` off per workflow (an override) once the learner's numbers on the canary look right; `ai_runs.canary` records which a run was. | ai_workflows.py |
| `AI_TIER_T3_MODEL` | `claude-sonnet-5-5` | Tier T3's model (adaptive thinking). | ai_workflows.py |
| `AI_TIER_T3_EFFORT` | `medium` | Tier T3's effort. | ai_workflows.py |
| `AI_TIER_T4_MODEL` | `claude-opus-5-5` | Tier T4's model (adaptive thinking). | ai_workflows.py |
| `AI_TIER_T4_EFFORT` | `medium` | Tier T4's effort. | ai_workflows.py |
| `SCHEDULE_MODEL` | — | Pins the schedule generator to one model (`ai_utils.MODELS["schedule"]`) at `labor.SCHEDULE_EFFORT`, skipping the `labor_schedule` ladder (T3 Sonnet 5.5 first, T4 Opus 5.5 for a hard week or the quality gate's rewrite — owner decision 3, 10/7/26) and its pre-router (`schedule_engine.schedule_route`). Unset: the ladder. A model before the 5.5 generation runs the old thinking-off shape. | schedule_engine.py |
| `SCHEDULE_CONTRACT` | `schema` | The schedule generator's output contract, read once per generation (`schedule_output.schedule_contract`; AI cost audit 10/7/26 #69, #70): `schema` — rows `{employee, role, start, end, note}`; `compact` — the same rows with one-letter keys `{e, r, s, t, n}` (~24 answer tokens a row to ~30); `shape` — slots `{r, s, t, c, n}` without names, whose people `schedule_engine.assign_shape_slots` solves over the same rules before every repair and gate step. Anything else reads as `schema`. Stays `schema` until `scripts/schedule_model_eval.py --prompts rerender,rerender:compact,rerender:shape` shows no quality loss on stored weeks. | schedule_output.py |
| `AI_REPLAY_SAMPLE_RATE` | `0.05` | Share of workflow runs whose model requests are kept (redacted, compressed, `ai_run_requests`) so the learner can replay a cheaper route on real inputs. `0` keeps none. | ai_orchestrator.py |
| `AI_SHADOW_REVIEW` | `"1"` | `0` stops the shadow reviewer (a Haiku rubric scoring a sample of passing runs off the request path). Gates are unaffected. | ai_orchestrator.py |
| `AI_BATCHES_WORKFLOWS` | `"dsr_narrative,shadow_arms,review_diagnosis,food_cost_diagnosis,competitor_insight,recipe_draft,weekly_digest,quiet_night_post"` | Comma-separated workflows allowed to batch (half price, answered within 24 hours); a workflow not named here is written synchronously. `shadow_arms` is the learner's weekly replays — they never run synchronously, so leaving it out stops them. `review_diagnosis` and `food_cost_diagnosis` are the morning root-cause reads (#58; left out, the 6am pass calls for every one synchronously at full price). The AI cost audit (10/7/26) added the weekly competitor read (#59, cutoff `competitor.BATCH_CUTOFF_HOURS` 3), the Tuesday recipe drafts (#60, 6 hours), the digest's narrative sent from 2am local on the digest day (#61, cut off at the 9am send) and the quiet-night post (#62, 6 hours): each falls back to a synchronous call when its answer is not back by its cutoff (`ai_batches` cutoff sweep). A value set here replaces the default: name every one to keep it. | ai_batches.py |
| `AI_SHADOW_WEEKLY_USD` | `"2"` | The most the learner's shadow replays may spend in 7 days — the replays and the rubric calls that score them, every ledger row under a `shadow:` correlation id (`ai_learning.run_shadow_arms`, job `ai_shadow_arms`). At most 20 replays a workflow a week either way. | ai_learning.py |
| `VALIDATION_RECHECK_DEDUPE_DAYS` | `"7"` | A validation check with no model call behind it (a stored text re-checked, e.g. the reply-draft flags at boot) is not written to `ai_validation_log` when the same text got the same verdict under the same rules version within this many days (W4). `0` writes every check. | ai_utils.py |
| `AI_INTERACTIVE_ESCALATION_SECONDS` | `20` | How long after an interactive read starts a second, stronger attempt may still begin (`ai_orchestrator.escalation_deadline` — the labor and marketing reads' T1 → T2). Past it the first attempt's outcome stands and the read's own fallback is served: an owner never waits twice. | ai_orchestrator.py |
| `DSR_BATCH_CUTOFF_MINUTES` | `"45"` | How long the DSR sweep waits for a batched narrative before writing it synchronously and discarding the batch's answer (`dsr.pipeline`, #20) — never past the end of quiet hours or the night's missing check, less 20 minutes. A night is batched only inside the restaurant's alert quiet hours (its push is held anyway); outside them it is written at once. | dsr/pipeline.py |

## Legacy and scripts only

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `GOOGLE_PLACE_ID` | — | Legacy single-restaurant runner (`main.py`) only. | main.py |
| `YELP_BUSINESS_ID` | — | Legacy `main.py` only. | main.py |
| `RESTAURANT_NAME` | `"Maplewood Kitchen"` | Legacy `main.py` only. | main.py |
| `OWNER_EMAIL` | `"owner@maplewoodkitchen.com"` | Legacy `main.py` only. | main.py |
| `SMTP_HOST` | — | Legacy SMTP for `reporter.py`'s standalone digest (Resend is the live path). | reporter.py |
| `SMTP_USER` | — | Legacy SMTP user. | reporter.py |
| `SMTP_PASS` | — | Legacy SMTP password. | reporter.py |
| `SMTP_FROM` | — | Legacy SMTP sender. | reporter.py |
| `DB` | — | `scripts/seed_review_account.py` only: the database to seed. | scripts/seed_review_account.py |
| `REVIEW_EMAIL` | `"appreview@cavnar.ai"` | `scripts/seed_review_account.py`: the App Store reviewer account. | scripts/seed_review_account.py |
| `REVIEW_USERNAME` | `"appreview"` | Same. | scripts/seed_review_account.py |
| `REVIEW_RESTAURANT` | `"The Copper Table"` | Same. | scripts/seed_review_account.py |

## Tuning knobs

Bounds, budgets, thresholds and pool sizes, each with a safe default. Change one only to answer a measured problem, and say which in the commit.

Changed or added by the AI cost audit (10/7/26):

- `AI_GLOBAL_PER_CLIENT_USD` — **30** (was 200): the shared AI pool is the larger of `AI_GLOBAL_MONTHLY_BUDGET_USD` ($1,500) and $30 a paying client, capped at `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` ($10,000). A full-tier restaurant spends $15–25 a month, so $200 each let the pool reach ~8x real use before it noticed a runaway; at $30 the floor covers the first fifty clients and the pool grows with real use after that.
- `AI_PLACES_GLOBAL_MONTHLY_USD` — **300**: Google Places across every restaurant and every unattributed request, a month. The per-restaurant ceilings (`AI_PLACES_DAILY_BUDGET_USD` $3, `AI_PLACES_MONTHLY_BUDGET_USD` $30) bound one restaurant and nothing bounded the fleet. About three times expected spend (a few dollars a restaurant a month); 80% pages Will; the review fetch is still never refused; 0 disables it.
- `INTERACTIVE_AI_SLOTS` — **2**: model calls made on a request thread at once, process-wide (`ai_utils`, the interactive guard). gunicorn has four request threads; two always stay free for logins, pages, webhooks and `/health`. An Ask turn holds one for the whole turn. `INTERACTIVE_AI_WAIT_SECONDS` (**3**) is how long a call waits for one before the owner is told "busy, try again in a moment"; `INTERACTIVE_AI_TIMEOUT` (**40**) is the per-try timeout such a call gets (with one retry) when its caller names none. Scheduler and background calls take no slot. Per process, like `ASK_MAX_CONCURRENT`.
- `DIGEST_WORKERS` (**2**) and `DIGEST_MAX_SECONDS` (**1200**): the weekly digest pass's pool and wall-clock bound (cursor `weekly_digest_cursor`); a restaurant not reached gives its day back to the next hourly pass.
- `OWNER_AI_JOB_WORKERS` — **3**: the owner AI job pool (`ai_async`, #57) — an invoice read, a recipe-card read and the Campaign Studio's three drafts run there as jobs the client polls, off the four request threads. Per process, like the other pools (CLAUDE.md, gunicorn `--workers`).

| Variable | Default | Read in |
|---|---|---|
| `ADMIN_BADGES_MAX_AGE_SECONDS` | `"300"` | admin_ops.py |
| `ADMIN_BUSY_RETRY_AFTER` | `"5"` | admin_ops.py |
| `ADMIN_CHURN_ISSUE_DAYS` | `"7"` | admin_ops.py |
| `ADMIN_FLEET_MAX_WAITERS` | `"1"` | admin_ops.py |
| `ADMIN_FLEET_TTL_SECONDS` | `"45"` | admin_ops.py |
| `ADMIN_FLEET_WAIT_SECONDS` | `"30"` | admin_ops.py |
| `ADMIN_HEAVY_CONCURRENCY` | `"1"` | admin_ops.py |
| `ADMIN_HEAVY_WAIT_SECONDS` | `"2"` | admin_ops.py |
| `ADMIN_JOB_WORKERS` | `"2"` | admin_routes.py |
| `ADMIN_RESOLUTION_MAX_DAYS` | `"30"` | admin_ops.py |
| `ADMIN_SIGNED_UNPAID_CRIT_DAYS` | `"30"` | admin_ops.py |
| `ADMIN_SIGNED_UNPAID_WARN_DAYS` | `"7"` | admin_ops.py |
| `ADMIN_SOURCE_FAIL_ISSUE` | `"3"` | admin_ops.py |
| `ADMIN_TASK_WORKERS` | `"2"` | ops.py |
| `ADMIN_WEBHOOK_STALE_HOURS` | `"24"` | admin_ops.py |
| `AI_BREAKER_OPEN_SECONDS` | `"60"` | ai_utils.py |
| `AI_BREAKER_PROBE_SECONDS` | `"120"` | ai_utils.py |
| `AI_BREAKER_STUCK_MINUTES` | `"15"` | ai_utils.py |
| `AI_BREAKER_THRESHOLD` | `"5"` | ai_utils.py |
| `AI_BUDGET_WARN_PCT` | `"80"` | ai_utils.py |
| `AI_CALLS_RETAIN_DAYS` | `"120"` | ai_utils.py |
| `AI_DAILY_BUDGET_USD` | `"10"` | ai_utils.py |
| `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` | `"10000"` | ai_utils.py |
| `AI_GLOBAL_MONTHLY_BUDGET_USD` | `"1500"` | ai_utils.py |
| `AI_GLOBAL_PER_CLIENT_USD` | `"30"` | ai_utils.py |
| `AI_HEALTH_RETAIN_DAYS` | `"365"` | ai_utils.py |
| `AI_MEMORY_SIZES_RETAIN_DAYS` | `"400"` | ai_utils.py |
| `AI_MONTHLY_BUDGET_USD` | `"150"` | ai_utils.py |
| `AI_PAGE_COOLDOWN_MINUTES` | `"60"` | ai_utils.py |
| `AI_PLACES_DAILY_BUDGET_USD` | `"3"` | ai_utils.py |
| `AI_PLACES_MONTHLY_BUDGET_USD` | `"30"` | ai_utils.py |
| `AI_PLACES_GLOBAL_MONTHLY_USD` | `"300"` | ai_utils.py |
| `AI_QUALITY_RETAIN_DAYS` | `"180"` | ai_utils.py |
| `AI_STATUS_DEGRADED_PCT` | `"20"` | ai_utils.py |
| `AI_STATUS_MIN_CALLS` | `"5"` | ai_utils.py |
| `AI_STATUS_OUTAGE_PCT` | `"50"` | ai_utils.py |
| `AI_TRACE_DAYS` | `"30"` | ai_utils.py |
| `AI_TRACE_KEEP_PER_ACTION` | `"10"` | ai_utils.py |
| `AI_TRACE_OUTPUT_CHARS` | `"12000"` | ai_utils.py |
| `AI_TRACE_PROMPT_CHARS` | `"40000"` | ai_utils.py |
| `AI_TRACE_SCHEDULE_OUTPUT_CHARS` | `"300000"` | ai_utils.py |
| `AI_TRACE_SCHEDULE_PROMPT_CHARS` | `"400000"` | ai_utils.py |
| `AI_TRIAL_DAILY_BUDGET_USD` | `"5"` | ai_utils.py |
| `AI_TRIAL_MONTHLY_BUDGET_USD` | `"50"` | ai_utils.py |
| `AI_TRIAL_POOL_DAILY_USD` | `"50"` | ai_utils.py |
| `AI_TRIAL_POOL_MONTHLY_USD` | `"500"` | ai_utils.py |
| `AI_UNPAID_DAILY_BUDGET_USD` | `"2"` | ai_utils.py |
| `AI_UNPAID_MONTHLY_BUDGET_USD` | `"25"` | ai_utils.py |
| `AI_VISIBILITY_CACHE_SECS` | `"21600"` | client_api.py |
| `AIVIS_429_MAX_SENDS` | `"4"` | client_api.py — sends one visibility question gets when Perplexity answers 429 (AI cost audit 10/7/26 #7) |
| `AIVIS_RUN_MAX_SECS` | `"75"` | client_api.py — one visibility run's wall clock; no backoff or new send past it |
| `AIVIS_SERVE_MAX_DAYS` | `"35"` | client_api.py — how old a stored visibility run a read still serves (else "not measured yet"; #9) |
| `ALERT_STORM_PER_HOUR` | `"10"` | notify.py |
| `ASK_LOOP_MAX_SECONDS` | `"60"` | ask_cavnar.py |
| `ASK_STREAM_SENTENCES` | `"1"` | ask_cavnar.py — `0` turns off the Ask stream's validated sentence preview (no `sentence` events, no streamed model call; AI cost audit 10/7/26 #68) |
| `ASK_MAX_CONCURRENT` | `"2"` | client_api.py |
| `BACKUP_EMAIL_MAX_BYTES` | `str(25 * 1024 * 1024` | scheduler.py |
| `BACKUP_EMAIL_MODE` | `"always"` | scheduler.py |
| `BACKUP_OFFSITE_MAX_DAYS` | `"35"` | scheduler.py |
| `BRIEF_INPUT_WAIT_UNTIL_HOUR` | `"8"` | scheduler.py — the Chicago hour until which the morning briefs (and the weekly digest) wait for the 5-6am sweeps they read to settle; from it they go on what is there (AI cost audit 10/7/26 #54) |
| `CAMPAIGN_ATTRIBUTION_SECONDS` | `str(10 * 60` | guest_marketing.py |
| `CLAIM_RECLAIM_MINUTES` | `"120"` | ops.py |
| `COMPETITOR_WORKERS` | `"3"` | scheduler.py — restaurants the weekly competitor analysis reads at once (AI cost audit 10/7/26 #55; was 1) |
| `DAILY_ALERT_PASS_SECONDS` | `"600"` | notify.py |
| `DIAGNOSES_BATCH_UNTIL` | `"5:30"` | scheduler.py — Chicago time after which the diagnoses' Message Batches are not sent (no time to land before the 6am pass; AI cost audit 10/7/26 #58) |
| `DIAGNOSES_MAX_SECONDS` | `str(40 * 60` | scheduler.py |
| `DIAGNOSES_WORKERS` | `"3"` | scheduler.py — restaurants the review and food cost diagnoses (and their batch planning) run at once (AI cost audit 10/7/26 #53; was 1) |
| `DIGEST_MAX_SECONDS` | `str(20 * 60` | scheduler.py |
| `DIGEST_WORKERS` | `"2"` | scheduler.py |
| `EMAIL_BOUNCE_CRIT_PCT` | `"8"` | admin_ops.py |
| `EMAIL_BOUNCE_WARN_PCT` | `"4"` | admin_ops.py |
| `EMAIL_COMPLAINT_CRIT_PCT` | `"0.3"` | admin_ops.py |
| `EMAIL_COMPLAINT_WARN_PCT` | `"0.1"` | admin_ops.py |
| `EMAIL_RATE_MIN_SENDS` | `"50"` | admin_ops.py |
| `FETCH_MAX_SECONDS` | `str(3 * 3600` | scheduler.py |
| `FETCH_STALE_HOURS` | `"12"` | admin_ops.py |
| `FETCH_WORKERS` | `"6"` | scheduler.py |
| `GUEST_SMS_PER_SECOND` | — | guest_marketing.py |
| `INTERACTIVE_AI_SLOTS` | `"2"` | ai_utils.py |
| `INTERACTIVE_AI_TIMEOUT` | `"40"` | ai_utils.py |
| `INTERACTIVE_AI_WAIT_SECONDS` | `"3"` | ai_utils.py |
| `INVENTORY_SYNC_MAX_SECONDS` | `str(15 * 60` | inventory_sync.py |
| `LABOR_PREWARM_MAX_SECONDS` | `str(20 * 60` | scheduler.py — the bound on the 3am Labor read pre-warm (AI cost audit 10/7/26 #100) |
| `LEARNING_HOLDOUT_PCT` | `str(HOLDOUT_DEFAULT_PCT` | rec_learning.py |
| `LEASE_OWNER_GONE_SECONDS` | `"240"` | ops.py |
| `LEASE_RENEW_SECONDS` | `"60"` | ops.py |
| `METRICS_SYNC_SECONDS` | `"1800"` | scheduler.py |
| `OPTIN_INVITE_SECONDS` | `"240"` | guest_marketing.py |
| `POS_RETRY_MAX_SECONDS` | `str(15 * 60` | scheduler.py |
| `PPLX_BACKOFF_BASE` | `"2"` | client_api.py — first step of a 429's exponential backoff (with jitter) when no Retry-After came |
| `PPLX_MIN_REQUEST_INTERVAL` | `"1.3"` | client_api.py |
| `PPLX_RETRY_AFTER_MAX` | `"8"` | client_api.py — the longest Retry-After a 429 is waited out; a longer one gives the question up |
| `PRICE_PERPLEXITY_SEARCH` | `"0.005"` | ai_utils.py |
| `PRICE_PLACES_DETAILS` | `"0.017"` | ai_utils.py |
| `PRICE_PLACES_NEARBY` | `"0.032"` | ai_utils.py |
| `PRICE_PLACES_TEXTSEARCH` | `"0.032"` | ai_utils.py |
| `PRICE_PLACES_ATMOSPHERE` | `"0.005"` | ai_utils.py |
| `PRICE_PLACES_CONTACT` | `"0.003"` | ai_utils.py |
| `PUSH_MAX_OVERFLOW` | `"20000"` | push.py |
| `PUSH_MAX_QUEUED` | `"500"` | push.py |
| `PUSH_MAX_WORKERS` | `"4"` | push.py |
| `OWNER_AI_JOB_WORKERS` | `"3"` | ai_async.py |
| `PUSH_OUTBOX_MAX_AGE_MINUTES` | `"120"` | push.py |
| `RETENTION_CHUNK_ROWS` | `"5000"` | ops.py |
| `RETENTION_MAX_SECONDS` | `str(10 * 60` | ops.py |
| `RETENTION_PASS_MAX_ROWS` | `"200000"` | ops.py |
| `REVIEW_NEWS_MAX_AGE_DAYS` | `"7"` | notify.py |
| `REVIEW_QUIET_MAX_PER_DAY` | `"2"` | fetcher.py — a Places-only listing whose Google count grew by fewer reviews a day than this over 14 days is fetched in the quiet slots only (AI cost audit 10/7/26 #45) |
| `REVIEW_QUIET_SLOTS` | `"8,16"` | fetcher.py — the Chicago review-fetch slots a quiet Places-only restaurant keeps (a subset of 8,12,16,20; never two adjacent skipped) |
| `REVIEW_REQUEST_DELAY_HOURS` | `DEFAULT_REVIEW_REQUEST_DELAY_HOURS` | guest_marketing.py |
| `RUN_DEAD_MINUTES` | `"10"` | ops.py |
| `RUN_PULSE_SECONDS` | `"60"` | ops.py |
| `RUN_REQUEST_TTL_MINUTES` | `"30"` | ops.py |
| `SCHEDULER_LEASE_STALE_SECONDS` | `"1800"` | ops.py |
| `SCHEDULER_TICK_SECONDS` | `"300"` | admin_routes.py, scheduler.py |
| `SCHEDULE_GEN_WORKERS` | `"2"` | schedule_engine.py |
| `STRIPE_RECONCILE_MAX_SECONDS` | `str(15 * 60` | billing_jobs.py |
| `SWEEP_MAX_SECONDS` | `str(45 * 60` | scheduler.py |
| `SWEEP_WORKERS` | `"1"` | scheduler.py |
| `TOKEN_REFRESH_MAX_SECONDS` | `str(10 * 60` | scheduler.py |
| `WEBHOOK_MAX_QUEUED` | `"500"` | webhooks.py |
| `WEBHOOK_MAX_WORKERS` | `"4"` | webhooks.py |
| `WEBHOOK_OUTBOX_MAX_AGE_HOURS` | `"24"` | webhooks.py |
| `WEEKLY_SWEEP_MAX_SECONDS` | `str(3 * 3600` | scheduler.py |

## Retention windows

`RETAIN_<TABLE>_DAYS` overrides one ledger's retention window (68 of them; `ops._RETENTION_DAYS` is the registry and its defaults, `DATABASE_SCHEMA.md` → retention). 0 keeps a table's rows forever.

`RETAIN_ACTIVITY_LOG_DAYS`, `RETAIN_ADMIN_EVENTS_DAYS`, `RETAIN_AIVIS_QUERIES_DAYS`, `RETAIN_AIVIS_RUNS_DAYS`, `RETAIN_AI_BATCHES_DAYS`, `RETAIN_AI_CLAIMS_DAYS`, `RETAIN_AI_READS_DAYS`, `RETAIN_AI_ROUTE_RECS_DAYS`, `RETAIN_AI_RUNS_DAYS`, `RETAIN_AI_RUN_REQUESTS_DAYS`, `RETAIN_AI_USAGE_DAYS`, `RETAIN_AI_VALIDATION_DAYS`, `RETAIN_ALERT_HOLDS_DAYS`, `RETAIN_ALERT_LOG_DAYS`, `RETAIN_ALERT_STORM_CAPS_DAYS`, `RETAIN_ASK_FEEDBACK_DAYS`, `RETAIN_ASK_MEMORY_ARCHIVE_DAYS`, `RETAIN_ASK_TOPICS_DAYS`, `RETAIN_ATTENDANCE_DAYS`, `RETAIN_BACKUP_RUNS_DAYS`, `RETAIN_BOOT_EVENTS_DAYS`, `RETAIN_BRIEF_DELIVERIES_DAYS`, `RETAIN_COMPETITOR_SNAPSHOTS_DAYS`, `RETAIN_CONTEXT_SECTIONS_DAYS`, `RETAIN_DATA_HEALTH_DAILY_DAYS`, `RETAIN_DRAFT_DETAIL_DAYS`, `RETAIN_EMAIL_LOG_DAYS`, `RETAIN_EXPIRED_SESSIONS_DAYS`, `RETAIN_HTTP_5XX_DAYS`, `RETAIN_INVENTORY_DAILY_DAYS`, `RETAIN_INVENTORY_HISTORY_DAYS`, `RETAIN_JOB_CLAIMS_DAYS`, `RETAIN_JOB_FAILURES_DAYS`, `RETAIN_JOB_RUNS_DAYS`, `RETAIN_JOB_RUN_REQUESTS_DAYS`, `RETAIN_LINK_TAPS_DAYS`, `RETAIN_LOGIN_HISTORY_DAYS`, `RETAIN_MISSED_WINDOWS_DAYS`, `RETAIN_MKT_MODEL_DRAFTS_DAYS`, `RETAIN_NOTIFICATION_OPENS_DAYS`, `RETAIN_OPERATOR_ALERTS_DAYS`, `RETAIN_PERSON_SIGNALS_DAYS`, `RETAIN_PLACES_CACHE_DAYS`, `RETAIN_POS_ARCHIVE_DAYS`, `RETAIN_PROVIDER_HEALTH_DAYS`, `RETAIN_PUSH_DELIVERIES_DAYS`, `RETAIN_PUSH_OUTBOX_DAYS`, `RETAIN_REC_EVENTS_DAYS`, `RETAIN_REC_RANK_BUILDS_DAYS`, `RETAIN_REC_SILENCES_DAYS`, `RETAIN_REPLY_REJECTIONS_DAYS`, `RETAIN_REQUEST_ROLLUPS_DAYS`, `RETAIN_RESOLUTION_HISTORY_DAYS`, `RETAIN_REVIEW_ERASE_DAYS`, `RETAIN_SCHEDULE_MODEL_CALLS_DAYS`, `RETAIN_SCHEDULE_VERSIONS_DAYS`, `RETAIN_SCHED_EDIT_ANSWERS_DAYS`, `RETAIN_SCHED_OBSERVATIONS_DAYS`, `RETAIN_SCHED_RECS_DAYS`, `RETAIN_SCHED_REJECTIONS_DAYS`, `RETAIN_SHIFT_FACTS_DAYS`, `RETAIN_SHIFT_SECTIONS_DAYS`, `RETAIN_SMS_LOG_DAYS`, `RETAIN_STAFF_COMMS_DAYS`, `RETAIN_STAFF_OPS_DAYS`, `RETAIN_STAFF_TRANSLATIONS_DAYS`, `RETAIN_STRIPE_EVENTS_SEEN_DAYS`, `RETAIN_SUPERSEDED_DRAFTS_DAYS`, `RETAIN_TASK_PHOTO_DAYS`, `RETAIN_TASK_SHEET_DAYS`, `RETAIN_VALUE_FIGURES_DAYS`, `RETAIN_VERSION_DETAIL_DAYS`, `RETAIN_WEBHOOK_DELIVERIES_DAYS`, `RETAIN_WEBHOOK_OUTBOX_DAYS`
