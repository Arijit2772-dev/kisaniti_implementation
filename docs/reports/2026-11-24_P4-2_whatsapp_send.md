# P4-2 WhatsApp sending through the Cloud API (messaging/v1 amendment A1.2), 24 Nov 2026

**Decisions used:** CHAT-55 (owner, 24 Nov):
- D1 = Meta-approved templates with an exact-match check;
- sale lines joined with " ; " in a template version 2;
- D3 = count strays, never store;
- the Meta test number sends.

**Contract:** `kisaniti/docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md` §A1.2.

## Scope
- **Text versions.** `SALE_INSTRUCTION` and `CANCELLATION` version 2: the same words as version 1, sale lines joined with " ; ", `UNREVIEWED`.
  - The core `render()` takes the joiner (version 1 keeps a new line).
  - New version-in-use rule: the newest fully reviewed version, else the newest. Version 1 stays in use until version 2 is reviewed.
- **Sender:** `messaging/whatsapp.py`.
  - Server settings: `KN_WA_TOKEN`, `KN_WA_PHONE_NUMBER_ID`, `KN_WA_API_VERSION`, `KN_WA_TEMPLATES`.
  - Meta's form of a text ({{1}}…), and the exact-match check (split, refill, compare).
  - One Cloud API call with a 10 s timeout, using the standard library only (no new dependency).
- **Send route** `POST /instructions/{id}/send` with `WHATSAPP`, which refuses with:
  - `CHANNEL_NOT_AVAILABLE` (no settings, or no Meta template for this version and language);
  - `TEMPLATE_UNREVIEWED`;
  - `TEMPLATE_MISMATCH`;
  - `OUTCOME_UNKNOWN` (the last send had no clear answer);
  - the existing re-checks.

  Possible results:
  - `QUEUED` with Meta's message ID;
  - `FAILED` with `PROVIDER_REFUSED` and Meta's codes;
  - `FAILED` with `PROVIDER_OUTCOME_UNKNOWN`.
- **Migration 0014:** `delivery_attempts.failure` and `error_codes`, with checks (a cause exactly when FAILED; codes only with a refusal). The attempt contract gains both fields, with 3 valid and 3 invalid examples.
- **Website:** on the Messages panel, a **Send through WhatsApp** button with a confirmation, and lines for "handed to WhatsApp" and "not sent: reason (code)". Seven new error and failure texts in English and Punjabi.
- **`scripts/wa_templates.py`:** prints the six template bodies to create in WhatsApp Manager, with sample values and the server's `KN_WA_TEMPLATES` line.
- **`deploy/compose.prod.yaml`:** passes the six `KN_WA_*` settings to the backend (empty means off).

## Exclusions
- Farmers' replies by webhook (A1.3): next item.
- The review of the version 2 texts: people, not code. No review is claimed.
- Template creation and approval in Meta, and putting the token on the server: the owner.
- Media, buttons, several numbers, opt-out keywords.
- No real message was sent: Meta is replaced by a fake in every test.

## Tests
Backend total: 473 passed, 0 failed (13 new in `tests/test_whatsapp_send.py`). The new tests check:
- Meta's form and the exact refill, in all three languages.
- A version 1 (multi-line) text never matches. A value with four spaces is refused.
- Settings are all or nothing; a bad phone ID, API version or mapping switches the channel off; an empty mapping is allowed.
- A version 1 instruction is refused (no template). An unreviewed text is refused. A text whose template changed after preparation is refused (`TEMPLATE_MISMATCH`). In each case Meta is not called.
- The happy path:
  - exactly one call, with the right URL, token, recipient digits, template name, language and values;
  - refilling Meta's form gives exactly the prepared text;
  - the number does not appear in the logs;
  - the second send is `ALREADY_SENT`, and a webhook `delivered` raises the attempt to DELIVERED.
- A 4xx keeps the code and never Meta's text. The instruction appears in the needs-attention queue, and a retry is allowed.
- Timeout, 5xx, no message ID and an unreadable answer each give `PROVIDER_OUTCOME_UNKNOWN`, and the retry is refused without calling Meta.
- A consent withdrawn after preparation gives BLOCKED, with no call.
- The database refuses a FAILED attempt without a cause, a cause on a QUEUED attempt, and codes on an unknown outcome.

Other checks:
- Contracts: 453 passed. Core tests: passed.
- Frontend lint, typecheck and i18n (756 keys each): passed.

## Known limits
- If the database write fails after Meta has accepted, the message is out with no attempt row. The log line (attempt and provider IDs, never the number) is the trace.
- The HTTP call happens inside the instruction's lock, for up to 10 s.

## What the owner must do before the first real send
1. Get the three version 2 texts (en, pa, hi) of both kinds reviewed. They are printed by `scripts/wa_templates.py`.
2. Create the six templates in WhatsApp Manager exactly as printed, then wait for Meta's approval.
3. Verify the friends' numbers as test recipients in the Meta dashboard.
4. Write the `KN_WA_*` lines into `~/kisaniti/.env` on the server.
5. Once the domain is live, set the webhook URL `https://app.kisaniti.in/api/v1/webhooks/whatsapp` in Meta.
