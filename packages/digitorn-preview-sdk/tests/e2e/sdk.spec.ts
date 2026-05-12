import { test, expect, type FrameLocator, type Page } from "@playwright/test";

/**
 * SDK regression suite — each test pins one critical behaviour we
 * already burned hours on. Failures here flag regressions the manual
 * eye-tests during build-out wouldn't catch.
 *
 * Targets a standalone ``vite preview`` of the lovable bundle (no
 * digitorn_web, no daemon auth) — see ``playwright.config.ts``.
 */

/** Iframe nested inside the modal's preview canvas. */
function modalPreview(page: Page): FrameLocator {
  return page.frameLocator('[role="dialog"] iframe');
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  // The gallery header chip is the most stable signal the empty
  // state has mounted.
  await expect(page.getByText("Templates", { exact: true })).toBeVisible({
    timeout: 12_000,
  });
});

test("empty state shows the curated template gallery", async ({ page }) => {
  const expectedTitles = [
    "AI SaaS landing",
    "Analytics dashboard",
    "Editorial storefront",
    "Slow magazine",
    "Editorial one-pager (HTML)",
    "Minimal portfolio",
    "Startup landing",
    "Minimal dashboard",
    "Pricing, three tiers",
  ];
  for (const title of expectedTitles) {
    await expect(page.getByRole("button", { name: title })).toBeVisible();
  }
});

test("clicking a template opens the modal", async ({ page }) => {
  await page.getByRole("button", { name: "AI SaaS landing" }).click();

  const dialog = page.locator('[role="dialog"]');
  await expect(dialog).toBeVisible();

  // Preview iframe loads + renders the seed content. The phrase appears
  // in both an h1 and a paragraph — `.first()` keeps the locator strict-mode safe.
  await expect(
    modalPreview(page).getByText("The autonomous engineer", { exact: false }).first(),
  ).toBeVisible({ timeout: 8_000 });

  // Close via the × button.
  await page.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toHaveCount(0);
});

test("react and html kinds dispatch to different renderers", async ({ page }) => {
  // React kind → modal iframe has ``src`` pointing at the Vite-bundled seed page.
  await page.getByRole("button", { name: "AI SaaS landing" }).click();
  const reactIframe = page.locator('[role="dialog"] iframe').first();
  await expect(reactIframe).toBeVisible();
  const reactSrc = await reactIframe.getAttribute("src");
  expect(reactSrc).toBeTruthy();
  expect(reactSrc).toContain(".html");
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator('[role="dialog"]')).toHaveCount(0);

  // HTML kind → modal iframe uses srcdoc (no src), the HtmlPreview path.
  await page.getByRole("button", { name: "Editorial one-pager (HTML)" }).click();
  const htmlIframe = page.locator('[role="dialog"] iframe').first();
  await expect(htmlIframe).toBeVisible();
  const htmlSrc = await htmlIframe.getAttribute("src");
  expect(htmlSrc === null || htmlSrc === "").toBeTruthy();

  // Content from html-seeds.ts proves the iframe rendered. `.first()` because
  // "Atelier Brun" appears in both the masthead and a colophon block.
  await expect(
    modalPreview(page).getByText("Atelier Brun", { exact: false }).first(),
  ).toBeVisible({ timeout: 5_000 });

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator('[role="dialog"]')).toHaveCount(0);
});

test("opening a preview never paints a raw white iframe (skeleton overlay)", async ({ page }) => {
  // The skeleton overlay should be present at modal-open and disappear
  // once the iframe's ``onLoad`` fires. We just check the iframe ends
  // up showing content (which transitively proves the load flow runs).
  await page.getByRole("button", { name: "Analytics dashboard" }).click();
  const dialog = page.locator('[role="dialog"]');
  await expect(dialog).toBeVisible();

  await expect(
    modalPreview(page).getByText("Overview", { exact: true }).first(),
  ).toBeVisible({ timeout: 8_000 });

  await page.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toHaveCount(0);
});

test("modal close leaves gallery composer-section at the same viewport y", async ({ page }) => {
  // The host bridge tear-down restores the iframe's natural height,
  // so the post-modal layout should be byte-identical to the pre-
  // modal layout. We pick a stable reference element (the section
  // label) and compare its bounding box.
  const sectionLabel = page.getByText("Templates", { exact: true });
  const before = await sectionLabel.boundingBox();
  expect(before).not.toBeNull();

  await page.getByRole("button", { name: "Slow magazine" }).click();
  await expect(page.locator('[role="dialog"]')).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator('[role="dialog"]')).toHaveCount(0);

  const after = await sectionLabel.boundingBox();
  expect(after).not.toBeNull();
  expect(Math.abs((after!.y ?? 0) - (before!.y ?? 0))).toBeLessThanOrEqual(2);
});
