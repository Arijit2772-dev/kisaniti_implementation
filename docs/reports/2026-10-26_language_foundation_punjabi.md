# Completion report — interface language foundation (Punjabi and English)

26 October 2026 • Decision CHAT-36 (planning repo). Scope: the language mechanism and the existing status page. No login, no new pages.

## Delivered
- Language in the path: `/pa` and `/en`. A path without a language redirects to the saved choice (cookie `kn_lang`), else the browser's Accept-Language (Punjabi or English), else English. Hindi is not offered yet; a Hindi browser gets English.
- Catalogs `src/i18n/dictionaries/{en,pa}.json`, a typed loader, `{placeholder}` filling that never reformats numbers, a language switch with a 44 px touch target, `<html lang>` set per page, Gurmukhi line height and font list.
- `npm run i18n:check` (also in CI): same keys in every language, no empty text, same placeholders, Punjabi text contains Gurmukhi. Not checked: unused keys; translation correctness.
- API issue codes are mapped to catalog text; the backend is not translated.
- No new dependency.

## Evidence
| Check | Result |
|---|---|
| `npm run build`, `typecheck`, `lint`, `i18n:check` | pass (17 keys in each language) |
| Live stack, `/` without preference, with `pa-IN`, with saved cookie against English browser, with Hindi browser | redirects to `/en`, `/pa`, `/pa`, `/en` |
| Phone-size screenshot of `/pa`, switch to English by tapping the link | Gurmukhi renders without clipping; URL and text change |

## Limits
- **The Punjabi text is AI-drafted and has not been reviewed by a native speaker** (`src/i18n/REVIEW.md`). It must not be shown to real FPO staff before review.
- Digit style, default language and font are proposals awaiting the owner (AUTH_AND_LANGUAGE_PROPOSAL §4).
- Checked in one browser (the app's) on one phone-size viewport; real phones and other browsers not tested.
- The CI stack check was changed to follow the redirect from `/` and to look for Punjabi text; it has not run on GitHub yet at the time of writing.
