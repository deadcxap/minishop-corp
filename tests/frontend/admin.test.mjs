import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as admin from "../../frontend/dist/admin/index.js";
import { fixture, CONTRACT_ID, SQUAD_ID, MEMBER_ID, MEMBER_MINISHOP_ID, OTHER_MINISHOP_ID, DIAGNOSTIC_ID } from "../fixtures/admin-api.mjs";

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
    return Response.json(result.payload, { status: result.status, headers: { "X-Corp-Request-ID": DIAGNOSTIC_ID } });
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
  const { api, target, view, click, fill, submit } = setup(context); await flush(); await click("Create subscription");
  fill("name", "New corporate fixture"); fill("tariff", "corp"); fill("ends_at", "2031-11-12"); fill("manager", "910011"); await click("Find account"); await click("Confirm manager");
  fill("squad", SQUAD_ID); await click("Verify squad");
  admin.updateView(view, { currentLang: "ru" });
  assert.equal(target.querySelector('[name="name"]').value, "New corporate fixture");
  assert.equal(target.querySelector('[name="ends_at"]').value, "2031-11-12");
  api.loseNextWrite = true; await submit();
  assert.match(target.textContent, /Запрос мог выполниться/);
  await click("Повторить тот же запрос");
  const writes = api.calls.filter((row) => row.method === "POST" && row.path === "/contracts");
  assert.equal(writes.length, 2); assert.deepEqual(writes[0].body, writes[1].body);
  assert.equal(writes[0].body.ends_at, "2031-11-13T00:00:00.000Z");
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
  assert.match(target.textContent, /No manager assigned/);
  assert.equal(target.querySelector('[name="manager"]').required, false);
  fill("name", "Contract without a manager"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].manager_user_id, null);
  assert.equal(api.contracts[0].name, "Contract without a manager");
  fill("manager", "910011"); await click("Find account"); await click("Confirm manager"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].manager_user_id, 910011);
  assert.doesNotMatch(target.textContent, /No manager assigned/);
});

test("reloading after manager deletion clears the stale account selection", async (context) => {
  const { api, target, click, open } = setup(context); await open();
  assert.equal(target.querySelector('[name="manager"]').value, "ms_40000000000040008000000000000001");
  api.contracts[0].manager_user_id = null; await click("Load saved terms");
  assert.equal(target.querySelector('[name="manager"]').value, "");
  assert.equal(target.querySelector('[name="account_query"]'), null);
  assert.match(target.textContent, /No manager assigned/);
});

test("invitation retries preserve UUID and recover the same code after a lost response", async (context) => {
  const { api, target, click, open } = setup(context); await open(); await click("Invitations");
  api.loseNextWrite = true;
  const form = target.querySelector('.corp-panel:not([hidden]) form');
  form.dispatchEvent(new document.defaultView.Event("submit", { bubbles: true, cancelable: true })); await flush();
  await click("Retry the same request");
  assert.equal(api.invitations.length, 1);
  const writes = api.calls.filter((row) => row.method === "POST"); assert.equal(writes.length, 2); assert.equal(writes[0].body.id, writes[1].body.id);
  assert.equal(target.querySelector('[name="code"]').value, api.invitations[0].code);
  await click('Refresh'); assert.equal(target.querySelector('[name="code"]').value, api.invitations[0].code);
});

test("reusable code has an explicit limit and rotating it preserves members", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open(); await click("Invitations");
  fill("kind", "reusable"); fill("use_limit", "7"); await submit();
  assert.equal(api.invitations[0].use_limit, 7); assert.ok(target.querySelector('[name="code"]').value.startsWith("CORP-"));
  await click("Rotate code"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.invitations.length, 2); assert.equal(api.invitations[1].use_limit, 7); assert.ok(api.invitations[0].revoked_at);
  assert.equal(api.members.length, 2);
  await click("Back to subscriptions"); await open(); await click("Invitations");
  assert.deepEqual([...target.querySelectorAll('[name="code"]')].map(el => el.value).sort(), api.invitations.map(row => row.code).sort());
  assert.ok(!target.textContent.includes('Hide code'));
});

test("member view treats missing metrics as unknown, escapes profiles and confirms queued exclusion", async (context) => {
  const { api, target, click, open } = setup(context); await open(); await click("Members");
  assert.equal(target.querySelector("script"), null); assert.match(target.textContent, /<script>alert\(1\)<\/script>/);
  const webOnly = target.querySelector('[data-member="50000000-0000-4000-8000-000000000002"]');
  assert.match(webOnly.textContent, /No data/); assert.match(webOnly.textContent, /No Telegram account/); assert.equal(webOnly.querySelector("a"), null);
  assert.ok(target.querySelector(`[data-member="${MEMBER_ID}"] img`));
  assert.ok(target.textContent.includes(MEMBER_MINISHOP_ID)); assert.ok(webOnly.textContent.includes(OTHER_MINISHOP_ID));
  assert.doesNotMatch(target.textContent, /#910021|#910022/);
  assert.equal(target.querySelector(`[data-member="${MEMBER_ID}"] a`).textContent, '@fixture_manager');
  await click("Remove from subscription"); await click("Confirm", target.querySelector("dialog"));
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
  await click("Members"); assert.ok([...target.querySelectorAll("button")].some((button) => button.textContent === "Remove from subscription"));
});

test("rotation preserves the visible list and retries recover the new code without minting another", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open(); await click("Invitations");
  fill("kind", "reusable"); await submit(); assert.ok(target.querySelector('[name="code"]'));
  api.loseNextWrite = true; await click("Rotate code"); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /request may have completed/i); assert.ok(api.invitations[0].revoked_at);
  await click("Retry the same request"); assert.equal(api.invitations.length, 2);
  assert.ok([...target.querySelectorAll('[name="code"]')].some(el => el.value === api.invitations[1].code));
});

test("legacy invitations explain the missing plaintext without revoking the working code", async (context) => {
  const { api, target, click, open } = setup(context);
  api.invitations.push({ id: '40000000-0000-4000-8000-000000000001', kind: 'reusable', used_count: 2, reserved_count: 0,
    use_limit: 10, revoked_at: null, created_at: '2028-01-01T00:00:00Z', code: null, link: null });
  await open(); await click('Invitations');
  assert.match(target.textContent, /only its hash is stored/); assert.equal(api.invitations[0].revoked_at, null);
  assert.equal(target.querySelector('[name="code"]'), null); assert.ok(api.calls.every(c => c.method === 'GET'));
});

test("read failures use translated messages and a retry without leaking a response body", async (context) => {
  const { api, target, click } = setup(context); await flush(); api.failList = true; await click("Refresh");
  assert.match(target.querySelector('[role="alert"]').textContent, /do not have access/);
  api.failList = false; await click("Refresh"); assert.equal(target.querySelector('[role="alert"]'), null);
});

test("an optional squad can be cleared and the saved null is decoded on reopening", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open();
  fill("squad", ""); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].external_squad_uuid, null);
  await click("Back to subscriptions"); await click(api.contracts[0].name);
  assert.equal(target.querySelector('[name="squad"]').value, "");
  assert.equal(target.querySelector('[name="squad"]').required, false);
});

test("new form defaults to today, has no refresh or user directory, and requires manager confirmation", async (context) => {
  const { api, target, click, fill, submit } = setup(context); await flush(); await click("Create subscription");
  const today = new Date();
  const expected = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  assert.equal(target.querySelector('[name="ends_at"]').value, expected);
  assert.equal(target.querySelector('[name="ends_at"]').type, "date");
  assert.ok(![...target.querySelectorAll("button")].some((v) => v.textContent === "Refresh"));
  assert.ok(!api.calls.some((v) => v.path === "/options/accounts"));
  assert.equal(target.querySelector('select[name="manager"]'), null);
  assert.equal(target.querySelector('[name="manager"]').placeholder, "Minishop ID, Telegram ID, @username or email");
  fill("name", "Selected manager"); fill("tariff", "corp"); fill("manager", "manager@example.invalid");
  await click("Find account"); await submit();
  assert.match(target.textContent, /confirm the manager selection/);
  assert.ok(!api.calls.some((v) => v.method === "POST"));
  await click("Confirm manager"); await submit();
  assert.equal(api.contracts[1].manager_user_id, 910011);
  assert.equal(api.contracts[1].external_squad_uuid, null);
});

test("date shortcuts use the selected date, calendar months and leap-year clamping", async (context) => {
  const { target, click, fill } = setup(context); await flush(); await click("Create subscription");
  for (const [start, label, expected] of [
    ["2031-01-31", "+month", "2031-02-28"], ["2032-01-31", "+month", "2032-02-29"],
    ["2032-02-29", "+year", "2033-02-28"], ["2031-10-31", "+3 months", "2032-01-31"],
    ["2031-08-31", "+6 months", "2032-02-29"], ["2031-12-31", "+day", "2032-01-01"],
    ["2031-12-28", "+week", "2032-01-04"],
  ]) {
    fill("ends_at", start); await click(label); assert.equal(target.querySelector('[name="ends_at"]').value, expected);
  }
});

test("editing another field preserves a legacy exact expiry until the date is explicitly changed", async (context) => {
  const { api, target, click, fill, submit, open } = setup(context); await open();
  const original = api.contracts[0].ends_at;
  fill("name", "Keep exact expiry"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].ends_at, original);
  fill("ends_at", "2031-10-15"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].ends_at, "2031-10-16T00:00:00.000Z");
});

test("host remount restores every form field and the stable creation ID without account enumeration", async (context) => {
  const { api, target, view, click, fill } = setup(context); await flush(); await click("Create subscription");
  fill("name", "Draft after remount"); fill("tariff", "corp"); fill("ends_at", "2032-02-29"); fill("squad", SQUAD_ID);
  fill("manager", "@fixture_manager"); await click("Find account"); await click("Confirm manager");
  const id = view.panels.get("terms").id;
  admin.unmountView(view);
  const next = admin.mountView("corporate-contracts", target, { currentLang: "ru" }); context.after(() => admin.unmountView(next)); await flush();
  for (const [name, value] of [["name", "Draft after remount"], ["tariff", "corp"], ["ends_at", "2032-02-29"], ["squad", SQUAD_ID], ["manager", "@fixture_manager"]]) {
    assert.equal(target.querySelector(`[name="${name}"]`).value, value);
  }
  assert.equal(next.panels.get("terms").id, id);
  assert.equal(next.panels.get("terms").managerId, 910011);
  assert.ok(!api.calls.some((v) => v.path === "/options/accounts" && !new URLSearchParams(v.search).get("q") && !new URLSearchParams(v.search).get("user_id")));
});

test("remount keeps the active contract tab and refuses to overwrite a newer saved revision", async (context) => {
  const { api, target, view, click, fill, submit, open } = setup(context); await open();
  fill("name", "Draft kept on terms"); await click("Synchronization");
  admin.unmountView(view);
  api.contracts[0].version++; api.contracts[0].name = "Another administrator's change";
  const next = admin.mountView("corporate-contracts", target, { currentLang: "en" }); context.after(() => admin.unmountView(next)); await flush();
  assert.equal(target.querySelector('#corp-tab-sync').getAttribute("aria-selected"), "true");
  await click("Terms"); assert.equal(target.querySelector('[name="name"]').value, "Draft kept on terms");
  await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.match(target.textContent, /terms have changed/);
  assert.equal(api.contracts[0].name, "Another administrator's change");
});

test("a lost create response survives remount and can only repeat its original payload", async (context) => {
  const { api, target, view, click, fill, submit } = setup(context); await flush(); await click("Create subscription");
  fill("name", "Pending creation"); fill("tariff", "corp"); fill("manager", "910011"); await click("Find account"); await click("Confirm manager");
  api.loseNextWrite = true; await submit();
  const first = api.calls.find((v) => v.method === "POST").body;
  admin.unmountView(view);
  const next = admin.mountView("corporate-contracts", target, { currentLang: "en" }); context.after(() => admin.unmountView(next)); await flush();
  assert.match(target.textContent, /request may have completed/i);
  await click("Retry the same request");
  assert.deepEqual(api.calls.filter((v) => v.method === "POST").map((v) => v.body), [first, first]);
  assert.equal(api.contracts.length, 2);
  const stored = JSON.parse(document.defaultView.sessionStorage.getItem("minishop-corp.admin-draft.v1"));
  assert.equal(stored.editor, null);
});

test("drafts are not restored for another administrator or after explicit discard", async (context) => {
  const { api, target, view, click, fill } = setup(context); await flush(); await click("Create subscription");
  fill("name", "Private draft"); admin.unmountView(view); api.actorUserId++;
  const next = admin.mountView("corporate-contracts", target, { currentLang: "en" }); context.after(() => admin.unmountView(next)); await flush();
  assert.equal(target.querySelector('[name="name"]'), null); assert.doesNotMatch(target.textContent, /Private draft/);
  await click("Create subscription"); fill("name", "Discard me"); await click("Back to subscriptions"); await click("Confirm", target.querySelector("dialog"));
  assert.equal(document.defaultView.sessionStorage.getItem("minishop-corp.admin-draft.v1"), null);
});

test("invitation secrets never enter the form draft, and revoked permissions clear it", async (context) => {
  const { api, target, click, submit, open } = setup(context); await open(); await click("Invitations"); await submit();
  const secret = target.querySelector('[name="code"]').value;
  assert.ok(!document.defaultView.sessionStorage.getItem("minishop-corp.admin-draft.v1").includes(secret));
  await click("Back to subscriptions"); api.failList = true; await click("Refresh");
  assert.equal(document.defaultView.sessionStorage.getItem("minishop-corp.admin-draft.v1"), null);
});

test("an unfinished replacement-manager search survives remount for an orphaned contract", async (context) => {
  const { api, target, view, fill, open } = setup(context); api.contracts[0].manager_user_id = null; await open();
  fill("manager", "unfinished@example.invalid"); admin.unmountView(view);
  const next = admin.mountView("corporate-contracts", target, { currentLang: "en" }); context.after(() => admin.unmountView(next)); await flush();
  assert.equal(target.querySelector('[name="manager"]').value, "unfinished@example.invalid");
  assert.equal(next.panels.get("terms").managerId, null);
});

test("disposing an older host view cannot erase the replacement view's draft", async (context) => {
  const { target, view, click, fill } = setup(context); await flush();
  const replacementTarget = document.createElement("main"); target.after(replacementTarget);
  const next = admin.mountView("corporate-contracts", replacementTarget, { currentLang: "en" }); context.after(() => admin.unmountView(next)); await flush();
  await click("Create subscription", replacementTarget);
  const field = replacementTarget.querySelector('[name="name"]'); field.value = "Newest view";
  field.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
  admin.unmountView(view);
  const record = JSON.parse(document.defaultView.sessionStorage.getItem("minishop-corp.admin-draft.v1"));
  assert.equal(record.editor.name, "Newest view");
  // Simulate a backwards clock correction between save and restoration.
  record.updated = Date.now() + 60_000;
  document.defaultView.sessionStorage.setItem("minishop-corp.admin-draft.v1", JSON.stringify(record));
  const thirdTarget = document.createElement("main"); target.after(thirdTarget);
  const third = admin.mountView("corporate-contracts", thirdTarget, { currentLang: "en" }); context.after(() => admin.unmountView(third)); await flush();
  assert.equal(thirdTarget.querySelector('[name="name"]').value, "Newest view");
});

test("a contract can be created without a manager and an existing assignment can be cleared", async (context) => {
  const { api, target, click, fill, submit } = setup(context); await flush(); await click("Create subscription");
  assert.equal(target.querySelector('[name="manager"]').required, false);
  fill("name", "Admin-only contract"); fill("tariff", "corp"); await submit();
  assert.equal(api.contracts[1].manager_user_id, null);
  await click("Back to subscriptions"); await click(api.contracts[0].name);
  await click("Leave without a manager"); await submit(); await click("Confirm", target.querySelector("dialog"));
  assert.equal(api.contracts[0].manager_user_id, null);
  assert.equal(target.querySelector('[name="manager"]').value, "");
  assert.match(target.textContent, /No manager assigned/);
});

test("public Minishop and Telegram identifiers find and confirm the same canonical manager", async (context) => {
  const { api, target, click, fill, submit } = setup(context); await flush(); await click("Create subscription");
  fill("name", "Manager by public identity"); fill("tariff", "corp");
  for (const identifier of ["ms_40000000000040008000000000000001", "771234567"]) {
    fill("manager", identifier); await click("Find account");
    assert.match(target.textContent, /ms_40000000000040008000000000000001/);
    assert.match(target.textContent, /Telegram ID: 771234567/);
    await click("Confirm manager");
  }
  await submit(); assert.equal(api.contracts[1].manager_user_id, 910011);
  await click("Back to subscriptions"); await click(api.contracts[1].name);
  assert.equal(target.querySelector('[name="manager"]').value, "ms_40000000000040008000000000000001");
});

test("lookup misses and API errors expose the response diagnostic ID", async (context) => {
  const { api, target, click, fill } = setup(context); await flush(); await click("Create subscription");
  fill("manager", "missing@example.invalid"); await click("Find account");
  assert.match(target.textContent, /Account not found/);
  assert.ok(target.textContent.includes(`Diagnostic ID: ${DIAGNOSTIC_ID}`));
  await click("Leave without a manager"); await click("Back to subscriptions");
  api.failList = true; await click("Refresh");
  assert.ok(target.querySelector('[role="alert"]').textContent.includes(DIAGNOSTIC_ID));
});

test('deleting a used invitation confirms, keeps members and retries a lost response', async (t) => {
  const { api, target, click, submit, open } = setup(t); await open(); await click('Invitations');
  await submit(); api.invitations[0].used_count = 1; await click('Refresh');
  const code = target.querySelector('[name="code"]').value;
  await click('Delete code'); await click('Cancel', target.querySelector('dialog')); assert.equal(api.invitations.length, 1);
  api.loseNextWrite = true; await click('Delete code'); await click('Confirm', target.querySelector('dialog'));
  await click('Retry the same request'); assert.equal(api.invitations.length, 0); assert.equal(api.members.length, 2);
  assert.equal(target.querySelector('[name="code"]'), null); assert.ok(!target.textContent.includes(code));
  assert.match(target.textContent, /Invitation code deleted/);
  const calls = api.calls.filter(c => c.method === 'DELETE'); assert.equal(calls.length, 2); assert.equal(calls[0].path, calls[1].path);
});

test('assigning a manager from members confirms and retries the same version without changing terms', async (t) => {
  const { api, target, click, open } = setup(t); await open(); await click('Members');
  const member = api.members[0], original = { ...api.contracts[0] };
  const card = () => target.querySelector(`[data-member="${member.id}"]`);
  await click('Make manager', card()); await click('Cancel', target.querySelector('dialog')); assert.equal(api.contracts[0].version, 1);
  api.loseNextWrite = true; await click('Make manager', card()); await click('Confirm', target.querySelector('dialog'));
  await click('Retry the same request'); assert.equal(api.contracts[0].version, 2);
  assert.equal(api.contracts[0].manager_user_id, member.profile.user_id);
  for (const key of ['name', 'tariff_key', 'ends_at', 'external_squad_uuid']) assert.equal(api.contracts[0][key], original[key]);
  assert.equal([...card().querySelectorAll('button')].find(b => b.textContent === 'Subscription manager').disabled, true);
  const calls = api.calls.filter(c => c.path.endsWith('/manager')); assert.equal(calls.length, 2); assert.deepEqual(calls[0].body, calls[1].body);
});
