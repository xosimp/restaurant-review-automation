# Staff PIN pepper — setup and rotation

The mechanism has existed since the employee tier shipped. This is the
procedure, which did not.

## What the pepper is for

A staff PIN is 4–8 digits. At the short end that is 10,000 candidates, which
any KDF loses to on commodity hardware if an attacker has the database in
hand — scrypt buys time per guess, and 10,000 guesses is not enough guesses
for that to matter.

`CAVNAR_PIN_PEPPER` is an application secret mixed into every PIN *before*
hashing. It lives in the environment, never in the database, so a dumped
database alone gives an attacker nothing to test candidates against.

**Without it set, every staff PIN in the estate is brute-forceable offline
the moment the database leaks.** `init_auth()` prints a warning at boot and
`auth.pin_pepper_health()` reports it; `scripts/check_pin_pepper.py` is the
one-line check.

## First-time setup — do this before the first real PIN exists

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set the output as `CAVNAR_PIN_PEPPER` in Railway → the service → Variables.
Redeploy. Confirm:

```bash
python3 scripts/check_pin_pepper.py
```

It should report `configured: True`, `unpeppered: 0`.

Setting it for the first time on a database that already has PINs is safe:
those hashes were written under version `v0` (no pepper) and still verify,
then get rewritten under the current version on the owner's next successful
sign-in. `unpeppered` counts down as staff sign in, and reaches 0 on its own.

## How rotation works

Hashes are stored version-tagged: `pepv1$<scrypt hash>`. An unversioned value
predates versioning and is read as `v0`, meaning "no pepper".

`verify_membership_pin()` decodes the version, verifies with the pepper that
version implies, and — on success — rewrites the hash under the *current*
version. So a rotation drains itself: every employee's next sign-in upgrades
their own hash, with no reissued PINs and no deploy window.

## Rotating

Rotation means two things happening in the right order. The hash prefix is
what makes the old pepper still usable for reads, so **the old value must stay
readable until every hash has been rewritten.**

1. **Bump the version constant.** In `auth.py`:

   ```python
   _PIN_PEPPER_VERSION = "v2"
   ```

2. **Teach `_peppered()` the old value.** It currently maps `v0` → no pepper
   and everything else → the current `CAVNAR_PIN_PEPPER`. For a real rotation
   it needs both:

   ```python
   def _peppered(pin, version=None):
       version = version or _PIN_PEPPER_VERSION
       if version == "v0":
           return f"::{pin}"
       if version == "v1":
           return f"{os.environ.get('CAVNAR_PIN_PEPPER_V1', '')}::{pin}"
       return f"{_pin_pepper()}::{pin}"
   ```

3. **Set both env vars.** `CAVNAR_PIN_PEPPER_V1` = the outgoing value,
   `CAVNAR_PIN_PEPPER` = the new one. Deploy.

4. **Wait for the drain.** `pin_pepper_health()` reports how many hashes are
   still on an older version. One full scheduling cycle (two weeks) covers
   essentially everyone; stragglers are staff who have not worked a shift.

5. **Force the remainder.** Anyone still on the old version after the drain
   window gets a PIN reset from the owner — `Account → Staff accounts → New
   PIN`. That writes a fresh hash at the current version.

6. **Remove the old value.** Only once `pin_pepper_health()` reports zero on
   the old version: delete `CAVNAR_PIN_PEPPER_V1` and drop its branch from
   `_peppered()`.

## When to rotate

- The value leaked, or might have (a shared screenshot, a log, a former
  contractor's shell history).
- Someone with production environment access left.
- Never on a schedule for its own sake. Rotation has a drain window during
  which two secrets are live; doing it without cause widens the surface it
  is meant to narrow.

## What NOT to do

- **Do not change `CAVNAR_PIN_PEPPER` without bumping the version.** Every
  existing hash silently stops verifying and the entire staff of every
  restaurant is locked out at once, with the only recovery being an owner
  reissuing every PIN by hand.
- **Do not remove the old value before the drain completes.** Same outcome,
  for whoever had not signed in yet.
- **Do not store it in the database, `.env` committed to git, or the repo.**
  Its entire value is being somewhere the database is not.
