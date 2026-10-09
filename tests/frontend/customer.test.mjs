import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as customer from "../../frontend/dist/customer/index.js";
import { customerFixture, CODE } from "../fixtures/customer-api.mjs";

const flush = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
function setup(t, viewName = "corporate-home", url = "https://fixture.invalid/", configure = () => {}) {
  t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: Date.parse("2028-01-01T00:00:00Z") });
  const window = new Window({ url });
  globalThis.window = window; globalThis.document = window.document;
  for (const key of ["HTMLElement", "HTMLInputElement"]) globalThis[key] = window[key];
  const target = document.createElement("main"); document.body.append(target);
  const api = customerFixture(), signals = [], navigation = [];
  configure(api);
  const props = { language: "en", host: { version: 1, request: (path, options) => { signals.push(options.signal); return api.hostRequest(path, options); },
    navigate: (view) => navigation.push(view), navigateSection: (section) => navigation.push(section) } };
  const view = customer.mountView(viewName, target, props);
  t.after(() => { customer.unmountView(view); window.happyDOM.abort(); });
  const click = async (text, root = target) => { const node = [...root.querySelectorAll("button")].find((b) => b.textContent === text && !b.closest("[hidden]")); assert.ok(node, text); node.click(); await flush(); };
  const fill = (code) => { const node = target.querySelector('[name="corporate_code"]'); node.value = code; node.dispatchEvent(new window.Event("input", { bubbles: true })); };
  const submit = async () => { target.querySelector("form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await flush(); };
  const tick = async (ms) => { t.mock.timers.tick(ms); await flush(); };
  const preview = async () => { await flush(); fill(CODE); await submit(); };
  const join = async () => { await preview(); await click("Activate corporate subscription"); await click("Confirm", target.querySelector("dialog")); };
  return { api, target, props, view, click, fill, submit, tick, preview, join, signals, navigation };
}

test("settings tile is a single compact entry without forms, actions or idle requests", async (t) => {
  const { api, target, props, view, tick, navigation } = setup(t, "corporate-card"); await flush();
  assert.equal(target.querySelector('input'), null); assert.equal(target.querySelectorAll('button').length, 1);
  assert.deepEqual(api.calls.map(c => c.path), ['/membership']);
  target.querySelector('button').click(); assert.deepEqual(navigation, ['corporate-home']);
  customer.updateView(view, { ...props, language: "ru" });
  assert.match(target.textContent, /Корпоративная подписка/);
  await tick(3600000); assert.equal(api.calls.length, 1);
});
test("joined settings tile shows only the subscription name and expiry", async (t) => {
  const { api, target, tick } = setup(t, "corporate-card", undefined, api => {
    api.respond('/membership/confirm', 'POST', { request_id: 'fixture-join' }); api.complete(); api.calls.length = 0;
  }); await flush();
  assert.match(target.textContent, /Valid until/); assert.ok(target.textContent.includes(api.base.contracts[0].name));
  assert.equal(target.querySelectorAll('button').length, 1); assert.equal(target.querySelector('form'), null);
  assert.doesNotMatch(target.textContent, /Do not pay|Leave corporate|Minishop plan/);
  await tick(3600000); assert.equal(api.calls.length, 1);
});
test("subscription page prefills a link and preserves input on host language updates", async (t) => {
  const { api, target, props, view, tick } = setup(t, 'corporate-home', `https://fixture.invalid/#corp_code=${CODE}`); await flush();
  assert.equal(target.querySelector('input').value, CODE); const count = api.calls.length;
  customer.updateView(view, { ...props, language: 'ru' });
  assert.equal(target.querySelector('input').value, CODE); assert.match(target.textContent, /Проверить код/);
  await tick(3600000); assert.equal(api.calls.length, count);
});
test("Telegram start parameter only prefills the code", async (t) => {
  const { target } = setup(t, "corporate-home", `https://fixture.invalid/?startapp=corp_${"0".repeat(32)}`); await flush();
  assert.equal(target.querySelector('input').value, CODE);
});
test("opening an invitation in the already mounted page replaces only the draft and does not submit it", async (t) => {
  const { api, target, view } = setup(t); await flush();
  const count = api.calls.length;
  window.location.hash = `corp_code=${CODE}`; window.dispatchEvent(new window.Event('hashchange'));
  assert.equal(target.querySelector('input').value, CODE); assert.equal(api.calls.length, count);
  customer.unmountView(view); window.location.hash = ''; window.dispatchEvent(new window.Event('hashchange'));
  assert.equal(target.children.length, 0); assert.equal(api.calls.length, count);
});
test("foreign and malformed link parameters do not fill or submit a corporate invitation", async (t) => {
  const { target, api } = setup(t, "corporate-home", "https://fixture.invalid/?startapp=gift_secret#corp_code=%3Cscript%3E"); await flush();
  assert.equal(target.querySelector('input').value, ""); assert.ok(api.calls.every((c) => c.method === "GET"));
});
test("preview immediately enables confirmation and explains replacement before accepting", async (t) => {
  const { api, target, click, preview } = setup(t); await preview();
  assert.match(target.textContent, /Paid days are not added or restored/);
  assert.equal(target.querySelector('[data-attempt="confirm"]').disabled, false);
  assert.equal(target.querySelector('.corp-wait'), null);
  assert.equal(target.querySelector('.corp-offer').textContent.includes('Internal tariff'), false);
  await click("Activate corporate subscription"); await click("Cancel", target.querySelector("dialog"));
  assert.equal(api.operations.length, 0);
  await click("Activate corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.operations.length, 1); assert.match(target.textContent, /still being applied/);
  assert.equal(target.querySelector('[name="corporate_code"]'), null);
});
test("lost confirmation response retries the identical intent and membership refresh recovers success", async (t) => {
  const { api, target, click, preview, tick } = setup(t); await preview();
  api.loseNextWrite = true; await click("Activate corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /request may have completed|request might have completed|may already have completed/i);
  await click("Retry the same request");
  const calls = api.calls.filter((c) => c.path === "/membership/confirm"); assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, calls[1].body);
  api.complete(); await tick(6000); assert.match(target.textContent, /Leave corporate subscription/); assert.equal(api.operations.length, 1);
});
test("429 shows a static retry hint without a countdown or automatic code checks", async (t) => {
  const { api, target, fill, submit, tick } = setup(t); await flush();
  api.previewError = { code: "minishop_corp_code_throttled", status: 429, wait: 3600 }; fill(CODE); await submit();
  assert.match(target.textContent, /60 min/); assert.equal(target.querySelector('[data-attempt]').disabled, false);
  await tick(60000); assert.equal(api.calls.filter((c) => c.method === "POST").length, 1);
  assert.match(target.textContent, /60 min/); assert.equal(target.querySelector('.corp-wait'), null);
});
test("invalid code and changed offer have localized errors without silently accepting a new version", async (t) => {
  const { api, target, fill, submit, tick, click } = setup(t); await flush(); fill("invalid"); await submit();
  assert.match(target.textContent, /code is unavailable/i); assert.equal(target.querySelector('input').value, "invalid");
  fill(CODE); await submit(); api.confirmError = "minishop_corp_offer_changed";
  await click("Activate corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /changed/); assert.ok(target.querySelector('input')); assert.equal(api.operations.length, 0);
});
test("Q-03 keeps the offer and instructs native billing cleanup without cancelling anything", async (t) => {
  const { api, target, preview, tick, click } = setup(t); await preview(); api.confirmError = "minishop_corp_renewal_policy_required";
  await click("Activate corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /auto-renewal/i); assert.equal(api.operations.length, 0); assert.equal(target.querySelector('[data-attempt="confirm"]').disabled, false);
});
for (const trial of [true, false]) test(`expired membership can leave and shows confirmed ${trial ? "trial" : "disabled"} result`, async (t) => {
  const { api, target, join, click, tick, navigation } = setup(t); await join(); api.complete(); api.trial = trial;
  api.base.contracts[0].ends_at = "2020-01-01T00:00:00Z"; await tick(6000); assert.match(target.textContent, /expired/);
  await click("Leave corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /still being updated/); assert.doesNotMatch(target.textContent, /standard trial was granted/);
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
  const { target, view, api, tick, signals } = setup(t, "corporate-manager"); await flush(); assert.match(target.textContent, /No corporate subscriptions/);
  const idle = api.calls.length; await tick(3600000); assert.equal(api.calls.length, idle);
  const sibling = document.createElement("aside"); target.append(sibling); customer.unmountView(view); const count = api.calls.length;
  await tick(60000); assert.equal(api.calls.length, count); assert.ok(signals.every((s) => s.aborted)); assert.deepEqual([...target.children], [sibling]);
});
test("completed activation stops polling and a fast departure immediately shows its receipt", async (t) => {
  const { api, target, join, tick, click } = setup(t); await join(); api.complete(); await tick(6000);
  const idle = api.calls.length; await tick(3600000); assert.equal(api.calls.length, idle);
  assert.doesNotMatch(target.textContent, /Internal tariff|Refresh|Next check/);
  const original = api.hostRequest;
  api.hostRequest = async (path, options) => {
    const response = await original(path, options);
    if (path.endsWith('/leave')) { api.complete(); return { ...response, operation: api.operations.at(-1) }; }
    return response;
  };
  await click('Leave corporate subscription'); await click('Confirm', target.querySelector('dialog'));
  assert.match(target.textContent, /standard trial was granted/); assert.ok(target.querySelector('form'));
  const finished = api.calls.length; await tick(3600000); assert.equal(api.calls.length, finished);
});
test("lost departure response retries the same UUID and shows no trial before completion", async (t) => {
  const { api, target, join, tick, click } = setup(t); await join(); api.complete(); await tick(6000);
  api.loseNextWrite = true; await click("Leave corporate subscription"); await click("Confirm", target.querySelector("dialog"));
  await click("Retry the same request"); const calls = api.calls.filter((c) => c.path.endsWith("/leave"));
  assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, calls[1].body); assert.equal(api.operations.length, 2);
  assert.doesNotMatch(target.textContent, /standard trial was granted/); api.complete(); await tick(6000);
  assert.match(target.textContent, /standard trial was granted/);
});
test("manager rotation recovers a lost response without minting a second code", async (t) => {
  const { api, target, click } = setup(t, "corporate-manager"); api.manager = true; api.seedCode(); await flush(); await click("Refresh");
  await click(api.base.contracts[0].name); await click("Invitations"); api.loseNextWrite = true;
  await click("Rotate code"); await click("Confirm", target.querySelector("dialog")); await click("Retry the same request");
  assert.equal(api.base.invitations.length, 2); assert.equal(target.querySelector('[name="code"]').value, api.base.invitations[1].code);
  const calls = api.calls.filter((c) => c.path.endsWith("/rotate")); assert.deepEqual(calls[0].body, calls[1].body);
  await click('Refresh'); assert.equal(target.querySelector('[name="code"]').value, api.base.invitations[1].code);
});
