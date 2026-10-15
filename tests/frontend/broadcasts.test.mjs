import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as admin from "../../frontend/dist/admin/index.js";
import { fixture } from "../fixtures/admin-api.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
function setup(context, count = 2) {
  const window = new Window({ url: "https://fixture.invalid/prefix/admin/broadcast" });
  for (const key of ["document", "HTMLElement", "HTMLInputElement"]) globalThis[key] = key === "document" ? window.document : window[key];
  const target = document.createElement("main"); document.body.append(target);
  document.cookie = "rw_webapp_csrf=synthetic-csrf";
  const api = fixture(), base = api.contracts[0];
  api.contracts = Array.from({length: count}, (_, index) => ({...base,
    id: `30000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    name: index ? `Subscription ${index + 1}` : "<script>Subscription</script>"}));
  const requests = []; let fail = false;
  context.mock.method(globalThis, "fetch", async (path, options) => {
    requests.push({ path, options });
    if (options.method === "GET") {
      const after = new URL(path, "https://fixture.invalid").searchParams.get("after");
      return Response.json({ok: true, contracts: api.contracts.filter(row => !after || row.id > after).slice(0, 50)});
    }
    assert.equal(options.headers["X-CSRF-Token"], "synthetic-csrf");
    assert.equal(options.credentials, "same-origin");
    if (fail) { fail = false; throw new Error("lost response"); }
    return Response.json({ok: true, target: "minishop-corp.selection:" + "1".repeat(32)});
  });
  const view = admin.mountView("corporate-broadcast-audience", target, {currentLang: "en", routePrefix: "/prefix"});
  context.after(() => { admin.unmountView(view); window.happyDOM.abort(); });
  const click = async text => { const node = [...target.querySelectorAll("button")].find(node => (node.getAttribute("aria-label") ?? node.textContent) === text); assert.ok(node, text); node.click(); await flush(); };
  const check = id => { const node = target.querySelector(`input[value="${api.contracts[id].id}"]`); node.checked = !node.checked; node.dispatchEvent(new window.Event("change")); };
  const submit = async () => { target.querySelector("form").dispatchEvent(new window.Event("submit", {bubbles: true, cancelable: true})); await flush(); };
  return {target, api, view, requests, click, check, submit, loseResponse: () => { fail = true; }};
}

test("corporate slot prepares a union without sending and opens a fresh native editor", async context => {
  const {target, api, view, requests, click, check, submit} = setup(context);
  assert.equal(requests.length, 0);
  await click("Choose subscriptions");
  assert.equal(target.querySelectorAll("script").length, 0);
  check(0); check(1);
  await click("Hide selection");
  assert.ok(target.querySelector("#corp-broadcast-selection").hidden);
  await click("Choose subscriptions");
  assert.equal(target.querySelectorAll("input:checked").length, 2);
  await submit();
  const writes = requests.filter(row => row.options.method === "POST");
  assert.equal(writes.length, 1); assert.match(writes[0].path, /\/broadcast-audiences$/);
  assert.deepEqual(JSON.parse(writes[0].options.body).contract_ids, api.contracts.map(row => row.id));
  const link = target.querySelector("a");
  assert.equal(new URL(link.href).pathname, "/prefix/admin/broadcast");
  assert.equal(new URL(link.href).searchParams.get("broadcast_target"), "minishop-corp.selection:" + "1".repeat(32));
  assert.equal(link.target, "_blank"); assert.match(link.rel, /noopener/);
  admin.updateView(view, {currentLang: "ru"});
  assert.match(target.textContent, /Выбрано: 2/);
  check(0); assert.equal(target.querySelector("a"), null);
  assert.equal(requests.filter(row => row.path === "/api/admin/broadcast").length, 0);
});

test("selection retries identical input after a lost response and aborts on unmount", async context => {
  const {target, view, requests, click, check, submit, loseResponse} = setup(context);
  await click("Choose subscriptions"); check(0); check(1); loseResponse(); await submit();
  assert.equal(target.querySelector("a"), null);
  await click("Retry the same request");
  const posts = requests.filter(row => row.options.method === "POST");
  assert.equal(posts.length, 2); assert.equal(posts[0].options.body, posts[1].options.body);
  admin.unmountView(view); assert.ok(requests.every(row => row.options.signal.aborted));
});

test("paginated selections persist and stop at 100 subscriptions", async context => {
  const {target, click, check} = setup(context, 101);
  await click("Choose subscriptions"); check(0); await click("Load more");
  assert.equal(target.querySelectorAll("input").length, 100);
  assert.equal(target.querySelector("input").checked, true);
  for (let index = 1; index < 100; index++) check(index);
  await click("Load more");
  assert.equal(target.querySelectorAll("input:disabled").length, 1);
  assert.match(target.textContent, /Selected: 100/);
});
