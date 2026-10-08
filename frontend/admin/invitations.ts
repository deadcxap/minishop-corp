import { translate } from "../shared/i18n";
import { ApiError, requestId } from "../shared/api";
import { Panel, button, confirm, date, el, field, input, select } from "../shared/ui";
import { invitation, issued, list, type Contract, type Invitation, type Issued } from "./data";

export class InvitationsPanel extends Panel {
  rows: Invitation[] = []; after: string | null = null; loaded = false;
  kind = "single"; limit = "10"; secret: Issued | null = null;
  constructor(language: string, private current: () => Contract) { super(language); void this.run(() => this.load()); }
  get path(): string { return `/contracts/${this.current().id}/invitations`; }
  async load(after?: string): Promise<void> {
    const page = list((await this.api.request(`${this.path}?limit=25${after ? `&after=${after}` : ""}`)).invitations, invitation);
    this.rows = after ? [...this.rows, ...page] : page;
    this.after = page.length === 25 ? page.at(-1)?.id ?? null : null; this.loaded = true;
  }
  async issue(row?: Invitation): Promise<void> {
    if (row && !await confirm(this.root, this.t("confirm_rotate"), `${this.t("code_change_hint")} ${this.t("rotation_hint")}`, this.language, this.controller.signal)) return;
    const path = row ? `${this.path}/${row.id}/rotate` : this.path;
    const id = requestId();
    const payload = row ? { id } : { id, kind: this.kind, use_limit: this.kind === "single" ? 1 : Number(this.limit) };
    await this.run(async () => {
      try { this.secret = issued(await this.api.request(path, "POST", payload)); }
      catch (error) {
        // A rotation may already have revoked the displayed credential.
        if (!(error instanceof ApiError) || error.uncertain) this.secret = null;
        throw error;
      }
      // Keep an issued credential even if refreshing the history fails later.
    }, true);
    if (this.alive && !this.retry && !this.error) await this.run(() => this.load());
  }
  async revoke(row: Invitation): Promise<void> {
    if (!await confirm(this.root, this.t("confirm_revoke"), this.t("code_change_hint"), this.language, this.controller.signal)) return;
    await this.run(async () => {
      const result = invitation((await this.api.request(`${this.path}/${row.id}/revoke`, "POST", {})).invitation);
      this.rows = this.rows.map((value) => value.id === result.id ? result : value);
      if (this.secret?.invitation.id === result.id) this.secret = null;
      this.notice = "admin_minishop_corp_revoked_notice";
    }, true);
  }
  copy(value: string): void {
    void this.run(async () => {
      try { await navigator.clipboard.writeText(value); this.notice = "admin_minishop_corp_copied"; }
      catch { this.notice = "admin_minishop_corp_copy_failed"; }
    });
  }
  render(): void {
    this.paint((body) => {
      const current = this.current(), expired = Date.parse(current.ends_at) <= Date.now();
      const actions = el("div", undefined, "corp-actions");
      actions.append(button(this.t("refresh"), () => { void this.run(() => this.load()); })); body.append(actions);
      if (expired) body.append(el("p", this.t("expired_codes"), "corp-notice"));
      else {
        const form = el("form", undefined, "corp-inline-form");
        const kind = select("kind", this.kind, [["single", this.t("single")], ["reusable", this.t("reusable")]], (value) => { this.kind = value; this.render(); });
        form.append(field(this.t("code_kind"), kind));
        if (this.kind === "reusable") {
          const limit = input("use_limit", this.limit, (value) => { this.limit = value; }, "number");
          limit.min = "1"; limit.max = "2147483647"; limit.step = "1"; limit.required = true;
          form.append(field(this.t("use_limit"), limit));
        }
        const generate = el("button", this.t("generate"), "corp-button corp-primary"); generate.type = "submit"; form.append(generate);
        form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.issue(); }); body.append(form);
      }
      if (this.secret) {
        const box = el("section", undefined, "corp-secret"); box.setAttribute("aria-label", this.t("code"));
        box.append(el("p", translate(this.language, "minishop_corp_code_shown_once")));
        if (!this.secret.code) box.append(el("p", this.t("code_lost"), "corp-notice"));
        for (const [key, value] of [["code", this.secret.code], ["link", this.secret.link]] as const) {
          if (!value) continue;
          const text = input(key, value, () => {}); text.readOnly = true; text.autocomplete = "off"; text.spellcheck = false;
          text.addEventListener("focus", () => text.select());
          box.append(field(this.t(key), text), button(this.t("copy"), () => this.copy(value)));
        }
        if (this.secret.code && !this.secret.link) box.append(el("p", this.t("link_missing")));
        box.append(button(this.t("hide_code"), () => { this.secret = null; this.render(); })); body.append(box);
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
        if (!row.revoked_at) {
          const tools = el("div", undefined, "corp-actions");
          if (row.kind === "reusable" && !expired) tools.append(button(this.t("rotate"), () => { void this.issue(row); }));
          tools.append(button(this.t("revoke"), () => { void this.revoke(row); }, "corp-danger")); card.append(tools);
        }
        rows.append(card);
      }
      body.append(rows);
      if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
    });
  }
  override destroy(): void { this.secret = null; super.destroy(); }
}
