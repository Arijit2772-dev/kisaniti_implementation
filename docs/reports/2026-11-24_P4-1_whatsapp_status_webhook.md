# P4-1 WhatsApp status webhook, built ahead of the Meta account (24 Nov 2026)

**Contract:** `kisaniti:docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md`, part A1.1. Only the part already settled by messaging/v1 §5 is built. Sending (A1.2) and inbound replies (A1.3) wait for owner decisions D1–D3, and template names and locale codes stay UNKNOWN until the Meta account exists.

## Scope
- Migration `0013_delivery_events`:
  - new append-only, tenant-isolated table `delivery_events` (attempt, status, provider time, error codes, body SHA-256, request ID; unique per attempt and status);
  - `delivery_attempts` gains `UNIQUE (id, fpo_id)` and a unique index on the WhatsApp provider message ID.
- `messaging/status.py`: the effective status is the attempt's own status raised by its events, with the order QUEUED < SENT < DELIVERED < READ, and FAILED only below DELIVERED. The result does not depend on the order or repetition of events.
- `messaging/webhook.py`:
  - `GET /api/v1/webhooks/whatsapp`: Meta verification with `KN_WA_VERIFY_TOKEN`.
  - `POST /api/v1/webhooks/whatsapp`: accepted only with a valid `X-Hub-Signature-256` (HMAC-SHA256 with `KN_WA_APP_SECRET`, constant-time compare).
  - Each status is reported as RECORDED, DUPLICATE, UNKNOWN_MESSAGE, UNRECOGNISED_STATUS or IGNORED_BACKWARDS.
  - Inbound messages are only counted (INBOUND_NOT_STORED).
  - Without the two settings, both endpoints answer 503 CHANNEL_NOT_AVAILABLE.
  - The recipient number in an event is never stored or logged.
- The readers (instruction `delivery.state`, attempt list, the ALREADY_SENT check) use the effective status.

## Tests (backend 460 passed; 71 new in `tests/test_whatsapp_webhook.py`, most of them the order-independence cases)
- The rule:
  - never backwards; FAILED only before DELIVERED; preview and blocked attempts are untouched;
  - for every combination and order of SENT, DELIVERED, READ and FAILED (64 cases), all orders give the same result, and repeats change nothing.
- Endpoint:
  - 503 without settings;
  - verification answers the challenge only with the right token (403 otherwise);
  - a wrong or missing signature gives 401 and stores nothing;
  - sent + delivered → DELIVERED;
  - a mixed body gives exactly DUPLICATE 1, IGNORED_BACKWARDS 1, RECORDED 1, UNKNOWN_MESSAGE 1, UNRECOGNISED_STATUS 1, and the state becomes READ.
- Isolation and privacy: the other FPO gets 404 for the instruction; the event row holds the owner's FPO and no phone number; no log line holds the number.
- `delivery_events` refuses UPDATE and DELETE.
- The existing test "the real WhatsApp channel does not exist yet" still passes: sending on WHATSAPP is still refused with CHANNEL_NOT_AVAILABLE.

## Exclusions
- A1.2 sending:
  - D1 (template or 24-hour text) and D2 (template names) are needed;
  - the cancellation rule counts PREVIEWED/SENT/DELIVERED/READ as "farmer was told", and QUEUED is decided with A1.2.
- A1.3 replies by webhook: D3 is needed.
- Not yet reachable by Meta: it needs the public HTTPS address (domain).

## Rollback
`git revert` of this commit. The migration has a downgrade; it can only be run before any event is stored, because events are append-only.
