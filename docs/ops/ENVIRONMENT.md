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
| `LOG_DRAIN_URL` | `""` (off) | An https endpoint every process (web and queue worker) also ships its JSON log lines to, in batches off the request path (`log_drain`, scale audit 10/19/26 OPS-8): Better Stack, Axiom, Datadog's HTTP intake, anything taking a JSON array POST. Off, nothing changes; plain http is refused. | log_drain.py |
| `LOG_DRAIN_TOKEN` | `""` | Sent as `Authorization: Bearer` to the drain. A secret. | log_drain.py |
| `LOG_DRAIN_LEVEL` | `"INFO"` | The lowest level shipped to the drain. | log_drain.py |
| `LOG_DRAIN_QUEUE_MAX`, `LOG_DRAIN_BATCH`, `LOG_DRAIN_FLUSH_SECONDS` | `5000`, `200`, `2` | The drain's bounded queue (oldest dropped and counted when full), lines per POST, seconds between sends. | log_drain.py |
| `PORT` | `5000` | The port `hosted_dashboard.py` binds when run directly (default 5000; run locally as `PORT=5050`, which the iOS debug build expects). | hosted_dashboard.py |
| `RUN_SCHEDULER_IN_WEB` | `"1"` (`"0"` with `SCHEDULER_PROCESS=1`) | 1 (default): the web process runs the scheduler thread. `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`. | hosted_dashboard.py |
| `SCHEDULER_PROCESS` | unset (off) | `1`: gunicorn's master also starts `worker.py --scheduler` — the scheduler loop as its own process in the web container, off the web process's GIL (scale audit 10/19/26 INF-13) — and the web process starts no scheduler thread. The lease still elects one runner. Rollback: unset it. | gunicorn.conf.py, hosted_dashboard.py |
| `SCHEDULER_PROCESS_SUPERVISED`, `QUEUE_WORKER_SUPERVISED` | set by gunicorn's master | `1` when the master supervises that process (set before the web worker forks): the platform check warns when a supervised queue worker goes silent. Never set them by hand. | gunicorn.conf.py, ops.py |
| `CAVNAR_SEASONS_DIR` | `event_intel/seasons` | Where the bundled season files load from. Unset in production; the test suite points it at its frozen copy, `tests/fixtures/seasons`, because the live files are refreshed daily (`.github/workflows/refresh-seasons.yml`). | event_intel/store.py |
| `ALLOW_LOCAL_SCHEDULER` | `""` | 1 lets a non-Railway backend run the scheduler and live sends. Never set it on a machine with production keys (CLAUDE.md). | scheduler.py |
| `RESTORE_FROM` | — | Boot-time database restore source (`db_restore.py`, `docs/ops/RECOVERY.md`). While set, the scheduler and live sends stay off. | admin_routes.py, db_restore.py, scheduler.py |
| `ALLOW_EMPTY_DATABASE` | — | 1 lets the app boot on an empty database where the volume's marker says client data existed (a deliberate fresh start); otherwise that boot is refused and `/health` fails (`status_manager.platform_emptied`). | status_manager.py |
| `CAVNAR_CONN_POOL` | `1` | Reuse SQLite connections per thread (10/3/26): a new connection re-reads the whole schema, ~2.6ms, against ~0.01ms for a pooled one. Each open handle still has a connection of its own, uncommitted work is rolled back on close, and state a caller changed is reset or the connection is never reused (`models.get_conn`). `0` opens and closes every connection for real — the kill switch if anything looks wrong. | models.py |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | SQLite's `synchronous` level on every new connection once WAL is confirmed (AI cost audit 10/7/26 #92). `NORMAL` under WAL never corrupts the file and keeps every commit through an app crash, kill or redeploy; a host power loss or OS crash can lose the commits since the last checkpoint (seconds of writes) — the file reads as it was a moment earlier. `FULL` (or `EXTRA`) fsyncs every commit again, SQLite's default. A database not in WAL always keeps FULL (`models.sqlite_synchronous`, `docs/ops/RECOVERY.md`). | models.py |
| `CAVNAR_FORCE_SECURE_COOKIES` | — | Force the `Secure` cookie flag (and HSTS) off Railway, e.g. behind another TLS proxy. | auth.py |
| `CAVNAR_SECRET_KEY_EPHEMERAL` | — | Set by the app itself when it had to mint a throwaway `SECRET_KEY`, so presence checks (the admin key panel) don't read it as configured (#125). Never set by hand. | hosted_dashboard.py |
| `SENTRY_DSN` | `""` | Sentry error reporting, in every process — the web process, the queue worker and the scheduler process (`sentry_setup`, scale audit 10/19/26 OPS-5), each event tagged with its `process`. Empty: off. | sentry_setup.py, http_layer.py |
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
| `INTAKE_EMAIL_DOMAIN` | `""` | The receiving domain a new invoice intake address is made on (the admin's Invoice intake card, `email_intake.default_domain`); unset, the domain of the newest address on file. | email_intake.py |
| `BUG_REPORT_EMAIL` | `"will@cavnar.ai"` | Where in-app bug reports go (default will@cavnar.ai). | emails.py, models.py |
| `SUPPORT_EMAIL` | — | The support address in guest marketing messages; falls back to `WILL_EMAIL`. | guest_marketing.py |
| `CAVNAR_POSTAL_ADDRESS` | `""` | The postal address in marketing email footers (CAN-SPAM). Empty leaves it out. | emails.py |
| `WILL_PHONE` | — | Operator paging by text (`ops.py`). | ops.py |
| `OPS_ONCALL_PHONES` | `""` | More phones a platform page is texted to, comma-separated, beside `WILL_PHONE` — a second responder (scale audit 10/19/26 OPS-4; `ops.oncall_phones`). | ops.py |
| `OPS_ONCALL_EMAILS` | `""` | More addresses a platform page is emailed to, comma-separated, beside `WILL_EMAIL` (`ops.oncall_emails`). | ops.py |
| `OPS_PAGER_KIND` | `""` | `pagerduty` sends every platform page to PagerDuty Events v2 (needs `OPS_PAGER_ROUTING_KEY`), deduplicated on the page's key; anything else with `OPS_PAGER_WEBHOOK_URL` set POSTs `{key, subject, lines, source}` there. Off until set (OPS-4: acknowledgement, phone escalation and a second on-call belong to a paging service). | ops.py |
| `OPS_PAGER_ROUTING_KEY` | `""` | The PagerDuty Events v2 integration (routing) key. A secret. | ops.py |
| `OPS_PAGER_WEBHOOK_URL` | `""` | A paging service's incoming-webhook URL (Better Stack, Opsgenie, ...). A secret. | ops.py |
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
| `WEB_PUSH_VAPID_PUBLIC_KEY` | — | Web Push for the installable staff portal (scheduler audit 10/10/26 MARKET-18): the VAPID public key, the 65-byte uncompressed P-256 point, base64url (`npx web-push generate-vapid-keys` prints a pair). Without it and the private key, Web Push is off and staff notices go by text/email as before. | web_push.py |
| `WEB_PUSH_VAPID_PRIVATE_KEY` | — | The VAPID private key, the 32-byte P-256 scalar, base64url. A pair that doesn't match is treated as unset. | web_push.py |
| `WEB_PUSH_SUBJECT` | `mailto:support@cavnar.ai` | The VAPID contact the push services see. | web_push.py |

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
| `TOAST_SCHEDULE_WRITE` | `""` (off) | 1 only once Toast has admitted Cavnar AI to its integration-partner programme and granted the labor write scope: the POS schedule push then writes shifts to Toast (`POST /labor/v1/shifts`). Off, the Toast push refuses and the preview says why (schedule audit 10/10/26 MARKET-7). | toast.py |
| `BACKOFFICE_API_BASE` | — | Back Office API host, once one exists (`docs/plans/BACK_OFFICE_INTEGRATION.md`). | backoffice.py |
| `POS_ARCHIVE_READS` | `"1"` | 1 (default): nightly readers read a closed day from the POS ticket archive instead of the POS API. | pos_archive.py |
| `POS_BACKFILL_MAX_STORES` | `"8"` | How many stores may backfill POS history at once (scale audit 10/19/26 ONB-1, `onboarding.backfill_admission`): a wave of new stores waits for a slot; a waiting store still archives last night. | onboarding.py |
| `POS_BACKFILL_DAYS_PER_NIGHT` | `"7"` | Days of history a backfilling store archives a night (ONB-5). 7 is 13 nights to 90 days; raise it for a wave once RPOWER agrees the fleet request budget. | pos_archive.py |
| `POS_BACKFILL_BYTES_PER_STORE_DAY` | `"650000"` | What one archived store-day is reckoned to cost on disk (the audit measured ~613 KB with indexes): the free-space gate's estimate of tonight's backfill. | onboarding.py |
| `PG_VOLUME_MAX_BYTES` | `"0"` | The Postgres volume's size in bytes, when set (the app cannot see another service's disk): a backfill waits once the database plus tonight's days would pass 80% of it. 0 = only the app volume is checked. | onboarding.py |
| `ONBOARDING_STUCK_DAYS` | `"14"` | The fleet onboarding board names a store not live this many days after signing up. | onboarding.py |

## Billing and contracts

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `PARTNER_BILLING` | `""` (off) | `1` turns the partner / reseller entity's billing on (scale audit 10/19/26 ONB-10): a store under a partner with `billing_mode` partner and a signed master agreement needs no DocuSign envelope or checkout of its own, and its tracker's contract and billing steps read done. Off, a partner can be recorded and stores attached and nothing about a store's contract or billing changes. A business decision before it is turned on (the report lists it). | onboarding.py |
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

## The job queue, the model gateway and Postgres (scheduler audit 10/10/26, H2)

Defaults change nothing in production: the queue is consumed in the web process, the gateway allocates nothing until an organisation budget is set, SQLite stays the database, blobs stay inline and the shadow is off. `docs/ops/POSTGRES_CUTOVER.md` and `docs/ops/RAILWAY_SCHEDULER_SPLIT.md` say when to set each.

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `JOB_QUEUE` | — | Retired (blind re-audit 10/11/26 ARCH-10): the queue is the one path; the in-process generation pool and the inline learners it switched back to are gone. Ignored if set. | — |
| `QUEUE_WORKER` | `"1"` | Unset or `1` (the default since blind re-audit 10/11/26 SCALE-17): the queue worker process (`worker.py --queue`, started beside gunicorn by `gunicorn.conf.py`) consumes the queue and the web process only enqueues — unless no worker has beaten its heartbeat for `QUEUE_WORKER_STALE_SECONDS`, when it consumes after all. `0`: the web process consumes its own queue and no worker is started (the rollback). | job_queue.py, worker.py, gunicorn.conf.py |
| `QUEUE_WORKER_PROCESS` | `"1"` | `0`: gunicorn's master starts no queue worker even in worker mode (a `worker.py` run some other way — the documented `sh -c` command — is the consumer). | job_queue.py, gunicorn.conf.py |
| `QUEUE_WORKER_MAX_JOBS` | `"20"` | The queue worker process recycles (stops claiming, tells the master — which starts its replacement then — finishes its rows and exits) after this many GENERATION rows — max-jobs-per-child, so a large generation's memory is returned. Bulk and POS rows do not count since the scale audit (10/19/26 A5-F3: a night of learners recycled it every 20 rows). `0`: never. | worker.py |
| `QUEUE_WORKER_BOOT_SINCE`, `QUEUE_WORKER_HANDOFF_FD` | set by gunicorn's master | The container's start (a worker waits for a web boot since then, not since its own start — INF-4) and the pipe a recycling worker says "draining" on (A5-F3). Never set them by hand. | gunicorn.conf.py, worker.py |
| `QUEUE_WORKER_MAX_RSS_MB` | `"1500"` | …or once its resident memory passes this. `0`: never. | worker.py |
| `QUEUE_GENERATION_MAX_RUNNING`, `QUEUE_BULK_MAX_RUNNING` | 3× / 2× the group's CPU slots | Rows of the group in flight at once across every process — CPU work and model waits together (a row waiting on a model answer gives its CPU slot back: `job_queue.model_wait`); also a consumer's thread count for the group. Under `GATEWAY_ORG_OTPM`, never more streams than it carries. | job_queue.py |
| `QUEUE_INTERACTIVE_RESERVED`, `QUEUE_SCHEDULED_RESERVED` | `"1"`, `"1"` | CPU slots of the generation group kept for owners' presses (always) and for the auto-draft (while drafts wait); each at most the slots less one. The drafts' slot is kept from a press only while presses still have 2 slots (`job_queue.PRESS_MIN_SLOTS`), or for a draft within `QUEUE_SCHEDULED_URGENT_SECONDS` of its deadline (scale audit 10/19/26 A5-F4: on two slots it left an owners' burst one slot all morning). | job_queue.py |
| `QUEUE_SCHEDULED_URGENT_SECONDS` | `"3600"` | How close to its deadline a waiting auto-draft holds the drafts' reserved slot against presses when presses would otherwise keep fewer than 2 slots. | job_queue.py |
| `AUTO_DRAFT_SPREAD_MINUTES` | `"120"` | The least spread: a restaurant's auto-draft row starts at its fixed place (a hash of its id) within the day's spread after the pass that queued it, so a timezone's drafts do not all become claimable at 6am. A day with more drafts than the generation slots clear in this time spreads over the time they take (drafts x measured seconds a generation / slots — `strategy_jobs.auto_draft_spread_seconds`), up to `AUTO_DRAFT_SPREAD_MAX_MINUTES`. | strategy_jobs.py |
| `AUTO_DRAFT_SPREAD_MAX_MINUTES` | `"360"` | The widest the day's auto-draft spread grows (scale audit 10/19/26 A5-F4). | strategy_jobs.py |
| `QUEUE_WORKER_STALE_SECONDS` | `"90"` | A worker silent this long is gone: the web process consumes again. | job_queue.py |
| `QUEUE_GENERATION_CONCURRENCY` | the container's (`job_queue.default_generation_slots`: its cores over `SCHEDULE_CPSAT_WORKERS`, held to its memory limit less `SCHEDULE_GEN_MEMORY_RESERVE_MB` over `SCHEDULE_GEN_SLOT_MB`, and to `SCHEDULE_GEN_WORKERS` when set) | Generations doing CODE work at once across EVERY process (CPU slots, a count of claimed rows); `GATEWAY_ORG_OTPM` caps the rows STREAMING a model answer instead (`model_gateway._stream_slot_wait`, A5-F8 10/19/26 — it used to hold these CPU slots, throttling code-only generations); a generation waiting on the model holds none (`QUEUE_GENERATION_MAX_RUNNING` bounds those). Was a fixed 2 until the scale audit of 10/19/26 (A5-F2). Keep slots x `SCHEDULE_CPSAT_WORKERS` at most the cores (a solve past them ends on the wall clock — the worker logs a warning and the platform check alerts on the rate) and slots x ~1.4 GB inside the container's memory (A5-F6/F7). | job_queue.py |
| `SCHEDULE_GEN_SLOT_MB` | `1400` | The memory one generation holds at its peak (parent growth plus its CP-SAT solve process, measured 1.0-1.4 GB at 60-250 people): the default generation slots are the container's memory limit (cgroup `memory.max`) less `SCHEDULE_GEN_MEMORY_RESERVE_MB`, over this. No limit read: the cores alone. | job_queue.py |
| `SCHEDULE_GEN_MEMORY_RESERVE_MB` | `1536` | What the container holds before any generation (the web process, the queue worker's baseline), kept out of the memory the generation slots are sized from. | job_queue.py |
| `QUEUE_BULK_CONCURRENCY` | `"2"` | Learner rows running at once across every process. | job_queue.py |
| `QUEUE_POS_IO_CONCURRENCY`, `QUEUE_POS_IO_MAX_RUNNING` | `"4"`, 2× | The `pos_io` group (the nightly POS archive rows — network-bound, scale audit 10/19/26 INF-6): rows doing work at once across every process, and in flight. RPOWER's own budget is its per-token bucket. | job_queue.py |
| `QUEUE_BULK_DEADLINE_HOURS` | `"22"` | A nightly queue row (archive, learner, food cost diagnosis) still unstarted this long after it became runnable expires instead of running, so a backlog never stacks under the next night's rows (INF-6). | strategy_jobs.py |
| `QUEUE_WORKER_ALERT_SECONDS` | `"300"` | The platform check warns when the queue worker the master supervises has not beaten its heartbeat for this long — the web process has the whole queue (INF-4). | ops.py |
| `QUEUE_BACKLOG_ALERT_HOURS` | `"6"` | …and when a bulk or POS row that may run has waited longer than this (INF-6). | ops.py |
| `BOUND_ALERT_RUNS` | `"3"` | …and when a job hit its time bound on each of its last this-many runs: it no longer covers every restaurant each period (INF-7). At least 2. | ops.py |
| `DSR_SWEEP_WORKERS` | `"4"` | Restaurants the DSR sweep runs at once, earliest deadline first (INF-1, A4-06). `1`: the serial sweep. | dsr/pipeline.py |
| `BRIEF_WORKERS` | `"4"` | Restaurants the morning brief pass briefs at once (INF-10). | morning_brief.py |
| `HTTP_5XX_ALERT_RATE`, `HTTP_5XX_ALERT_MIN_REQUESTS` | `"0.02"`, `"50"` | The platform check pages when customer requests' 5xx share over the last five minutes passes the rate, with at least that many requests (OPS-7). | platform_monitor.py |
| `HTTP_P95_SLO_MS`, `HTTP_P95_ALERT_MIN_REQUESTS` | `"5000"`, `"200"` | …and warns when customer requests' p95 over the last fifteen minutes is past the SLO, with at least that many requests. | platform_monitor.py |
| `QUEUE_LEASE_SECONDS` | `"120"` | A claimed row's lease, renewed every quarter of it; a row whose lease ran out (its process died) is claimed again and resumed. | job_queue.py |
| `QUEUE_POLL_SECONDS` | `"2"` | How often an idle worker looks for rows. | job_queue.py |
| `QUEUE_INTERACTIVE_BURST`, `QUEUE_INTERACTIVE_PER_HOUR` | `"6"`, `"6"` | Per-tenant token bucket for owners' presses: past it a tenant waits behind tenants that have tokens (never refused while nobody else waits). | job_queue.py |
| `QUEUE_SCHEDULED_BURST`, `QUEUE_SCHEDULED_PER_HOUR` | `"2"`, `"2"` | The same for auto-drafts. | job_queue.py |
| `QUEUE_BULK_BURST`, `QUEUE_BULK_PER_HOUR` | `"30"`, `"120"` | The same for learner rows. | job_queue.py |
| `GATEWAY_ORG_OTPM`, `GATEWAY_ORG_ITPM` | `"0"` (off) | The organisation's output / input tokens a minute the model gateway allocates by class, PER MODEL FAMILY (each family its own minute since 10/19/26, A4-02; `GATEWAY_FAMILY_LIMITS` names a family's own). Set from the Anthropic console's organisation limits. | model_gateway.py |
| `GATEWAY_SHARE_INTERACTIVE`, `GATEWAY_SHARE_SCHEDULED`, `GATEWAY_SHARE_BULK` | `"0.5"`, `"0.3"`, `"0.2"` | Each class's guaranteed share; unused share is borrowed, never the interactive reserve by the others. | model_gateway.py |
| `GATEWAY_TENANT_OTPM` | `"0"` (off) | One restaurant's output tokens a minute. | model_gateway.py |
| `GATEWAY_INTERACTIVE_WAIT_SECONDS`, `GATEWAY_SCHEDULED_WAIT_SECONDS`, `GATEWAY_BULK_WAIT_SECONDS` | `"10"`, `"120"`, `"60"` | How long a call over its allowance waits for the next minute: then an owner's or a generation's call goes anyway, a bulk call is refused (its queue row retries). | model_gateway.py |
| `GATEWAY_OUTPUT_RESERVE_CAP` | `"16000"` | The most output tokens a call reserves before its answer says. | model_gateway.py |
| `GATEWAY_GENERATION_STREAM_OTPM` | `"7200"` | Output tokens a minute one streaming generation spends, for the generation concurrency cap. | model_gateway.py |
| `AI_BREAKER_SYNC_SECONDS` | `"5"` | How often a process reads the shared provider breaker (`ai_breakers`). | ai_utils.py |
| `BAR_CACHE_TTL_SECONDS` | `"600"` | The Bar page's kept payload is rebuilt at least this often even when its data stamp is unchanged (a safety net; every Bar write and data change already rebuilds it). | bar/cache.py |
| `BAR_CACHE_ENTRIES` | `"16"` | How many restaurants' Bar payloads one process keeps (LRU). | bar/cache.py |
| `BAR_BUILD_CONCURRENCY` | `"2"` | How many cold Bar page builds one process runs at once; a request beyond it waits for one to finish (food & bar audit 10/16/26, SCALE-08). | bar/cache.py |
| `DATABASE_URL` | — | A `postgres://` URL makes Postgres the database behind `models.get_conn` (db_dialect). **Set only at the cutover** (POSTGRES_CUTOVER.md). | models.py, db_dialect.py |
| `DB_BACKEND` | — | `sqlite` pins SQLite even with `DATABASE_URL` set (the rollback). | models.py, db_dialect.py |
| `SHADOW_DATABASE_URL` | — | The Postgres the shadow writes the normalized schedule model to while SQLite is primary. | pg_shadow.py |
| `PG_SHADOW_WRITE` | — | `1` notes every schedule revision in `pg_shadow_outbox`; the `pg_shadow` job writes them to `SHADOW_DATABASE_URL`. | pg_shadow.py |
| `LOADTEST_STAGING` | unset | `1` lets `scripts/loadtest/seed_staging.py` and `nightly_sim.py` run — on a STAGING database with no provider keys only, never production (scale audit 10/19/26). | scripts/loadtest |
| `LOADTEST_USERS` | `scripts/loadtest/users.csv` | The logins file `scripts/loadtest/locustfile.py` reads (written by `seed_staging.py`; git-ignored). | scripts/loadtest |
| `PG_POOL_MAX` | `20` | On Postgres: connections a process may have checked out by threads holding none — the per-process cap (blind re-audit 10/11/26 SCALE-11). The web process plus `worker.py` stay under Railway's `max_connections` (100) at the defaults. | db_dialect.py |
| `PG_POOL_OVERFLOW` | `20` | More connections, only for a thread that already holds one (a nested `get_conn`; a wait there could deadlock). Process ceiling = `PG_POOL_MAX + PG_POOL_OVERFLOW`. | db_dialect.py |
| `PG_POOL_IDLE` | `5` | Idle Postgres connections a process keeps for reuse. | db_dialect.py |
| `PG_POOL_MAX_LIFETIME` | `1800` | Seconds a Postgres connection lives before it is retired at its next return. | db_dialect.py |
| `PG_POOL_IDLE_SECONDS` | `300` | Seconds an idle Postgres connection is kept. | db_dialect.py |
| `PG_LOCK_TIMEOUT_MS` | `30000` | On Postgres: a lock wait gives up after this (SQLite's 30 s busy_timeout); `0` = never (scale audit 10/19/26 DB-06). | db_dialect.py |
| `PG_STATEMENT_TIMEOUT_MS` | `300000` | On Postgres: a statement is cancelled after this; `0` = never. | db_dialect.py |
| `PG_IDLE_TX_TIMEOUT_MS` | `300000` | On Postgres: a session idle inside a transaction is ended after this; `0` = never. | db_dialect.py |
| `PG_PREPARE_THRESHOLD` | unset | `off` never prepares statements server-side — set it behind PgBouncer in transaction mode older than 1.21 (DB-07). | db_dialect.py |
| `PG_POOL_TIMEOUT` | `30` | Seconds a checkout waits for a free connection before it fails, said ("database connection pool exhausted"). | db_dialect.py |
| `PG_CONNECT_TIMEOUT_S` | `5` | Seconds a new Postgres connection waits for the server before it fails (min 2; it was a fixed 10 — DB-22). | db_dialect.py |
| `PG_CONNECT_BACKOFF_S` | `3` | After a failed connect, checkouts that need a new connection fail at once ("database unreachable … retrying within") for this long instead of each waiting the connect timeout — a restarting database (10/19/26 PITR redeploy, DB-22). | db_dialect.py |
| `PG_POOL_PING_AFTER_S` | `30` | An idle pooled connection is always checked (closed, broken, socket status) before reuse; one idle longer than this is also asked `SELECT 1` (DB-22). | db_dialect.py |
| `CAVNAR_TEST_PG_URL` | — | Tests only: a SCRATCH Postgres server (never production's). The suite's database runs there — every test's database its own, from a template (tests/conftest.py); `tests/test_q2_postgres.py`'s live half runs. CI's `postgres` job sets it. | tests/conftest.py, tests/test_q2_postgres.py |
| `QUEUE_WAIT_ALERT_SECONDS` | `"120"` | The platform check warns when owners' generations wait longer than this to start (the queue's interactive p95 over the last hour, five samples at least). | ops.py |
| `MAINTENANCE_READ_ONLY` | — | `1`: the cutover's write freeze — every write request answers 503 with a sentence, the scheduler and its sends stop (`scheduler.scheduling_allowed`); reads and `/health` still answer. | http_layer.py, scheduler.py |
| `TARGET_DATABASE_URL` | — | `scripts/run_pg_copy.py`'s target when `--database-url` is not given. | scripts/run_pg_copy.py |
| `PG_COPY_BATCH_ROWS` | `"2000"` | Rows a batch in the copy and the export. | pg_copy.py |
| `BLOB_STORE` | `"db"` | Where model-call and quality blobs live: `db` (inline, as before), `s3` (the `BLOB_S3_*` bucket), `local` (`BLOB_STORE_DIR`; development only — the SQLite backup does not carry it). | blob_store.py |
| `BLOB_S3_ENDPOINT`, `BLOB_S3_BUCKET`, `BLOB_S3_ACCESS_KEY_ID`, `BLOB_S3_SECRET_ACCESS_KEY`, `BLOB_S3_REGION`, `BLOB_S3_PREFIX` | —, —, —, —, `"auto"`, `"cavnar-blobs/"` | The blob bucket (https only; never the backup bucket, whose lifecycle rule expires objects). Give `model-calls/` a lifecycle rule of ~100 days. | blob_store.py |
| `BLOB_STORE_DIR` | `<volume>/blobs` | `BLOB_STORE=local`'s folder. | blob_store.py |
| `BACKUP_MULTIPART_THRESHOLD`, `BACKUP_MULTIPART_PART_BYTES` | 512 MiB, 256 MiB | Past the threshold the off-site copy goes up in parts (SCALE-20: the single PUT refused anything over 5 GiB). | offsite_backup.py |
| `RETAIN_JOBS_DAYS`, `RETAIN_GENERATION_WATCHERS_DAYS`, `RETAIN_PG_SHADOW_OUTBOX_DAYS` | `"14"`, `"7"`, `"30"` | Retention of the queue's rows, the generation watchers and the shadow outbox. | ops.py |

## Model behaviour

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `RESPONSE_VALIDATION_MODE` | — | JSON `{surface: "shadow"\|"enforce", "*": …}`; default enforce everywhere. A surface in shadow logs the verdict but shows the original text (`response_validation.mode_for`). | response_validation.py |
| `PROVIDER_PROBES_SKIP` | — | Comma-separated provider names whose health probe is skipped (`provider_health._skipped`). | provider_health.py |
| `DEMO_PASSWORD` | — | Password for the seeded demo accounts (`demo_seed.py`). | demo_seed.py |
| `AI_BATCHES_ENABLED` | `"1"` | The Message Batches kill switch (`ai_batches.enabled`, AI cost audit 10/7/26 #19): `0` sends every batchable call synchronously at list price. Batches only ever go where `scheduler.scheduling_allowed()` — never from a local backend, whatever this says. | ai_batches.py |
| `AI_BATCH_CALLBACK_WORKERS` | `"2"` | Threads that run the batch collector's callbacks (`ai_batches`, platform re-audit 10/7/26 #1): a callback may call a model synchronously (a fallback past its cutoff, the DSR's one-tier escalation), so it never runs on the scheduler loop thread; the collector waits at most `CALLBACK_INLINE_WAIT_SECONDS` (20) for the ones it handed over and stops claiming past `CALLBACK_MAX_PENDING` (20). Daemon threads: a callback a deploy cuts short is told again by the stale sweep. Per process. | ai_batches.py |
| `AI_TIER_T1_MODEL` | — (Claude Haiku 5.5, `claude-haiku-5-5`) | The model behind tier T1 for every workflow whose ladder names it (`ai_workflows`, AI orchestration design 10/7/26). Unset: Claude Haiku 5.5 ($0.10/$0.50 — scheduler audit 10/10/26 AI-27), held to the canary restaurants while `AI_TIER_CANARY` names T1. Set: that model for every restaurant, never canaried (the operator's choice; `claude-haiku-4-5-20251001` rolls back). | ai_workflows.py |
| `AI_TIER_CANARY` | `"T1"` | The tiers whose new model reaches only `AI_CANARY_RESTAURANTS` (`ai_workflows.tier_canaried`, AI-27): a run for any other restaurant — or a call built for no one restaurant, like a batch — keeps the tier's previous model. Empty once the learner's numbers on the canary look right: every restaurant moves. | ai_workflows.py |
| `AI_TIER_T1_PREVIOUS_MODEL` | `claude-haiku-4-5-20251001` | What T1 runs on outside the canary while `AI_TIER_CANARY` names T1. | ai_workflows.py |
| `OWNER_RULES_READ` | `"1"` | `0` stops the model reading the owner's words into typed rules (`owner_rules.read`, scheduler audit 10/10/26 RM-9): the plain reader and the readings already cached still hold. The test suite runs with `0`. | owner_rules.py |
| `SCHEDULE_RULES_MODEL` | `claude-haiku-5-5` | The model that reads each sentence of the owner's notes, a draft's instruction and staff notes into a typed rule (`owner_rules`, scheduler audit 10/10/26 RM-9). Structured output, cached by sentence; code validates every rule it proposes. | ai_utils.py |
| `SCHEDULE_NOTE_WRITE` | `"1"` | `0` stops the model writing the note from the finished week (`schedule_narrative`, TRUST-15); the generation's own bullets, checked against the final rows, stand. The test suite runs with `0`. | schedule_narrative.py |
| `STAFFING_ELASTICITY_APPLY` | `"0"` | `1` lets the measured value of one more person (staffing_elasticity) move a shift requirement and price the solver's objective. OFF by default since the blind re-audit (10/11/26). The estimator it was held for is fixed (G1, 10/12/26): it decides on the uncovered-no-show natural experiment only, under one Holm family per restaurant run, a move binds after two agreeing weeks and only for 21 days, as a target level on the weekdays it was measured at (null simulation: +1 on 0% of cells in a 20-cell run, 40-60% before). Turning it on is the lead's decision. The weekly job measures and stores either way. | staffing_elasticity.py |
| `SCHEDULE_CALIBRATION_AUTOPROMOTE` | `"0"` | `1` lets the weekly calibration write new Shift Quality weights on its own (the owner can Undo). OFF by default since 10/11/26. Its hold-out is honest since G1 (10/12/26): fitted on older weeks, checked on held-out weeks (Williams' test), then on new weeks (null simulation: 1.3% promotable, 42% before). Keep it off until `schedule_calibration.fleet_readout()` says `recommend_on`. | schedule_calibration.py |
| `SCHEDULE_NOTE_MODEL` | `claude-haiku-5-5` | The model that writes "Cavnar AI's note" — three bullets from the FINISHED week, its changes and what it misses, each checked against the final rows (`schedule_narrative`, TRUST-15 / AI-21). | ai_utils.py |
| `AI_TIER_T2_MODEL` | `claude-sonnet-5` | Tier T2's model. | ai_workflows.py |
| `REVIEW_ANALYSIS_MODEL`, `CLAUDE_REPORTER_MODEL`, `REVIEW_INSIGHT_MODEL`, `DRAFTER_MODEL`, `INVENTORY_INSIGHT_MODEL`, `LABOR_INSIGHT_MODEL`, `CLAUDE_MODEL`, `MARKETING_MODEL`, `GUEST_MARKETING_MODEL`, `RECIPE_MODEL`, `INVOICE_MODEL`, `SALES_AUDIT_NOTES_MODEL`, `ASK_CAVNAR_MODEL`, `ASK_SUMMARY_MODEL`, `DSR_NARRATIVE_MODEL`, `TASK_SHEET_MODEL`, `STAFF_BRIEF_MODEL`, `STAFF_TRANSLATION_MODEL`, `STAFF_ANSWER_MODEL` | — | Each call site's own model (`ai_utils.MODELS`, read by `model_for`). On an orchestrated workflow, set and not blank, it pins the workflow: that model on the call site's own route, one rung, no escalation, no canary (`ai_workflows.pinned_model`, context re-audit 10/7/26 #11 — they were silently ignored once a call ran on a ladder); unset, the policy's ladder runs on the `AI_TIER_T*_MODEL` tiers. `CLAUDE_MODEL` and `CLAUDE_REPORTER_MODEL` are each read by several purposes, so one pins all of them. Railway sets none (10/7/26). `SCHEDULE_MODEL` is the schedule's own pin (below). | ai_utils.py |
| `AI_CANARY_RESTAURANTS` | `"4"` | Comma-separated restaurant ids that get a canaried workflow's cheap first rung (`ai_workflows.canary_start`, context re-audit 10/7/26 #3). A policy with `canary` set — `labor_insight`, `draft_response`, `staff_answer`, `task_sheet_starter`, each Haiku-first where the call site ran Sonnet before the registry — starts on T1 only for these; every other restaurant starts one rung up (T2), as before. The default is Simple EJ's Demo (Will's test account). Empty: no canary restaurant; `*`: every restaurant (the canary over for every canaried workflow at once). The console turns `canary` off per workflow (an override) once the learner's numbers on the canary look right; `ai_runs.canary` records which a run was. | ai_workflows.py |
| `AI_TIER_T3_MODEL` | `claude-sonnet-5-5` | Tier T3's model (adaptive thinking). | ai_workflows.py |
| `AI_TIER_T3_EFFORT` | `medium` | Tier T3's effort. | ai_workflows.py |
| `AI_TIER_T4_MODEL` | `claude-opus-5-5` | Tier T4's model (adaptive thinking). | ai_workflows.py |
| `AI_TIER_T4_EFFORT` | `medium` | Tier T4's effort. | ai_workflows.py |
| `SCHEDULE_CPSAT_SECONDS` | `20` | The CP-SAT assignment's budget per solve (`schedule_cpsat`, scheduler audit 10/10/26 RM-11) — a deterministic work budget when `SCHEDULE_CPSAT_DETERMINISTIC` is on, with four times it as the wall-clock backstop. | schedule_cpsat.py |
| `SCHEDULE_CPSAT_WORKERS` | `4` | CP-SAT's search workers (its C++ search releases the GIL; Railway runs 1 gunicorn worker × 4 threads). | schedule_cpsat.py |
| `SCHEDULE_CPSAT_DETERMINISTIC` | `1` | `1`: a fixed seed, interleaved workers and a deterministic budget — same inputs, same week. `0`: the wall clock bounds the search (faster to a first answer, not reproducible). | schedule_cpsat.py |
| `SCHEDULE_CPSAT_DET_PER_KVAR` | `0.6` | Deterministic seconds per thousand model variables (`schedule_cpsat.work_budget`, blind re-audit 10/11/26 SCALE-19): a solve's budget is the stated one raised to this, never lower — a fixed 7.5 s Stage 1 found no week at 250 people. | schedule_cpsat.py |
| `SCHEDULE_CPSAT_DET_CAP` | `120` | The most a size-scaled budget is raised to (deterministic seconds). | schedule_cpsat.py |
| `SCHEDULE_CPSAT_LEAN_MIN_VARS` | `15000` | From this many variables a solve runs the memory-bounded portfolio (default_lp, max_lp, no_lp and the LNS workers — not nine full subsolvers): measured at 250 people 0.6 GB instead of 1.6-2.0 GB, and faster (STRESS-11). | schedule_cpsat.py |
| `SCHEDULE_CPSAT_ISOLATE` | `process` | `process`: a large solve runs in a solver process of its own (exec'd, never forked), so what CP-SAT allocated goes back to the OS when it returns (SCALE-18); `0`: the kill switch — every solve in this process. | schedule_cpsat.py |
| `SCHEDULE_CPSAT_ISOLATE_MIN_VARS` | `5000` | The smallest model solved in a child process. | schedule_cpsat.py |
| `SCHEDULE_CPSAT_MEMORY_MB` | `2048` | CP-SAT's memory bound per solve, over what the process already holds (`max_memory_in_mb`); a solve it ends is recorded (`memory_capped`) and counted by the platform check. | schedule_cpsat.py |
| `SCHEDULE_SOLVER_WORK_PER_SECOND` | `8000000` | The backtracking solver's work budget rate (`schedule_solver.WorkClock`, scheduler fix round 10/10/26 ARCH-26): search nodes, placements, scans and LNS rounds counted as work, this many to one budget second — the same draft gives the same week whatever the box's load. Calibrated on synthetic weeks (8–9M units/s of search on this hardware). | schedule_solver.py |
| `SCHEDULE_SOLVER_DETERMINISTIC` | `1` | `1`: the backtracking solver's budget is work (above), the wall clock only its emergency cap (`wall_capped` reported). `0`: the wall clock is its budget again (not reproducible under load). | schedule_solver.py |
| `SCHEDULE_ASSIGN_ENGINE` | `cpsat` | Who re-solves the assignment in the repair loop's solver stage: `cpsat` (`schedule_cpsat.improve`) or `backtracking` (`schedule_solver.improve`, also the fallback). | schedule_engine.py |
| `SCHEDULE_SOLVER_DRAFT_SECONDS` | `25` | The solver-first draft's budget inside a generation (shapes then people), held to the job's clock. | schedule_engine.py |
| `SCHEDULE_STATE_STRICT` | unset | `1` makes a generation result's undeclared key (a misspelling, or a key no stage writes) raise instead of being logged and counted (schedule_state, ARCH-5). The test suite sets it (tests/conftest.py); production leaves it unset. | schedule_state.py |
| `SCHEDULE_RESOLVE_SECONDS` | `6` | The Studio's and Ask's re-solve of chosen days (MARKET-13). | schedule_cpsat.py |
| `SCHEDULE_DRAFT_CRITIC` | — | `1` turns on the deterministic+critic review of a code-built week for every restaurant (`schedule_critic`, AI-8); else the `draft_v1` arm `code_critic`. | schedule_critic.py |
| `SCHEDULE_CRITIC_MODEL` | `claude-sonnet-5-5` | The model the critic runs on. | ai_utils.py |
| `SCHEDULE_DRAFT_SOURCE` | — | Where every restaurant's schedule draft comes from (`schedule_fallback.draft_source`, scheduler audit 10/10/26 contract #7): `model`, `carry_forward` (the last published week carried forward and repaired, no model call) or `solver` (the week shaped in code and solved, no model call). Unset: the week's `draft_v1` arm (a restaurant's pin there is its own override), else the solver — production's default since the blind re-audit of 10/11/26 (ARCH-1); `model` is the way back to the model's draft. A code source never calls the drafting model on any pass, the quality gate's and a redo's included (BUGSGEN-9). On `model`, a day the model could not write is drafted in code, the solver first (AI-11, BUGSGEN-21). | schedule_fallback.py |
| `SCHEDULE_CODE_JOB_SECONDS` | `600` | The generation's deadline when its draft is made in code (no model call planned — ARCH-18), instead of the model's 15-40 minutes; a deadline a model call was planned against is never shortened. | schedule_engine.py |
| `SCHEDULE_MANAGERS_IN_SOLVE` | `1` | The solver-first draft chooses the managers inside its solve from the manager plan's candidates, the owner's word (standing shifts, locked rows, standing positions) held (ARCH-23). `0` pins the plan's rows as before — the rollback. | schedule_engine.py |
| `SCHEDULE_ALLOW_TOP_TIER` | — | `1` lets a schedule route name Claude Fable or Mythos (`schedule_engine.SCHEDULE_BARRED_MODELS`, scheduler audit 10/10/26 AI-30); unset, a pin or tier override naming one runs on Opus 5.5 instead and the run's steps say so. Set only once `schedule_eval` shows a measured ceiling below it. | schedule_engine.py |
| `SCHEDULE_MODEL` | — | Pins the schedule generator to one model (`ai_utils.MODELS["schedule"]`) at `labor.SCHEDULE_EFFORT`, skipping the `labor_schedule` ladder (T3 Sonnet 5.5 first, T4 Opus 5.5 for a hard week or the quality gate's rewrite — owner decision 3, 10/7/26) and its pre-router (`schedule_engine.schedule_route`). Unset: the ladder. A model before the 5.5 generation runs the old thinking-off shape. | schedule_engine.py |
| `SCHEDULE_CONTRACT` | `schema` | The schedule generator's output contract, read once per generation (`schedule_output.schedule_contract`; AI cost audit 10/7/26 #69, #70): `schema` — rows `{employee, role, start, end, note}`; `compact` — the same rows with one-letter keys `{e, r, s, t, n}` (~24 answer tokens a row to ~30); `shape` — slots `{r, s, t, c, n}` without names, whose people `schedule_engine.assign_shape_slots` solves over the same rules before every repair and gate step. Anything else reads as `schema`. Stays `schema` until `scripts/schedule_model_eval.py --prompts rerender,rerender:compact,rerender:shape` shows no quality loss on stored weeks. | schedule_output.py |
| `AI_REPLAY_SAMPLE_RATE` | `0.05` | Share of workflow runs whose model requests are kept (redacted, compressed, `ai_run_requests`) so the learner can replay a cheaper route on real inputs. `0` keeps none. | ai_orchestrator.py |
| `AI_SHADOW_REVIEW` | `"1"` | `0` stops the shadow reviewer (a Haiku rubric scoring a sample of passing runs off the request path). Gates are unaffected. | ai_orchestrator.py |
| `AI_BATCHES_WORKFLOWS` | `"dsr_narrative,shadow_arms,review_diagnosis,food_cost_diagnosis,competitor_insight,recipe_draft,weekly_digest,quiet_night_post,labor_schedule"` | Comma-separated workflows allowed to batch (half price, answered within 24 hours); a workflow not named here is written synchronously. `shadow_arms` is the learner's weekly replays — they never run synchronously, so leaving it out stops them. `review_diagnosis` and `food_cost_diagnosis` are the morning root-cause reads (#58; left out, the 6am pass calls for every one synchronously at full price). The AI cost audit (10/7/26) added the weekly competitor read (#59, cutoff `competitor.BATCH_CUTOFF_HOURS` 3), the Tuesday recipe drafts (#60, 6 hours), the digest's narrative sent from 2am local on the digest day (#61, cut off at the 9am send) and the quiet-night post (#62, 6 hours): each falls back to a synchronous call when its answer is not back by its cutoff (`ai_batches` cutoff sweep). A value set here replaces the default: name every one to keep it. A workflow listed here batches only while its `ai_workflows` policy's `batch` flag is on too — the console's "May batch" (re-audit 10/7/26 #10; `quiet_night_post` reads `marketing_content`'s). | ai_batches.py |
| `AI_WORKFLOW_OFF` | unset | Comma-separated `ai_workflows` workflows that make no model call — a per-workflow kill switch (re-audit 10/7/26 #10). `ai_orchestrator.generate` records the run `off` and raises an `ai_utils.AIRefused` (`WorkflowOffRefused`), so the caller serves its own fallback as on a refusal. Read at every run; a restart with it changed is all it takes. | ai_orchestrator.py |
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

Changed or added by the scale audit fix round (10/19/26, F5 — AI infrastructure, models and costs). Every one is config; the defaults keep today's restaurants' spend where it was except where noted:

- `AI_PLACES_GLOBAL_MAX_MONTHLY_USD` — **0**, none (was 3000) (A4-04): the Places fleet ceiling had the absolute maximum the AI pool lost in SCALE-11 and met normal use at ~300-860 restaurants. The pool now grows by `AI_PLACES_GLOBAL_PER_CLIENT_USD` for every paying AND non-demo trial restaurant (trials' Places spend counts in it); the per-restaurant $3 day / $30 month and the 80% page are the runaway bounds. A maximum set above 0 still binds.
- `AI_TRIAL_POOL_PER_TRIAL_DAILY_USD` (**2.5**), `AI_TRIAL_POOL_PER_TRIAL_MONTHLY_USD` (**25**), `AI_TRIAL_POOL_MAX_DAILY_USD` (**500**), `AI_TRIAL_POOL_MAX_MONTHLY_USD` (**5000**) (A4-05): the trial pool is the larger of its floor (`AI_TRIAL_POOL_DAILY_USD` $50 / `AI_TRIAL_POOL_MONTHLY_USD` $500) and the per-trial allowance times the non-demo trials, at most the maximum (`ai_utils.trial_pool_budgets`). One fixed $500 bound at ~33 trials; the maximum binds near ~330 trials at expected use — raise it, or provision a rollout as `active`, before then. 0 for a maximum: none.
- `GATEWAY_FAMILY_LIMITS` — unset (A4-02): JSON `{"<model id>": {"itpm": n, "otpm": n}}` — one model family's own tokens a minute in the gateway (the provider meters each model apart). Every family not named gets `GATEWAY_ORG_OTPM` / `GATEWAY_ORG_ITPM`. Setting any limit turns the allocator on.
- `AI_RETRY_AFTER_MAX_SECONDS` (**30**), `AI_RETRY_AFTER_INTERACTIVE_MAX_SECONDS` (**5**) (A4-02): a retried call waits the provider's `retry-after` plus jitter (else full jitter over the exponential step); past these an unattended / an owner's call is not retried. A call refused by the provider's rate limit after its retries is never a breaker failure (it records a `rate_limited` health event once a minute).
- `INTERACTIVE_AI_PER_TENANT_MAX` — **0**, none (SEC-06): a hard ceiling on the interactive slots one restaurant holds. With none, the slots are still fair (`ai_utils.FairSlots`): a lone restaurant may use every slot, and under contention a freed slot goes to the waiting restaurant holding the fewest, then the one served longest ago.
- `ASK_MAX_PER_TENANT` — **1** (SEC-06): of the `ASK_MAX_CONCURRENT` streamed Ask turns, how many one restaurant may hold (0: no ceiling). A polled turn runs on the lane below instead.
- `AI_LANE_ENABLED` (**1**), `AI_LANE_WORKERS` (**6**), `AI_LANE_PER_TENANT` (**2**), `AI_LANE_QUEUE_MAX` (**32**), `AI_LANE_QUEUE_WAIT_SECONDS` (**20**) (A4-01): the interactive lane (`ai_lane`) — an Ask turn the client polls runs here, off the request threads, at most two per restaurant at once and fair between restaurants; a turn left waiting past the wait is answered "busy". Per process, in memory (the CLAUDE.md caveat on gunicorn `--workers`). `0` for `AI_LANE_ENABLED` streams every turn as before.
- `AI_BATCH_CANCEL_GRACE_SECONDS` — **600** (A4-07): a batch cancelled because every item still out in it was past its cutoff is given this long to end (its answered items are then used, the cancelled ones fall back) before its items fall back one by one anyway. 0 switches the cancel-first path off (every late item falls back at once, as before).
- `AI_CANARY_PERCENT` — **0** (A4-10): a stable share of ALL restaurants (by a hash of the id) in the T1 canary beside `AI_CANARY_RESTAURANTS`. No eval in the repo yet backs a cheaper first rung for real restaurants, so the default adds none; set e.g. 10 to let the learner judge Haiku on real data.
- `INVOICE_MODEL` — default **`claude-opus-5-5`** (was `claude-opus-5`) (A4-11): the same Opus tier a fifth cheaper; read when a scan runs. `claude-opus-5` rolls back.
- `SCHEDULE_T3_RUN_CAP_USD` — **1.5** (A5-F9): the dollars a model schedule run may spend while on T3 (Sonnet 5.5, $0.29-0.41 a measured week); past it the code fallback writes the week. Once the gate escalates to T4 the policy's $6 is the cap. 0: the policy's cap throughout. (The model draft is reached only by a pin or `SCHEDULE_DRAFT_SOURCE=model`.)
- `LABOR_PREWARM_WORKERS` — **4** (was a fixed 2), `COMPETITOR_DAILY_WORKERS` — **3** (was a fixed 1) (A4-09): restaurants the 3am Labor pre-warm and the morning competitor rating check work on at once — ~480 and ~460 restaurants a pass before. Concurrency only; spend is unchanged.
- `COMPETITOR_CHECK_EVERY_DAYS` — **1** (A4-13): days between a restaurant's competitor rating checks (`competitor.check_ratings`); a full set is ~$0.33 a check, ~$8.6 a restaurant a month daily. 3 is about twice a week beside the Monday read. `COMPETITOR_DAILY_CHECK_MAX` — **15**: competitors re-read per check.
- `AI_VISIBILITY_SPREAD_DAYS` — **1** (A4-15): spread the weekly AI-visibility checks over this many days from Monday by restaurant id (one Perplexity rate limit, one worker). 1: everyone from Monday, as before. The console's `/admin/api/ai/capacity` shows calls refused by each provider's rate limit.
- `AI_TRACE_TEXT_MAX_MB` — **512** (A4-14): the most kept trace text (`ai_calls.prompt_z` / `output_z`) may take; past it the nightly prune drops the oldest text first. Today's few MB never meet it. 0: no budget.

Changed or added by the scheduler audit (10/10/26, SCALE-11 — the fleet-wide AI stop):

- `AI_GLOBAL_PER_CLIENT_USD` — **150** (was 30) and `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` — **0**, none (was 10,000): the shared pool is now an EMERGENCY ceiling that grows by each paying client's own monthly ceiling (`AI_MONTHLY_BUDGET_USD`), so it binds only on spend no tenant ceiling bounds (the platform's own and admin-run calls) or every tenant at its ceiling at once. The absolute $10,000 refused every client's AI mid-month at ~400-650 paying restaurants. Set `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` above 0 only on purpose.
- `AI_EMERGENCY_STOP` — unset: `1` refuses every model call for every client (create_with_retry and the batch gate) until it is cleared — the manually armed stop. Never set by the code.
- `AI_RUNAWAY_FACTOR` (**6**), `AI_RUNAWAY_MIN_USD` (**5**), `AI_RUNAWAY_RUNS` (**4**), `AI_RUNAWAY_BASELINE_DAYS` (**7**), `AI_RUNAWAY_FLEET_MIN_USD` (**50**): the spend-rate runaway detector (`ai_utils.runaway_refusal`, `check_fleet_rate`). One restaurant's workflow whose last hour cost at least the larger of $5, four of the workflow's own run caps (`ai_workflows` caps) and six times its own hourly rate over the last seven days is paused — that workflow, for that restaurant — until its last hour is back under the line, with one health event and one page an hour. The fleet's last hour past six times its own rate (and $50) pages, naming the biggest spenders; it stops nothing. `AI_RUNAWAY_FACTOR=0` turns the detector off.

Changed or added by the AI cost audit (10/7/26):

- `AI_GLOBAL_PER_CLIENT_USD` — **30** (was 200; **150** since 10/10/26, above): the shared AI pool is the larger of `AI_GLOBAL_MONTHLY_BUDGET_USD` ($1,500) and $30 a paying client, capped at `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` ($10,000). A full-tier restaurant spends $15–25 a month, so $200 each let the pool reach ~8x real use before it noticed a runaway; at $30 the floor covers the first fifty clients and the pool grows with real use after that.
- `AI_PLACES_GLOBAL_MONTHLY_USD` — **300**: the floor of the platform's Google Places ceiling — every restaurant and every unattributed request, a month. The per-restaurant ceilings (`AI_PLACES_DAILY_BUDGET_USD` $3, `AI_PLACES_MONTHLY_BUDGET_USD` $30) bound one restaurant and nothing bounded the fleet. Since the platform re-audit (10/7/26 #9) the ceiling grows like the AI pool: the larger of this floor and `AI_PLACES_GLOBAL_PER_CLIENT_USD` (**15**) a paying client (active, past due, internal), never above `AI_PLACES_GLOBAL_MAX_MONTHLY_USD` (**3000**) — a fixed $300 met normal use at ~30-60 restaurants and then refused every Places call but the review fetch, platform-wide (`ai_utils.places_global_monthly_budget`). About three times expected spend (a few dollars a restaurant a month); 80% pages Will; the review fetch is still never refused; 0 disables it.
- `INTERACTIVE_AI_SLOTS` — **2**: model calls made on a request thread at once, process-wide (`ai_utils`, the interactive guard). gunicorn has four request threads; two always stay free for logins, pages, webhooks and `/health`. An Ask turn holds one for the whole turn. `INTERACTIVE_AI_WAIT_SECONDS` (**3**) is how long a call waits for one before the owner is told "busy, try again in a moment"; `INTERACTIVE_AI_TIMEOUT` (**40**) is the per-try timeout such a call gets (with one retry) when its caller names none — the invoice read (120s) and the recipe-card read (90s) name their own (platform re-audit 10/7/26 #4). A workflow run (`ai_orchestrator.generate`) holds one slot for its attempts and its reviewer gate (#6). Scheduler and background calls take no slot — a background pool (the shadow reviews, the learner's scoring) runs through `ai_utils.background_runner`, never "on a request" (#3). Per process, like `ASK_MAX_CONCURRENT`.
- `DIGEST_WORKERS` (**2**) and `DIGEST_MAX_SECONDS` (**1200**): the weekly digest pass's pool and wall-clock bound (cursor `weekly_digest_cursor`); a restaurant not reached gives its day back to the next hourly pass.
- `OWNER_AI_JOB_WORKERS` — **3**: the owner AI job pool (`ai_async`, #57) — an invoice read, a recipe-card read, the Campaign Studio's three drafts and the owner's live AI-visibility check run there as jobs the client polls, off the four request threads. Per process, like the other pools (CLAUDE.md, gunicorn `--workers`).

Added by the scale fix round (10/19/26, FE-01 — `home_snapshots.py`):

- `HOME_SNAPSHOTS` — **1**: Home's four slow reads (the morning brief, good news, the monthly review, the cross-module links; web and phone) are kept per restaurant, business day and viewer scope in `home_snapshots` and served from the row. `0` builds every read on the request, as before.
- `HOME_SNAPSHOT_REFRESH_SECONDS` — **900**: a kept answer older than this is rebuilt off the request on its next read even when no stamped input moved (a moved input rebuilds it at once). The stale answer is served meanwhile.
- `HOME_SNAPSHOT_WORKERS` (**2**) and `HOME_SNAPSHOT_MAX_PENDING` (**200**): the background rebuild pool, and how many rebuilds may wait for it before a stale read is served without starting one. Per process, like the other pools (CLAUDE.md, gunicorn `--workers`).
- `ACTIVITY_TTL_SECONDS` — **300** (was a fixed 45): how long one restaurant's activity feed (`activity.feed`, `/api/activity`) is kept per process. The web polls it every 2 minutes (was 1), so a viewer reads a kept build on most ticks (FE-05).
- `SILENT_PUSH_JITTER_SECONDS` — **600**: the window a restaurant's `dsr` widget wake (`push.fire_silent`) is spread over — each restaurant at its own fixed offset (a hash of its id), sent by the job queue (kind `silent_push`) instead of every owner phone at once when held reports are released (FE-12). `0` sends at once, as before.

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
| `AI_EMERGENCY_STOP` | `""` | ai_utils.py |
| `AI_GLOBAL_MAX_MONTHLY_BUDGET_USD` | `"0"` | ai_utils.py — none unless set (10/10/26, SCALE-11; was 10000) |
| `AI_GLOBAL_MONTHLY_BUDGET_USD` | `"1500"` | ai_utils.py |
| `AI_GLOBAL_PER_CLIENT_USD` | `"150"` | ai_utils.py (10/10/26, SCALE-11; was 30) |
| `AI_HEALTH_RETAIN_DAYS` | `"365"` | ai_utils.py |
| `AI_MEMORY_SIZES_RETAIN_DAYS` | `"400"` | ai_utils.py |
| `AI_MONTHLY_BUDGET_USD` | `"150"` | ai_utils.py |
| `AI_PAGE_COOLDOWN_MINUTES` | `"60"` | ai_utils.py |
| `AI_PLACES_DAILY_BUDGET_USD` | `"3"` | ai_utils.py |
| `AI_PLACES_MONTHLY_BUDGET_USD` | `"30"` | ai_utils.py |
| `AI_PLACES_GLOBAL_MONTHLY_USD` | `"300"` | ai_utils.py |
| `AI_PLACES_GLOBAL_PER_CLIENT_USD` | `"15"` | ai_utils.py |
| `AI_PLACES_GLOBAL_MAX_MONTHLY_USD` | `"0"` (none; was 3000 until 10/19/26) | ai_utils.py |
| `AI_QUALITY_RETAIN_DAYS` | `"180"` | ai_utils.py |
| `AI_RUNAWAY_BASELINE_DAYS` | `"7"` | ai_utils.py |
| `AI_RUNAWAY_FACTOR` | `"6"` | ai_utils.py |
| `AI_RUNAWAY_FLEET_MIN_USD` | `"50"` | ai_utils.py |
| `AI_RUNAWAY_MIN_USD` | `"5"` | ai_utils.py |
| `AI_RUNAWAY_RUNS` | `"4"` | ai_utils.py |
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
| `AI_TRIAL_SCHEDULE_DAILY_USD` | `"0"` (then the runs floor below) | ai_utils.py — a trial's own daily allowance for the schedule's calls (`SCHEDULE_ACTIONS`), apart from its $5 day (re-audit 10/11/26 AI-20) |
| `AI_TRIAL_SCHEDULE_RUNS` | `"2"` | ai_utils.py — the trial schedule allowance is never below this many `labor_schedule` run caps ($6 each); 0: the schedule shares the trial's day |
| `AI_BUDGET_FORECAST_MIN_DAYS` | `"3"` | ai_utils.py — days into the month before its spend is projected; the platform pool pages once a month when the projection passes its ceiling (AI-19) |
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
| `AUTO_PUBLISH_MAX_SECONDS` | `str(15 * 60)` (900) | scheduler.py — the auto-publish pass's wall-clock bound (scheduler audit 10/10/26, ARCH-23) |
| `AUTO_PUBLISH_WORKERS` | `"2"` | scheduler.py |
| `BACKUP_EMAIL_MAX_BYTES` | `str(25 * 1024 * 1024` | scheduler.py |
| `BACKUP_EMAIL_MODE` | `"always"` | scheduler.py |
| `BACKUP_OFFSITE_MAX_DAYS` | `"35"` | scheduler.py |
| `BRIEF_INPUT_WAIT_UNTIL_HOUR` | `"8"` | scheduler.py — the Chicago hour until which the morning briefs (and the weekly digest) wait for the 5-6am sweeps they read to settle; from it they go on what is there (AI cost audit 10/7/26 #54) |
| `CAMPAIGN_ATTRIBUTION_SECONDS` | `str(10 * 60` | guest_marketing.py |
| `CLAIM_RECLAIM_MINUTES` | `"120"` | ops.py |
| `COMPETITOR_WORKERS` | `"3"` | scheduler.py — restaurants the weekly competitor analysis reads at once (AI cost audit 10/7/26 #55; was 1) |
| `DAILY_ALERT_PASS_SECONDS` | `"600"` | notify.py |
| `DIAGNOSES_BATCH_UNTIL` | `"5:30"` | scheduler.py — Chicago time after which the diagnoses' Message Batches are not sent (no time to land before the 6am pass; AI cost audit 10/7/26 #58) |
| `DIAGNOSES_FALLBACK_LATEST` | `"6:45"` | scheduler.py — Chicago time at which the 6am diagnoses passes stop waiting for their own batch: from 6am each runs once nothing of its batch is still out, at the latest at this time (re-audit P5; the briefs wait for both, until `BRIEF_INPUT_WAIT_UNTIL_HOUR`) |
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
| `ACTIVITY_TTL_SECONDS` | `"300"` | activity.py |
| `SILENT_PUSH_JITTER_SECONDS` | `"600"` | push.py |
| `HOME_SNAPSHOTS` | `"1"` | home_snapshots.py |
| `HOME_SNAPSHOT_REFRESH_SECONDS` | `"900"` | home_snapshots.py |
| `HOME_SNAPSHOT_WORKERS` | `"2"` | home_snapshots.py |
| `HOME_SNAPSHOT_MAX_PENDING` | `"200"` | home_snapshots.py |
| `PUSH_OUTBOX_MAX_AGE_MINUTES` | `"120"` | push.py |
| `RETENTION_CHUNK_ROWS` | `"5000"` | ops.py |
| `RETENTION_MAX_SECONDS` | `str(10 * 60` | ops.py |
| `RETENTION_PASS_MAX_ROWS` | `"200000"` | ops.py — the per-table per-pass floor of the cap; the cap is the larger of it and `RETENTION_INFLOW_FACTOR` x the rows that aged out in the last day (DB-03) |
| `RETENTION_INFLOW_FACTOR` | `"2"` | ops.py — the retention cap follows a table's inflow: this many days of what ages out a day (scale audit 10/19/26 DB-03) |
| `PARTITION_DROPS_PER_PASS` | `"1"` | pg_partitions.py — on Postgres, month partitions wholly past a partitioned table's window dropped per table per nightly prune (DB-03) |
| `REVIEW_NEWS_MAX_AGE_DAYS` | `"7"` | notify.py |
| `REVIEW_QUIET_MAX_PER_DAY` | `"2"` | fetcher.py — a Places-only listing whose Google count grew by fewer reviews a day than this over 14 days is fetched in the quiet slots only (AI cost audit 10/7/26 #45) |
| `REVIEW_QUIET_SLOTS` | `"8,16"` | fetcher.py — the Chicago review-fetch slots a quiet Places-only restaurant keeps (a subset of 8,12,16,20; never two adjacent skipped) |
| `REVIEW_REQUEST_DELAY_HOURS` | `DEFAULT_REVIEW_REQUEST_DELAY_HOURS` | guest_marketing.py |
| `RUN_DEAD_MINUTES` | `"10"` | ops.py |
| `RUN_PULSE_SECONDS` | `"60"` | ops.py |
| `RUN_REQUEST_TTL_MINUTES` | `"30"` | ops.py |
| `SCHEDULER_LEASE_STALE_SECONDS` | `"1800"` | ops.py |
| `SCHEDULER_TICK_SECONDS` | `"300"` | admin_routes.py, scheduler.py |
| `SCHEDULE_GEN_WORKERS` | unset (no ceiling) | An operator's ceiling on the default generation slots (`job_queue.default_generation_slots`); it was the fixed default of 2 until 10/19/26 | schedule_engine.py |
| `SCHEDULE_PARALLEL_CALLS` | `"3"` | schedule_engine.py |
| `SCHEDULE_PARALLEL_SPLIT_ROWS` | `"600"` | schedule_engine.py — a week an owner waits for past this many rows is written in parallel department parts (AI-21); `0` off |
| `SCHEDULE_REFUSAL_FALLBACK` | — (off) | labor.py — `1` sends the synchronous schedule call with the server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`; never in a batch). ON in production since 10/14/26: one live call each on claude-sonnet-5-5 and claude-opus-5-5 returned end_turn with the beta and `fallbacks: "default"` (AI-30) |
| `SCHEDULE_CALL_POOL_WORKERS` | `job_queue.max_running("generation") x SCHEDULE_PARALLEL_CALLS` | schedule_engine.py |
| `SCHEDULE_SLIM_REDO` | `"1"` | schedule_prompt.py — a model redo (an owner's redo the code could not draft, the quality gate's rewrite) is sent the redone dates and the seam only (AI-28); `0` sends the whole week's context again |
| `STRIPE_RECONCILE_MAX_SECONDS` | `str(15 * 60` | billing_jobs.py |
| `STUDIO_CACHE_BUDGET_MB` | `"512"` | schedule_engine.py — the Studio's kept inputs, sized in bytes, each week measured as kept and its scorer as it fills (SCALE-22; replaces `STUDIO_CACHE_BUDGET_ROWS`) |
| `STUDIO_WHAT_IF_SECONDS` | `"8"` | schedule_engine.py — the what-if's bound on a request thread (SCALE-21: 98 s at 250 people); a generation keeps its sixty evaluations |
| `SCHEDULE_FILL_SCORE_WORK` | `"600000"` | schedule_engine.py — rows one fill/trim pass's scorer may read before the pass goes back to its own order (SCALE-15: was 6 seconds) |
| `SCHEDULE_FILL_SCORE_TOTAL_WORK` | `"2400000"` | schedule_engine.py — the same across a generation's scorers (was 24 seconds) |
| `STUDIO_CACHE_PER_RESTAURANT` | `"3"` | schedule_engine.py |
| `STUDIO_CACHE_SECONDS` | `"900"` | schedule_engine.py |
| `PREPUBLISH_CACHE_SECONDS` | `"300"` | How long a pre-publish review (the saved week's, or the rows on screen by their text) is served from its cache; the key holds everything the review reads, so this is a backstop (it was a fixed 30 s — scale audit 10/19/26 A5-F5). prepublish.py |
| `SHIFT_HISTORY_CACHE_SECONDS` | `"900"` | The backstop on the parsed shift history web requests share (`labor._shared_history`, keyed on client_data's history stamp — scale audit 10/19/26 A5-F5). labor.py |
| `SHIFT_HISTORY_CACHE_ROWS` | `"300000"` | The most shift rows that shared history holds across restaurants (least recently used out first; 0 turns it off). labor.py |
| `SWEEP_MAX_SECONDS` | `str(45 * 60` | scheduler.py |
| `SWEEP_WORKERS` | `"1"` | scheduler.py |
| `TOKEN_REFRESH_MAX_SECONDS` | `str(10 * 60` | scheduler.py |
| `WEBHOOK_MAX_QUEUED` | `"500"` | webhooks.py |
| `WEBHOOK_MAX_WORKERS` | `"4"` | webhooks.py |
| `WEBHOOK_OUTBOX_MAX_AGE_HOURS` | `"24"` | webhooks.py |
| `WEEKLY_SWEEP_MAX_SECONDS` | `str(3 * 3600` | scheduler.py |

## Retention windows

`RETAIN_<TABLE>_DAYS` overrides one ledger's retention window (68 of them; `ops._RETENTION_DAYS` is the registry and its defaults, `DATABASE_SCHEMA.md` → retention). 0 keeps a table's rows forever.

`RETAIN_ACTIVITY_LOG_DAYS`, `RETAIN_ADMIN_EVENTS_DAYS`, `RETAIN_AIVIS_QUERIES_DAYS`, `RETAIN_AIVIS_RUNS_DAYS`, `RETAIN_AI_BATCHES_DAYS`, `RETAIN_AI_CLAIMS_DAYS`, `RETAIN_AI_GATE_VERDICTS_DAYS`, `RETAIN_AI_READS_DAYS`, `RETAIN_AI_ROUTE_RECS_DAYS`, `RETAIN_AI_RUNS_DAYS`, `RETAIN_AI_RUN_REQUESTS_DAYS`, `RETAIN_AI_USAGE_DAYS`, `RETAIN_AI_VALIDATION_DAYS`, `RETAIN_ALERT_HOLDS_DAYS`, `RETAIN_ALERT_LOG_DAYS`, `RETAIN_ALERT_STORM_CAPS_DAYS`, `RETAIN_ASK_FEEDBACK_DAYS`, `RETAIN_ASK_MEMORY_ARCHIVE_DAYS`, `RETAIN_ASK_TOPICS_DAYS`, `RETAIN_ATTENDANCE_DAYS`, `RETAIN_BACKUP_RUNS_DAYS`, `RETAIN_BOOT_EVENTS_DAYS`, `RETAIN_BRIEF_DELIVERIES_DAYS`, `RETAIN_COMPETITOR_SNAPSHOTS_DAYS`, `RETAIN_COMPLIANCE_RECORDS_DAYS`, `RETAIN_CONTEXT_SECTIONS_DAYS`, `RETAIN_DATA_HEALTH_DAILY_DAYS`, `RETAIN_DRAFT_DETAIL_DAYS`, `RETAIN_EMAIL_INTAKE_DAYS` (received intake mail, 180, floor 90), `RETAIN_EMAIL_INTAKE_UNROUTED_DAYS` (mail to no intake address, 30, floor 7), `RETAIN_EMAIL_LOG_DAYS`, `RETAIN_FINTECH_FILE_CONTENT_DAYS` (a received invoice file's text and its object-store copy, 365, floor 90), `RETAIN_EXPIRED_SESSIONS_DAYS`, `RETAIN_HTTP_5XX_DAYS`, `RETAIN_INVENTORY_DAILY_DAYS`, `RETAIN_INVENTORY_HISTORY_DAYS`, `RETAIN_JOB_CLAIMS_DAYS`, `RETAIN_JOB_FAILURES_DAYS`, `RETAIN_JOB_RUNS_DAYS`, `RETAIN_JOB_RUN_REQUESTS_DAYS`, `RETAIN_LINK_TAPS_DAYS`, `RETAIN_LIVE_ACTIVITY_RUNS_DAYS`, `RETAIN_LIVE_ACTIVITY_TOKENS_DAYS`, `RETAIN_LIVE_LABOR_DAYS`, `RETAIN_LIVE_LABOR_READS_DAYS`, `RETAIN_LOGIN_HISTORY_DAYS`, `RETAIN_MISSED_WINDOWS_DAYS`, `RETAIN_MKT_MODEL_DRAFTS_DAYS`, `RETAIN_NOTIFICATION_OPENS_DAYS`, `RETAIN_OFFER_PLATFORMS_DAYS`, `RETAIN_OPERATOR_ALERTS_DAYS`, `RETAIN_PERSON_SIGNALS_DAYS`, `RETAIN_PLACES_CACHE_DAYS`, `RETAIN_POS_ARCHIVE_DAYS`, `RETAIN_POS_LINES_DAYS` (the ticket lines alone, default the archive's; floor 400 — every day is rolled into `pos_item_daily` first, DB-02), `RETAIN_POS_ITEM_DAILY_DAYS` (the lines by item and day, default the archive's; floor 400), `RETAIN_POS_PUSH_DAYS`, `RETAIN_PROVIDER_HEALTH_DAYS`, `RETAIN_PUSH_DELIVERIES_DAYS`, `RETAIN_PUSH_OUTBOX_DAYS`, `RETAIN_REC_EVENTS_DAYS`, `RETAIN_REC_RANK_BUILDS_DAYS`, `RETAIN_REC_SILENCES_DAYS`, `RETAIN_REPLY_REJECTIONS_DAYS`, `RETAIN_REQUEST_ROLLUPS_DAYS`, `RETAIN_RESOLUTION_HISTORY_DAYS`, `RETAIN_REVIEW_ERASE_DAYS`, `RETAIN_SCHEDULE_METRICS_DAYS`, `RETAIN_SCHEDULE_MODEL_CALLS_DAYS`, `RETAIN_SCHEDULE_NOTICES_DAYS`, `RETAIN_SCHEDULE_VERSIONS_DAYS`, `RETAIN_SCHED_CALIBRATION_RUNS_DAYS`, `RETAIN_SCHED_EDIT_ANSWERS_DAYS`, `RETAIN_SCHED_GENERATION_TAGS_DAYS`, `RETAIN_SCHED_LEARNING_RECORDS_DAYS`, `RETAIN_SCHED_OBSERVATIONS_DAYS`, `RETAIN_SCHED_PUBLISH_OVERRIDES_DAYS`, `RETAIN_SCHED_RECS_DAYS`, `RETAIN_SCHED_REVIEW_DISMISSALS_DAYS`, `RETAIN_SCHED_REJECTIONS_DAYS`, `RETAIN_SHIFT_FACTS_DAYS`, `RETAIN_SHIFT_SECTIONS_DAYS`, `RETAIN_SMS_LOG_DAYS`, `RETAIN_STAFF_COMMS_DAYS`, `RETAIN_STAFF_OPS_DAYS`, `RETAIN_STAFF_TRANSLATIONS_DAYS`, `RETAIN_STRIPE_EVENTS_SEEN_DAYS`, `RETAIN_SUBDAILY_CLAIMS_DAYS` (claims of a period under a day — live labor, intraday, the loop's ticks — 2, floor 1; DB-14), `RETAIN_SUPERSEDED_DRAFTS_DAYS`, `RETAIN_TASK_PHOTO_DAYS`, `RETAIN_TASK_SHEET_DAYS`, `RETAIN_VALUE_FIGURES_DAYS`, `RETAIN_VERSION_DETAIL_DAYS`, `RETAIN_WEBHOOK_DELIVERIES_DAYS`, `RETAIN_WEBHOOK_OUTBOX_DAYS`
