# P4-3 Farmers' WhatsApp replies by webhook (messaging/v1 amendment A1.3), 24 Nov 2026

**Decisions used:** CHAT-55, D3 = (a): a number we never sent to is only counted.

**Contract:** `kisaniti/docs/contracts/L5_MESSAGING_v1_AMENDMENT_A1_CLOUD_API.md` §A1.3.

## Scope
- **`messaging/webhook.py`:** each inbound message is matched and stored as a reply, or counted with an explicit outcome. `INBOUND_NOT_STORED` is gone.
  - Matching by Meta's reply context, else by the newest message sent to that number.
  - Ambiguous across FPOs: not stored.
- **Migration 0015:** `replies.provider_message_id` (unique) and checks (a webhook reply has Meta's ID and no user).
- **Website:** a reply is labelled "from the farmer on WhatsApp" or "entered by staff" (English and Punjabi).

## Exclusions
- Media, buttons, locations, opt-out keywords.
- Free-text answers inside the 24-hour window (D1 allows them only as replies; not built).
- Replies over 500 characters are counted, not stored.

## Tests
Backend total: 481 passed, 0 failed (8 new in `tests/test_whatsapp_replies.py`). Messages are sent first through the real send path, with Meta replaced by a fake. The new tests check:
- A reply with context is stored once (a Meta retry gives `REPLY_DUPLICATE`), and a reply without context goes to the newest message. The source is WEBHOOK with no user, and it is valid against `sales.reply/v1`.
- A stranger, even with a valid reply context, is `REPLY_UNMATCHED`: no reply row, and the number in no log line and no row.
- Image and empty messages are unsupported; 501 characters is too long; 500 is stored.
- A number two FPOs wrote to:
  - without context it is ambiguous;
  - with context it is stored only in the right FPO;
  - the other FPO sees nothing (404 on the instruction).
- The database refuses a webhook reply without Meta's ID, a staff reply with one, and a webhook reply with a user.

Other checks: frontend lint, typecheck and i18n (757 keys) passed.
