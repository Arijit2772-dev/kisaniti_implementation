# Slice 8 — the manager sends from his own WhatsApp (handover/v1)

26 October 2026. Requested by the owner: "OK/approve, Send to farmers, yes/no confirmation, Punjabi message links", one tap per farmer until the Meta account exists. Contract frozen first (planning repo `docs/contracts/L5_HANDOVER_CONTRACT_v1.md`, CHAT-49; product `contracts/handover/v1`, 6 schemas, 10 valid and 14 invalid examples). `messaging/v1` is unchanged.

## What the manager does now
1. Reviews the plan and approves it (existing review box).
2. Presses **Send to farmers** on the plan page. The system prepares the messages and asks: "Send this to 3 farmers?", lists them with their languages, lists anyone who cannot be sent to and why, and asks him to tick that the texts are not yet checked by a native speaker.
3. **Yes, show the links** (or **No**, which does nothing). Yes makes one `wa.me` link per farmer: the farmer's number and the exact stored text in their language.
4. He taps **Open WhatsApp** on a farmer's row, presses Send in WhatsApp, and returns to press **I sent it** (or **I did not send it**). The system records his statement; it cannot see delivery.

One tap reaches one farmer. Reaching everyone with one tap needs the Meta API (not built). A manager free-text note is not built (needs its own decision).

## What was built
- Migration `0012_handover` (3 append-only tenant tables; a new access-log reason `PHONE_LINK_ISSUED`), `backend/src/kisaniti_api/handover/routes.py` (preview, the yes, list, view, report), one widened rule in `messaging/routes.py` (a farmer reported as sent counts as reached for a cancellation; contract §5), `frontend/src/components/HandoverPanel.tsx`, 39 catalog keys in Punjabi and English (709 keys each), `scripts/chain.py` step `handover` plus a cancellation hand-over in `close`.
- Rules enforced: eligibility re-checked at the yes and at every view (consent withdrawn, contact changed, language changed, reservation released each withhold a link with a reason); a stale count (`CONFIRMATION_STALE`), no one to send to (`NOTHING_TO_SEND`), unreviewed texts need an explicit tick (`UNREVIEWED_NOT_ACKNOWLEDGED`); links need both `messages.send` and `contacts.view_phone`; a viewer and the other FPO get no link and no number; each link shown writes one access-log row; links are never stored or logged; a second press covers only farmers not yet reported as sent.

## Evidence
- Backend: 319 passed (15 new in `test_handover.py`, 1 new regression test in `test_org.py`); ruff, format, mypy clean. Contracts: 441 passed. Frontend: lint, typecheck, i18n check (709 keys), build.
- Chain: 14 steps pass on the rebuilt stack. Three planted bugs in the new code were each caught at step `handover`: the link drops the last character of the text; the stale-count check is off; the link's digits lose the country code. The code was restored and the clean run passed again.
- In the website (signed in as a fictional manager, English and Punjabi): the question lists three farmers, the box and Yes work, the links open in a new tab with the right digits and Punjabi, Hindi and English text, "I sent it" shows "You said: sent". WhatsApp itself was not opened (no real phone in this environment).

## Bug found on the way (BUG-008)
A full backend run failed twice on a lot showing no current location. Cause: the placement was stamped with the application's clock and read back against the database's clock; with the application ahead (a Docker VM, or two machines) it was invisible. Fixed (the database stamps it) with a regression test that fails on the old code.

## Limits
- The system cannot know whether WhatsApp opened, a message was sent, delivered or read; "sent" is the manager's word.
- The texts are unreviewed; the acknowledgement is a DEMO policy and can be tightened to a refusal once a reviewer exists.
- Fictional numbers only until the privacy controls and legal review. A `wa.me` link contains the number and text, so it must only ever be opened on the manager's own device.
- Not exercised: opening WhatsApp on a real phone; the Punjabi button wording has no native-speaker review.
E
python3 - <<'E'
import os
d=os.path.expanduser('~/Documents/kisaniti/docs/')
def sub(f,old,new):
    s=open(d+f).read(); assert s.count(old)==1,(f,old[:50]); open(d+f,'w').write(s.replace(old,new))
sub('PRODUCT_FLOW_STATUS.md',"**44 steps: done 30, started 7, not started 6, waiting 1.** (After slice 7, 26 Oct.)","**45 steps: done 31, started 7, not started 6, waiting 1.** (After slice 8, 26 Oct.)")
sub('PRODUCT_FLOW_STATUS.md',"| 4. Send the instruction to the farmers on WhatsApp | 8 | 5 | 1 | 2 | 0 |","| 4. Send the instruction to the farmers on WhatsApp | 9 | 6 | 1 | 2 | 0 |")
sub('PRODUCT_FLOW_STATUS.md',"| S4.8 | Pages: messages per farmer | done |","| S4.9 | The manager sends from his own WhatsApp: Send to farmers, yes/no, one link per farmer, reports (handover/v1, CHAT-49) | done | AI | one tap per farmer; the system cannot see delivery; texts unreviewed (acknowledgement required); manager free-text note not built; report `…_slice8_handover.md` |\n| S4.8 | Pages: messages per farmer | done |")
sub('PRODUCT_FLOW_STATUS.md',"| The manager's own WhatsApp (O69, undecided) |","| One tap that sends to everyone (needs the Meta Cloud API); a manager's free-text note (O69) |")
sub('BUILD_LEDGER.md',"- [ ] **Next: Slice 6**","- [x] **Slice 8** the manager's own WhatsApp (handover/v1, CHAT-49; report `…_slice8_handover.md`): Send to farmers, yes/no, one link per farmer, manager reports sent\n- [ ] **Next: Slice 6**")
sub('BUILD_LEDGER.md',"| Research: forecasting evaluation kit","| Slice 8: handover/v1: Send to farmers, yes/no, one wa.me link per farmer, manager-reported outcome; BUG-008 fixed | done; the system cannot see delivery; texts unreviewed | `backend/src/kisaniti_api/handover/`, migration 0012, `frontend/src/components/HandoverPanel.tsx`, chain step `handover` | 319 backend tests, 441 contract checks, 14-step chain, 3 planted bugs caught |\n| Research: forecasting evaluation kit")
s=open(d+'OPEN_ITEMS.md').read()
old="| YOU | open (direction chosen, limit to confirm) |"
assert s.count(old)==1
s=s.replace(old,"| YOU | decided 26 Oct: option B built as handover/v1 (CHAT-49); the manager's free-text note and the Meta channel stay open |")
open(d+'OPEN_ITEMS.md','w').write(s)
E
sed -i '' 's/`messages.send`, `contacts.view_phone`/&/' /dev/null; cd ~/Documents/kisaniti_implementation && git add -A -- backend core frontend contracts scripts docs .github .gitignore 2>&1 | tail -1; git status --short | head -30