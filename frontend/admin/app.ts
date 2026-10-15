import { translate } from "../shared/i18n";
import { ApiError, number } from "../shared/api";
import { Panel, button, confirm, date, el, iconButton, person, refreshButton } from "../shared/ui";
import { icon } from "../shared/icons";
import type { CorporateMember } from "../shared/members";
import { contract, list, type Contract } from "./data";
import { Editor } from "./editor";
import { InvitationsPanel } from "../shared/invitations-panel";
import { AdminMembersPanel } from "./members";
import { SynchronizationPanel } from "./synchronization";
import { DraftStore, type EditorDraft, type Tab } from "./draft";
import "../shared/styles.css";
import "../shared/controls.css";
import "../shared/workspace.css";
import "./styles.css";

const tabs: Tab[] = ["terms", "members", "invitations", "sync"];

export class AdminView extends Panel {
  rows: Contract[] = []; after: string | null = null; loaded = false;
  mode: "list" | "new" | "detail" = "list"; current: Contract | null = null; tab: Tab = "terms";
  panels = new Map<Tab, Panel>();
  drafts: DraftStore | null = null;
  restoredEditor: EditorDraft | null = null;
  initialized = false;
  constructor(target: HTMLElement, language: string) {
    super(language); this.root.className = "minishop-corp corp-workspace corp-admin"; target.append(this.root);
    for (const event of ["input", "change", "click"]) this.root.addEventListener(event, () => this.persist(), { signal: this.controller.signal });
    void this.run(() => this.initialize());
  }
  async initialize(): Promise<void> {
    const context = await this.api.request("/options/context");
    if (!this.alive) return;
    this.drafts = new DraftStore(number(context.actor_user_id));
    const state = this.drafts.read();
    await this.load();
    if (state && this.alive) {
      if (state.contractId) {
        try { this.current = contract((await this.api.request(`/contracts/${encodeURIComponent(state.contractId)}`)).contract); }
        catch (error) {
          if (!(error instanceof ApiError) || error.status !== 404) throw error;
          this.drafts.clear(); this.initialized = true; return;
        }
        this.mode = "detail";
      } else if (state.editor) this.mode = "new";
      if (this.mode !== "list") {
        this.restoredEditor = state.editor; this.panel("terms");
        this.tab = this.mode === "new" ? "terms" : state.tab; this.panel(this.tab);
      }
    }
    this.initialized = true;
  }
  persist(): void {
    if (!this.initialized || !this.drafts || !this.alive) return;
    if (this.mode === "list") { this.drafts.clear(); return; }
    const editor = this.panels.get("terms");
    this.drafts.write({ contractId: this.current?.id ?? null, tab: this.tab,
      editor: editor instanceof Editor && (this.mode === "new" || editor.dirty() || editor.pending) ? editor.snapshot() : null });
  }
  override failed(error: unknown): void {
    if (!this.alive) return;
    if (error instanceof ApiError && [401, 403].includes(error.status)) {
      (this.drafts ?? new DraftStore(0)).clear(); this.clearPanels();
      this.current = null; this.mode = "list"; this.rows = []; this.loaded = true; this.error = error;
      this.render();
    }
  }
  get locked(): boolean { return this.busy || Boolean(this.retry) || [...this.panels.values()].some((panel) => panel.busy || panel.retry); }
  async load(after?: string): Promise<void> {
    const rows = list((await this.api.request(`/contracts?limit=25${after ? `&after=${after}` : ""}`)).contracts, contract);
    this.rows = after ? [...this.rows, ...rows] : rows; this.loaded = true;
    this.after = rows.length === 25 ? rows.at(-1)?.id ?? null : null;
  }
  clearPanels(): void { this.panels.forEach((panel) => panel.destroy()); this.panels.clear(); }
  async canLeave(): Promise<boolean> {
    if (this.locked) return false;
    const editor = this.panels.get("terms");
    return !(editor instanceof Editor && editor.dirty()) || confirm(this.root, this.t("discard"), this.t("discard_hint"), this.language, this.controller.signal);
  }
  async open(row: Contract | null): Promise<void> {
    if (!await this.canLeave() || !this.alive) return;
    if (row) await this.run(async () => {
      const latest = contract((await this.api.request(`/contracts/${row.id}`)).contract);
      this.clearPanels(); this.current = latest; this.mode = "detail"; this.tab = "terms"; this.panel("terms");
    });
    else { this.clearPanels(); this.current = null; this.mode = "new"; this.tab = "terms"; this.panel("terms"); this.render(); }
  }
  async back(): Promise<void> {
    if (!await this.canLeave() || !this.alive) return;
    this.clearPanels(); this.mode = "list"; this.current = null; await this.run(() => this.load());
  }
  async assignManager(member: CorporateMember): Promise<void> {
    if (!this.current || this.confirming || !await this.canLeave()) return;
    const current = this.current;
    const revoke = current.manager_user_id === member.profile.user_id;
    if (!await confirm(this.root, this.t(revoke ? "revoke_manager" : "make_manager"), `${person(member.profile)}\n${this.t(revoke ? "revoke_manager_hint" : "make_manager_hint")}`, this.language, this.controller.signal)) return;
    const payload = { expected_version: current.version };
    await this.run(async () => {
      const saved = contract((await this.api.request(`/contracts/${current.id}/members/${member.id}/manager`, revoke ? "DELETE" : "POST", payload)).contract);
      this.current = saved; this.rows = this.rows.map((row) => row.id === saved.id ? saved : row);
      this.panels.get("terms")?.destroy(); this.panels.delete("terms"); this.restoredEditor = null;
      this.panels.get("members")?.render();
      this.notice = revoke ? "admin_minishop_corp_manager_revoked" : "admin_minishop_corp_manager_assigned";
    }, true);
  }
  async removeContract(): Promise<void> {
    if (!this.current || this.confirming || !await this.canLeave()) return;
    const current = this.current;
    if (!await confirm(this.root, this.t("delete_contract"), `${current.name}\n${this.t("delete_contract_hint")}`, this.language, this.controller.signal)) return;
    await this.run(async () => {
      await this.api.request(`/contracts/${current.id}`, "DELETE");
      this.clearPanels(); this.current = null; this.mode = "list"; this.restoredEditor = null;
      this.rows = this.rows.filter((row) => row.id !== current.id); this.drafts?.clear();
      this.notice = "admin_minishop_corp_contract_deleted";
    }, true);
  }
  panel(tab: Tab): Panel {
    const existing = this.panels.get(tab); if (existing) return existing;
    const current = (): Contract => { if (!this.current) throw new Error("missing_contract"); return this.current; };
    const panel = tab === "terms" ? new Editor(this.language, this.current, (value) => {
      if (!this.alive) return;
      this.current = value; this.mode = "detail";
      this.rows = this.rows.map((row) => row.id === value.id ? value : row);
      this.render();
    }, this.restoredEditor) : tab === "members" ? new AdminMembersPanel(this.language, current, (row) => this.assignManager(row))
      : tab === "invitations" ? new InvitationsPanel(this.language, current)
        : new SynchronizationPanel(this.language, current);
    if (tab === "terms") this.restoredEditor = null;
    panel.failed = (error) => this.failed(error);
    panel.root.id = `corp-panel-${tab}`; panel.root.setAttribute("role", "tabpanel");
    panel.root.setAttribute("aria-labelledby", `corp-tab-${tab}`);
    panel.changed = () => { if (this.alive) this.render(); };
    this.panels.set(tab, panel); return panel;
  }
  switchTab(tab: Tab): void {
    if (this.locked) return;
    this.tab = tab; this.panel(tab); this.render();
    this.root.querySelector<HTMLButtonElement>(`#corp-tab-${tab}`)?.focus();
  }
  render(): void {
    this.paint((body) => {
      const heading = el("div", undefined, "corp-heading");
      const title = el("div", undefined, "corp-heading-title"); title.append(el("h2", this.mode === "list" ? this.t("title") : this.mode === "new" ? this.t("new_contract") : this.current?.name));
      heading.append(title);
      if (this.mode === "list") {
        const actions = el("div", undefined, "corp-actions");
        const create = button(this.t("new_contract"), () => { void this.open(null); }, "corp-primary"); create.prepend(icon("plus"));
        actions.append(refreshButton(this.t("refresh"), () => { void this.run(() => this.initialized ? this.load() : this.initialize()); }, this.busy), create);
        heading.append(actions); body.append(heading);
        if (!this.loaded) body.append(el("p", translate(this.language, "wa_minishop_corp_loading")));
        else if (!this.rows.length) {
          const empty = el("p", this.t("empty_contracts"), "corp-empty"); empty.setAttribute("role", "status"); body.append(empty);
        }
        const rows = el("div", undefined, "corp-contract-list");
        for (const row of this.rows) {
          const card = el("article", undefined, "corp-contract"); card.dataset.contract = row.id;
          const open = button(row.name, () => { void this.open(row); }, "corp-link");
          const group = el("div", undefined, "corp-contract-name"); group.append(open, el("small", row.tariff_key));
          const end = el("div", undefined, "corp-contract-date"); end.append(el("small", this.t("ends_at")), el("span", date(row.ends_at, this.language)));
          const expired = Date.parse(row.ends_at) <= Date.now();
          card.append(group, end, el("span", `${this.t("members")}: ${row.member_count}`, "corp-contract-members"),
            el("span", this.t(expired ? "expired" : "active"), `corp-pill corp-status-${expired ? "expired" : "active"}`)); rows.append(card);
        }
        if (this.rows.length) body.append(rows);
        if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
        return;
      }
      const back = iconButton(this.t("back"), "back", () => { void this.back(); }); back.disabled = this.locked; heading.prepend(back); body.append(heading);
      if (this.current) {
        const remove = iconButton(this.t("delete_contract"), "trash", () => { void this.removeContract(); }, "corp-danger");
        remove.disabled = this.locked; heading.append(remove);
        const expired = Date.parse(this.current.ends_at) <= Date.now();
        const info = el("div", undefined, "corp-meta"); info.append(el("span", this.t(expired ? "expired" : "active"), `corp-pill corp-status-${expired ? "expired" : "active"}`),
          el("span", date(this.current.ends_at, this.language)), el("span", `${this.t("members")}: ${this.current.member_count}`)); body.append(info);
        const nav = el("div", undefined, "corp-tabs"); nav.setAttribute("role", "tablist"); nav.setAttribute("aria-label", this.t("title"));
        for (const tab of tabs) {
          const item = button(this.t(tab), () => this.switchTab(tab)); item.id = `corp-tab-${tab}`; item.dataset.focus = `tab-${tab}`;
          item.setAttribute("role", "tab"); item.setAttribute("aria-selected", String(this.tab === tab)); item.setAttribute("aria-controls", `corp-panel-${tab}`);
          item.tabIndex = this.tab === tab ? 0 : -1; item.disabled = this.locked;
          item.addEventListener("keydown", (event) => {
            let index = tabs.indexOf(tab);
            if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
            else if (event.key === "ArrowLeft") index = (index + tabs.length - 1) % tabs.length;
            else if (event.key === "Home") index = 0;
            else if (event.key === "End") index = tabs.length - 1;
            else return;
            event.preventDefault(); const next = tabs[index]; if (next) this.switchTab(next);
          }); nav.append(item);
        } body.append(nav);
      }
      for (const key of this.mode === "new" ? ["terms"] as const : tabs) {
        const panel = this.panels.get(key);
        const node = panel?.root ?? el("section");
        node.id = `corp-panel-${key}`; node.hidden = key !== this.tab;
        node.setAttribute("role", this.mode === "new" ? "region" : "tabpanel");
        if (this.mode === "new") { node.removeAttribute("aria-labelledby"); node.setAttribute("aria-label", this.t(key)); }
        else { node.removeAttribute("aria-label"); node.setAttribute("aria-labelledby", `corp-tab-${key}`); }
        body.append(node);
      }
    });
    this.persist();
  }
  override update(language: string): void { super.update(language); this.panels.forEach((panel) => panel.update(language)); }
  override destroy(): void { this.persist(); this.clearPanels(); super.destroy(); }
}
