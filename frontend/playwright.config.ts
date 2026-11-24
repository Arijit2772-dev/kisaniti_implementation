// Browser tests (FULL_PRODUCT_PLAN Phase 1 item 6) against a running stack: phone (375 px) and laptop widths.
// Run from frontend/: npx playwright test        KN_E2E_URL picks the site (default http://localhost:3000).
// Each test makes its own fictional FPO with the operator command (docker compose exec backend ...).
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.KN_E2E_URL ?? "http://localhost:3000",
    ignoreHTTPSErrors: false,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "phone-375", use: { browserName: "chromium", viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true } },
    { name: "laptop-1280", use: { browserName: "chromium", viewport: { width: 1280, height: 800 } } },
  ],
});
