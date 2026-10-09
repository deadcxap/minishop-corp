import { ApiError, requestId } from "../shared/api";
import { translate, type AdminKey } from "../shared/i18n";
import { Panel, button, confirm, date, el, field, input, person, select } from "../shared/ui";
import { account, contract, list, sameTerms, squad, tariff, terms,
  type Account, type Contract, type Squad, type Tariff, type Terms } from "./data";
import { addPeriod, calendarDay, endOfDay, expiryDay } from "./calendar";
import type { EditorDraft, Submission } from "./draft";

const periods: [AdminKey, number, number][] = [
  ["add_day", 1, 0], ["add_week", 7, 0], ["add_month", 0, 1],
  ["add_three_months", 0, 3], ["add_six_months", 0, 6], ["add_year", 0, 12],
];

export class Editor extends Panel {
  readonly id: string;
  original: Contract | null;
  name: string; tariffKey: string; end: string; managerId: number | null; squadId: string;
  readonly initialEnd: string;
  endTouched = false;
  tariffs: Tariff[] = []; selected: Account | null = null; candidate: Account | null = null;
  verified: Squad | null = null; query = ""; searched = false;
  loaded = false;
  searchDiagnostic: string | null = null;
  pending: Submission | null = null;
  constructor(language: string, current: Contract | null, private saved: (value: Contract) => void, draft: EditorDraft | null = null) {
    super(language); this.original = current; this.id = draft?.id ?? current?.id ?? requestId();
    this.name = current?.name ?? ""; this.tariffKey = current?.tariff_key ?? "";
    this.end = current ? expiryDay(current.ends_at) : calendarDay(); this.initialEnd = draft?.initialEnd ?? this.end;
    this.managerId = current?.manager_user_id ?? null; this.squadId = current?.external_squad_uuid ?? "";
    this.query = this.managerId === null ? "" : String(this.managerId);
    if (draft) {
      this.original = draft.original; this.name = draft.name; this.tariffKey = draft.tariffKey;
      this.end = draft.end; this.endTouched = draft.endTouched; this.managerId = draft.managerId;
      this.squadId = draft.squadId; this.query = draft.query; this.pending = draft.pending;
    }
    void this.run(() => this.load()).then(() => {
      const pending = this.pending;
      if (this.alive && pending) {
        this.retry = () => this.deliver(pending); this.render(); this.changed();
      }
    });
  }
  snapshot(): EditorDraft {
    return { id: this.id, original: this.original, name: this.name, tariffKey: this.tariffKey,
      end: this.end, initialEnd: this.initialEnd, endTouched: this.endTouched,
      managerId: this.managerId, squadId: this.squadId, query: this.query, pending: this.pending };
  }
  async load(): Promise<void> {
    this.tariffs = list((await this.api.request("/options/tariffs")).tariffs, tariff);
    if (this.managerId !== null) {
      const result = await this.api.request(`/options/accounts?user_id=${this.managerId}`);
      this.selected = list(result.accounts, account).find((row) => row.user_id === this.managerId) ?? null;
      if (this.selected && this.query === String(this.managerId)) this.query = this.selected.minishop_id;
    }
    this.loaded = true;
    if (/^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(this.squadId) && !this.verified) {
      this.verified = squad((await this.api.request(`/options/squad?uuid=${encodeURIComponent(this.squadId)}`)).squad);
    }
  }
  expiry(): string { return this.original && !this.endTouched ? this.original.ends_at : endOfDay(this.end); }
  dirty(): boolean {
    const old = this.original;
    return old ? this.name !== old.name || this.tariffKey !== old.tariff_key || this.squadId !== (old.external_squad_uuid ?? "")
      || this.managerId !== old.manager_user_id || (this.managerId === null && Boolean(this.query.trim())) || this.endTouched
      : Boolean(this.name || this.tariffKey || this.managerId || this.squadId || this.query || this.end !== this.initialEnd);
  }
  async search(): Promise<void> {
    if (!this.query.trim()) return;
    const result = await this.api.request(`/options/accounts?q=${encodeURIComponent(this.query.trim())}`);
    this.searchDiagnostic = this.api.lastRequestId ?? null;
    this.candidate = list(result.accounts, account)[0] ?? null; this.searched = true;
  }
  async submit(): Promise<void> {
    if (this.squadId && this.verified?.uuid !== this.squadId) { this.error = new ApiError("admin_minishop_corp_squad_required", 400); this.render(); return; }
    if (this.managerId === null && this.query.trim()) {
      this.error = new ApiError("admin_minishop_corp_manager_required", 400); this.render(); return;
    }
    let expiry: string;
    try { expiry = this.expiry(); }
    catch { this.error = new ApiError("admin_minishop_corp_invalid_date", 400); this.render(); return; }
    const draft: Terms = { name: this.name.trim(), tariff_key: this.tariffKey, external_squad_uuid: this.squadId || null,
      ends_at: expiry, manager_user_id: this.managerId };
    if (this.original && !await confirm(this.root, this.t("confirm_terms"), `${this.t("confirm_terms_hint")}\n${draft.name} · ${draft.tariff_key} · ${date(draft.ends_at, this.language)}`, this.language, this.controller.signal)) return;
    const submission: Submission = { terms: draft, previous: this.original };
    this.pending = submission; this.changed();
    await this.run(() => this.deliver(submission), true);
  }
  async deliver(submission: Submission): Promise<void> {
    const { terms: draft, previous } = submission;
    try {
      let result: Contract;
      try {
        result = contract((await this.api.request(previous ? `/contracts/${this.id}` : "/contracts", previous ? "PUT" : "POST",
          previous ? { ...draft, expected_version: previous.version } : { ...draft, id: this.id })).contract);
      } catch (error) {
        if (!previous || !(error instanceof ApiError) || error.code !== "minishop_corp_version_conflict") throw error;
        const latest = contract((await this.api.request(`/contracts/${this.id}`)).contract);
        if (!sameTerms(draft, terms(latest))) throw error;
        result = latest;
      }
      this.original = result; this.name = result.name; this.tariffKey = result.tariff_key;
      this.end = expiryDay(result.ends_at); this.endTouched = false;
      this.managerId = result.manager_user_id; this.squadId = result.external_squad_uuid ?? "";
      this.query = this.managerId === null ? "" : this.selected?.minishop_id ?? String(this.managerId);
      this.pending = null;
      this.notice = "admin_minishop_corp_saved";
      if (this.alive) this.saved(result);
    } catch (error) {
      if (error instanceof ApiError && !error.uncertain) this.pending = null;
      throw error;
    }
  }
  render(): void {
    this.paint((body) => {
      if (this.original) body.append(button(this.t("reload_saved"), () => { void this.reload(); }));
      if (!this.loaded) {
        body.append(el("p", translate(this.language, "wa_minishop_corp_loading")));
        if (this.error) body.append(button(this.t("retry_load"), () => { void this.run(() => this.load()); }));
        return;
      }
      const form = el("form", undefined, "corp-form");
      const name = input("name", this.name, (v) => { this.name = v; }); name.required = true; name.maxLength = 200;
      const plans: [string, string][] = [["", this.t("choose")], ...this.tariffs.map((v): [string, string] => [v.key,
        `${v.names[this.language.startsWith("ru") ? "ru" : "en"]} · ${v.key}${v.hidden ? ` (${this.t("hidden")})` : ""}`])];
      if (this.tariffKey && !this.tariffs.some((v) => v.key === this.tariffKey)) plans.push([this.tariffKey, this.tariffKey]);
      const plan = select("tariff", this.tariffKey, plans, (v) => { this.tariffKey = v; }); plan.required = true;
      const end = input("ends_at", this.end, (v) => { this.end = v; this.endTouched = true; }, "date"); end.required = true;
      end.max = "9999-12-30";
      const dates = el("div", undefined, "corp-wide corp-stack");
      dates.append(field(this.t("ends_at"), end, this.t("date_hint").replace("{timezone}", Intl.DateTimeFormat().resolvedOptions().timeZone)));
      const additions = el("div", undefined, "corp-actions corp-date-actions");
      for (const [label, days, months] of periods) additions.append(button(this.t(label), () => {
        try { this.end = addPeriod(this.end || calendarDay(), days, months); this.endTouched = true; this.error = null; }
        catch { this.error = new ApiError("admin_minishop_corp_invalid_date", 400); }
        this.render();
      }));
      dates.append(additions);
      if (this.original && !this.endTouched && Date.parse(endOfDay(this.end)) !== Date.parse(this.original.ends_at)) {
        dates.append(el("small", `${this.t("saved_expiry")}: ${date(this.original.ends_at, this.language)}`));
      }
      form.append(field(this.t("name"), name), field(this.t("tariff"), plan), dates);
      if (!this.tariffs.length) form.append(el("p", this.t("no_tariffs"), "corp-alert"));
      const squadBox = el("div", undefined, "corp-wide");
      const squadInput = input("squad", this.squadId, (v) => { this.squadId = v.trim().toLowerCase(); this.verified = null; });
      squadBox.append(field(this.t("squad"), squadInput, this.t("squad_hint")), button(this.t("verify_squad"), () => {
        if (this.squadId) void this.run(async () => { this.verified = squad((await this.api.request(`/options/squad?uuid=${encodeURIComponent(this.squadId)}`)).squad); });
      }));
      if (this.verified?.uuid === this.squadId) squadBox.append(el("p", `${this.t("squad_verified")}: ${this.verified.name}`, "corp-notice"));
      const managers = el("div", undefined, "corp-wide corp-stack");
      if (this.managerId === null && !this.query.trim()) managers.append(el("p", this.t("manager_missing"), "corp-footnote"));
      const search = input("manager", this.query, (v) => {
        this.query = v; this.managerId = null; this.selected = null; this.candidate = null; this.searched = false; this.searchDiagnostic = null; this.render();
      }); search.maxLength = 320; search.placeholder = this.t("account_query"); search.autocomplete = "off";
      search.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); void this.run(() => this.search()); } });
      managers.append(field(this.t("manager"), search, this.t("manager_hint")));
      const find = button(this.t("search"), () => { void this.run(() => this.search()); }); find.disabled = !this.query.trim(); managers.append(find);
      if (this.managerId !== null || this.query) managers.append(button(this.t("clear_manager"), () => {
        this.managerId = null; this.query = ""; this.selected = null; this.candidate = null; this.searched = false; this.render();
      }));
      if (this.candidate) {
        const candidate = this.candidate;
        const result = el("div", undefined, "corp-card");
        result.append(el("p", `${person(candidate)} · ${candidate.minishop_id}${candidate.username ? ` · @${candidate.username}` : ""}${candidate.telegram_id ? ` · ${this.t("telegram_id")}: ${candidate.telegram_id}` : ""}`),
          button(this.t("confirm_manager"), () => { this.managerId = candidate.user_id; this.selected = candidate; this.candidate = null; this.render(); }));
        managers.append(result);
      } else if (this.searched && !this.selected) {
        managers.append(el("p", this.t("empty_accounts")));
        if (this.searchDiagnostic) managers.append(el("small", translate(this.language, "minishop_corp_diagnostic_id").replace("{id}", this.searchDiagnostic)));
      }
      if (this.managerId !== null) managers.append(el("p", `${this.t("manager_selected")}: ${this.selected ? person(this.selected) : ""} · ${this.selected?.minishop_id ?? "#" + this.managerId}`, "corp-notice"));
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
      this.end = expiryDay(current.ends_at); this.endTouched = false;
      this.managerId = current.manager_user_id; this.squadId = current.external_squad_uuid ?? "";
      this.query = this.managerId === null ? "" : String(this.managerId); this.selected = null; this.candidate = null; this.searched = false;
      this.verified = null; await this.load();
      if (this.alive) this.saved(current);
    });
  }
}
