import { expect, test } from "@playwright/test";
import { NEW_PASSWORD, createFpo, expectNoSidewaysScroll, firstSignIn, signIn, t } from "./helpers";

// Every page in the menu, with the heading it must show (English catalog, the same text the site uses)
const PAGES: [string, string][] = [
  ["/en", "Welcome"],
  ["/en/members", t.members.title],
  ["/en/lots", t.lots.title],
  ["/en/prices", t.prices.title],
  ["/en/models", t.models.title],
  ["/en/plans", t.plans.title],
  ["/en/contacts", t.contacts.title],
  ["/en/messages", t.messages.title],
  ["/en/sales", t.sales.title],
  ["/en/devices", t.devices.title],
  ["/en/users", t.users.title],
  ["/en/status", t.app.name], // public page: the brand is its h1, "System status" its section
];

test("a wrong password is refused with a message and no session", async ({ page }) => {
  const { username } = createFpo("wrong");
  await signIn(page, username, "not the password at all");
  await expect(page.getByText(t.signIn.failed)).toBeVisible();
  await page.goto("/en/members");
  await expect(page).toHaveURL(/\/en\/sign-in$/);
});

test("first sign-in forces a new password; every page loads without errors or sideways scroll", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`${page.url()}: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`${page.url()}: ${m.text()}`);
  });
  await firstSignIn(page, "pages");
  for (const [path, heading] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }), path).toContainText(heading);
    await expect(page.locator("footer.build"), path).toContainText("Build ");
    await expectNoSidewaysScroll(page);
  }
  expect(errors).toEqual([]);
});

test("add a member, register a lot for them, and see its exact stock", async ({ page }) => {
  await firstSignIn(page, "lots");
  const name = `Fictional Farmer ${Date.now() % 100000}`;
  await page.goto("/en/members");
  await page.getByLabel(t.members.name).fill(name);
  await page.getByLabel(t.members.village).fill("Fictional village");
  await page.getByRole("button", { name: t.members.submit }).click();
  await expect(page.getByRole("cell", { name })).toBeVisible();
  await expectNoSidewaysScroll(page);

  await page.goto("/en/lots");
  await page.getByLabel(t.lots.member).selectOption({ label: name });
  const commodity = page.getByLabel(t.lots.commodity);
  const first = await commodity.locator("option:not([value=''])").first().getAttribute("value");
  expect(first, "a commodity must exist (seed the fixture prices first)").toBeTruthy();
  await commodity.selectOption(first!);
  await page.getByLabel(t.lots.quantity).fill("1250.000");
  await page.getByRole("button", { name: t.lots.submit }).click();
  const row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toContainText("1250.000");
  await expectNoSidewaysScroll(page);
});

test("switch to Punjabi and back, then sign out", async ({ page }) => {
  await firstSignIn(page, "lang");
  await page.getByRole("button", { name: /ਪੰਜਾਬੀ/ }).first().click();
  await expect(page).toHaveURL(/\/pa(\/|$)/);
  await expect(page.locator("html")).toHaveAttribute("lang", "pa");
  await expect(page.locator("footer.build")).toContainText("ਬਿਲਡ");
  await expectNoSidewaysScroll(page);
  await page.getByRole("button", { name: "English" }).first().click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.getByRole("button", { name: t.shell.signOut }).click();
  await expect(page).toHaveURL(/\/en\/sign-in$/);
  await page.goto("/en/lots");
  await expect(page).toHaveURL(/\/en\/sign-in$/);
});

test("the new password works for the next sign-in", async ({ page }) => {
  const username = await firstSignIn(page, "again");
  await page.getByRole("button", { name: t.shell.signOut }).click();
  await signIn(page, username, NEW_PASSWORD);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Welcome");
});
