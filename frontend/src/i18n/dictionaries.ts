import "server-only";
import type { Locale } from "./config";

const dictionaries = {
  pa: () => import("./dictionaries/pa.json").then((m) => m.default),
  en: () => import("./dictionaries/en.json").then((m) => m.default),
};

export type Dictionary = Awaited<ReturnType<(typeof dictionaries)["en"]>>;

export const getDictionary = (locale: Locale): Promise<Dictionary> => dictionaries[locale]();

/** Fill {name} placeholders. Quantities and prices are passed as already-formatted exact strings, never re-rounded here. */
export function format(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (whole, key: string) => values[key] ?? whole);
}
