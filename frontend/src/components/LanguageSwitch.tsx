import { LANGUAGE_NAME, LOCALES, type Locale } from "@/i18n/config";

/** For pages that work without signing in. `path` is the page under the language, e.g. "/sign-in". */
export function LanguageSwitch({ lang, label, path = "" }: { lang: Locale; label: string; path?: string }) {
  return (
    <nav aria-label={label} className="lang-switch">
      {LOCALES.map((l) =>
        l === lang ? (
          <strong key={l} lang={l} aria-current="true">
            {LANGUAGE_NAME[l]}
          </strong>
        ) : (
          <a key={l} href={`/${l}${path}`} lang={l} hrefLang={l}>
            {LANGUAGE_NAME[l]}
          </a>
        ),
      )}
    </nav>
  );
}
