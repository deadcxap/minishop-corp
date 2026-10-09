import assert from "node:assert/strict";
import en from "../../locales/en.json" with { type: "json" };
import ru from "../../locales/ru.json" with { type: "json" };
import { fixture, CONTRACT_ID, SQUAD_ID } from "../fixtures/admin-api.mjs";

export async function adminCases(browser) {
  let passed = 0;
  for (const language of ["ru", "en"]) for (const width of [375, 1280]) for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, locale: language === "ru" ? "ru-RU" : "en-US" });
    const api = fixture(), errors = [];
    const t = (key) => (language === "ru" ? ru : en)[`admin_minishop_corp_${key}`];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.context().addCookies([{ name: "rw_webapp_csrf", value: "synthetic-browser-csrf", url: process.env.CORP_PREVIEW_URL }]);
    await page.route("**/api/admin/minishop-corp/**", async (route) => {
      const req = route.request(), method = req.method();
      if (method !== "GET") assert.equal(req.headers()["x-csrf-token"], "synthetic-browser-csrf");
      const result = api.respond(req.url(), method, method === "GET" ? undefined : req.postDataJSON());
      await route.fulfill({ status: result.status, contentType: "application/json", body: JSON.stringify(result.payload) });
    });
    const fit = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const snap = async (name) => { await fit(); await page.screenshot({ path: `.local/screenshots/admin-${name}-${language}-${width}-${theme}.png`, fullPage: true }); };
    const button = (key, root = page) => root.getByRole("button", { name: t(key), exact: true });
    const tab = (key) => page.getByRole("tab", { name: t(key), exact: true });
    const write = async (control) => {
      const response = page.waitForResponse((r) => r.url().includes("/api/admin/minishop-corp/") && r.request().method() !== "GET");
      await control.click(); await response;
    };
    const ready = async () => { await page.locator('.corp-panel:visible[aria-busy="false"]').waitFor(); };
    await page.goto(`${process.env.CORP_PREVIEW_URL}/?${new URLSearchParams({ audience: "admin", language, theme })}`);
    await page.locator(`[data-contract="${CONTRACT_ID}"]`).waitFor(); await snap("list");
    await page.getByRole("button", { name: api.contracts[0].name, exact: true }).click(); await ready();
    await page.locator('[name="name"]').fill("Updated corporate fixture");
    await button("save").click(); await page.getByRole("dialog").waitFor(); await snap("confirmation");
    await write(button("confirm", page.getByRole("dialog"))); await ready();
    assert.equal(api.contracts[0].version, 2); await snap("terms");
    api.contracts[0].manager_user_id = null;
    await button("reload_saved").click(); await ready();
    assert.ok(await page.getByText(t("manager_missing"), { exact: true }).isVisible());
    await snap("manager-deleted");
    await page.locator('[name="name"]').fill("Contract without a manager");
    await button("save").click(); await write(button("confirm", page.getByRole("dialog"))); await ready();
    assert.equal(api.contracts[0].manager_user_id, null);
    await page.locator('[name="manager"]').selectOption("910011");
    await button("save").click(); await write(button("confirm", page.getByRole("dialog"))); await ready();
    assert.equal(api.contracts[0].manager_user_id, 910011);
    // Keyboard navigation activates the next registered tab and loads its content.
    await tab("terms").focus(); await page.keyboard.press("ArrowRight"); await ready();
    assert.equal(await tab("members").getAttribute("aria-selected"), "true");
    await page.locator(".corp-member img").waitFor(); await snap("members");
    assert.equal(await page.locator(".corp-member script").count(), 0);
    assert.match(await page.locator(".corp-member").last().innerText(), language === "ru" ? /Нет данных/ : /No data/);
    await button("exclude").first().click(); await write(button("confirm", page.getByRole("dialog"))); await ready();
    assert.equal(api.members.length, 2); assert.equal(api.members[0].state, "leaving");
    await tab("invitations").click(); await ready();
    await write(button("generate")); await ready(); assert.equal(api.invitations[0].kind, "single");
    await button("revoke").click(); await write(button("confirm", page.getByRole("dialog"))); await ready();
    await page.locator('[name="kind"]').selectOption("reusable"); await page.locator('[name="use_limit"]').fill("9");
    await write(button("generate")); await ready(); await snap("invitations");
    assert.equal(api.invitations[1].use_limit, 9);
    await button("rotate").click(); await write(button("confirm", page.getByRole("dialog"))); await ready();
    assert.ok(api.invitations[1].revoked_at); assert.equal(api.invitations[2].use_limit, 9);
    await tab("sync").click(); await ready(); await snap("sync");
    assert.ok(await page.getByText((language === "ru" ? ru : en).minishop_corp_panel_unconfirmed, { exact: true }).isVisible());
    await button("back").click(); await button("new_contract").click(); await ready();
    await page.locator('[name="name"]').fill("New fixture from browser");
    await page.locator('[name="tariff"]').selectOption("corp");
    await page.locator('[name="ends_at"]').fill("2031-12-15T12:00");
    await page.locator('[name="squad"]').fill(SQUAD_ID); await button("verify_squad").click(); await ready();
    await page.locator('[name="manager"]').selectOption("910011"); await write(button("save")); await ready();
    assert.equal(api.contracts.length, 2); assert.equal(api.contracts[1].ends_at, "2031-12-15T12:00:00.000Z");
    assert.deepEqual(errors, []); await fit(); await page.close(); passed++;
  }
  return passed;
}
