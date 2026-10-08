import { errorText, translate } from "../shared/i18n";
import { Panel, button, date, el } from "../shared/ui";
import { list, synchronization, syncOperation, type Contract, type Sync, type SyncOperation } from "./data";

export class SynchronizationPanel extends Panel {
  progress: Sync | null = null; rows: SyncOperation[] = []; after: string | null = null;
  private timer: ReturnType<typeof setInterval>;
  constructor(language: string, private current: () => Contract) {
    super(language); void this.run(() => this.load());
    this.timer = setInterval(() => { if (!this.root.hidden && !document.hidden && !this.busy && !this.confirming && this.rows.length <= 25) void this.run(() => this.load()); }, 10000);
  }
  get path(): string { return `/contracts/${this.current().id}/synchronization`; }
  async load(after?: string): Promise<void> {
    const [status, data] = await Promise.all([this.api.request(this.path), this.api.request(`${this.path}/operations?limit=25${after ? `&after=${after}` : ""}`)]);
    this.progress = synchronization(status.synchronization);
    this.current().member_count = this.progress.current_members;
    const rows = list(data.operations, syncOperation); this.rows = after ? [...this.rows, ...rows] : rows;
    this.after = rows.length === 25 ? rows.at(-1)?.id ?? null : null;
  }
  render(): void {
    this.paint((body) => {
      body.append(button(this.t("refresh"), () => { void this.run(() => this.load()); }), el("p", this.t("sync_hint")), el("small", this.t("auto_refresh")));
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
      if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
    });
  }
  override destroy(): void { clearInterval(this.timer); super.destroy(); }
}
