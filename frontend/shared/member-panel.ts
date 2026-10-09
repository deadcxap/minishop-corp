import { ApiError, requestId, object, number, type ApiClient } from "./api";
import { translate } from "./i18n";
import type { CorporateMember, MemberAvatar, Statistic } from "./members";
import { Panel, button, confirm, el, person } from "./ui";
import { list, nullable, string } from "./api";
import { avatar, member, operation } from "./members-data";

export class MembersPanel extends Panel {
  rows: CorporateMember[] = []; after: string | null = null; loaded = false;
  avatars = new Map<string, MemberAvatar>(); private loadingAvatars = false; private generation = 0;
  private timer: ReturnType<typeof setInterval>;
  constructor(language: string, private current: () => { id: string; member_count?: number; manager_user_id?: number | null },
    private scope: "admin" | "manager" = "admin", client?: (signal: AbortSignal) => ApiClient,
    private promote?: (row: CorporateMember) => Promise<void>) {
    super(language, client); void this.run(() => this.load());
    this.timer = setInterval(() => {
      if (this.root.hidden || document.hidden || this.busy || this.retry || this.confirming || this.rows.length > 25
        || !this.rows.some((row) => row.state === "pending" || row.state === "leaving")) return;
      void this.run(() => this.load());
    }, 10000);
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError && (this.scope === "manager" || [401, 403, 404].includes(error.status))) {
      this.rows = []; this.avatars.clear(); this.after = null;
    }
  }
  get path(): string { return `/${this.scope === "admin" ? "contracts" : "managed-contracts"}/${this.current().id}/members`; }
  async load(after?: string): Promise<void> {
    const [data, details] = await Promise.all([
      this.api.request(`${this.path}?limit=25${after ? `&after=${after}` : ""}`),
      this.scope === "admin" ? this.api.request(`/contracts/${this.current().id}`) : Promise.resolve(null),
    ]);
    if (details) this.current().member_count = number(object(details.contract).member_count);
    const page = list(data.members, member); this.after = nullable(data.next_after, string);
    this.rows = after ? [...this.rows, ...page] : page; this.loaded = true; this.generation++;
    void this.loadAvatars();
  }
  async loadAvatars(): Promise<void> {
    if (this.loadingAvatars || !this.alive) return;
    this.loadingAvatars = true;
    const generation = this.generation;
    const pending = this.rows.filter((row) => row.avatar_path && !this.avatars.has(row.id));
    const worker = async (): Promise<void> => {
      for (let row = pending.shift(); row && this.alive && generation === this.generation; row = pending.shift()) {
        try {
          const data = await this.api.request(`${this.path}/${row.id}/avatar`);
          this.avatars.set(row.id, avatar(data.avatar));
        } catch (error) {
          this.avatars.set(row.id, { data_url: null, state: "unavailable", updated_at: null });
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
            this.rows = this.rows.filter((item) => item.id !== row.id); this.error = error;
            if ([401, 403].includes(error.status) || error.code === "minishop_corp_contract_missing") {
              this.failed(error); this.changed();
            }
          }
        }
        if (this.alive) this.render();
      }
    };
    try { await Promise.all(Array.from({ length: 4 }, worker)); }
    finally { this.loadingAvatars = false; if (this.alive && generation !== this.generation) void this.loadAvatars(); }
  }
  async exclude(row: CorporateMember): Promise<void> {
    if (!await confirm(this.root, this.t("confirm_exclude"), `${person(row.profile)}\n${this.t("exclude_hint")}`, this.language, this.controller.signal)) return;
    const request_id = requestId();
    await this.run(async () => {
      const result = operation((await this.api.request(`${this.path}/${row.id}/exclude`, "POST", { request_id })).operation);
      if (result.state === "succeeded") this.rows = this.rows.filter((item) => item.id !== row.id);
      else this.rows = this.rows.map((item) => item.id === row.id ? { ...item, state: "leaving", operation: result } : item);
      this.notice = "admin_minishop_corp_exclude_queued";
    }, true);
  }
  metric(value: Statistic, bytes = false, limit = false): HTMLElement {
    let text = this.t("unknown");
    if (value.value !== null) {
      if (limit && value.value === 0) text = this.t("unlimited");
      else if (!bytes) text = new Intl.NumberFormat(this.language).format(value.value);
      else {
        const i = Math.min(4, Math.floor(Math.log2(Math.max(1, value.value)) / 10));
        const units = ["unit_b", "unit_kib", "unit_mib", "unit_gib", "unit_tib"] as const;
        text = `${new Intl.NumberFormat(this.language, { maximumFractionDigits: 1 }).format(value.value / 1024 ** i)} ${this.t(units[i] ?? "unit_b")}`;
      }
    }
    const node = el("span", text, `corp-metric-value corp-${value.state}`);
    node.title = translate(this.language, `wa_minishop_corp_data_${value.state}`);
    return node;
  }
  render(): void {
    this.paint((body) => {
      const actions = el("div", undefined, "corp-actions");
      actions.append(button(this.t("refresh"), () => { void this.run(() => this.load()); })); body.append(actions);
      if (!this.loaded) body.append(el("p", translate(this.language, "wa_minishop_corp_loading")));
      else if (!this.rows.length) body.append(el("p", this.t("empty_members"), "corp-empty"));
      for (const row of this.rows) {
        const card = el("article", undefined, "corp-member corp-card"); card.dataset.member = row.id;
        const identity = el("div", undefined, "corp-identity");
        const photo = this.avatars.get(row.id);
        const name = person(row.profile);
        const parts = name.split(/\s+/).filter(Boolean);
        const initials = (parts.length > 1 ? parts.slice(0, 2).map((part) => part[0]).join("") : name.slice(0, 2)).toUpperCase();
        const imageBox = el("div", undefined, "corp-avatar");
        imageBox.setAttribute("aria-label", this.t("avatar"));
        if (photo?.data_url && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(photo.data_url) && photo.data_url.length <= 700000) {
          const image = el("img"); image.src = photo.data_url; image.alt = person(row.profile); image.width = 44; image.height = 44;
          image.title = photo.state === "stale" ? translate(this.language, "wa_minishop_corp_data_stale") : this.t("avatar");
          image.addEventListener("error", () => { image.remove(); imageBox.textContent = initials; }, { once: true }); imageBox.append(image);
        } else {
          imageBox.textContent = initials;
          imageBox.title = translate(this.language, photo?.state === "unavailable" ? "wa_minishop_corp_data_unavailable"
            : !photo && row.avatar_path ? "wa_minishop_corp_loading" : "wa_minishop_corp_avatar_missing");
        }
        const profile = el("div", undefined, "corp-profile"); profile.append(el("h3", name));
        const link = row.profile.telegram_url;
        if (link && /^(https:\/\/t\.me\/[A-Za-z0-9_]+|tg:\/\/user\?id=\d+)$/.test(link)) {
          const anchor = el("a", row.profile.username ? `@${row.profile.username}` : "Telegram"); anchor.href = link; anchor.target = "_blank"; anchor.rel = "noopener noreferrer"; profile.append(anchor);
        } else {
          if (row.profile.username) profile.append(el("small", `@${row.profile.username}`));
          profile.append(el("small", this.t("no_telegram")));
        }
        const id = el("small", row.profile.minishop_id, "corp-account-id"); id.title = row.profile.minishop_id;
        profile.append(id);
        identity.append(imageBox, profile); card.append(identity);
        for (const [label, used, max, bytes] of [["traffic", row.statistics.traffic_used_bytes, row.statistics.traffic_limit_bytes, true],
          ["devices", row.statistics.device_count, row.statistics.device_limit, false]] as const) {
          const metric = el("div", undefined, "corp-metric"); metric.append(el("small", this.t(label)), this.metric(used, bytes), el("span", "/"), this.metric(max, bytes, true)); card.append(metric);
        }
        const tools = el("div", undefined, "corp-stack"); tools.append(el("span", this.t(`state_${row.state}`), "corp-pill"));
        if (this.scope === "admin" && this.promote) {
          const assigned = this.current().manager_user_id === row.profile.user_id;
          const manager = button(this.t(assigned ? "current_manager" : "make_manager"), () => { void this.promote?.(row); });
          manager.disabled = assigned || row.state !== "active"; tools.append(manager);
        }
        if (row.operation && row.operation.state !== "succeeded") {
          tools.append(el("small", this.t(`state_${row.operation.state}`)));
          if (row.operation.error_code) tools.append(el("small", translate(this.language, "minishop_corp_operation_failed")));
        }
        const remove = button(this.t("exclude"), () => { void this.exclude(row); }, "corp-danger"); remove.disabled = row.state === "leaving";
        tools.append(remove); card.append(tools); body.append(card);
      }
      if (this.after) body.append(button(this.t("load_more"), () => { void this.run(() => this.load(this.after ?? undefined)); }));
    });
  }
  override destroy(): void { clearInterval(this.timer); this.avatars.clear(); super.destroy(); }
}
