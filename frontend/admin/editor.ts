import { ApiError, requestId } from "../shared/api";
import { translate } from "../shared/i18n";
import { Panel, button, confirm, date, el, field, input, person, select } from "../shared/ui";
import { account, contract, list, nullable, number, sameTerms, squad, tariff, terms,
  type Account, type Contract, type Squad, type Tariff, type Terms } from "./data";

export class Editor extends Panel {
  readonly id: string;
  original: Contract | null;
  name: string; tariffKey: string; end: string; managerId: number | null; squadId: string;
  tariffs: Tariff[] = []; accounts: Account[] = []; selected: Account | null = null;
  verified: Squad | null = null; query = ""; nextPage: number | null = null;
  loaded = false;
  constructor(language: string, current: Contract | null, private saved: (value: Contract) => void) {
    super(language); this.original = current; this.id = current?.id ?? requestId();
    this.name = current?.name ?? ""; this.tariffKey = current?.tariff_key ?? "";
    this.end = current ? new Date(current.ends_at).toISOString().slice(0, -1) : "";
    this.managerId = current?.manager_user_id ?? null; this.squadId = current?.external_squad_uuid ?? "";
    this.query = this.managerId === null ? "" : String(this.managerId);
    void this.run(() => this.load());
  }
  async load(): Promise<void> {
    const [plans, users] = await Promise.all([
      this.api.request("/options/tariffs"), this.api.request(`/options/accounts?q=${encodeURIComponent(this.query)}`),
    ]);
    this.tariffs = list(plans.tariffs, tariff); this.accounts = list(users.accounts, account);
    this.nextPage = nullable(users.next_page, number);
    this.selected = this.accounts.find((row) => row.user_id === this.managerId) ?? this.selected;
    this.loaded = true;
    if (this.squadId && !this.verified) this.verified = squad((await this.api.request(`/options/squad?uuid=${encodeURIComponent(this.squadId)}`)).squad);
  }
  dirty(): boolean {
    const old = this.original;
    return old ? this.name !== old.name || this.tariffKey !== old.tariff_key || this.squadId !== old.external_squad_uuid
      || this.managerId !== old.manager_user_id || this.end !== new Date(old.ends_at).toISOString().slice(0, -1)
      : Boolean(this.name || this.tariffKey || this.end || this.managerId || this.squadId);
  }
  async search(page = 0): Promise<void> {
    const result = await this.api.request(`/options/accounts?q=${encodeURIComponent(this.query)}&page=${page}`);
    this.accounts = page ? [...this.accounts, ...list(result.accounts, account)] : list(result.accounts, account);
    this.nextPage = nullable(result.next_page, number);
  }
  async submit(): Promise<void> {
    if (this.verified?.uuid !== this.squadId) { this.error = new ApiError("admin_minishop_corp_squad_required", 400); this.render(); return; }
    if (this.managerId === null && this.original?.manager_user_id !== null) return;
    const draft: Terms = { name: this.name.trim(), tariff_key: this.tariffKey, external_squad_uuid: this.squadId,
      ends_at: new Date(this.end + "Z").toISOString(), manager_user_id: this.managerId };
    if (this.original && !await confirm(this.root, this.t("confirm_terms"), `${this.t("confirm_terms_hint")}\n${draft.name} · ${draft.tariff_key} · ${date(draft.ends_at, this.language)}`, this.language, this.controller.signal)) return;
    const previous = this.original;
    await this.run(async () => {
      let result: Contract;
      try {
        result = contract((await this.api.request(previous ? `/contracts/${this.id}` : "/contracts", previous ? "PUT" : "POST",
          previous ? { ...draft, expected_version: previous.version } : { ...draft, id: this.id })).contract);
      } catch (error) {
        if (!previous || !(error instanceof ApiError) || error.code !== "minishop_corp_version_conflict") throw error;
        // A lost PUT response may be followed by a version conflict on retry.
        const latest = contract((await this.api.request(`/contracts/${this.id}`)).contract);
        if (!sameTerms(draft, terms(latest))) throw error;
        result = latest;
      }
      this.original = result; this.name = result.name; this.tariffKey = result.tariff_key;
      this.end = new Date(result.ends_at).toISOString().slice(0, -1);
      this.managerId = result.manager_user_id; this.squadId = result.external_squad_uuid;
      this.notice = "admin_minishop_corp_saved";
      if (this.alive) this.saved(result);
    }, true);
  }
  render(): void {
    this.paint((body) => {
      const actions = el("div", undefined, "corp-actions");
      actions.append(button(this.t("refresh"), () => { void this.run(() => this.load()); }));
      if (this.original) actions.append(button(this.t("reload_saved"), () => { void this.reload(); }));
      body.append(actions);
      if (!this.loaded) { body.append(el("p", translate(this.language, "wa_minishop_corp_loading"))); return; }
      const form = el("form", undefined, "corp-form");
      const name = input("name", this.name, (v) => { this.name = v; }); name.required = true; name.maxLength = 200;
      const plans: [string, string][] = [["", this.t("choose")], ...this.tariffs.map((v): [string, string] => [v.key,
        `${v.names[this.language.startsWith("ru") ? "ru" : "en"]} · ${v.key}${v.hidden ? ` (${this.t("hidden")})` : ""}`])];
      if (this.tariffKey && !this.tariffs.some((v) => v.key === this.tariffKey)) plans.push([this.tariffKey, this.tariffKey]);
      const plan = select("tariff", this.tariffKey, plans, (v) => { this.tariffKey = v; }); plan.required = true;
      const end = input("ends_at", this.end, (v) => { this.end = v; }, "datetime-local"); end.required = true; end.step = "0.001";
      form.append(field(this.t("name"), name), field(this.t("tariff"), plan), field(this.t("ends_at"), end, this.t("date_hint")));
      if (!this.tariffs.length) form.append(el("p", this.t("no_tariffs"), "corp-alert"));
      const squadBox = el("div", undefined, "corp-wide");
      const squadInput = input("squad", this.squadId, (v) => { this.squadId = v.trim().toLowerCase(); this.verified = null; }); squadInput.required = true;
      squadBox.append(field(this.t("squad"), squadInput, this.t("squad_hint")), button(this.t("verify_squad"), () => {
        void this.run(async () => { this.verified = squad((await this.api.request(`/options/squad?uuid=${encodeURIComponent(this.squadId)}`)).squad); });
      }));
      if (this.verified?.uuid === this.squadId) squadBox.append(el("p", `${this.t("squad_verified")}: ${this.verified.name}`, "corp-notice"));
      const managers = el("div", undefined, "corp-wide corp-stack");
      if (this.original?.manager_user_id === null) managers.append(el("p", this.t("manager_missing"), "corp-alert"));
      const search = input("account_query", this.query, (v) => { this.query = v; }); search.maxLength = 100;
      managers.append(field(this.t("account_query"), search), button(this.t("search"), () => { void this.run(() => this.search()); }));
      const users = new Map(this.accounts.map((row) => [row.user_id, row]));
      if (this.selected) users.set(this.selected.user_id, this.selected);
      const choices: [string, string][] = [["", this.t("choose")], ...[...users.values()].map((v): [string, string] => [String(v.user_id), `${person(v)} · #${v.user_id}${v.username ? ` · @${v.username}` : ""}`])];
      if (this.managerId !== null && !users.has(this.managerId)) choices.push([String(this.managerId), `#${this.managerId}`]);
      const manager = select("manager", this.managerId === null ? "" : String(this.managerId), choices, (v) => {
        this.managerId = v ? Number(v) : null; this.selected = users.get(Number(v)) ?? null;
      }); manager.required = this.original?.manager_user_id !== null;
      managers.append(field(this.t("manager"), manager, this.t("manager_hint")));
      if (!this.accounts.length) managers.append(el("p", this.t("empty_accounts")));
      if (this.nextPage !== null) managers.append(button(this.t("load_more"), () => { void this.run(() => this.search(this.nextPage ?? 0)); }));
      const save = el("button", this.t("save"), "corp-button corp-primary"); save.type = "submit";
      form.append(squadBox, managers, save);
      form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.submit(); });
      body.append(form, el("p", this.t("conditions_notice"), "corp-footnote"));
    });
  }
  async reload(): Promise<void> {
    if (this.dirty() && !await confirm(this.root, this.t("discard"), this.t("discard_hint"), this.language, this.controller.signal)) return;
    await this.run(async () => {
      const current = contract((await this.api.request(`/contracts/${this.id}`)).contract);
      this.original = current; this.name = current.name; this.tariffKey = current.tariff_key;
      this.end = new Date(current.ends_at).toISOString().slice(0, -1);
      this.managerId = current.manager_user_id; this.squadId = current.external_squad_uuid;
      this.query = this.managerId === null ? "" : String(this.managerId); this.selected = null;
      this.verified = null; await this.load();
      if (this.alive) this.saved(current);
    });
  }
}
