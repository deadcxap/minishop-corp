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
    const result = await fetch(`/api/plugins/minishop-corp${relative}`, options);
    const payload = await result.json();
    if (!result.ok || !payload.ok) throw new Error("request_failed");
    const { ok, ...data } = payload;
    return data;
  },
};
const view = audience === "admin" ? plugin.sections[0].view : plugin.views[0].view;
const instance = module.mountView(view, document.querySelector("#view"), {
  language, currentLang: language, host,
});
window.addEventListener("pagehide", () => module.unmountView(instance), { once: true });
