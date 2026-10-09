import assert from "node:assert/strict";
import { test } from "node:test";
import { Window } from "happy-dom";
import * as customer from "../../frontend/dist/customer/index.js";
import * as admin from "../../frontend/dist/admin/index.js";

function target() {
  const window = new Window();
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.HTMLElement = window.HTMLElement;
  globalThis.HTMLInputElement = window.HTMLInputElement;
  const element = window.document.createElement("main");
  window.document.body.append(element);
  return element;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("customer uses scoped host request, supports language changes and cleanup", async () => {
  const element = target();
  let signal;
  const props = { language: "ru", host: { version: 1, request: async (path, options) => {
    assert.ok(["/membership", "/managed-contracts?limit=1"].includes(path));
    signal = options.signal;
    return path === "/membership" ? { membership: null, last_departure: null } : { contracts: [] };
  } } };
  const view = customer.mountView("corporate-home", element, props);
  assert.equal(element.querySelector("section").getAttribute("aria-busy"), "true");
  await flush();
  assert.match(element.textContent, /Корпоративная подписка/);
  assert.match(element.textContent, /Проверить код/);
  customer.updateView(view, { ...props, language: "en" });
  assert.match(element.textContent, /Corporate subscription/);
  customer.unmountView(view);
  assert.equal(element.children.length, 0);
  assert.equal(signal.aborted, true);
});

test("failed request gives a localized message and leaves the host DOM alone", async () => {
  const element = target();
  const sibling = document.createElement("aside");
  element.append(sibling);
  const view = customer.mountView("corporate-home", element, {
    language: "ru", host: { version: 1, request: async () => { throw new Error("secret"); } },
  });
  await flush();
  assert.match(element.textContent, /Не удалось загрузить/);
  assert.doesNotMatch(element.textContent, /secret/);
  customer.unmountView(view);
  assert.deepEqual([...element.children], [sibling]);
});

test("admin uses its own authenticated endpoint", async (context) => {
  const element = target();
  context.mock.method(globalThis, "fetch", async (path, options) => {
    assert.ok(["/api/admin/minishop-corp/contracts?limit=25", "/api/admin/minishop-corp/options/context"].includes(path));
    assert.equal(options.credentials, "same-origin");
    return Response.json({ ok: true, contracts: [], actor_user_id: 910001 });
  });
  const view = admin.mountView("corporate-contracts", element, { currentLang: "en" });
  await flush();
  assert.match(element.textContent, /No corporate subscriptions yet/);
  admin.unmountView(view);
});
