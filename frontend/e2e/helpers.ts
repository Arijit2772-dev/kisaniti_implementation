import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page } from "@playwright/test";

// Playwright runs from frontend/; the English catalog gives the exact texts the site shows
export const t: typeof import("../src/i18n/dictionaries/en.json") = JSON.parse(readFileSync(resolve("src/i18n/dictionaries/en.json"), "utf8"));
export const NEW_PASSWORD = "browser test passphrase plain words";
const REPO = resolve("..");

/** A fictional FPO and its admin, made with the operator command; returns the one-time password. */
export function createFpo(label: string): { username: string; temporary: string } {
  const suf = randomBytes(4).toString("hex");
  const username = `e2e.${label}.${suf}`;
  const out = execFileSync(
    "docker",
    ["compose", "exec", "-T", "backend", "python", "-m", "kisaniti_api.access.cli", "create-fpo", "--name", `Browser test ${label} (fictional) ${suf}`, "--admin-username", username, "--synthetic"],
    { cwd: REPO, encoding: "utf8" },
  );
  return { username, temporary: JSON.parse(out).temporary_password as string };
}

export async function signIn(page: Page, username: string, password: string) {
  await page.goto("/en/sign-in");
  await page.getByLabel(t.signIn.username).fill(username);
  await page.getByLabel(t.signIn.password).fill(password);
  await page.getByRole("button", { name: t.signIn.submit }).click();
}

/** First sign-in of a new admin: the site insists on a new password, then lands on the home page. */
export async function firstSignIn(page: Page, label: string): Promise<string> {
  const { username, temporary } = createFpo(label);
  await signIn(page, username, temporary);
  await expect(page).toHaveURL(/\/en\/change-password$/);
  await page.getByLabel(t.changePassword.current).fill(temporary);
  await page.getByLabel(new RegExp(`^${t.changePassword.new}`)).fill(NEW_PASSWORD); // the label also holds the hint
  await page.getByLabel(t.changePassword.confirm).fill(NEW_PASSWORD);
  await page.getByRole("button", { name: t.changePassword.submit }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Welcome");
  return username;
}

/** Nothing wider than the screen: on a phone the page must not scroll sideways. */
export async function expectNoSidewaysScroll(page: Page) {
  const { scroll, width } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: window.innerWidth }));
  expect(scroll, `page is ${scroll}px wide on a ${width}px screen`).toBeLessThanOrEqual(width);
}
