import { ApiError, string } from "../shared/api";
import { Panel, button, el, field } from "../shared/ui";
import { contract, list, type Contract } from "./data";

export function broadcastHref(routePrefix: string, target?: string): string {
  const path = `${routePrefix.replace(/\/$/, "")}/admin/broadcast`;
  return target ? `${path}?${new URLSearchParams({ broadcast_target: target })}` : path;
}

/** A normal new-tab link keeps the host draft and starts with a fresh audience catalog. */
export function broadcastLink(label: string, routePrefix: string, target?: string): HTMLAnchorElement {
  const link = el("a", label, "corp-button corp-primary");
  link.href = broadcastHref(routePrefix, target); link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
}

export class BroadcastAudienceView extends Panel {
  rows: Contract[] = []; after: string | null = null; loaded = false; expanded = false;
  selected = new Set<string>(); target: string | null = null;
  constructor(target: HTMLElement, language: string, readonly routePrefix = "") {
    super(language); this.root.className = "minishop-corp corp-broadcast";
    target.append(this.root); this.render();
  }
  async load(after?: string): Promise<void> {
    const rows = list((await this.api.request(`/contracts?limit=50${after ? `&after=${encodeURIComponent(after)}` : ""}`)).contracts, contract);
    this.rows = after ? [...this.rows, ...rows] : rows;
    this.after = rows.length === 50 ? rows.at(-1)?.id ?? null : null; this.loaded = true;
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError && [401, 403].includes(error.status)) {
      this.rows = []; this.selected.clear(); this.target = null; this.loaded = false;
    }
  }
  async prepare(): Promise<void> {
    if (!this.selected.size || this.selected.size > 100) return;
    const ids = [...this.selected].sort();
    this.target = null;
    await this.run(async () => {
      const target = string((await this.api.request("/broadcast-audiences", "POST", { contract_ids: ids })).target);
      if (!/^minishop-corp\.(?:contract|selection):[a-f0-9]{32}$/.test(target)) throw new ApiError("invalid_response");
      this.target = target;
    }, true);
  }
  render(): void {
    this.paint((body) => {
      body.append(el("h3", this.t("broadcast_title")), el("p", this.t("broadcast_hint")));
      const toggle = button(this.t(this.expanded ? "broadcast_hide" : "broadcast_choose"), () => {
        this.expanded = !this.expanded;
        if (this.expanded && !this.loaded) void this.run(() => this.load()); else this.render();
      });
      toggle.setAttribute("aria-expanded", String(this.expanded)); body.append(toggle);
      if (!this.expanded) return;
      if (!this.loaded) { body.append(button(this.t("refresh"), () => { void this.run(() => this.load()); })); return; }
      if (!this.rows.length) { body.append(el("p", this.t("empty_contracts"), "corp-empty")); return; }
      const form = el("form"); form.addEventListener("submit", (event) => { event.preventDefault(); void this.prepare(); });
      const options = el("div", undefined, "corp-broadcast-options");
      for (const row of this.rows) {
        const check = el("input"); check.type = "checkbox"; check.name = "broadcast_contract";
        check.value = row.id; check.checked = this.selected.has(row.id); check.dataset.focus = `broadcast-${row.id}`;
        check.disabled = !check.checked && this.selected.size >= 100;
        check.addEventListener("change", () => {
          if (check.checked) this.selected.add(row.id); else this.selected.delete(row.id);
          this.target = null; this.render();
        });
        options.append(field(row.name, check, `${this.t("members")}: ${row.member_count}`));
      }
      form.append(options);
      if (this.after) form.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
      const summary = el("p", this.t("broadcast_selected").replace("{count}", String(this.selected.size)));
      summary.setAttribute("role", "status"); form.append(summary, el("p", this.t("broadcast_recipients_hint")));
      const submit = el("button", this.t("broadcast_prepare"), "corp-button"); submit.type = "submit"; submit.disabled = !this.selected.size;
      form.append(submit); body.append(form);
      if (this.target) {
        body.append(broadcastLink(this.t("broadcast_open"), this.routePrefix, this.target), el("p", this.t("broadcast_new_tab")));
      }
    });
  }
}
