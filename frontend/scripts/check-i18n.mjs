// Checks the language catalogs (CHAT-36). Exit 1 on any problem. Run: npm run i18n:check
// Checks: same keys in every language; no empty text; the same {placeholders}; Punjabi text contains Gurmukhi script.
// Not checked (yet): unused keys, and whether the translation is correct: that needs a native speaker (see REVIEW.md).
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const dir = new URL("../src/i18n/dictionaries/", import.meta.url).pathname;
const files = readdirSync(dir).filter((f) => f.endsWith(".json"));
const flat = (obj, prefix = "") =>
  Object.entries(obj).flatMap(([k, v]) => (typeof v === "object" ? flat(v, `${prefix}${k}.`) : [[`${prefix}${k}`, v]]));
const catalogs = Object.fromEntries(files.map((f) => [f.replace(".json", ""), new Map(flat(JSON.parse(readFileSync(join(dir, f), "utf8"))))]));
const LITERAL = new Set(["app.name", "home.fpo"]); // brand name and the acronym FPO stay in Latin script
const placeholders = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
const problems = [];
const base = catalogs.en;
if (!base) problems.push("missing en.json");
for (const [lang, cat] of Object.entries(catalogs)) {
  for (const key of base?.keys() ?? []) if (!cat.has(key)) problems.push(`${lang}: missing key ${key}`);
  for (const [key, text] of cat) {
    if (base && !base.has(key)) problems.push(`${lang}: extra key ${key}`);
    if (typeof text !== "string" || text.trim() === "") problems.push(`${lang}: empty text for ${key}`);
    else if (base?.has(key) && placeholders(text) !== placeholders(base.get(key))) problems.push(`${lang}: placeholders differ for ${key}`);
    if (lang === "pa" && typeof text === "string" && !LITERAL.has(key) && !/[਀-੿]/.test(text)) problems.push(`pa: no Gurmukhi text for ${key}`);
  }
}
if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`i18n ok: ${Object.keys(catalogs).join(", ")}; ${base.size} keys each`);
