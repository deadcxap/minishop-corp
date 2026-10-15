import { requestId } from "../shared/api";
import { MembersPanel } from "../shared/member-panel";
import type { CorporateMember } from "../shared/members";
import { button, confirm, date, el, field, input, person, refreshButton } from "../shared/ui";
import { account, contract, list, operation, type Account, type Contract } from "./data";

export class AdminMembersPanel extends MembersPanel {
  query = ""; candidate: Account | null = null; searched = false;
  constructor(language: string, private contract: () => Contract, promote: (row: CorporateMember) => Promise<void>) {
    super(language, contract, "admin", undefined, promote);
  }
  async search(): Promise<void> {
    if (!this.query.trim()) return;
    const result = await this.api.request(`/options/accounts?q=${encodeURIComponent(this.query.trim())}`);
    this.candidate = list(result.accounts, account)[0] ?? null; this.searched = true;
  }
  async add(candidate: Account): Promise<void> {
    let current: Contract | null = null;
    await this.run(async () => { current = contract((await this.api.request(`/contracts/${this.contract().id}`)).contract); });
    if (!current || !this.alive) return;
    const accepted: Contract = current;
    const payload = { request_id: requestId(), user_id: candidate.user_id, expected_version: accepted.version };
    const details = `${person(candidate)} · ${candidate.minishop_id}\n${accepted.name} · ${accepted.tariff_key} · ${date(accepted.ends_at, this.language)}\n${this.t("add_member_hint")}`;
    if (!await confirm(this.root, this.t("add_member"), details, this.language, this.controller.signal)) return;
    await this.run(async () => {
      operation((await this.api.request(this.path, "POST", payload)).operation);
      this.candidate = null; this.query = ""; this.searched = false;
      this.notice = "admin_minishop_corp_member_queued";
      await this.load();
    }, true);
  }
  protected override renderActions(body: HTMLElement): void {
    const form = el("form", undefined, "corp-inline-form");
    const search = input("member_query", this.query, (value) => {
      this.query = value; this.candidate = null; this.searched = false; this.render();
    });
    search.maxLength = 320; search.required = true; search.autocomplete = "off"; search.placeholder = this.t("account_query");
    form.append(field(this.t("add_member"), search));
    const find = el("button", this.t("search"), "corp-button"); find.type = "submit"; find.disabled = !this.query.trim();
    form.append(find, refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy));
    form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.run(() => this.search()); });
    body.append(form);
    if (this.candidate) {
      const candidate = this.candidate;
      const card = el("div", undefined, "corp-card");
      card.append(el("p", `${person(candidate)} · ${candidate.minishop_id}${candidate.username ? ` · @${candidate.username}` : ""}${candidate.telegram_id ? ` · ${this.t("telegram_id")}: ${candidate.telegram_id}` : ""}`),
        button(this.t("add_member"), () => { void this.add(candidate); }, "corp-primary"));
      body.append(card);
    } else if (this.searched) body.append(el("p", this.t("empty_accounts"), "corp-empty"));
  }
}
