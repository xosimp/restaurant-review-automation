# Security — Cavnar AI

What protects the platform, where each control lives, and the environment
variables it depends on. Written after the enterprise security audit
(Sep 2026). Read `RECOVERY.md` (beside this file) for what to do when something is broken.

## Environment variables the controls depend on

| Variable | Required | What happens without it |
|---|---|---|
| `SECRET_KEY` | yes | Flask sessions get a random per-boot key (every deploy logs everyone out); signed guest join links fall back to a dev value; the Google connect state cannot be signed at all (`gmb._state_secret` raises) |
| `CAVNAR_PIN_PEPPER` | **yes** | Staff PINs **cannot be set** (`auth.set_membership_pin` raises). Existing PINs still verify. |
| `CREDENTIAL_KEY` | yes (production) | Client OAuth/POS tokens are stored **unencrypted** and a warning is printed once at boot. Generate: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `STRIPE_WEBHOOK_SECRET`, `DOCUSIGN_WEBHOOK_SECRET` | yes | Webhooks are rejected |
| `BACKUP_ENCRYPTION_KEY` | yes | The emailed backup copy is skipped (local snapshots still run) |
| `ADMIN_REQUIRE_2FA` | opt-in (`1`) | Set it **after** every admin account has enrolled in Account → Security; unset, an admin without two-factor can still open `/admin` |
| `ALLOW_LEGACY_JOIN_LINKS` | default `1` | `0` retires bare-integer `/join/<id>` links (set once printed QR codes are reissued) |
| `HIBP_DISABLED` | default off | `1` skips the breached-password lookup (tests set this) |
| `BACKUP_RETAIN_DAYS` | default `7` | local snapshot retention |

## Authentication

- Passwords: scrypt (`werkzeug.security`); minimum 8 characters everywhere, including admin resets; **breached-password check** on every set via HIBP k-anonymity (`security.password_pwned`, fails open).
- Sessions: 24-byte tokens, **SHA-256 at rest**, 30-day absolute, 8-hour idle for web, 14-hour staff sessions; `HttpOnly`/`SameSite=Lax`/`Secure` on Railway; HSTS when cookies are secure.
- **Login throttling is durable** (`security.py`, table `login_attempts`): keyed by account (5 failures / 15 min → 5 min lock, escalating to 30 min and 24 h across a day) and by IP (25 attempts naming accounts; 5 naming none). Survives deploys and workers. A lock writes `login_locked` to the owner's activity log.
- Password reset: 32-byte token, 1-hour expiry, **hashed at rest** (`models._hash_reset_token`).
- Temporary passwords are **never stored**: emailed once at creation; a fresh one is minted at contract signing.
- 2FA: email/SMS OTP hashed at rest, backup codes, 30-day trusted devices, revocable. **Admins must have it on once `ADMIN_REQUIRE_2FA=1`** (`auth._admin_two_factor_missing`) — enrol first, then set the variable.

## Authorization

- Roles in `permissions.py`: owner, client, manager, member, employee, **support**. Support reads the admin console and may open view-as; every admin write returns 403 (`auth.admin_required`).
- Module-view gates fail closed; billing/entitlement gates fail open by design.
- Every admin write is recorded in `admin_events` **before it runs** (`admin_routes._audit_admin_write`).

## Tenant isolation

Identity comes from the session row, never the request. Owners switch locations only inside their group. Row access is `WHERE id=? AND restaurant_id=?`. Public pages use hashed or signed tokens (`/s/<token>`, issue links, `/join/<token>`).

## Data at rest

- `CREDENTIAL_KEY` encrypts `gmb_*`, `ig_token`, `fb_page_token`, `toast_*` columns transparently (`credentials.py`): `update_restaurant` encrypts, `get_restaurant`/`get_all_restaurants` decrypt. Rows written before the key still read; a rotated key yields `None`, never ciphertext.
- Backups: nightly snapshot on the volume (7 days), integrity-checked; emailed copy Fernet-encrypted and redacted (sessions, codes, devices, tokens, reset tokens).
- Retention: per-restaurant `data_retention_months`; operational ledgers on `ops._RETENTION_DAYS`; `login_attempts` pruned after 2 days.

## Takeover response

`/admin/freeze/<restaurant_id>` (`security.freeze_restaurant`): every session and trusted device for the restaurant's logins is revoked and each must reset its password before signing in. Logged for the owner (`account_frozen`) and in `admin_events`. `/admin/send-reset-link/<user_id>` emails a one-hour reset link instead of setting a password by hand.

## What the owner can see

Account → Security → Activity carries sign-ins, lockouts, freezes, PIN resets, memory deletions and every automation switch change. Account → Security → *What Cavnar AI remembers* lists every fact the assistant holds, with its source, and a Forget button.

## Known gaps (read the audit)

- The web CSP still allows `'unsafe-inline'` scripts: the dashboard's inline event handlers need a nonce-based rewrite first.
- Guest phone numbers are plaintext (looked up by value for consent and STOP).
- Request rate limits beyond login and paid actions belong at the gateway (Cloudflare).
