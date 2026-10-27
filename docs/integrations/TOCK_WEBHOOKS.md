# Tock webhooks — what we committed to

**Status: BUILT 10/20/26 (`guest_hub.receive_tock`, `webhook_routes.tock_webhook`); waiting on Tock to switch the webhooks on and confirm the payload format. Set `TOCK_WEBHOOK_TOKENS` (`5:<token>`) in Railway first.** Events are kept raw in `tock_events` and read tolerantly; once the real format is known, adjust the readers and run `guest_hub.reprocess_tock(5)`. Tock's own marketing opt-in is stored and shown (`guest_contacts.tock_marketing_optin`), never treated as texting consent.

Original status: requested, not built. On 10/13/26 Will replied to Resy | Tock support (Pualani Tapia, Manager of Support)
on the thread "Support Help Request". Cc'd: Danny Wurst and Erik Baylis (ebaylis@baylishospitality.com).

The reply asked for read-only webhook access for Simple EJ's Kitchen & Tap (prod rid 5). Tock will register these exact
values, so the receivers must be built to match them. Do not rename them.

## How events are applied (audit #25 A06-09, before go-live)

- **Order.** Tock retries deliveries, so an event can arrive after a newer one. Each booking keeps the newest applied
  event's own time (`tock_bookings.event_ms`, from the record's `updatedAt`/`modifiedAt`, else the event's
  `occurredAt`/`timestamp`/`createdAt`); an older event is stored in `tock_events` and not applied — a late
  "booked" never revives a cancelled party or its covers. `reprocess_tock` re-applies in event time, not arrival
  order. When Tock confirms its format, check which field carries the change time and name it first in
  `guest_hub._EVENT_TIME_BODY` / `_EVENT_TIME_EVENT` / `_EVENT_TIME_ENVELOPE`.
- **Whose phone.** On a reservation or walk-in, a guest's phone, email and marketing flag are read from its guest
  object only (or the booking's own top-level fields when there is none), never from a venue or staff object
  elsewhere in the body. A guest-profile event's body is the guest, and is read whole.
- **Still to do once the format is confirmed:** replace the alias lists in `guest_hub` with Tock's exact keys, add
  one real payload (every guest detail replaced) to `tests/fixtures/providers/`, and run `guest_hub.reprocess_tock(5)`.

## Endpoints (one per webhook)

| Webhook | Events requested | URL |
|---|---|---|
| Reservations | created, modified, cancelled, seated, completed, no-show | `https://dashboard.cavnar.ai/webhooks/tock/reservations` |
| Walk-ins | created, seated | `https://dashboard.cavnar.ai/webhooks/tock/walkins` |
| Guest profile | created, updated | `https://dashboard.cavnar.ai/webhooks/tock/guests` |

## Authorization header

`X-Cavnar-Webhook-Token:<value>`, in Tock's format: `headername:headervalue` with no spaces.

The token value was not put in the email. Will sends it to Tock separately, by a secure method. It belongs in the
environment, never in the repo. When the receivers are built, add it to `docs/ops/ENVIRONMENT.md`.

## What we told them it's for

- Reservations and walk-ins feed demand forecasting and labor scheduling (`reservation_feeds` → `demand_signals`).
- Guest profiles are used to recognise regulars and give managers context before service.
- We only read data. Nothing is written back to Tock.
- Guest contact details are not used for marketing unless the guest consented through Tock.
- We also offered a polling API to backfill historical reservations.

## To build once Tock confirms the payload format

1. Three routes in `webhook_routes.py` at the paths above.
2. Each route checks `X-Cavnar-Webhook-Token` against the env secret with a constant-time compare, and returns 401 otherwise.
3. Each route is idempotent on Tock's event id.
4. Bookings and walk-ins land in `reservation_feeds` / `demand_signals`.
5. Guest profiles go through the consent rules in `project_ejs_guest_profiles`: a recognition key, not texting consent.
