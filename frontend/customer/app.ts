import { ApiError, requestId } from "../shared/api";
import type { CustomerProps } from "../shared/host";
import { customerText, errorText, type CustomerKey } from "../shared/i18n";
import { Panel, button, confirm, date, el, field, input } from "../shared/ui";
import { CustomerApi } from "./api";
import { customerOperation, membership, offer, list, nullable, summary,
  type CustomerOperation, type Membership, type Offer } from "./data";
import { invitationFromLocation } from "./links";
import "../shared/styles.css";
import "../shared/controls.css";
import "./styles.css";

const live = (op: CustomerOperation | null): boolean => Boolean(op && ["pending", "running", "retry"].includes(op.state));

export class CustomerView extends Panel {
  current: Membership | null = null; proposal: Offer | null = null; operation: CustomerOperation | null = null;
  lastDeparture: CustomerOperation | null = null; loaded = false; manager = false;
  private linkCode = invitationFromLocation();
  code = this.linkCode; private offeredCode = "";
  private timer: ReturnType<typeof setTimeout> | null = null;
  private followLink = (): void => {
    const code = invitationFromLocation();
    if (!code || code === this.linkCode || this.current || this.busy || this.retry || this.confirming) return;
    this.linkCode = code; this.code = code; this.proposal = null; this.offeredCode = ""; this.render();
  };
  constructor(target: HTMLElement, public props: CustomerProps) {
    super(props.language, (signal) => new CustomerApi(() => this.props.host, signal));
    this.root.className = "minishop-corp corp-customer"; target.append(this.root);
    window.addEventListener("hashchange", this.followLink);
    window.addEventListener("popstate", this.followLink);
    void this.run(() => this.load());
  }
  c(key: CustomerKey): string { return customerText(this.language, key); }
  override async run(task: () => Promise<void>, write = false): Promise<void> {
    await super.run(task, write);
    this.scheduleOperation();
  }
  private scheduleOperation(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (!this.alive || !live(this.operation) || this.operation?.kind === "reconcile" || this.retry || this.error) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (document.hidden || this.busy || this.confirming) this.scheduleOperation();
      else void this.run(() => this.load());
    }, 5000);
  }
  override failed(error: unknown): void {
    if (error instanceof ApiError) {
      if (["minishop_corp_offer_changed", "minishop_corp_invitation_unavailable"].includes(error.code)) this.proposal = null;
      if ([401, 403].includes(error.status)) { this.current = null; this.lastDeparture = null; this.operation = null; this.manager = false; this.loaded = false; }
    }
  }
  async load(): Promise<void> {
    const data = await this.api.request("/membership");
    this.current = nullable(data.membership, membership);
    this.lastDeparture = nullable(data.last_departure, customerOperation);
    this.operation = this.current?.operation ?? null;
    if (this.current) { this.proposal = null; this.code = ""; this.offeredCode = ""; }
    this.loaded = true;
    this.manager = list((await this.api.request("/managed-contracts?limit=1")).contracts, summary).length > 0;
  }
  async inspect(): Promise<void> {
    if (this.busy || this.retry) return;
    const code = this.code;
    await this.run(async () => {
      this.proposal = null;
      const result = await this.api.request("/invitations/preview", "POST", { code });
      this.proposal = offer(result); this.offeredCode = code;
    });
  }
  async join(): Promise<void> {
    const proposal = this.proposal, code = this.offeredCode;
    if (!proposal || this.busy || this.retry || this.confirming) return;
    if (!await confirm(this.root, this.c("join_confirm"), `${proposal.contract.name}\n${date(proposal.contract.ends_at, this.language)}\n${this.c("replacement")}`, this.language, this.controller.signal)) return;
    const payload = { request_id: requestId(), code, offer: { invitation_id: proposal.invitation_id,
      contract_id: proposal.contract.id, contract_version: proposal.contract.version } };
    await this.run(async () => {
      this.operation = customerOperation((await this.api.request("/membership/confirm", "POST", payload)).operation);
      this.proposal = null; this.code = ""; this.offeredCode = "";
      await this.load();
    }, true);
  }
  async leave(): Promise<void> {
    const current = this.current;
    if (!current?.can_leave || this.busy || this.retry || this.confirming) return;
    if (!await confirm(this.root, this.c("leave_confirm"), `${current.contract.name}\n${this.c("leave_hint")}`, this.language, this.controller.signal)) return;
    const payload = { request_id: requestId() };
    await this.run(async () => {
      this.operation = customerOperation((await this.api.request(`/memberships/${current.id}/leave`, "POST", payload)).operation);
      this.current = { ...current, state: "leaving", can_leave: false, operation: this.operation };
      await this.load();
    }, true);
  }
  render(): void {
    this.paint((body) => {
      body.append(el("h2", this.c("title")));
      if (!this.loaded) {
        body.append(el("p", this.c("loading")));
        if (this.error) body.append(button(this.t("retry_load"), () => { void this.run(() => this.load()); }));
        return;
      }
      if (this.manager) body.append(button(this.c("manager_title"), () => this.props.host.navigate("corporate-manager")));
      if (this.error && live(this.operation) && !this.retry) body.append(button(this.t("retry_load"), () => { void this.run(() => this.load()); }));
      if (this.current) {
        const current = this.current, expired = Date.parse(current.contract.ends_at) <= Date.now();
        const card = el("div", undefined, "corp-card");
        card.append(el("h3", current.contract.name), el("p", `${this.c("ends_at")}: ${date(current.contract.ends_at, this.language)}`),
          el("span", this.t(`state_${current.state}`), "corp-pill"), el("p", this.c("payment_notice"), "corp-notice"));
        if (expired) card.append(el("p", this.c("expired_notice"), "corp-notice"));
        const leave = button(this.c("leave"), () => { void this.leave(); }, "corp-danger"); leave.disabled = !current.can_leave;
        card.append(leave); body.append(card);
      }
      if (this.operation && live(this.operation)) {
        const status = el("div", undefined, "corp-notice"); status.setAttribute("role", "status");
        status.append(el("p", this.c(this.operation.kind === "join" ? "joining" : this.operation.kind === "reconcile" ? "reconciling" : "leaving")));
        if (this.operation.error_code) status.append(el("p", errorText(this.language, new Error(this.operation.error_code))));
        if (this.operation.kind !== "reconcile") status.append(el("small", this.c("operation_wait")));
        body.append(status);
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
          offer.append(el("h3", this.proposal.contract.name), el("p", `${this.c("ends_at")}: ${date(this.proposal.contract.ends_at, this.language)}`),
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
      }
      body.append(button(this.c("support"), () => this.props.host.navigateSection("support")));
    });
  }
  updateProps(props: CustomerProps): void { this.props = props; this.update(props.language); this.followLink(); }
  override destroy(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    window.removeEventListener("hashchange", this.followLink); window.removeEventListener("popstate", this.followLink);
    this.code = ""; this.linkCode = ""; this.offeredCode = ""; this.proposal = null; super.destroy();
  }
}
