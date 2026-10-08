import assert from "node:assert/strict";
import { chromium } from "playwright-core";
import { adminCases } from "./admin.mjs";

const browser = await chromium.launch();
let passed = 0;
try {
  for (const audience of ["customer", "admin"]) {
    for (const language of ["ru", "en"]) {
      for (const width of [375, 1280]) {
        for (const theme of ["light", "dark"]) {
          const page = await browser.newPage({ viewport: { width, height: 800 } });
          const errors = [];
          page.on("pageerror", (error) => errors.push(error.message));
          await page.goto(`${process.env.CORP_PREVIEW_URL}/?${new URLSearchParams({ audience, language, theme })}`);
          await page.locator('.minishop-corp[aria-busy="false"]').waitFor();
          if (audience === "customer") {
            const message = await page.locator(".minishop-corp [role=status]").innerText();
            assert.match(message, language === "ru" ? /скоро появится/ : /will be available/);
          } else {
            assert.equal(await page.getByRole("button", { name: language === "ru" ? "Создать контракт" : "Create contract", exact: true }).isVisible(), true);
            assert.equal(await page.locator("[role=alert]").count(), 0);
          }
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
          assert.deepEqual(errors, []);
          await page.screenshot({ path: `.local/screenshots/${audience}-${language}-${width}-${theme}.png` });
          await page.close();
          passed++;
        }
      }
    }
  }
  passed += await adminCases(browser);
} finally {
  await browser.close();
}
console.log(`PASS: ${passed} browser cases (native host smoke and admin workflows; RU/EN, mobile/desktop, light/dark)`);
