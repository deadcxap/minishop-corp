import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright-core";

const sessions = JSON.parse(await readFile(".local/runtime/core-ui-sessions.json", "utf8"));
const base = process.env.CORP_CORE_URL;
const browser = await chromium.launch();
let passed = 0;
try {
  for (const audience of ["customer", "admin"]) {
    for (const language of ["ru", "en"]) {
      for (const width of [375, 1280]) {
        const context = await browser.newContext({ viewport: { width, height: 850 } });
        await context.addCookies([
          { name: "rw_webapp_session", value: sessions[`${audience}-${language}-${width}`], url: base, httpOnly: true },
          // Complete synthetic login cookies on this HTTP-only local stand. Core's
          // real login sets both as Secure; those providers are outside this test.
          { name: "rw_webapp_csrf", value: randomUUID(), url: base },
          { name: "minishop_lang", value: language, url: base },
        ]);
        const page = await context.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(base + (audience === "admin" ? "/admin/corporate-contracts" : "/home") + `?lang=${language}`);
        const label = audience === "admin"
          ? (language === "ru" ? "Создать контракт" : "Create contract")
          : (language === "ru" ? "Проверить код" : "Check code");
        try {
          await page.locator('.minishop-corp[aria-busy="false"]').first().waitFor({ timeout: 30000 });
          await page.getByRole("button", { name: label, exact: true }).waitFor({ timeout: 30000 });
        } catch (error) {
          await page.screenshot({ path: `.local/screenshots/core-failed-${audience}-${language}-${width}.png`, fullPage: true });
          console.error((await page.locator('body').innerText()).slice(0, 1200));
          throw error;
        }
        assert.deepEqual(errors, []);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.locator('.minishop-corp').first().scrollIntoViewIfNeeded();
        await page.screenshot({ path: `.local/screenshots/core-${audience}-${language}-${width}.png` });
        if (audience === "customer") {
          const code = "CORP-" + "0".repeat(32); // Explicitly invalid synthetic code.
          let previews = 0;
          page.on("request", (request) => {
            if (request.url().endsWith("/minishop-corp/invitations/preview")) previews++;
          });
          await page.goto(base + "/extensions/minishop-corp/corporate-home#corp_code=" + code);
          const input = page.locator(".minishop-corp input");
          try {
            await input.waitFor();
          } catch (error) {
            await page.screenshot({ path: `.local/screenshots/core-link-failed-${language}-${width}.png`, fullPage: true });
            console.error((await page.locator('body').innerText()).slice(0, 1200));
            throw error;
          }
          assert.equal(await input.inputValue(), code);
          assert.equal(previews, 0, "Opening a link must not submit it");
          const [response] = await Promise.all([
            page.waitForResponse((reply) => reply.url().endsWith("/minishop-corp/invitations/preview")),
            page.getByRole("button", { name: label, exact: true }).click(),
          ]);
          assert.ok([400, 429].includes(response.status()), `${response.status()} ${await response.text()}`);
          assert.equal((await response.json()).ok, false);
          await page.locator(".minishop-corp [role=alert]").waitFor();
          assert.equal(await input.inputValue(), code);
          assert.deepEqual(errors, []);
        }
        await context.close();
        passed++;
      }
    }
  }
} finally {
  await browser.close();
}
console.log(`PASS: ${passed} full Minishop shell cases with real APIs and signed plugin assets`);
