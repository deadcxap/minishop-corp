import { AdminApi, ApiError, type ApiClient } from "./api";
import { translate, type LocaleKey, adminText, type AdminKey, errorText } from "./i18n";

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
export function button(text: string, action: () => void, style = ""): HTMLButtonElement {
  const node = el("button", text, `corp-button ${style}`);
  node.type = "button"; node.addEventListener("click", action); return node;
}
export function field(label: string, input: HTMLElement, hint?: string): HTMLLabelElement {
  const node = el("label", undefined, "corp-field");
  node.append(el("span", label), input);
  if (hint) node.append(el("small", hint));
  return node;
}
export function input(name: string, value: string, change: (value: string) => void, type = "text"): HTMLInputElement {
  const node = el("input"); node.type = type; node.value = value; node.name = name; node.dataset.focus = name;
  node.addEventListener("input", () => change(node.value)); return node;
}
export function select(name: string, value: string, choices: [string, string][], change: (value: string) => void): HTMLSelectElement {
  const node = el("select"); node.name = name; node.dataset.focus = name;
  for (const [key, label] of choices) { const item = el("option", label); item.value = key; node.append(item); }
  node.value = value; node.addEventListener("change", () => change(node.value)); return node;
}
export function date(value: string | null, language: string): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Intl.DateTimeFormat(language.startsWith("ru") ? "ru" : "en", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}
export function person(value: { first_name: string | null; last_name: string | null; username: string | null; minishop_id: string }): string {
  return [value.first_name, value.last_name].filter(Boolean).join(" ") || (value.username ? `@${value.username}` : value.minishop_id);
}
export function confirm(root: HTMLElement, title: string, message: string, language: string, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = el("dialog", undefined, "corp-dialog");
    const heading = el("h3", title); heading.id = "corp-confirm-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const finish = (result: boolean): void => { signal.removeEventListener("abort", abort); dialog.close(); dialog.remove(); resolve(result); };
    const abort = (): void => finish(false);
    const actions = el("div", undefined, "corp-actions");
    actions.append(button(adminText(language, "cancel"), () => finish(false)), button(adminText(language, "confirm"), () => finish(true), "corp-danger"));
    dialog.append(heading, el("p", message), actions);
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    signal.addEventListener("abort", abort, { once: true });
    // Keep the modal outside repaintable panel bodies and inside our own root.
    (root.closest(".minishop-corp") ?? root).append(dialog); dialog.showModal();
  });
}

/** A panel owns its DOM, requests and uncertain write intent; no global handlers. */
export abstract class Panel {
  readonly root = el("section", undefined, "corp-panel");
  readonly body = el("div");
  readonly controller = new AbortController();
  readonly api: ApiClient;
  busy = false;
  error: unknown = null;
  notice: LocaleKey | null = null;
  retry: (() => Promise<void>) | null = null;
  changed: () => void = () => {};
  constructor(public language: string, client: (signal: AbortSignal) => ApiClient = (signal) => new AdminApi(signal)) {
    this.api = client(this.controller.signal); this.root.append(this.body);
  }
  get alive(): boolean { return !this.controller.signal.aborted; }
  get confirming(): boolean { return (this.root.closest(".minishop-corp") ?? this.root).querySelector("dialog[open]") !== null; }
  t(key: AdminKey): string { return adminText(this.language, key); }
  abstract render(): void;
  failed(_error: unknown): void {}
  update(language: string): void { if (this.language !== language) { this.language = language; this.render(); } }
  destroy(): void { this.controller.abort(); this.root.remove(); this.retry = null; }
  async run(task: () => Promise<void>, write = false): Promise<void> {
    if (this.busy || !this.alive) return;
    this.busy = true; this.error = null; this.notice = null; this.render(); this.changed();
    try { await task(); this.retry = null; }
    catch (error) {
      this.error = error;
      this.failed(error);
      this.retry = write && (!(error instanceof ApiError) || error.uncertain) ? task : null;
    } finally { this.busy = false; if (this.alive) { this.render(); this.changed(); } }
  }
  paint(draw: (body: HTMLElement) => void): void {
    const active = this.root.contains(document.activeElement) ? document.activeElement : null;
    const focus = active instanceof HTMLElement ? active.dataset.focus : undefined;
    const cursor = active instanceof HTMLInputElement && active.type === "text" ? active.selectionStart : null;
    this.body.replaceChildren(); this.root.setAttribute("aria-busy", String(this.busy));
    if (this.error) this.body.append(el("p", errorText(this.language, this.error), "corp-alert"));
    this.body.querySelector(".corp-alert")?.setAttribute("role", "alert");
    if (this.notice) { const note = el("p", translate(this.language, this.notice), "corp-notice"); note.setAttribute("role", "status"); this.body.append(note); }
    if (this.retry) {
      const notice = el("div", undefined, "corp-notice");
      notice.append(el("p", this.t("uncertain")), button(this.t("retry_write"), () => { if (this.retry) void this.run(this.retry, true); }));
      this.body.append(notice);
    }
    const content = el("fieldset", undefined, "corp-content"); content.disabled = this.busy || this.retry !== null;
    draw(content); this.body.append(content);
    if (focus) {
      const next = this.body.querySelector<HTMLElement>(`[data-focus="${focus}"]`);
      if (next && !(next instanceof HTMLInputElement && next.disabled)) {
        next.focus(); if (cursor !== null && next instanceof HTMLInputElement) next.setSelectionRange(cursor, cursor);
      }
    }
  }
}
