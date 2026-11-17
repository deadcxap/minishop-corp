import { errorText, translate } from "../shared/i18n";
import { Panel, button, date, el, refreshButton } from "../shared/ui";
import { list, synchronization, syncOperation, type Contract, type Sync, type SyncOperation } from "./data";

interface SyncPage { progress: Sync; rows: SyncOperation[] }

export class SynchronizationPanel extends Panel {
  progress: Sync | null = null; rows: SyncOperation[] = []; after: string | null = null;
  countChanged: () => void = () => {};
  private paginated = false;
  private readonly details = el("div", undefined, "corp-sync-data");
  private readonly more = button("", () => { void this.run(() => this.load(this.after ?? undefined)); });
  private timer: ReturnType<typeof setInterval>;
  constructor(language: string, private current: () => Contract) {
    super(language); void this.run(() => this.load());
    this.timer = setInterval(() => {
      if (this.paginated) return;
      void this.poll(() => this.readPage(), (page) => {
        if (JSON.stringify(page) === JSON.stringify({ progress: this.progress, rows: this.rows })) return;
        this.applyPage(page);
        this.renderDetails();
      });
    }, 10000);
  }
  get path(): string { return `/contracts/${this.current().id}/synchronization`; }
  private async readPage(after?: string): Promise<SyncPage> {
    const [status, data] = await Promise.all([this.api.request(this.path), this.api.request(`${this.path}/operations?limit=25${after ? `&after=${after}` : ""}`)]);
    return { progress: synchronization(status.synchronization), rows: list(data.operations, syncOperation) };
  }
  private applyPage({ progress, rows }: SyncPage, after?: string): void {
    this.progress = progress;
    const current = this.current();
    if (current.member_count !== progress.current_members) {
      current.member_count = progress.current_members;
      this.countChanged();
    }
    this.rows = after ? [...this.rows, ...rows] : rows;
    this.after = rows.length === 25 ? rows.at(-1)?.id ?? null : null;
    this.paginated = Boolean(after);
  }
  async load(after?: string): Promise<void> {
    this.applyPage(await this.readPage(after), after);
  }
  render(): void {
    this.paint((body) => {
      const heading = el("div", undefined, "corp-panel-heading");
      const description = el("div"); description.append(el("p", this.t("sync_hint")), el("small", this.t("auto_refresh")));
      heading.append(description, refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy));
      this.more.textContent = this.t("load_more");
      this.renderDetails();
      body.append(heading, this.details, this.more);
    });
  }
  private renderDetails(): void {
    const body = this.details;
    body.replaceChildren();
    this.more.hidden = !this.after;
    if (!this.progress) { body.append(el("p", translate(this.language, "wa_minishop_corp_loading"))); return; }
    const progress = this.progress;
    const counters = el("dl", undefined, "corp-counters");
    for (const key of ["confirmed", "awaiting_dispatch", "joining", "departing", "pending", "running", "retrying"] as const) {
      const item = el("div"); item.append(el("dt", this.t(key)), el("dd", String(progress[key]))); counters.append(item);
    }
    body.append(el("p", `${this.t("version")}: ${progress.version} · ${this.t("members")}: ${progress.current_members}`), counters,
      el("p", `${this.t("last_checked")}: ${date(progress.last_checked_at, this.language)}`), el("p", `${this.t("next_sweep")}: ${date(progress.next_sweep_at, this.language)}`),
      el("p", progress.sweep_running ? this.t("sweep_running") : `${this.t("sweep_completed")}: ${date(progress.sweep_completed_at, this.language)}`));
    if (!this.rows.length) body.append(el("p", this.t("no_operations"), "corp-empty"));
    for (const row of this.rows) {
      const card = el("article", undefined, "corp-card");
      card.append(el("h3", `#${row.user_id} · ${this.t(`kind_${row.kind}`)}`), el("span", this.t(`state_${row.state}`), "corp-pill"),
        el("p", `${this.t("version")}: ${row.contract_version} · ${this.t("attempts")}: ${row.attempts}`),
        el("p", `${this.t("next_attempt")}: ${date(row.next_attempt_at, this.language)}`));
      if (row.error_code) card.append(el("p", errorText(this.language, new Error(row.error_code)), "corp-alert"));
      body.append(card);
    }
  }
  override destroy(): void { clearInterval(this.timer); super.destroy(); }
}
