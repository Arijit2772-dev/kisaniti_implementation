// Interface languages (CHAT-36). Language lives in the path (/pa, /en) so a link opens in the language it was shared in.
// It is never inferred from a name, religion or caste: only from the user's own choice or browser preference.
export const LOCALES = ["pa", "en"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";
export const LANG_COOKIE = "kn_lang";

export const hasLocale = (value: string): value is Locale => (LOCALES as readonly string[]).includes(value);

/** Language names are shown in their own script and are never translated. */
export const LANGUAGE_NAME: Record<Locale, string> = { pa: "ਪੰਜਾਬੀ", en: "English" };

/** Saved choice first, then the browser's Accept-Language (highest q first), else the default. */
export function pickLocale(saved: string | undefined, acceptLanguage: string | null): Locale {
  if (saved && hasLocale(saved)) return saved;
  const ranked = (acceptLanguage ?? "")
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.map((p) => p.trim()).find((p) => p.startsWith("q="));
      return { primary: tag.toLowerCase().split("-")[0], q: q ? Number.parseFloat(q.slice(2)) : 1 };
    })
    .filter((x) => x.primary !== "" && Number.isFinite(x.q) && x.q > 0)
    .sort((a, b) => b.q - a.q);
  for (const { primary } of ranked) if (hasLocale(primary)) return primary;
  return DEFAULT_LOCALE;
}
