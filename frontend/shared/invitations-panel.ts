import { translate } from "./i18n";
import { ApiError, requestId, type ApiClient } from "./api";
import { Panel, button, confirm, date, el, field, input, refreshButton, select } from "./ui";
import { invitationCode } from "./invitation-code";
import { invitation, issued, list, type Invitation } from "../admin/data";

export class InvitationsPanel extends Panel {
  rows: Invitation[] = []; after: string | null = null; loaded = false;
  kind = "single"; limit = "10";
  editing: string | null = null; editLimit = "";
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
  }
  async remove(row: Invitation): Promise<void> {
    if (!await confirm(this.root, this.t("delete_invitation"), this.t("delete_invitation_hint"), this.language, this.controller.signal)) return;
    await this.run(async () => {
      await this.api.request(`${this.path}/${row.id}`, "DELETE");
      this.rows = this.rows.filter((value) => value.id !== row.id);
      this.notice = "admin_minishop_corp_invitation_deleted";
    }, true);
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) this.rows = [];
  }
  render(): void {
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
      const rows = el("div", undefined, "corp-stack");
      for (const row of this.rows) {
        const card = el("article", undefined, "corp-card"); card.dataset.invitation = row.id;
        const status = row.revoked_at ? "revoked" : expired ? "expired" : row.used_count >= row.use_limit ? "exhausted"
          : row.used_count + row.reserved_count >= row.use_limit ? "capacity_reserved" : "active";
        const heading = el("div", undefined, "corp-heading"); heading.append(el("h3", this.t(row.kind)), el("span", this.t(status), "corp-pill"));
        card.append(heading, el("p", `${this.t("issued_at")}: ${date(row.created_at, this.language)}`),
          el("p", `${this.t("used")}: ${row.used_count} / ${row.use_limit} · ${this.t("reserved")}: ${row.reserved_count}`));
        card.append(invitationCode(this, row));
        const tools = el("div", undefined, "corp-actions");
        if (!row.revoked_at) {
          if (row.kind === "reusable" && !expired) {
            tools.append(button(this.t("rotate"), () => { void this.issue(row); }), button(this.t("edit_limit"), () => {
              this.editing = row.id; this.editLimit = String(row.use_limit); this.render();
            }));
          }
          if (this.scope === "admin") tools.append(button(this.t("revoke"), () => { void this.revoke(row); }, "corp-danger"));
        }
        if (this.scope === "admin") {
          const remove = button(this.t("delete_invitation"), () => { void this.remove(row); }, "corp-danger");
          remove.disabled = row.reserved_count > 0;
          if (remove.disabled) card.append(el("small", translate(this.language, "minishop_corp_invitation_busy")));
          tools.append(remove);
        }
        card.append(tools);
        if (this.editing === row.id && !row.revoked_at && !expired) {
          const form = el("form", undefined, "corp-inline-form");
          const limit = input("edit_use_limit", this.editLimit, (value) => { this.editLimit = value; }, "number");
          limit.min = String(Math.max(1, row.used_count + row.reserved_count)); limit.max = "2147483647"; limit.step = "1"; limit.required = true;
          form.append(field(this.t("use_limit"), limit, this.t("edit_limit_hint")));
          const save = el("button", this.t("save"), "corp-button corp-primary"); save.type = "submit"; form.append(save,
            button(this.t("cancel"), () => { this.editing = null; this.render(); }));
          form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.saveLimit(row); });
          card.append(form);
        }
        rows.append(card);
      }
      body.append(rows);
      if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
    });
  }
  override destroy(): void { this.rows = []; super.destroy(); }
}
