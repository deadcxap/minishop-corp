import { ApiError, requestId } from "../shared/api";
import type { CustomerProps } from "../shared/host";
import { customerText, errorText, translate, type CustomerKey } from "../shared/i18n";
import { Panel, button, confirm, date, el, field, input } from "../shared/ui";
import { CustomerApi } from "./api";
import { customerOperation, membership, offer, list, nullable, number, summary,
  type CustomerOperation, type Membership, type Offer, type Tariff } from "./data";
import { invitationFromLocation } from "./links";
import "../shared/styles.css";
import "../shared/controls.css";
import "./styles.css";

const live = (op: CustomerOperation | null): boolean => Boolean(op && ["pending", "running", "retry"].includes(op.state));

export class CustomerView extends Panel {
  current: Membership | null = null; proposal: Offer | null = null; operation: CustomerOperation | null = null;
  lastDeparture: CustomerOperation | null = null; loaded = false; manager = false;
  code = invitationFromLocation(); private offeredCode = "";
  waitUntil = 0; private polledAt = Date.now(); private timer: ReturnType<typeof setInterval>;
  constructor(target: HTMLElement, public props: CustomerProps) {
    super(props.language, (signal) => new CustomerApi(() => this.props.host, signal));
    this.root.className = "minishop-corp corp-customer"; target.append(this.root);
    void this.run(() => this.load());
    this.timer = setInterval(() => {
      this.countdown();
      if (document.hidden || this.busy || this.retry || this.confirming) return;
      if (Date.now() - this.polledAt >= (live(this.operation) ? 5000 : 30000)) void this.run(() => this.load());
    }, 1000);
  }
  c(key: CustomerKey): string { return customerText(this.language, key); }
  wait(seconds: number): void { this.waitUntil = Math.max(this.waitUntil, Date.now() + seconds * 1000); }
  get remaining(): number { return Math.max(0, Math.ceil((this.waitUntil - Date.now()) / 1000)); }
  override failed(error: unknown): void {
    if (error instanceof ApiError) {
      if (error.retryAfter !== null) this.wait(error.retryAfter);
      if (["minishop_corp_offer_changed", "minishop_corp_invitation_unavailable"].includes(error.code)) this.proposal = null;
      if ([401, 403].includes(error.status)) { this.current = null; this.lastDeparture = null; this.operation = null; this.manager = false; this.loaded = false; }
    }
  }
  async load(): Promise<void> {
    this.polledAt = Date.now();
    const data = await this.api.request("/membership");
    this.current = nullable(data.membership, membership);
    this.lastDeparture = nullable(data.last_departure, customerOperation);
    this.operation = this.current?.operation ?? null;
    if (this.current) { this.proposal = null; this.code = ""; this.offeredCode = ""; }
    this.loaded = true;
    this.manager = list((await this.api.request("/managed-contracts?limit=1")).contracts, summary).length > 0;
  }
  async inspect(): Promise<void> {
    if (this.remaining || this.busy || this.retry) return;
    const code = this.code;
    await this.run(async () => {
      this.proposal = null;
      const result = await this.api.request("/invitations/preview", "POST", { code });
      this.wait(number(result.retry_after)); this.proposal = offer(result); this.offeredCode = code;
    });
  }
  async join(): Promise<void> {
    const proposal = this.proposal, code = this.offeredCode;
    if (!proposal || this.remaining || this.busy || this.retry) return;
    if (!await confirm(this.root, this.c("join_confirm"), `${proposal.contract.name}\n${date(proposal.contract.ends_at, this.language)}\n${this.c("replacement")}`, this.language, this.controller.signal)) return;
    const payload = { request_id: requestId(), code, offer: { invitation_id: proposal.invitation_id,
      contract_id: proposal.contract.id, contract_version: proposal.contract.version } };
    await this.run(async () => {
      this.operation = customerOperation((await this.api.request("/membership/confirm", "POST", payload)).operation);
      this.proposal = null; this.code = ""; this.offeredCode = "";
    }, true);
    if (!this.error && !this.retry) await this.run(() => this.load());
  }
  async leave(): Promise<void> {
    const current = this.current;
    if (!current?.can_leave || this.busy || this.retry) return;
    if (!await confirm(this.root, this.c("leave_confirm"), `${current.contract.name}\n${this.c("leave_hint")}`, this.language, this.controller.signal)) return;
    const payload = { request_id: requestId() };
    await this.run(async () => {
      this.operation = customerOperation((await this.api.request(`/memberships/${current.id}/leave`, "POST", payload)).operation);
      this.current = { ...current, state: "leaving", can_leave: false, operation: this.operation };
    }, true);
  }
  countdown(): void {
    const remaining = this.remaining;
    const label = this.root.querySelector<HTMLElement>("[data-countdown]");
    if (label) { label.hidden = remaining === 0; label.textContent = `${this.c("wait")}: ${remaining} ${this.c("seconds")}`; }
    for (const node of this.root.querySelectorAll<HTMLButtonElement>("[data-attempt]")) node.disabled = remaining > 0;
  }
  tariff(value: Tariff | null): HTMLElement {
    const box = el("div", undefined, "corp-stack");
    if (!value) { box.append(el("p", this.c("tariff_missing"))); return box; }
    box.append(el("h3", value.names[this.language.startsWith("ru") ? "ru" : "en"]));
    const limit = (v: number | null, bytes = false): string => v === null ? this.t("unknown") : v === 0 ? this.t("unlimited")
      : bytes ? `${new Intl.NumberFormat(this.language, { maximumFractionDigits: 2 }).format(v / 1024 ** 3)} ${this.t("unit_gib")}` : String(v);
    box.append(el("p", `${this.c("traffic_limit")}: ${limit(value.traffic_limit_bytes, true)} · ${this.c("devices_limit")}: ${limit(value.hwid_device_limit)}`));
    return box;
  }
  render(): void {
    this.paint((body) => {
      const heading = el("div", undefined, "corp-heading"); heading.append(el("h2", this.c("title")), button(this.t("refresh"), () => { void this.run(() => this.load()); })); body.append(heading);
      if (!this.loaded) { body.append(el("p", this.c("loading"))); return; }
      if (this.manager) body.append(button(this.c("manager_title"), () => this.props.host.navigate("corporate-manager")));
      if (this.current) {
        const current = this.current, expired = Date.parse(current.contract.ends_at) <= Date.now();
        const card = el("div", undefined, "corp-card");
        card.append(el("h3", current.contract.name), this.tariff(current.tariff), el("p", `${this.c("ends_at")}: ${date(current.contract.ends_at, this.language)}`),
          el("span", this.t(`state_${current.state}`), "corp-pill"), el("p", this.c("payment_notice"), "corp-notice"));
        if (expired) card.append(el("p", this.c("expired_notice"), "corp-notice"));
        const leave = button(this.c("leave"), () => { void this.leave(); }, "corp-danger"); leave.disabled = !current.can_leave;
        card.append(leave); body.append(card);
      }
      if (this.operation && live(this.operation)) {
        const status = el("div", undefined, "corp-notice"); status.setAttribute("role", "status");
        status.append(el("p", this.c(this.operation.kind === "join" ? "joining" : this.operation.kind === "reconcile" ? "reconciling" : "leaving")));
        if (this.operation.error_code) status.append(el("p", errorText(this.language, new Error(this.operation.error_code))));
        status.append(el("small", this.c("operation_wait"))); body.append(status);
      } else if (this.operation && ["failed", "cancelled"].includes(this.operation.state)) body.append(el("p", this.c("operation_problem"), "corp-alert"));
      if (!this.current && !live(this.operation)) {
        if (this.lastDeparture?.departure) {
          const receipt = this.lastDeparture.departure;
          const note = el("p", `${this.c(receipt.kind === "trial" ? "trial_result" : "disabled_result")}${receipt.kind === "trial" ? ` ${date(receipt.ends_at, this.language)}` : ""}`, "corp-notice");
          note.setAttribute("role", "status"); body.append(note);
          body.append(button(this.c("personal"), () => this.props.host.navigateSection("home")));
        }
        if (this.proposal) {
          const offer = el("section", undefined, "corp-card corp-offer");
          offer.append(el("h3", this.proposal.contract.name), this.tariff(this.proposal.tariff), el("p", `${this.c("ends_at")}: ${date(this.proposal.contract.ends_at, this.language)}`),
            el("p", this.c("replacement"), "corp-notice"));
          const join = button(this.c("join"), () => { void this.join(); }, "corp-primary"); join.dataset.attempt = "confirm";
          offer.append(join, button(this.c("different_code"), () => { this.proposal = null; this.offeredCode = ""; this.render(); })); body.append(offer);
        } else {
          const form = el("form", undefined, "corp-inline-form");
          const code = input("corporate_code", this.code, (v) => { this.code = v; }); code.maxLength = 256; code.required = true; code.autocomplete = "off"; code.spellcheck = false;
          const submit = el("button", this.c("check_code"), "corp-button corp-primary"); submit.type = "submit"; submit.dataset.attempt = "preview";
          form.append(field(this.c("code"), code, this.c("code_hint")), submit);
          form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) void this.inspect(); }); body.append(form);
        }
        const wait = el("p", undefined, "corp-wait"); wait.dataset.countdown = ""; body.append(wait);
      }
      body.append(button(this.c("support"), () => this.props.host.navigateSection("support")));
    });
    this.countdown();
  }
  updateProps(props: CustomerProps): void { this.props = props; this.update(props.language); }
  override destroy(): void { clearInterval(this.timer); this.code = ""; this.offeredCode = ""; this.proposal = null; super.destroy(); }
}
