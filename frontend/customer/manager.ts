import { ApiError } from "../shared/api";
import type { CustomerProps } from "../shared/host";
import { customerText, type CustomerKey } from "../shared/i18n";
import { MembersPanel } from "../shared/member-panel";
import { Panel, button, date, el, iconButton, refreshButton } from "../shared/ui";
import { icon } from "../shared/icons";
import { InvitationsPanel } from "../shared/invitations-panel";
import { CustomerApi } from "./api";
import { list, summary, type Summary } from "./data";

const denied = (error: unknown): boolean => error instanceof ApiError && [401, 403, 404].includes(error.status);

type Tab = "members" | "invitations";
export class ManagerView extends Panel {
  rows: Summary[] = []; after: string | null = null; loaded = false; current: Summary | null = null;
  tab: Tab = "members"; panels = new Map<Tab, Panel>();
  constructor(target: HTMLElement, public props: CustomerProps) {
    super(props.language, (signal) => new CustomerApi(() => this.props.host, signal));
    this.root.className = "minishop-corp corp-workspace corp-customer"; target.append(this.root);
    void this.run(() => this.load());
  }
  c(key: CustomerKey): string { return customerText(this.language, key); }
  get locked(): boolean { return this.busy || Boolean(this.retry) || [...this.panels.values()].some((panel) => panel.busy || panel.retry); }
  clear(): void { this.panels.forEach((panel) => panel.destroy()); this.panels.clear(); this.current = null; }
  override failed(error: unknown): void { if (denied(error)) { this.clear(); this.rows = []; this.after = null; } }
  async load(after?: string): Promise<void> {
    const page = list((await this.api.request(`/managed-contracts?limit=25${after ? `&after=${after}` : ""}`)).contracts, summary);
    this.rows = after ? [...this.rows, ...page] : page; this.after = page.length === 25 ? page.at(-1)?.id ?? null : null; this.loaded = true;
  }
  async open(row: Summary): Promise<void> {
    await this.run(async () => { const latest = summary((await this.api.request(`/managed-contracts/${row.id}`)).contract);
      if (!this.alive) return;
      this.clear(); this.current = latest; this.tab = "members"; this.panel("members"); });
  }
  panel(tab: Tab): Panel {
    const existing = this.panels.get(tab); if (existing) return existing;
    const current = (): Summary => { if (!this.current) throw new Error("missing_contract"); return this.current; };
    const host = (): CustomerProps["host"] => this.props.host;
    const panel = tab === "members" ? new MembersPanel(this.language, current, "manager", (signal) => new CustomerApi(host, signal))
      : new InvitationsPanel(this.language, current, "manager", (signal) => new CustomerApi(host, signal));
    panel.changed = () => {
      if (!this.alive) return;
      if (denied(panel.error)) { this.failed(panel.error); this.error = new ApiError("wa_minishop_corp_manager_revoked", 400); }
      this.render();
    };
    this.panels.set(tab, panel); return panel;
  }
  switchTab(tab: Tab): void {
    if (this.locked) return;
    this.tab = tab; this.panel(tab); this.render(); this.root.querySelector<HTMLButtonElement>(`#corp-manager-tab-${tab}`)?.focus();
  }
  render(): void {
    this.paint((body) => {
      const heading = el("div", undefined, "corp-panel-heading"), title = el("div");
      if (!this.current) {
        title.append(el("h2", this.c("manager_title")), el("small", this.c("manager_hint")));
        heading.append(title, refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy)); body.append(heading);
        if (!this.loaded) body.append(el("p", this.c("loading")));
        else if (!this.rows.length) body.append(el("p", this.c("manager_empty"), "corp-empty"));
        const rows = el("div", undefined, "corp-contract-list");
        for (const row of this.rows) {
          const card = button("", () => { void this.open(row); }, "corp-managed-contract");
          card.setAttribute("aria-label", row.name);
          const label = el("span", undefined, "corp-managed-label");
          label.append(el("strong", row.name), el("small", `${this.c("ends_at")}: ${date(row.ends_at, this.language)}`));
          card.append(label, icon("chevron")); rows.append(card);
        }
        if (this.rows.length) body.append(rows);
        if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
        return;
      }
      const back = iconButton(this.t("back"), "back", () => { this.clear(); void this.run(() => this.load()); }); back.disabled = this.locked;
      title.append(el("h2", this.current.name), el("small", `${this.c("ends_at")}: ${date(this.current.ends_at, this.language)}`));
      heading.classList.add("corp-manager-heading"); heading.append(back, title); body.append(heading);
      if (Date.parse(this.current.ends_at) <= Date.now()) body.append(el("p", this.t("expired"), "corp-notice"));
      const nav = el("div", undefined, "corp-tabs"); nav.setAttribute("role", "tablist"); nav.setAttribute("aria-label", this.c("manager_title"));
      for (const tab of ["members", "invitations"] as const) {
        const node = button(this.t(tab), () => this.switchTab(tab)); node.id = `corp-manager-tab-${tab}`; node.dataset.focus = `manager-tab-${tab}`;
        node.setAttribute("role", "tab"); node.setAttribute("aria-selected", String(tab === this.tab)); node.setAttribute("aria-controls", `corp-manager-panel-${tab}`);
        node.tabIndex = tab === this.tab ? 0 : -1; node.disabled = this.locked;
        node.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault(); this.switchTab(event.key === "Home" ? "members" : event.key === "End" ? "invitations" : tab === "members" ? "invitations" : "members");
        }); nav.append(node);
      }
      body.append(nav);
      for (const tab of ["members", "invitations"] as const) {
        const node = this.panels.get(tab)?.root ?? el("section"); node.hidden = tab !== this.tab;
        node.id = `corp-manager-panel-${tab}`; node.setAttribute("role", "tabpanel"); node.setAttribute("aria-labelledby", `corp-manager-tab-${tab}`); body.append(node);
      }
    });
  }
  updateProps(props: CustomerProps): void { this.props = props; this.update(props.language); this.panels.forEach((panel) => panel.update(props.language)); }
  override destroy(): void { this.clear(); super.destroy(); }
}
