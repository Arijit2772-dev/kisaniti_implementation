"use client";
import { LANGUAGE_NAME, LOCALES, type Locale } from "@/i18n/config";
import { api, hardNavigate } from "@/lib/api-client";

/** Saves the choice on the account (so it follows the user to other devices), then opens the same page in that language. */
export function ShellLanguage({ lang, csrf, label }: { lang: Locale; csrf: string; label: string }) {
  async function choose(next: Locale) {
    await api("/auth/language", { method: "PUT", body: { language: next }, csrf });
    hardNavigate(window.location.pathname.replace(/^\/(pa|en)(?=\/|$)/, `/${next}`));
  }
  return (
    <nav aria-label={label} className="lang-switch">
      {LOCALES.map((l) =>
        l === lang ? (
          <strong key={l} lang={l} aria-current="true">
            {LANGUAGE_NAME[l]}
          </strong>
        ) : (
          <button key={l} type="button" lang={l} className="link" onClick={() => void choose(l)}>
            {LANGUAGE_NAME[l]}
          </button>
        ),
      )}
    </nav>
  );
}
