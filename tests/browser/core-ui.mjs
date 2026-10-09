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
        if (audience === "admin") {
          await page.getByRole("button", { name: label, exact: true }).click();
          const name = page.locator('.minishop-corp [name="name"]');
          await name.waitFor();
          const managerId = sessions[`${audience}-${language}-${width}-manager`];
          const choices = await (await page.request.get(base + `/api/admin/minishop-corp/options/accounts?user_id=${managerId}`)).json();
          const manager = choices.accounts[0];
          assert.notEqual(manager.telegram_id, manager.user_id);
          for (const identifier of [manager.minishop_id, String(manager.telegram_id)]) {
            await page.locator('[name="manager"]').fill(identifier);
            const found = page.waitForResponse((reply) => reply.url().includes("/options/accounts?q="));
            await page.getByRole("button", { name: language === "ru" ? "Найти аккаунт" : "Find account", exact: true }).click();
            const reply = await found; assert.equal(reply.status(), 200);
            assert.equal((await reply.json()).accounts[0].user_id, manager.user_id);
            assert.match(reply.headers()["x-corp-request-id"], /^[a-f0-9]{32}$/);
            await page.getByRole("button", { name: language === "ru" ? "Подтвердить управляющего" : "Confirm manager", exact: true }).click();
          }
          await page.getByRole("button", { name: language === "ru" ? "Не назначать управляющего" : "Leave without a manager", exact: true }).click();
          await name.fill("Full-shell retained draft");
          const plans = await page.locator('[name="tariff"] option').evaluateAll((rows) => rows.map((row) => row.value).filter(Boolean));
          if (plans[0]) await page.locator('[name="tariff"]').selectOption(plans[0]);
          await page.locator('[name="ends_at"]').fill("2031-10-31");
          await page.locator('[name="manager"]').fill("manager@example.invalid");
          await page.clock.setFixedTime(new Date(Date.now() + 20_000));
          const fresh = page.waitForResponse((reply) => new URL(reply.url()).pathname.endsWith("/me") && new URL(reply.url()).searchParams.has("fresh"));
          await page.evaluate(() => window.dispatchEvent(new Event("focus")));
          await fresh;
          assert.equal(await name.inputValue(), "Full-shell retained draft");
          // The user's deep URL and a real browser reload must preserve the draft.
          await page.goto(base + "/admin/corporate-contracts?plugin=minishop-corp&tab=operations");
          await name.waitFor();
          assert.equal(await name.inputValue(), "Full-shell retained draft");
          await page.reload();
          try {
            await name.waitFor();
          }
          catch (error) {
            await page.screenshot({ path: `.local/screenshots/core-draft-failed-${language}-${width}.png`, fullPage: true });
            console.error({ errors, content: (await page.locator('body').innerText()).slice(0, 1600), draft: await page.evaluate(() => sessionStorage.getItem("minishop-corp.admin-draft.v1")) });
            throw error;
          }
          assert.equal(await name.inputValue(), "Full-shell retained draft");
          assert.equal(await page.locator('[name="ends_at"]').inputValue(), "2031-10-31");
          assert.equal(await page.locator('[name="manager"]').inputValue(), "manager@example.invalid");
          if (width === 1280) {
            await page.locator('[data-admin-section="users"]').click();
            await page.locator('[data-admin-section="corporate-contracts"]').click();
            await name.waitFor();
            assert.equal(await name.inputValue(), "Full-shell retained draft");
          }
          await page.waitForFunction(() => !document.querySelector('.minishop-corp[aria-busy="true"], .minishop-corp [aria-busy="true"]'));
          assert.deepEqual(errors, []);
          await page.screenshot({ path: `.local/screenshots/core-draft-${language}-${width}.png`, fullPage: true });
        }
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
        console.log(`PASS: full shell ${audience}/${language}/${width}`);
        passed++;
      }
    }
  }
} finally {
  await browser.close();
}
console.log(`PASS: ${passed} full Minishop shell cases with real APIs and signed plugin assets`);
