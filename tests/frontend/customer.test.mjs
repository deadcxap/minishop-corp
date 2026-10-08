import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as customer from "../../frontend/dist/customer/index.js";
import { customerFixture, CODE } from "../fixtures/customer-api.mjs";

const flush = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
function setup(t, viewName = "corporate-card", url = "https://fixture.invalid/") {
  t.mock.timers.enable({ apis: ["Date", "setInterval"], now: Date.parse("2028-01-01T00:00:00Z") });
  const window = new Window({ url });
  globalThis.window = window; globalThis.document = window.document;
  for (const key of ["HTMLElement", "HTMLInputElement"]) globalThis[key] = window[key];
  const target = document.createElement("main"); document.body.append(target);
  const api = customerFixture(), signals = [], navigation = [];
  const props = { language: "en", host: { version: 1, request: (path, options) => { signals.push(options.signal); return api.hostRequest(path, options); },
    navigate: (view) => navigation.push(view), navigateSection: (section) => navigation.push(section) } };
  const view = customer.mountView(viewName, target, props);
  t.after(() => { customer.unmountView(view); window.happyDOM.abort(); });
  const click = async (text, root = target) => { const node = [...root.querySelectorAll("button")].find((b) => b.textContent === text && !b.closest("[hidden]")); assert.ok(node, text); node.click(); await flush(); };
  const fill = (code) => { const node = target.querySelector('[name="corporate_code"]'); node.value = code; node.dispatchEvent(new window.Event("input", { bubbles: true })); };
  const submit = async () => { target.querySelector("form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await flush(); };
  const tick = async (ms) => { t.mock.timers.tick(ms); await flush(); };
  const preview = async () => { await flush(); fill(CODE); await submit(); };
  const join = async () => { await preview(); await tick(61000); await click("Join group"); await click("Confirm", target.querySelector("dialog")); };
  return { api, target, props, view, click, fill, submit, tick, preview, join, signals, navigation };
}

test("home card without a subscription prefills a link, preserves input on host updates and never joins automatically", async (t) => {
  const { api, target, props, view } = setup(t, "corporate-card", `https://fixture.invalid/#corp_code=${CODE}`); await flush();
  assert.equal(target.querySelector('input').value, CODE); assert.ok(api.calls.every((c) => c.method === "GET"));
  customer.updateView(view, { ...props, language: "ru" });
  assert.equal(target.querySelector('input').value, CODE); assert.match(target.textContent, /Проверить код/);
});
test("Telegram start parameter only prefills the code", async (t) => {
  const { target } = setup(t, "corporate-home", `https://fixture.invalid/?startapp=corp_${"0".repeat(32)}`); await flush();
  assert.equal(target.querySelector('input').value, CODE);
});
test("foreign and malformed link parameters do not fill or submit a corporate invitation", async (t) => {
  const { target, api } = setup(t, "corporate-card", "https://fixture.invalid/?startapp=gift_secret#corp_code=%3Cscript%3E"); await flush();
  assert.equal(target.querySelector('input').value, ""); assert.ok(api.calls.every((c) => c.method === "GET"));
});
test("preview enforces the shared confirmation delay and explains replacement before accepting", async (t) => {
  const { api, target, click, tick, preview } = setup(t); await preview();
  assert.match(target.textContent, /Paid days are not added or restored/);
  assert.equal(target.querySelector('[data-attempt="confirm"]').disabled, true);
  await click("Join group"); assert.equal(api.operations.length, 0);
  await tick(61000); await click("Join group"); await click("Cancel", target.querySelector("dialog"));
  assert.equal(api.operations.length, 0);
  await click("Join group"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.operations.length, 1); assert.match(target.textContent, /still being applied/);
  assert.equal(target.querySelector('[name="corporate_code"]'), null);
});
test("lost confirmation response retries the identical intent and membership refresh recovers success", async (t) => {
  const { api, target, click, preview, tick } = setup(t); await preview(); await tick(61000);
  api.loseNextWrite = true; await click("Join group"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /request may have completed|request might have completed|may already have completed/i);
  await click("Retry the same request");
  const calls = api.calls.filter((c) => c.path === "/membership/confirm"); assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, calls[1].body);
  api.complete(); await tick(6000); assert.match(target.textContent, /Leave group/); assert.equal(api.operations.length, 1);
});
test("nested host 429 payload keeps the hourly cooldown and no extra code checks run", async (t) => {
  const { api, target, fill, submit, tick } = setup(t); await flush();
  api.previewError = { code: "minishop_corp_code_throttled", status: 429, wait: 3600 }; fill(CODE); await submit();
  assert.match(target.textContent, /3600 sec/); assert.equal(target.querySelector('[data-attempt]').disabled, true);
  await tick(60000); await submit(); assert.equal(api.calls.filter((c) => c.method === "POST").length, 1);
  assert.match(target.textContent, /3540 sec/);
});
test("invalid code and changed offer have localized errors without silently accepting a new version", async (t) => {
  const { api, target, fill, submit, tick, click } = setup(t); await flush(); fill("invalid"); await submit();
  assert.match(target.textContent, /code is unavailable/i); assert.equal(target.querySelector('input').value, "invalid");
  await tick(61000); fill(CODE); await submit(); await tick(61000); api.confirmError = "minishop_corp_offer_changed";
  await click("Join group"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /changed/); assert.ok(target.querySelector('input')); assert.equal(api.operations.length, 0);
});
test("Q-03 keeps the offer and instructs native billing cleanup without cancelling anything", async (t) => {
  const { api, target, preview, tick, click } = setup(t); await preview(); await tick(61000); api.confirmError = "minishop_corp_renewal_policy_required";
  await click("Join group"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /auto-renewal/i); assert.equal(api.operations.length, 0); assert.ok(target.querySelector('[data-attempt="confirm"]').disabled);
});
for (const trial of [true, false]) test(`expired membership can leave and shows confirmed ${trial ? "trial" : "disabled"} result`, async (t) => {
  const { api, target, join, click, tick, navigation } = setup(t); await join(); api.complete(); api.trial = trial;
  api.base.contracts[0].ends_at = "2020-01-01T00:00:00Z"; await tick(6000); assert.match(target.textContent, /expired/);
  await click("Leave group"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /still being updated/); assert.doesNotMatch(target.textContent, /On departure, a standard trial was granted/);
  api.complete(); await tick(6000); assert.match(target.textContent, trial ? /standard trial was granted/ : /without a trial/);
  await click("Go to personal subscription"); assert.deepEqual(navigation, ["home"]);
});
test("manager controls use only scoped routes, preserve the rotation limit and handle revoked access", async (t) => {
  const { api, target, click } = setup(t, "corporate-manager"); api.manager = true; api.seedCode(); await flush(); await click("Refresh");
  await click(api.base.contracts[0].name); assert.match(target.textContent, /No Telegram account/);
  await click("Invitations"); assert.match(target.textContent, /4 \/ 12/); await click("Rotate code"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.base.invitations[1].use_limit, 12); assert.ok(target.querySelector('[name="code"]'));
  api.manager = false; await click("Refresh"); assert.equal(target.querySelector('[name="code"]'), null); assert.match(target.textContent, /revoked/);
  assert.ok(api.calls.every((c) => c.path.startsWith("/managed-contracts")));
});
test("manager without assignments has an empty view and unmount aborts requests and polling", async (t) => {
  const { target, view, api, tick, signals } = setup(t, "corporate-manager"); await flush(); assert.match(target.textContent, /No corporate contracts/);
  const sibling = document.createElement("aside"); target.append(sibling); customer.unmountView(view); const count = api.calls.length;
  await tick(60000); assert.equal(api.calls.length, count); assert.ok(signals.every((s) => s.aborted)); assert.deepEqual([...target.children], [sibling]);
});
test("lost departure response retries the same UUID and shows no trial before completion", async (t) => {
  const { api, target, join, tick, click } = setup(t); await join(); api.complete(); await tick(6000);
  api.loseNextWrite = true; await click("Leave group"); await click("Confirm", target.querySelector("dialog"));
  await click("Retry the same request"); const calls = api.calls.filter((c) => c.path.endsWith("/leave"));
  assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, calls[1].body); assert.equal(api.operations.length, 2);
  assert.doesNotMatch(target.textContent, /standard trial was granted/); api.complete(); await tick(6000);
  assert.match(target.textContent, /standard trial was granted/);
});
test("manager rotation recovers a lost response without minting a second code", async (t) => {
  const { api, target, click } = setup(t, "corporate-manager"); api.manager = true; api.seedCode(); await flush(); await click("Refresh");
  await click(api.base.contracts[0].name); await click("Invitations"); api.loseNextWrite = true;
  await click("Rotate code"); await click("Confirm", target.querySelector("dialog")); await click("Retry the same request");
  assert.equal(api.base.invitations.length, 2); assert.equal(target.querySelector('[name="code"]'), null);
  const calls = api.calls.filter((c) => c.path.endsWith("/rotate")); assert.deepEqual(calls[0].body, calls[1].body);
  assert.match(target.textContent, /code cannot be shown again/);
});
