import { fixture, CONTRACT_ID, MEMBER_ID } from "./admin-api.mjs";
export const CODE = "CORP-00000000000000000000000000000000";
export function customerFixture() {
  const base = fixture();
  const contract = () => { const { id, name, ends_at, version, expired } = base.contracts[0]; return { id, name, ends_at, version, expired }; };
  const tariff = { key: "corp", names: { ru: "Внутренний тариф", en: "Internal tariff" }, traffic_limit_bytes: 21474836480, hwid_device_limit: 5, traffic_strategy: "NO_RESET" };
  const state = { base, calls: [], manager: false, membership: null, last_departure: null, operations: [], loseNextWrite: false,
    previewError: null, confirmError: null, trial: true, wait: 60 };
  const ok = (payload, status = 200) => ({ status, payload: { ok: true, ...payload } });
  const fail = (error, status = 400, retry_after = null) => ({ status, payload: { ok: false, error, retry_after } });
  const operation = (kind, request_id) => ({ id: request_id, membership_id: MEMBER_ID, kind, state: "pending", attempts: 0,
    error_code: null, next_attempt_at: "2029-01-01T00:00:00Z", updated_at: "2029-01-01T00:00:00Z", departure: null });
  const handle = (url, method, body) => {
    const parsed = new URL(url, "https://fixture.invalid"), path = parsed.pathname.replace("/api/plugins/minishop-corp", "");
    state.calls.push({ path, method, body });
    if (path.startsWith("/managed-contracts")) {
      if (path === "/managed-contracts") return ok({ contracts: state.manager ? [contract()] : [] });
      if (!state.manager) return fail("minishop_corp_contract_missing", 404);
      if (path.endsWith("/invitations") && method === "GET") return ok({ invitations: base.invitations.filter((v) => v.kind === "reusable" && !v.revoked_at) });
      return base.respond(parsed.href.replace("/api/plugins/minishop-corp/managed-contracts", "/api/admin/minishop-corp/contracts").replace("/managed-contracts", "/api/admin/minishop-corp/contracts"), method, body);
    }
    if (path === "/membership" && method === "GET") {
      if (state.membership) { state.membership.contract = contract(); state.membership.tariff = tariff; }
      return ok({ membership: state.membership, last_departure: state.membership ? null : state.last_departure });
    }
    if (path === "/invitations/preview") {
      if (state.previewError) return fail(state.previewError.code, state.previewError.status, state.previewError.wait);
      if (body.code !== CODE) return fail("minishop_corp_invitation_unavailable", 400, state.wait);
      return ok({ offer: { invitation_id: "40000000-0000-4000-8000-000000000001", contract: contract() }, tariff, retry_after: state.wait });
    }
    if (path === "/membership/confirm" || path.endsWith("/leave")) {
      const old = state.operations.find((v) => v.id === body.request_id); if (old) return ok({ operation: old });
      if (path.endsWith("confirm") && state.confirmError) return fail(state.confirmError, 409, state.wait);
      const op = operation(path.endsWith("confirm") ? "join" : "leave", body.request_id); state.operations.push(op);
      state.membership = { id: MEMBER_ID, state: op.kind === "join" ? "pending" : "leaving", contract: contract(), tariff,
        can_leave: false, joined_at: null, operation: op, notice: "wa_minishop_corp_payment_notice", expiry_notice: null };
      return ok({ operation: op }, 202);
    }
    if (path.startsWith("/operations/")) return ok({ operation: state.operations.find((v) => v.id === path.split("/").at(-1)) });
    throw new Error(`Unexpected customer fixture route: ${method} ${path}`);
  };
  state.respond = (path, method = "GET", body) => {
    const result = handle(path, method, body);
    if (method !== "GET" && state.loseNextWrite) { state.loseNextWrite = false; throw new Error("synthetic_lost_response"); }
    return structuredClone(result);
  };
  state.hostRequest = async (path, options = {}) => {
    const result = state.respond(path, options.method ?? "GET", options.body ? JSON.parse(options.body) : undefined);
    if (result.status >= 500 || result.status === 429) throw Object.assign(new Error("service_unavailable"), { status: result.status, payload: result.payload });
    if (!result.payload.ok) throw result.payload;
    const { ok: _ok, ...data } = result.payload; return data;
  };
  state.complete = () => {
    const op = state.operations.at(-1); op.state = "succeeded";
    if (op.kind === "join") { state.membership.state = "active"; state.membership.can_leave = true; }
    else { op.departure = { kind: state.trial ? "trial" : "disabled", ends_at: "2030-11-01T00:00:00Z" }; state.last_departure = op; state.membership = null; }
  };
  state.seedCode = () => base.invitations.push({ id: "40000000-0000-4000-8000-000000000001", contract_id: CONTRACT_ID, kind: "reusable", use_limit: 12,
    used_count: 4, reserved_count: 0, revoked_at: null, created_at: "2029-01-01T00:00:00Z", code: CODE, link: `https://example.invalid/#corp_code=${CODE}` });
  return state;
}
