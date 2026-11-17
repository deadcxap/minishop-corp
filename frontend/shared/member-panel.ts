import { ApiError, requestId, object, number, type ApiClient } from "./api";
import { translate } from "./i18n";
import type { CorporateMember, MemberAvatar, MembersPage, Statistic } from "./members";
import { Panel, button, confirm, el, iconButton, person, refreshButton } from "./ui";
import { icon } from "./icons";
import { list, nullable, string } from "./api";
import { avatar, member, operation } from "./members-data";

export class MembersPanel extends Panel {
  rows: CorporateMember[] = []; after: string | null = null; loaded = false;
  countChanged: () => void = () => {};
  private paginated = false;
  avatars = new Map<string, MemberAvatar>(); private loadingAvatars = false; private generation = 0;
  private readonly table = el("table", undefined, "corp-members");
  private readonly head = el("thead");
  private readonly entries = el("tbody");
  private readonly scroll = el("div", undefined, "corp-table-scroll");
  private readonly empty = el("p");
  private readonly more = button("", () => { void this.run(() => this.load(this.after ?? undefined)); });
  private timer: ReturnType<typeof setInterval>;
  constructor(language: string, private current: () => { id: string; member_count?: number; manager_user_id?: number | null },
    private scope: "admin" | "manager" = "admin", client?: (signal: AbortSignal) => ApiClient,
    private promote?: (row: CorporateMember) => Promise<void>) {
    super(language, client);
    this.scroll.tabIndex = 0; this.scroll.setAttribute("role", "region");
    this.table.append(this.head, this.entries); this.scroll.append(this.table);
    queueMicrotask(() => { if (this.alive) void this.run(() => this.load()); });
    this.timer = setInterval(() => {
      if (this.paginated || !this.rows.some((row) => row.state === "pending" || row.state === "leaving")) return;
      void this.poll(() => this.readPage(), (page) => {
        const previous = this.rows;
        this.applyPage(page);
        this.updateRows(previous);
      });
    }, 10000);
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError && (this.scope === "manager" || [401, 403, 404].includes(error.status))) {
      this.rows = []; this.avatars.clear(); this.after = null;
    }
  }
  protected renderActions(body: HTMLElement): void {
    const actions = el("div", undefined, "corp-panel-heading");
    actions.append(el("h3", this.t("members")), refreshButton(this.t("refresh"), () => { void this.run(() => this.load()); }, this.busy)); body.append(actions);
  }
  get path(): string { return `/${this.scope === "admin" ? "contracts" : "managed-contracts"}/${this.current().id}/members`; }
  private async readPage(after?: string): Promise<MembersPage & { count: number | null }> {
    const [data, details] = await Promise.all([
      this.api.request(`${this.path}?limit=25${after ? `&after=${after}` : ""}`),
      this.scope === "admin" ? this.api.request(`/contracts/${this.current().id}`) : Promise.resolve(null),
    ]);
    return { members: list(data.members, member), next_after: nullable(data.next_after, string),
      count: details ? number(object(details.contract).member_count) : null };
  }
  private applyPage(page: MembersPage & { count: number | null }, after?: string): void {
    const current = this.current();
    if (page.count !== null && page.count !== current.member_count) {
      current.member_count = page.count;
      this.countChanged();
    }
    this.after = page.next_after; this.paginated = Boolean(after);
    this.rows = after ? [...this.rows, ...page.members] : page.members;
    this.loaded = true; this.generation++;
    void this.loadAvatars();
  }
  async load(after?: string): Promise<void> { this.applyPage(await this.readPage(after), after); }
  async loadAvatars(): Promise<void> {
    if (this.loadingAvatars || !this.alive) return;
    this.loadingAvatars = true;
    const generation = this.generation;
    const pending = this.rows.filter((row) => row.avatar_path && !this.avatars.has(row.id));
    const worker = async (): Promise<void> => {
      for (let row = pending.shift(); row && this.alive && generation === this.generation; row = pending.shift()) {
        try {
          const data = await this.api.request(`${this.path}/${row.id}/avatar`);
          if (!this.alive) return;
          this.avatars.set(row.id, avatar(data.avatar));
        } catch (error) {
          if (!this.alive) return;
          this.avatars.set(row.id, { data_url: null, state: "unavailable", updated_at: null });
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
            this.rows = this.rows.filter((item) => item.id !== row.id); this.error = error;
            if ([401, 403].includes(error.status) || error.code === "minishop_corp_contract_missing") {
              this.failed(error); this.changed();
            }
            if (this.alive) this.render();
            continue;
          }
        }
        const current = this.rows.find((item) => item.id === row.id);
        if (current) {
          const entry = Array.from(this.entries.querySelectorAll<HTMLTableRowElement>("tr"))
            .find((item) => item.dataset.member === current.id);
          entry?.querySelector(".corp-avatar")?.replaceWith(this.renderAvatar(current));
        }
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
  private renderAvatar(row: CorporateMember): HTMLElement {
    const photo = this.avatars.get(row.id);
    const name = person(row.profile);
    const parts = name.split(/\s+/).filter(Boolean);
    const initials = (parts.length > 1 ? parts.slice(0, 2).map((part) => part[0]).join("") : name.slice(0, 2)).toUpperCase();
    const imageBox = el("div", undefined, "corp-avatar");
    imageBox.setAttribute("aria-label", this.t("avatar"));
    if (photo?.data_url && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(photo.data_url) && photo.data_url.length <= 700000) {
      const image = el("img"); image.src = photo.data_url; image.alt = name; image.width = 44; image.height = 44;
      image.title = photo.state === "stale" ? translate(this.language, "wa_minishop_corp_data_stale") : this.t("avatar");
      image.addEventListener("error", () => { image.remove(); imageBox.textContent = initials; }, { once: true }); imageBox.append(image);
    } else {
      imageBox.textContent = initials;
      imageBox.title = translate(this.language, photo?.state === "unavailable" ? "wa_minishop_corp_data_unavailable"
        : !photo && row.avatar_path ? "wa_minishop_corp_loading" : "wa_minishop_corp_avatar_missing");
    }
    return imageBox;
  }
  private renderMember(row: CorporateMember): HTMLTableRowElement {
    const entry = el("tr", undefined, "corp-member"); entry.dataset.member = row.id;
    const participant = el("td");
    const identity = el("div", undefined, "corp-identity");
    const name = person(row.profile);
    const assigned = this.current().manager_user_id === row.profile.user_id;
    const profile = el("div", undefined, "corp-profile");
    const heading = el("h3", name);
    if (assigned) {
      const badge = el("span", undefined, "corp-manager-badge");
      badge.title = this.t("current_manager"); badge.setAttribute("role", "img"); badge.setAttribute("aria-label", badge.title);
      badge.append(icon("shield")); heading.append(badge);
    }
    profile.append(heading);
    const link = row.profile.telegram_url;
    if (link && /^(https:\/\/t\.me\/[A-Za-z0-9_]+|tg:\/\/user\?id=\d+)$/.test(link)) {
      const anchor = el("a", row.profile.username ? `@${row.profile.username}` : "Telegram");
      anchor.href = link; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
      anchor.dataset.focus = `telegram-${row.id}`; profile.append(anchor);
    } else {
      if (row.profile.username) profile.append(el("small", `@${row.profile.username}`));
      profile.append(el("small", this.t("no_telegram")));
    }
    const id = el("small", row.profile.minishop_id, "corp-account-id"); id.title = row.profile.minishop_id;
    profile.append(id);
    const status = el("div", undefined, "corp-member-status");
    const progress = row.operation?.state === "succeeded" ? null : row.operation;
    if (row.state !== "active") status.append(el("span", this.t(`state_${row.state}`), "corp-pill"));
    if (progress && progress.state !== row.state) status.append(el("small", this.t(`state_${progress.state}`)));
    if (status.childElementCount) profile.append(status);
    if (progress?.error_code) profile.append(el("small", translate(this.language, "minishop_corp_operation_failed")));
    identity.append(this.renderAvatar(row), profile); participant.append(identity); entry.append(participant);
    for (const [used, max, bytes] of [[row.statistics.traffic_used_bytes, row.statistics.traffic_limit_bytes, true],
      [row.statistics.device_count, row.statistics.device_limit, false]] as const) {
      const cell = el("td"), metric = el("div", undefined, "corp-metric");
      metric.append(this.metric(used, bytes), el("span", "/"), this.metric(max, bytes, true)); cell.append(metric); entry.append(cell);
    }
    const controls = el("td"), actions = el("div", undefined, "corp-row-actions");
    if (this.scope === "admin" && this.promote) {
      const manager = iconButton(this.t(assigned ? "revoke_manager" : "make_manager"), assigned ? "managerRemove" : "managerAdd",
        () => { void this.promote?.(row); }, "corp-ghost");
      manager.dataset.focus = `manager-${row.id}`;
      manager.disabled = !assigned && row.state !== "active"; actions.append(manager);
    }
    const remove = iconButton(this.t("exclude"), "userRemove", () => { void this.exclude(row); }, "corp-ghost corp-danger");
    remove.dataset.focus = `exclude-${row.id}`; remove.disabled = row.state === "leaving";
    actions.append(remove); controls.append(actions); entry.append(controls);
    return entry;
  }
  private updateListVisibility(): void {
    this.empty.hidden = this.rows.length > 0;
    this.scroll.hidden = this.rows.length === 0;
    this.more.hidden = !this.after;
  }
  private updateRows(previous: CorporateMember[]): void {
    const oldRows = new Map(previous.map((row) => [row.id, row]));
    const entries = new Map(Array.from(this.entries.querySelectorAll<HTMLTableRowElement>("tr"), (entry) => [entry.dataset.member, entry]));
    const active = document.activeElement;
    const focus = active instanceof HTMLElement && this.entries.contains(active) ? active.dataset.focus : undefined;
    for (const [index, row] of this.rows.entries()) {
      const existing = entries.get(row.id);
      const entry = existing && JSON.stringify(oldRows.get(row.id)) === JSON.stringify(row) ? existing : this.renderMember(row);
      if (existing !== entry) existing?.replaceWith(entry);
      const position = this.entries.children.item(index);
      if (position !== entry) this.entries.insertBefore(entry, position);
      entries.delete(row.id);
    }
    for (const entry of entries.values()) entry.remove();
    this.updateListVisibility();
    if (focus && document.activeElement !== active) {
      Array.from(this.entries.querySelectorAll<HTMLElement>("[data-focus]:not(:disabled)"))
        .find((item) => item.dataset.focus === focus)?.focus({ preventScroll: true });
    }
  }
  render(): void {
    this.paint((body) => {
      this.renderActions(body);
      this.table.setAttribute("aria-label", this.t("members"));
      this.scroll.setAttribute("aria-label", this.t("members"));
      const headings = el("tr");
      for (const key of ["participant", "traffic", "devices", "actions"] as const) {
        const heading = el("th", this.t(key)); heading.scope = "col"; headings.append(heading);
      }
      this.head.replaceChildren(headings);
      this.entries.replaceChildren(...this.rows.map((row) => this.renderMember(row)));
      this.empty.textContent = this.loaded ? this.t("empty_members") : translate(this.language, "wa_minishop_corp_loading");
      this.empty.classList.toggle("corp-empty", this.loaded);
      this.more.textContent = this.t("load_more");
      this.updateListVisibility();
      body.append(this.empty, this.scroll, this.more);
    });
  }
  override destroy(): void { clearInterval(this.timer); this.avatars.clear(); super.destroy(); }
}
