# App Store submission — what to enter, and where

Everything App Store Connect asks for that cannot be set from the codebase.
Written against audit #8. Keep it in sync with `PrivacyInfo.xcprivacy` — the
two are checked against each other by Apple, and a disagreement between them
is its own rejection.

---

## 1. Review account (required — this is the one that blocks approval)

The app has no self-serve signup. `/mobile/api/register` returns 403 unless
`ALLOW_PUBLIC_SIGNUP` is set, and Apple and Google sign-in both refuse an
unknown identity. **A reviewer with no account cannot get past the login
screen**, which is an automatic Guideline 2.1 rejection.

Create the account against production:

```bash
DB=/app/data/reviews.db python3 scripts/seed_review_account.py
```

It prints a username and password once. Put them in **App Store Connect →
your app → App Review Information → Sign-In Required**, with "Sign-in
required" ticked.

The script sets two-factor **off** deliberately — a reviewer cannot receive a
code — and marks the account `internal`, so it is fully entitled but never
counted as a client for revenue, onboarding emails or the monthly summary.

Verify it works before you submit, from a machine that is not yours:

```bash
curl -s -X POST https://dashboard.cavnar.ai/mobile/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"appreview","password":"THE-PASSWORD"}'
```

A pass looks like `"ok": true` and a token. Anything else, fix it before
submitting rather than after.

---

## 2. Review notes

Paste into **App Review Information → Notes**. The payment paragraph is the
one that matters: without it a reviewer sees the word Stripe and reaches for
Guideline 3.1.1.

> Cavnar AI is a business-to-business service for independent restaurants.
> Accounts are created by us for a restaurant after a service agreement is
> signed — there is no consumer signup, and nothing is sold inside the app.
>
> PAYMENTS: The app does not offer any purchase. There is no subscribe,
> upgrade or checkout flow. The Billing screen displays an existing business
> subscription and offers one link to Stripe's billing portal so an owner can
> update the payment method on the account their business already holds. No
> digital content or feature is sold to the user in the app.
>
> ACCOUNT DELETION: Account > Close account submits a deletion request in the
> app, which is recorded on our server and sent to us. Because service runs
> under a signed agreement with a 30-day notice period, the account is wound
> down by us rather than deleted instantly; the request is initiated entirely
> in the app and the screen explains the timeline.
>
> SIGN-IN: Please use the credentials above. "Continue with Apple" and
> "Continue with Google" are for existing account holders who linked those
> identities, and will report that no account exists.
>
> PERMISSIONS: The app requests notifications after sign-in, when the main
> screen loads, to deliver alerts about the restaurant's reviews, labor cost
> and food cost. Face ID is optional and only re-locks the app on return.
>
> DATA: The business data shown is the restaurant's own operational data —
> Google reviews, staff hours, ingredient costs. The review account is
> pre-populated so every screen has content.

---

## 3. Privacy nutrition label

Must match `ios/CavnarAI/CavnarAI/PrivacyInfo.xcprivacy` exactly. Set under
**App Privacy → Data Collection**.

Answer **"Yes, we collect data from this app"**, then for every item below:
- Linked to the user: **Yes**
- Used for tracking: **No**
- Purpose: **App Functionality**

| App Store Connect item | Under | What it is in the app |
|---|---|---|
| Email Address | Contact Info | Account holder's sign-in and alert address |
| Name | Contact Info | `owner_name`, and the sign-off on review replies |
| Phone Number | Contact Info | Owner's SMS alerts, plus guest text-club numbers a restaurant adds under its own consent flow |
| Other Financial Info | Financial Info | The restaurant's sales, labor cost and food cost figures. **Not** payment details — cards live only with Stripe and are never entered in the app |
| Device ID | Identifiers | A UUID the app generates for itself and keeps in the Keychain, to recognise a returning device at sign-in and address push. Not the IDFA or IDFV |
| Customer Support | Other Data | The message a user writes in Report a Bug |
| Other Diagnostic Data | Diagnostics | App version, iOS version, device model and current screen, attached to a bug report |

**Do not tick** Purchases, Location, Contacts, Browsing History, Search
History, Health, Sensitive Info, or Advertising Data. None are collected.

Tracking: answer **No** to "Do you or your third-party partners use data for
tracking purposes?" The app has no ads, analytics or attribution SDK.

---

## 4. Everything else on the form

| Field | Answer |
|---|---|
| Export compliance | No — `ITSAppUsesNonExemptEncryption` is `false` in the plist; the app uses only standard HTTPS |
| Content rights | No third-party content |
| Age rating | 4+ (no objectionable content) |
| Category | Business (secondary: Productivity) |
| Privacy policy URL | `https://cavnar.ai/privacy` |
| Support URL | `https://cavnar.ai` |
| Account deletion | Supported in-app — Account > Close account |

---

## 5. Checked in code, no action needed

Recorded so nobody re-litigates them at submission time:

- **Sign in with Apple** is implemented natively with `ASAuthorizationController`
  and offered alongside Google, satisfying Guideline 4.8. The entitlement is
  in `project.yml`.
- **Release compiles out the API base-URL override entirely** and hardcodes
  `https://dashboard.cavnar.ai`, so a shipped build cannot be repointed and
  certificate pinning cannot be turned off.
- **Debug code is `#if DEBUG`**, including a Release stub for
  `DebugFrameWatchdog` so the archive compiles. Release builds clean.
- **No secrets in the binary.**
- **`aps-environment`** follows the build configuration rather than being
  hardcoded, so a TestFlight build registers against production APNs.
- **Permissions are push and Face ID only.** Push is requested after sign-in
  when Home has loaded, not at launch. Face ID has a specific usage string.
- **`NSAllowsLocalNetworking`** is present for the Debug `--lan` device-testing
  path. Apple documents it as usable without justification during App Review —
  it is not `NSAllowsArbitraryLoads` and should not be confused with it.
- **Dynamic Type** works: every custom font goes through helpers that pass
  `relativeTo:`.
- **Offline** writes queue to a durable outbox and drain on reconnect.
- Every outbound link in the app returns 200.
