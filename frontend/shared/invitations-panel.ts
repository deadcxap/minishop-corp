import { translate } from "./i18n";
import { ApiError, requestId, type ApiClient } from "./api";
import { Panel, button, calendarDate, confirm, el, field, iconButton, input, refreshButton, select } from "./ui";
import { icon } from "./icons";
import { invitationCopy, type InvitationSecret } from "./invitation-code";
import { invitation, issued, list, type Invitation } from "../admin/data";

export class InvitationsPanel extends Panel {
  rows: Invitation[] = []; after: string | null = null; loaded = false;
  kind = "single"; limit = "10";
  editing: string | null = null; editLimit = "";
  private copying: { id: string; key: InvitationSecret } | null = null;
  constructor(language: string, private current: () => { id: string; ends_at: string }, private scope: "admin" | "manager" = "admin", client?: (signal: AbortSignal) => ApiClient) {
    super(language, client); void this.run(() => this.load());
  }
  get path(): string { return `/${this.scope === "admin" ? "contracts" : "managed-contracts"}/${this.current().id}/invitations`; }
  async load(after?: string): Promise<void> {
    const page = list((await this.api.request(`${this.path}?limit=25${after ? `&after=${after}` : ""}`)).invitations, invitation);
    this.rows = after ? [...new Map([...this.rows, ...page].map((row) => [row.id, row])).values()] : page;
    this.after = page.length === 25 ? page.at(-1)?.id ?? null : null; this.loaded = true;
  }
  async issue(row?: Invitation): Promise<void> {
    if (row && !await confirm(this.root, this.t("confirm_rotate"), `${this.t("code_change_hint")} ${this.t("rotation_hint")}`, this.language, this.controller.signal)) return;
    const path = row ? `${this.path}/${row.id}/rotate` : this.path;
    const id = requestId();
    const payload = row ? { id } : { id, kind: this.kind, use_limit: this.kind === "single" ? 1 : Number(this.limit) };
    await this.run(async () => {
      const result = issued(await this.api.request(path, "POST", payload));
      this.rows = this.rows.filter((value) => value.id !== result.invitation.id);
      if (row) this.rows = this.rows.map((value) => value.id === row.id ? { ...value, revoked_at: result.invitation.created_at } : value);
      if (this.scope === "manager") this.rows = this.rows.filter((value) => !value.revoked_at);
      this.rows.unshift(result.invitation); this.loaded = true;
    }, true);
  }
  async revoke(row: Invitation): Promise<void> {
    if (!await confirm(this.root, this.t("confirm_revoke"), this.t("code_change_hint"), this.language, this.controller.signal)) return;
    await this.run(async () => {
      const result = invitation((await this.api.request(`${this.path}/${row.id}/revoke`, "POST", {})).invitation);
      this.rows = this.rows.map((value) => value.id === result.id ? result : value);
      this.notice = "admin_minishop_corp_revoked_notice";
    }, true);
  }
  async saveLimit(row: Invitation): Promise<void> {
    const payload = { expected_version: row.version, use_limit: Number(this.editLimit) };
    await this.run(async () => {
      const saved = invitation((await this.api.request(`${this.path}/${row.id}`, "PUT", payload)).invitation);
      this.rows = this.rows.map((value) => value.id === saved.id ? saved : value); this.editing = null;
      this.notice = "admin_minishop_corp_invitation_updated";
    }, true);
    if (this.editing === null) this.focusEdit(row.id);
  }
  private focusEdit(id: string): void { this.root.querySelector<HTMLElement>(`[data-focus="edit_limit-${id}"]`)?.focus(); }
  async remove(row: Invitation): Promise<void> {
    if (!await confirm(this.root, this.t("delete_invitation"), this.t("delete_invitation_hint"), this.language, this.controller.signal)) return;
    await this.run(async () => {
      await this.api.request(`${this.path}/${row.id}`, "DELETE");
      this.rows = this.rows.filter((value) => value.id !== row.id);
      this.notice = "admin_minishop_corp_invitation_deleted";
    }, true);
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) { this.rows = []; this.copying = null; this.editing = null; }
  }
  render(): void {
    const scrollLeft = this.root.querySelector(".corp-table-scroll")?.scrollLeft ?? 0;
    this.paint((body) => {
      const current = this.current(), expired = Date.parse(current.ends_at) <= Date.now();
      const refresh = refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy);
      if (expired) {
        const heading = el("div", undefined, "corp-panel-heading"); heading.append(el("h3", this.t("invitations")), refresh);
        body.append(heading, el("p", this.t("expired_codes"), "corp-notice"));
      }
      else {
        const form = el("form", undefined, "corp-inline-form");
        const kind = select("kind", this.kind, [["single", this.t("single")], ["reusable", this.t("reusable")]], (value) => { this.kind = value; this.render(); });
        form.append(field(this.t("code_kind"), kind));
        if (this.kind === "reusable") {
          const limit = input("use_limit", this.limit, (value) => { this.limit = value; }, "number");
          limit.min = "1"; limit.max = "2147483647"; limit.step = "1"; limit.required = true;
          form.append(field(this.t("use_limit"), limit));
        }
        const generate = el("button", this.t("generate"), "corp-button corp-primary"); generate.type = "submit"; form.append(generate, refresh);
        form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.issue(); }); body.append(form);
      }
      if (!this.loaded) body.append(el("p", translate(this.language, "wa_minishop_corp_loading")));
      else if (!this.rows.length) body.append(el("p", this.t("empty_invitations"), "corp-empty"));
      if (!this.rows.length) return;
      const scroll = el("div", undefined, "corp-table-scroll");
      scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", this.t("invitations"));
      scroll.dataset.focus = "invitations-table";
      const table = el("table", undefined, "corp-invitations");
      table.append(el("caption", this.t("invitations"), "corp-sr-only"));
      const head = el("thead"), headings = el("tr");
      for (const key of ["invitation_type", "issued_at", "usage_limit", "copy", "actions"] as const) {
        const cell = el("th", this.t(key)); cell.scope = "col"; headings.append(cell);
      }
      head.append(headings); table.append(head);
      for (const row of this.rows) {
        const group = el("tbody"); group.dataset.invitation = row.id;
        const line = el("tr");
        const status = row.revoked_at ? "revoked" : expired ? "expired" : row.used_count >= row.use_limit ? "exhausted"
          : row.used_count + row.reserved_count >= row.use_limit ? "capacity_reserved" : "active";
        group.dataset.status = status;
        const kind = el("td"), symbol = el("span", undefined, "corp-code-kind");
        symbol.title = `${this.t(row.kind)} · ${this.t(status)}`;
        symbol.setAttribute("role", "img"); symbol.setAttribute("aria-label", symbol.title); symbol.tabIndex = 0;
        symbol.append(icon(row.kind === "single" ? "ticket" : "repeat")); kind.append(symbol);
        const created = el("td"), stamp = el("time", calendarDate(row.created_at, this.language)); stamp.dateTime = row.created_at; created.append(stamp);
        const usage = el("td", undefined, "corp-invitation-usage");
        usage.append(el("span", `${row.used_count} / ${row.use_limit}`, "corp-usage-count"));
        if (row.reserved_count > 0) {
          const reserved = el("small", `+${row.reserved_count}`, "corp-reserved-count");
          reserved.title = `${this.t("reserved")}: ${row.reserved_count}`;
          reserved.setAttribute("aria-label", reserved.title); usage.append(reserved);
        }
        if (status !== "active") usage.append(el("small", this.t(status), "corp-invitation-status"));
        const copies = el("td");
        copies.append(invitationCopy(this, row, (key) => {
          this.copying = { id: row.id, key }; this.render();
          const field = this.root.querySelector<HTMLInputElement>('[name="copy_value"]'); field?.focus(); field?.select();
        }));
        const actions = el("td"), tools = el("div", undefined, "corp-row-actions");
        const action = (label: Parameters<Panel["t"]>[0], name: Parameters<typeof iconButton>[1], run: () => void, style = ""): HTMLButtonElement => {
          const node = iconButton(this.t(label), name, run, `corp-ghost ${style}`);
          node.dataset.focus = `${label}-${row.id}`; return node;
        };
        if (!row.revoked_at) {
          if (row.kind === "reusable" && !expired) {
            tools.append(action("rotate", "refresh", () => { void this.issue(row); }), action("edit_limit", "edit", () => {
              this.editing = row.id; this.editLimit = String(row.use_limit); this.render();
              this.root.querySelector<HTMLInputElement>('[name="edit_use_limit"]')?.focus();
            }));
          }
          if (this.scope === "admin") tools.append(action("revoke", "ban", () => { void this.revoke(row); }, "corp-danger"));
        }
        if (this.scope === "admin") {
          const remove = action("delete_invitation", "trash", () => { void this.remove(row); }, "corp-danger");
          remove.disabled = row.reserved_count > 0;
          if (remove.disabled) remove.title = translate(this.language, "minishop_corp_invitation_busy");
          tools.append(remove);
        }
        actions.append(tools); line.append(kind, created, usage, copies, actions); group.append(line);
        const detail = el("td"); detail.colSpan = 5;
        if (this.editing === row.id && !row.revoked_at && !expired) {
          const form = el("form", undefined, "corp-inline-form");
          const limit = input("edit_use_limit", this.editLimit, (value) => { this.editLimit = value; }, "number");
          limit.min = String(Math.max(1, row.used_count + row.reserved_count)); limit.max = "2147483647"; limit.step = "1"; limit.required = true;
          form.append(field(this.t("use_limit"), limit, this.t("edit_limit_hint")));
          const save = el("button", this.t("save"), "corp-button corp-primary"); save.type = "submit"; form.append(save,
            button(this.t("cancel"), () => { this.editing = null; this.render(); this.focusEdit(row.id); }));
          form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.saveLimit(row); });
          detail.append(form);
        }
        if (this.copying?.id === row.id) {
          const key = this.copying.key, value = row[key];
          if (value) {
            const fallback = el("div", undefined, "corp-copy-fallback");
            const text = input("copy_value", value, () => {}); text.readOnly = true; text.autocomplete = "off"; text.spellcheck = false;
            text.addEventListener("focus", () => text.select());
            const close = (): void => { this.copying = null; this.render(); this.root.querySelector<HTMLElement>(`[data-focus="copy-${key}-${row.id}"]`)?.focus(); };
            text.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
            fallback.append(field(this.t(key), text, this.t("copy_failed")), iconButton(this.t("close"), "close", close, "corp-ghost"));
            detail.append(fallback);
          }
        }
        if (detail.hasChildNodes()) { const details = el("tr", undefined, "corp-invitation-detail"); details.append(detail); group.append(details); }
        table.append(group);
      }
      scroll.append(table); body.append(scroll);
      if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
    });
    const scroll = this.root.querySelector(".corp-table-scroll"); if (scroll) scroll.scrollLeft = scrollLeft;
  }
  override destroy(): void { this.rows = []; this.copying = null; super.destroy(); }
}
