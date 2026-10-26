# Completion report — slice 4: contacts and consent, templates, the preview channel, delivery records

26 October 2026 • Contract `messaging/v1` (frozen first, CHAT-46; text in `kisaniti/docs/contracts/L5_MESSAGING_CONTRACT_v1.md`). Fictional FPOs, farmers and phone numbers only; **nothing was or can be sent to anyone** in this version.

## What it does
An approved plan becomes one **instruction** per farmer: the exact text in that farmer's recorded language, built from a versioned template and the approved facts, frozen with the contact and consent that allowed it. A **preview channel** shows that text and the masked recipient and sends nothing. Every attempt is a **delivery record**. The manager sees what is ready, what is blocked and why, and can prepare a **cancellation** message after a release.

## Delivered
- **Contacts**: phone numbers in append-only versions, normalised to E.164 (a 10-digit Indian mobile becomes `+91…`); shown **masked** (`+91•••••••001`) unless the role holds `contacts.view_phone`; each unmasked view is written to the append-only `personal_data_access_log`. No column exists for Aadhaar, bank, land, caste, religion or income (checked by a test over every table), and a request with any extra field is refused.
- **Consent**: per member and per kind (advice, WhatsApp, research), append-only events with notice version, method and recorder; current state derived; withdrawal takes effect at the next check.
- **Templates**: two templates (`SALE_INSTRUCTION`, `CANCELLATION`), each with Punjabi, Hindi and English variants, versioned files, a text fingerprint and a per-variant review status. **All variants are `UNREVIEWED`**: no native speaker has read them (I wrote them), and the pages say so.
- **Rendering** (pure, in the core): the stored fact strings are inserted unchanged (quantities keep three decimals), dates carry the month name of the language with Western digits, a value that looks like a placeholder is never expanded again, and an unfilled or unknown placeholder is an error.
- **Instructions and delivery records**: one per (plan, member, kind); blocked ones carry codes (no contact, no consent, withdrawn, inactive, no template); a re-check at send time adds "language changed" and "reservation no longer active" and records a `BLOCKED` attempt instead of sending; a changed language writes a new version rather than switching silently; cancellation goes only to farmers whose sale message was previewed and links it.
- **Queue** of blocked messages; **pages in Punjabi and English**: contacts and consent, messages per plan on the plan page (prepare, show text, preview), a messages page with the queue and the review status of each language.
- **Smoke test** extended (direct and through the website).

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 265 passed (22 new in `test_messaging.py`); ruff, format, strict mypy clean; the new file ran three times in a row without a failure |
| Core tests | 563 passed (30 new: phones, masking, dates, rendering, local IDs) |
| Contract examples | 379 passed (58 for `messaging/v1`: 22 valid, 36 invalid; the instruction and attempt examples are real builder output) |
| Exact text, three languages | for three farmers with Punjabi, Hindi and English, each message equals a hand-written expected string (own month tables in the test), amounts exact (`1200.000`, `800.000`, `500.000`), the manager's English UI language did not change a Punjabi message, no price or promise in any text |
| Consent and language re-checks | consent withdrawn after preparing: the send is recorded `BLOCKED` with `CONSENT_WITHDRAWN`, the frozen text is unchanged, a later grant allows the preview; language changed: `LANGUAGE_CHANGED`, then a new version in the new language that links the old one; reservation released: `COMMITMENT_NOT_ACTIVE` |
| Privacy | across a full flow no role without `contacts.view_phone` ever received a number in any response; no phone digit appears in any captured log record at DEBUG level, nor in `instructions`, `delivery_attempts` or the audit log; a refused number is not echoed; the preview shows the recipient masked even to the admin |
| Mutation checks | consent not re-checked at send; phone never masked; preview showing the full number; language change ignored; access log switched off; reservation not re-checked; duplicate instructions on re-prepare; template review status hard-coded; cancellation to every farmer: each made a named test fail |
| Isolation | another FPO gets the same 404 on nine contact, consent, plan-instruction, instruction and send probes, audited; nothing it tried changed A's data; all five tables are invisible without the tenant and append-only for the tenant role and the owner |
| Frontend | lint, typecheck, build pass; 582 keys in both dictionaries |
| In the browser | an invalid number refused with the message; two fictional numbers saved (admin sees them, `+919000000001`); consent recorded; "Prepare the messages" made two instructions; the preview showed `PREVIEW — nothing was sent`, the masked recipient `+91•••••••001` and the Punjabi text with `2400.000 ਕਿਲੋ` and `20 ਅਕਤੂਬਰ 2026`; the Punjabi messages page lists both templates as not yet reviewed |
| Smoke test | PASS directly and through the website |

## Findings and mistakes
- A first version of "prepare again" skipped a prepared instruction whose language had changed, because a second check ran for every earlier instruction, not only blocked ones; the language test caught it.
- Two of my mutation runs did not apply (the code had been reformatted); I noticed that the output showed no failure *and* a grep for my edit found nothing, and redid them properly. Both were then caught.
- My splitter for long SQL lines left an empty string in the file; removed.
- The contract commit message said 31 examples; there were 33 (and 58 after the instruction and attempt examples).
- A test counted rows of a whole table and failed because other tests' data was still there; it now counts the FPO's own rows.

## Limits
- **No real send.** The WhatsApp channel (S4.5) needs your Meta account and approved templates; the delivery-status webhook (S4.6) needs a public HTTPS address; both are specified (statuses and the "never move backwards" rule) but not built. A preview is never `SENT` or `DELIVERED` and cannot count as delivery evidence.
- **Translations are unreviewed.** Punjabi and Hindi texts are mine; the real channel must refuse an unreviewed variant and I have written that into the contract, but it cannot be tested until the channel exists.
- **Erasure and data-access screens are not built** (privacy contract §8, PR-T04), and neither is a retention job; therefore **fictional numbers only**. Also not built: gating planning on advice consent (PR-T02's second half; it would change plan behaviour), a reviewed consent notice text (the demo notice is a version name), shared household phones, provider template names and locale codes.
- **No proof of ownership:** a valid number is not shown to belong to the farmer.
- **Mandi names** in messages are the price source's English names, also in Punjabi and Hindi texts.
- **The database holds phone numbers unencrypted at column level** (encryption at rest is a hosting matter, not done here).
