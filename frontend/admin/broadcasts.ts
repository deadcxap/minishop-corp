import { ApiError, string } from "../shared/api";
import { Panel, button, el, refreshButton } from "../shared/ui";
import { icon } from "../shared/icons";
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
    super(language); this.root.className = "minishop-corp corp-workspace corp-broadcast";
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
    const scroll = this.root.querySelector(".corp-broadcast-options")?.scrollTop ?? 0;
    this.paint((body) => {
      const toggle = button("", () => {
        this.expanded = !this.expanded;
        if (this.expanded && !this.loaded) void this.run(() => this.load()); else this.render();
      });
      toggle.className = "corp-broadcast-toggle"; toggle.dataset.focus = "broadcast-toggle";
      toggle.setAttribute("aria-label", this.t(this.expanded ? "broadcast_hide" : "broadcast_choose"));
      toggle.setAttribute("aria-expanded", String(this.expanded)); toggle.setAttribute("aria-controls", "corp-broadcast-selection");
      toggle.append(icon("users"), el("span", this.t("broadcast_group")));
      if (this.selected.size) toggle.append(el("span", String(this.selected.size), "corp-broadcast-count"));
      toggle.append(icon("chevron")); body.append(toggle);
      const selection = el("div", undefined, "corp-broadcast-selection"); selection.id = "corp-broadcast-selection";
      selection.hidden = !this.expanded; body.append(selection);
      if (!this.expanded) return;
      if (!this.loaded) { selection.append(refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy)); return; }
      if (!this.rows.length) { selection.append(el("p", this.t("empty_contracts"), "corp-broadcast-hint")); return; }
      const form = el("form"); form.addEventListener("submit", (event) => { event.preventDefault(); void this.prepare(); });
      form.className = "corp-broadcast-selection"; form.id = selection.id; selection.replaceWith(form);
      form.append(el("p", this.t("broadcast_hint"), "corp-broadcast-hint"));
      const options = el("div", undefined, "corp-broadcast-options");
      for (const row of this.rows) {
        const check = el("input"); check.type = "checkbox"; check.name = "broadcast_contract";
        check.value = row.id; check.checked = this.selected.has(row.id); check.dataset.focus = `broadcast-${row.id}`;
        check.disabled = !check.checked && this.selected.size >= 100;
        check.addEventListener("change", () => {
          if (check.checked) this.selected.add(row.id); else this.selected.delete(row.id);
          this.target = null; this.render();
        });
        const option = el("label", undefined, "corp-broadcast-option");
        const members = el("small"); members.title = `${this.t("members")}: ${row.member_count}`;
        members.append(el("span", `${this.t("members")}: `, "corp-sr-only"), String(row.member_count));
        option.append(check, el("span", row.name), members); options.append(option);
      }
      form.append(options);
      if (this.after) form.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }, "corp-ghost"));
      const summary = el("p", this.t("broadcast_selected").replace("{count}", String(this.selected.size)));
      summary.setAttribute("role", "status");
      const footer = el("div", undefined, "corp-broadcast-footer"); footer.append(summary);
      if (this.target) {
        const link = broadcastLink(this.t("broadcast_open"), this.routePrefix, this.target);
        link.append(icon("external")); link.title = this.t("broadcast_new_tab"); footer.append(link);
      } else {
        const submit = el("button", this.t("broadcast_prepare"), "corp-button corp-primary"); submit.type = "submit"; submit.disabled = !this.selected.size;
        footer.append(submit);
      }
      form.append(footer);
      if (this.target) form.append(el("small", this.t("broadcast_new_tab")));
      const details = el("details", undefined, "corp-broadcast-details");
      details.append(el("summary", this.t("broadcast_details")), el("p", this.t("broadcast_recipients_hint"))); form.append(details);
    });
    const options = this.root.querySelector(".corp-broadcast-options"); if (options) options.scrollTop = scroll;
  }
}
