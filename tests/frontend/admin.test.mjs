import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as admin from "../../frontend/dist/admin/index.js";
import { fixture, CONTRACT_ID, SQUAD_ID, MEMBER_ID } from "../fixtures/admin-api.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
function setup(context) {
  const window = new Window({ url: "https://fixture.invalid" });
  for (const key of ["document", "HTMLElement", "HTMLInputElement"]) globalThis[key] = key === "document" ? window.document : window[key];
  const target = document.createElement("main"); document.body.append(target);
  document.cookie = "rw_webapp_csrf=synthetic-csrf";
  const api = fixture();
  const signals = [];
  context.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(options.credentials, "same-origin"); signals.push(options.signal);
    if (options.method !== "GET") assert.equal(options.headers["X-CSRF-Token"], "synthetic-csrf");
    const result = api.respond(path, options.method, options.body ? JSON.parse(options.body) : undefined);
    return Response.json(result.payload, { status: result.status });
  });
  const view = admin.mountView("corporate-contracts", target, { currentLang: "en" });
  context.after(() => { admin.unmountView(view); window.happyDOM.abort(); });
  const click = async (text, root = target) => {
    const node = [...root.querySelectorAll("button")].find((button) => button.textContent === text && !button.closest("[hidden]"));
    assert.ok(node, `Button not found: ${text}`); node.click(); await flush();
  };
  const fill = (name, value) => {
    const node = target.querySelector(`[name="${name}"]`); assert.ok(node, name); node.value = value;
    node.dispatchEvent(new window.Event(node.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  };
  const submit = async () => { const form = target.querySelector(".corp-panel:not([hidden]) form"); form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await flush(); };
  const open = async () => { await flush(); await click(api.contracts[0].name); };
  return { api, target, view, click, fill, submit, open, signals };
}

test("editor preserves draft on host updates and sends UTC with a stable create ID after network loss", async (context) => {
  const { api, target, view, click, fill, submit } = setup(context); await flush(); await click("Create contract");
  fill("name", "New corporate fixture"); fill("tariff", "corp"); fill("ends_at", "2031-11-12T10:11:12.123"); fill("manager", "910011");
  fill("squad", SQUAD_ID); await click("Verify squad");
  admin.updateView(view, { currentLang: "ru" });
  assert.equal(target.querySelector('[name="name"]').value, "New corporate fixture");
  assert.equal(target.querySelector('[name="ends_at"]').value, "2031-11-12T10:11:12.123");
  api.loseNextWrite = true; await submit();
  assert.match(target.textContent, /Запрос мог выполниться/);
  await click("Повторить тот же запрос");
  const writes = api.calls.filter((row) => row.method === "POST" && row.path === "/contracts");
  assert.equal(writes.length, 2); assert.deepEqual(writes[0].body, writes[1].body);
  assert.equal(writes[0].body.ends_at, "2031-11-12T10:11:12.123Z");
  assert.equal(api.contracts.length, 2); assert.match(target.textContent, /Условия сохранены/);
});

test("terms require confirmation and resolve a lost PUT without overwriting a newer version", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open();
  fill("name", "Updated fixture"); await submit(); assert.ok(target.querySelector("dialog[open]"));
  await click("Cancel", target.querySelector("dialog")); assert.equal(api.calls.filter((row) => row.method === "PUT").length, 0);
  api.loseNextWrite = true; await submit(); await click("Confirm", target.querySelector("dialog"));
  await click("Retry the same request");
  assert.equal(api.contracts[0].version, 2); assert.equal(api.contracts[0].name, "Updated fixture");
  fill("name", "Unsaved fixture"); api.contracts[0].version = 3; api.contracts[0].name = "Someone else's change";
  await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /terms have changed/);
  assert.equal(target.querySelector('[name="name"]').value, "Unsaved fixture");
  assert.equal(api.contracts[0].name, "Someone else's change");
});

test("a contract with a deleted manager stays editable and allows assigning a replacement", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context);
  api.contracts[0].manager_user_id = null; await open();
  assert.match(target.textContent, /manager account was deleted/);
  assert.equal(target.querySelector('[name="manager"]').required, false);
  fill("name", "Contract without a manager"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].manager_user_id, null);
  assert.equal(api.contracts[0].name, "Contract without a manager");
  fill("manager", "910011"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].manager_user_id, 910011);
  assert.doesNotMatch(target.textContent, /manager account was deleted/);
});

test("reloading after manager deletion clears the stale account selection", async (context) => {
  const { api, target, click, open } = setup(context); await open();
  assert.equal(target.querySelector('[name="manager"]').value, "910011");
  api.contracts[0].manager_user_id = null; await click("Load saved terms");
  assert.equal(target.querySelector('[name="manager"]').value, "");
  assert.equal(target.querySelector('[name="account_query"]').value, "");
  assert.match(target.textContent, /manager account was deleted/);
});

test("invitation write retries preserve UUID and show recovery for a lost one-time credential", async (context) => {
  const { api, target, click, open } = setup(context); await open(); await click("Invitations");
  api.loseNextWrite = true;
  const form = target.querySelector('.corp-panel:not([hidden]) form');
  form.dispatchEvent(new document.defaultView.Event("submit", { bubbles: true, cancelable: true })); await flush();
  await click("Retry the same request");
  assert.equal(api.invitations.length, 1); assert.match(target.textContent, /code cannot be shown again/);
  const writes = api.calls.filter((row) => row.method === "POST"); assert.equal(writes.length, 2); assert.equal(writes[0].body.id, writes[1].body.id);
  assert.equal(target.querySelector('[name="code"]'), null);
});

test("reusable code has an explicit limit and rotating it preserves members", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open(); await click("Invitations");
  fill("kind", "reusable"); fill("use_limit", "7"); await submit();
  assert.equal(api.invitations[0].use_limit, 7); assert.ok(target.querySelector('[name="code"]').value.startsWith("CORP-"));
  await click("Rotate code"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.invitations.length, 2); assert.equal(api.invitations[1].use_limit, 7); assert.ok(api.invitations[0].revoked_at);
  assert.equal(api.members.length, 2);
  await click("Hide code"); assert.equal(target.querySelector('[name="code"]'), null);
});

test("member view treats missing metrics as unknown, escapes profiles and confirms queued exclusion", async (context) => {
  const { api, target, click, open } = setup(context); await open(); await click("Members");
  assert.equal(target.querySelector("script"), null); assert.match(target.textContent, /<script>alert\(1\)<\/script>/);
  const webOnly = target.querySelector('[data-member="50000000-0000-4000-8000-000000000002"]');
  assert.match(webOnly.textContent, /No data/); assert.match(webOnly.textContent, /No Telegram account/); assert.equal(webOnly.querySelector("a"), null);
  assert.ok(target.querySelector(`[data-member="${MEMBER_ID}"] img`));
  await click("Remove from group"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /Removal accepted/); assert.match(target.textContent, /Leaving/);
  assert.equal(api.members.length, 2); assert.equal(api.members[0].operation.state, "pending");
  assert.ok(api.calls.some((row) => row.path === `/contracts/${CONTRACT_ID}/members/${MEMBER_ID}/exclude`));
  assert.ok(api.calls.every((row) => row.method !== "DELETE"));
});

test("unmount aborts requests and scoped DOM cleanup preserves host siblings", async (context) => {
  const { target, view, signals, click, open } = setup(context);
  const aside = document.createElement("aside"); target.append(aside); await open(); await click("Synchronization");
  assert.match(target.textContent, /Retrying after an error/); assert.match(target.textContent, /not been confirmed/);
  admin.unmountView(view); assert.deepEqual([...target.children], [aside]); assert.ok(signals.every((signal) => signal.aborted));
});

test("contract pagination retains earlier rows and uses the last UUID as the cursor", async (context) => {
  const { api, target, click } = setup(context);
  api.contracts = Array.from({ length: 26 }, (_, i) => ({ ...api.contracts[0], id: `30000000-0000-4000-8000-${String(i + 1).padStart(12, "0")}`, name: `Fixture ${i + 1}` }));
  await flush(); await click("Refresh"); assert.equal(target.querySelectorAll("[data-contract]").length, 25);
  await click("Load more"); assert.equal(target.querySelectorAll("[data-contract]").length, 26);
  assert.ok(api.calls.at(-1).search.includes("after=30000000-0000-4000-8000-000000000025"));
});

test("expired contracts retain member management but do not offer new codes", async (context) => {
  const { api, target, click, open } = setup(context);
  api.contracts[0].ends_at = "2020-01-01T00:00:00Z"; await open(); await click("Invitations");
  assert.match(target.textContent, /Renew it before issuing/);
  assert.ok(![...target.querySelectorAll("button")].some((button) => button.textContent === "Generate code"));
  await click("Members"); assert.ok([...target.querySelectorAll("button")].some((button) => button.textContent === "Remove from group"));
});

test("network loss during rotation hides the possibly revoked code until the same request resolves", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open(); await click("Invitations");
  fill("kind", "reusable"); await submit(); assert.ok(target.querySelector('[name="code"]'));
  api.loseNextWrite = true; await click("Rotate code"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(target.querySelector('[name="code"]'), null); assert.ok(api.invitations[0].revoked_at);
  await click("Retry the same request"); assert.equal(api.invitations.length, 2);
  assert.match(target.textContent, /code cannot be shown again/);
});

test("read failures use translated messages and a retry without leaking a response body", async (context) => {
  const { api, target, click } = setup(context); await flush(); api.failList = true; await click("Refresh");
  assert.match(target.querySelector('[role="alert"]').textContent, /do not have access/);
  api.failList = false; await click("Refresh"); assert.equal(target.querySelector('[role="alert"]'), null);
});
