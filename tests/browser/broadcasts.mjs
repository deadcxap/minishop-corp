import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright-core";

const sessions = JSON.parse(await readFile(".local/runtime/core-ui-sessions.json", "utf8"));
const base = process.env.CORP_CORE_URL;
const browser = await chromium.launch();
let passed = 0;
try {
  for (const language of ["ru", "en"]) for (const width of [375, 1280]) {
    const csrf = randomUUID();
    const context = await browser.newContext({ viewport: { width, height: 950 } });
    await context.addCookies([
      { name: "rw_webapp_session", value: sessions[`admin-${language}-${width}`], url: base, httpOnly: true },
      { name: "rw_webapp_csrf", value: csrf, url: base },
      { name: "minishop_lang", value: language, url: base },
    ]);
    const headers = { "X-CSRF-Token": csrf, Origin: base };
    const choices = await (await context.request.get(base + "/api/admin/minishop-corp/options/tariffs")).json();
    const tariff = choices.tariffs[0]?.key; assert.ok(tariff);
    const allIds = Array.from({length: 12}, () => randomUUID()), ids = allIds.slice(-2);
    for (const [index, id] of allIds.entries()) {
      const reply = await context.request.post(base + "/api/admin/minishop-corp/contracts", { headers, data: {
        id, name: `Broadcast ${language} ${width} ${index + 1}`, tariff_key: tariff, manager_user_id: null,
        ends_at: new Date(Date.now() + 86400000 * 30).toISOString(),
      }});
      assert.equal(reply.status(), 201, await reply.text());
    }
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/admin/broadcast?lang=${language}`);
    const slot = page.locator(".corp-broadcast");
    const choose = () => slot.getByRole("button", { name: language === "ru" ? "Выбрать подписки" : "Choose subscriptions", exact: true });
    await choose().waitFor();
    assert.ok((await slot.boundingBox()).height <= 64, "The collapsed picker must remain a single compact row");
    await page.screenshot({path: `.local/screenshots/broadcast-collapsed-${language}-${width}.png`});
    await choose().click();
    await page.locator('.corp-broadcast[aria-busy="false"] form').waitFor();
    for (const id of ids) {
      // Other full-shell scenarios may have created more than one page of subscriptions.
      while (!await slot.locator(`input[value="${id}"]`).count()) {
        await slot.getByRole("button", { name: language === "ru" ? "Загрузить ещё" : "Load more", exact: true }).click();
        await page.locator('.corp-broadcast[aria-busy="false"]').waitFor();
      }
      const checkbox = slot.locator(`input[value="${id}"]`);
      await checkbox.scrollIntoViewIfNeeded();
      const scroll = await slot.locator(".corp-broadcast-options").evaluate(node => node.scrollTop);
      await checkbox.check();
      assert.ok(Math.abs(await slot.locator(".corp-broadcast-options").evaluate(node => node.scrollTop) - scroll) <= 1, "Selection must not reset the scroll position");
    }
    assert.ok((await slot.locator(".corp-broadcast-options").boundingBox()).height <= 222);
    await slot.getByRole("button", { name: language === "ru" ? "Скрыть выбор" : "Hide selection", exact: true }).click();
    assert.equal(await slot.locator("input:visible").count(), 0);
    await choose().focus(); await page.keyboard.press("Enter");
    assert.equal(await slot.locator("input:checked").count(), 2);
    await slot.getByRole("button", { name: language === "ru" ? "Подготовить аудиторию" : "Prepare audience", exact: true }).click();
    const link = slot.getByRole("link", { name: language === "ru" ? "Открыть редактор рассылки" : "Open broadcast editor", exact: true });
    await link.waitFor();
    const href = await link.getAttribute("href"), target = new URL(href, base).searchParams.get("broadcast_target");
    assert.match(target, /^minishop-corp\.selection:[a-f0-9]{32}$/);
    assert.equal(await page.locator(".minishop-corp").evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 2)), true);
    await page.screenshot({path: `.local/screenshots/broadcast-selection-${language}-${width}.png`, fullPage: true});
    const next = context.waitForEvent("page"); await link.click(); const editor = await next;
    editor.on("pageerror", error => errors.push(error.message));
    await editor.waitForLoadState("networkidle");
    assert.equal(await editor.evaluate(() => window.opener), null);
    assert.equal(new URL(editor.url()).searchParams.get("broadcast_target"), target);
    const form = editor.locator(".admin-form").filter({has: editor.locator(".broadcast-setup-grid")});
    assert.match(await form.locator(".broadcast-audience-control").innerText(), new RegExp(`Broadcast ${language} ${width}`));
    await form.locator('[contenteditable="true"]').first().fill("Synthetic corporate broadcast — not sent");
    await form.locator('.broadcast-schedule-toggle [role="checkbox"]').click();
    const response = editor.waitForResponse(reply => new URL(reply.url()).pathname === "/api/admin/broadcast" && reply.request().method() === "POST");
    await form.locator("button").filter({has: editor.locator('svg')}).last().click();
    const created = await response;
    assert.equal(created.status(), 200, await created.text());
    const payload = await created.json();
    assert.equal(payload.broadcast.target, target); assert.equal(payload.broadcast.status, "scheduled");
    assert.equal((await context.request.delete(base + `/api/admin/broadcasts/${payload.broadcast.broadcast_id}`, {headers})).status(), 200);
    await editor.screenshot({path: `.local/screenshots/broadcast-editor-${language}-${width}.png`, fullPage: true});
    // A saved target stays selected after one of its contracts is deleted.
    assert.equal((await context.request.delete(base + `/api/admin/minishop-corp/contracts/${ids[0]}`, {headers})).status(), 200);
    const stale = await context.newPage();
    await stale.goto(base + href);
    await stale.waitForLoadState("networkidle");
    assert.equal(new URL(stale.url()).searchParams.get("broadcast_target"), target);
    const unavailable = await context.request.post(base + "/api/admin/broadcast", {headers, data: {
      target, text: "Synthetic", channels: ["telegram"], scheduled_at: new Date(Date.now() + 3600000).toISOString(),
    }});
    assert.equal(unavailable.status(), 400); assert.equal((await unavailable.json()).error, "invalid_audience");
    assert.equal((await context.request.delete(base + `/api/admin/minishop-corp/contracts/${ids[1]}`, {headers})).status(), 200);
    for (const id of allIds.slice(0, -2)) assert.equal((await context.request.delete(base + `/api/admin/minishop-corp/contracts/${id}`, {headers})).status(), 200);
    assert.deepEqual(errors, []); await context.close(); passed++;
  }
  console.log(`PASS: ${passed} native corporate broadcast scenarios (RU/EN, mobile/desktop)`);
} finally { await browser.close(); }
