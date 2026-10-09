import assert from "node:assert/strict";
import en from "../../locales/en.json" with { type: "json" };
import ru from "../../locales/ru.json" with { type: "json" };
import { customerFixture, CODE } from "../fixtures/customer-api.mjs";

export async function customerCases(browser) {
  let passed = 0;
  for (const language of ["ru", "en"]) for (const width of [375, 1280]) for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, locale: language === "ru" ? "ru-RU" : "en-US" });
    const api = customerFixture(), errors = [];
    const dictionary = language === "ru" ? ru : en;
    const t = (key) => dictionary[`wa_minishop_corp_${key}`], a = (key) => dictionary[`admin_minishop_corp_${key}`];
    const button = (name, root = page) => root.getByRole("button", { name, exact: true });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.context().addCookies([{ name: "rw_webapp_csrf", value: "synthetic-customer-csrf", url: process.env.CORP_PREVIEW_URL }]);
    await page.clock.install({ time: new Date("2028-01-01T00:00:00Z") });
    await page.route("**/api/plugins/minishop-corp/**", async (route) => {
      const req = route.request(), method = req.method();
      if (method !== "GET") assert.equal(req.headers()["x-csrf-token"], "synthetic-customer-csrf");
      const result = api.respond(req.url(), method, method === "GET" ? undefined : req.postDataJSON());
      await route.fulfill({ status: result.status, contentType: "application/json", body: JSON.stringify(result.payload) });
    });
    const ready = async () => {
      await page.locator('.minishop-corp[aria-busy="false"]').waitFor();
      await page.waitForFunction(() => !document.querySelector('.corp-panel:not([hidden])[aria-busy="true"]'));
    };
    const snap = async (name) => {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: `.local/screenshots/${name}-${language}-${width}-${theme}.png`, fullPage: true });
    };
    const write = async (control) => {
      const done = page.waitForResponse((r) => r.url().includes("/api/plugins/minishop-corp/") && r.request().method() === "POST");
      await control.click(); await done; await ready();
    };
    const base = `${process.env.CORP_PREVIEW_URL}/?${new URLSearchParams({ audience: "customer", language, theme, view: "corporate-card" })}`;
    await page.goto(`${base}#corp_code=${CODE}`); await ready();
    assert.equal(await page.locator('form').count(), 0); assert.equal(await page.locator('.minishop-corp button').count(), 1);
    await snap('settings-tile');
    const idle = api.calls.length; await page.clock.fastForward(120000); await ready(); assert.equal(api.calls.length, idle);
    await page.locator('.corp-settings-button').click(); await page.locator('[name="corporate_code"]').waitFor(); await ready();
    assert.equal(await page.locator('[name="corporate_code"]').inputValue(), CODE);
    assert.ok(api.calls.every((v) => v.method === "GET")); await snap("customer-code");
    // Keyboard submission and the native-host shape of retry_after are exercised.
    await page.locator('[name="corporate_code"]').focus(); await page.keyboard.press("Enter");
    await page.locator(".corp-offer").waitFor(); await ready();
    assert.ok(await button(t("join")).isEnabled()); await snap("customer-offer");
    assert.doesNotMatch(await page.locator('.corp-offer').innerText(), /Internal tariff|Внутренний тариф/);
    await button(t("join")).click();
    await page.getByRole("dialog").waitFor(); await snap("customer-confirmation");
    await write(button(a("confirm"), page.getByRole("dialog")));
    assert.equal(api.operations.length, 1); await page.getByText(t("joining"), { exact: true }).waitFor();
    api.complete(); await page.clock.fastForward(6000); await button(t("leave")).waitFor();
    await ready(); const active = api.calls.length; await page.clock.fastForward(120000); await ready(); assert.equal(api.calls.length, active);
    assert.equal(await button(a('refresh')).count(), 0);
    await page.goto(base); await ready(); await snap('settings-active');
    assert.ok((await page.locator('.corp-settings-tile').innerText()).includes(api.base.contracts[0].name));
    assert.equal(await page.locator('.minishop-corp button').count(), 1);
    api.base.contracts[0].ends_at = "2020-01-01T00:00:00Z";
    await page.locator('.corp-settings-button').click(); await page.getByText(t("expired_notice"), { exact: true }).waitFor(); await snap("customer-expired");
    await button(t("leave")).click(); await write(button(a("confirm"), page.getByRole("dialog")));
    api.trial = theme === "light"; api.complete(); await page.clock.fastForward(6000);
    await button(t("personal")).waitFor(); await snap("customer-departure");
    assert.equal(api.operations.length, 2); passed++;

    api.manager = true; api.seedCode(); api.base.contracts[0].ends_at = "2030-10-01T00:00:00Z";
    await page.goto(`${base.replace("view=corporate-card", "view=corporate-manager")}`); await ready();
    await button(api.base.contracts[0].name).click();
    await page.locator(".corp-member img").waitFor(); await snap("manager-members");
    await button(a("exclude")).first().click(); await write(button(a("confirm"), page.getByRole("dialog")));
    assert.equal(api.base.members.length, 2); assert.equal(api.base.members[0].state, "leaving");
    await page.getByRole("tab", { name: a("members"), exact: true }).focus(); await page.keyboard.press("ArrowRight");
    await button(a("rotate")).waitFor(); await button(a("rotate")).click(); await write(button(a("confirm"), page.getByRole("dialog")));
    await page.locator('[name="code"]').waitFor(); assert.equal(api.base.invitations[1].use_limit, 12); await snap("manager-code");
    api.manager = false; await button(a("refresh"), page.locator('[role="tabpanel"]:visible')).click();
    await page.getByText(t("manager_revoked"), { exact: true }).waitFor();
    assert.equal(await page.locator('[name="code"]').count(), 0); assert.deepEqual(errors, []);
    await snap("manager-revoked"); passed++; await page.close();
  }
  return passed;
}
