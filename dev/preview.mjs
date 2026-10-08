// A small test host, deliberately separate from the Minishop application shell.
const parameters = new URLSearchParams(location.search);
const audience = parameters.get("audience") === "admin" ? "admin" : "customer";
const language = parameters.get("language") === "en" ? "en" : "ru";
document.documentElement.lang = language;
document.documentElement.dataset.theme = parameters.get("theme") ?? "light";
const path = audience === "admin" ? "/api/admin/plugins/runtime" : "/api/extensions/runtime";
const response = await fetch(path);
const runtime = await response.json();
if (!response.ok || !runtime.ok) throw new Error("runtime_unavailable");
const plugin = runtime.plugins.find((entry) => entry.id === "minishop-corp");
if (!plugin) throw new Error("plugin_not_installed");
for (const href of plugin.styles) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.append(link);
}
const module = await import(plugin.entry);
const host = {
  version: 1,
  async request(relative, options) {
    const headers = new Headers(options?.headers);
    if (options?.body) headers.set("Content-Type", "application/json");
    if (options?.method && options.method !== "GET") {
      headers.set("X-CSRF-Token", document.cookie.match(/(?:^|;\s*)rw_webapp_csrf=([^;]*)/)?.[1] ?? "");
    }
    const result = await fetch(`/api/plugins/minishop-corp${relative}`, { ...options, headers, credentials: "same-origin" });
    const payload = await result.json();
    if (result.status >= 500 || result.status === 429) throw Object.assign(new Error("service_unavailable"), { status: result.status, payload });
    if (!payload.ok) throw payload;
    const { ok, ...data } = payload;
    return data;
  },
  navigate(view) { location.search = new URLSearchParams({ audience, language, view, theme: parameters.get("theme") ?? "light" }); },
  navigateSection(section) { document.querySelector(".development-label").textContent = `Development navigation: ${section}`; },
};
// Explicit view selection lets browser fixtures exercise manager UI; this read-only
// preview is not the host's discovery policy or a production navigation shell.
const requestedView = parameters.get("view");
const view = audience === "admin" ? plugin.sections[0].view
  : ["corporate-home", "corporate-card", "corporate-manager"].includes(requestedView) ? requestedView : "corporate-home";
const instance = module.mountView(view, document.querySelector("#view"), {
  language, currentLang: language, host,
});
window.addEventListener("pagehide", () => module.unmountView(instance), { once: true });
