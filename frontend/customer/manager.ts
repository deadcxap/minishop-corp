import { ApiError, requestId } from "../shared/api";
import type { CustomerProps } from "../shared/host";
import { customerText, type CustomerKey } from "../shared/i18n";
import { MembersPanel } from "../shared/member-panel";
import { Panel, button, confirm, date, el } from "../shared/ui";
import { invitationCode } from "../shared/invitation-code";
import { invitation, issued, type Invitation } from "../admin/data";
import { CustomerApi } from "./api";
import { list, summary, type Summary } from "./data";

const denied = (error: unknown): boolean => error instanceof ApiError && [401, 403, 404].includes(error.status);

class ManagerCode extends Panel {
  row: Invitation | null = null; loaded = false;
  constructor(language: string, private current: () => Summary, host: () => CustomerProps["host"]) {
    super(language, (signal) => new CustomerApi(host, signal)); void this.run(() => this.load());
  }
  c(key: CustomerKey): string { return customerText(this.language, key); }
  get path(): string { return `/managed-contracts/${this.current().id}/invitations`; }
  async load(): Promise<void> {
    this.row = list((await this.api.request(this.path)).invitations, invitation)[0] ?? null;
    this.loaded = true;
  }
  override failed(error: unknown): void { if (denied(error)) this.row = null; }
  async rotate(): Promise<void> {
    const row = this.row;
    if (!row || this.busy || this.retry) return;
    if (!await confirm(this.root, this.t("confirm_rotate"), `${this.t("code_change_hint")} ${this.t("rotation_hint")}`, this.language, this.controller.signal)) return;
    const payload = { id: requestId() };
    await this.run(async () => {
      const result = issued(await this.api.request(`${this.path}/${row.id}/rotate`, "POST", payload));
      this.row = result.invitation;
    }, true);
  }
  render(): void {
    this.paint((body) => {
      body.append(button(this.t("refresh"), () => { void this.run(() => this.load()); }));
      if (!this.loaded) { body.append(el("p", this.c("loading"))); return; }
      if (!this.row) { body.append(el("p", this.c("manager_no_code"), "corp-empty")); return; }
      const row = this.row, expired = Date.parse(this.current().ends_at) <= Date.now();
      body.append(el("h3", this.t("reusable")), el("p", `${this.t("used")}: ${row.used_count} / ${row.use_limit} · ${this.t("reserved")}: ${row.reserved_count}`));
      if (expired) body.append(el("p", this.t("expired_codes"), "corp-notice"));
      else body.append(button(this.t("rotate"), () => { void this.rotate(); }, "corp-primary"));
      body.append(invitationCode(this, row));
    });
  }
  override destroy(): void { this.row = null; super.destroy(); }
}

type Tab = "members" | "invitations";
export class ManagerView extends Panel {
  rows: Summary[] = []; after: string | null = null; loaded = false; current: Summary | null = null;
  tab: Tab = "members"; panels = new Map<Tab, Panel>();
  constructor(target: HTMLElement, public props: CustomerProps) {
    super(props.language, (signal) => new CustomerApi(() => this.props.host, signal));
    this.root.className = "minishop-corp corp-customer"; target.append(this.root);
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
      : new ManagerCode(this.language, current, host);
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
      body.append(el("h2", this.c("manager_title")), el("p", this.c("manager_hint")));
      if (!this.current) {
        body.append(button(this.t("refresh"), () => { void this.run(() => this.load()); }));
        if (!this.loaded) body.append(el("p", this.c("loading")));
        else if (!this.rows.length) body.append(el("p", this.c("manager_empty"), "corp-empty"));
        for (const row of this.rows) {
          const card = el("div", undefined, "corp-card"); card.append(button(row.name, () => { void this.open(row); }, "corp-link"), el("p", date(row.ends_at, this.language))); body.append(card);
        }
        if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
        return;
      }
      const back = button(this.t("back"), () => { this.clear(); void this.run(() => this.load()); }); back.disabled = this.locked;
      body.append(back, el("h3", this.current.name), el("p", date(this.current.ends_at, this.language)));
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
