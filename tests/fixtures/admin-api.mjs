// Synthetic API fixture for UI flows; real host services are covered by PostgreSQL tests.
export const CONTRACT_ID = "30000000-0000-4000-8000-000000000001";
export const SQUAD_ID = "20000000-0000-4000-8000-000000000001";
export const MEMBER_ID = "50000000-0000-4000-8000-000000000001";
export const MINISHOP_ID = "ms_40000000000040008000000000000001";
export const TELEGRAM_ID = 771234567;
export const DIAGNOSTIC_ID = "1234567890abcdef1234567890abcdef";
export const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aTgAAAABJRU5ErkJggg==";
const now = "2030-01-01T00:00:00Z";
const profile = { user_id: 910011, first_name: "Fixture", last_name: "Manager", username: "fixture_manager", telegram_url: "https://t.me/fixture_manager" };
const available = (value) => ({ value, state: "available" });
const unknown = { value: null, state: "unavailable" };
export function fixture() {
  const state = {
    calls: [], actorUserId: 910001, loseNextWrite: false, failList: false, pending: false,
    contracts: [{ id: CONTRACT_ID, name: "Fixture organization — a long name for responsive contract management", tariff_key: "corp",
      external_squad_uuid: SQUAD_ID, manager_user_id: 910011, ends_at: "2030-10-01T12:42:23.123Z", version: 1, expired: false, member_count: 2 }],
    invitations: [],
    members: [
      { id: MEMBER_ID, state: "active", joined_at: now, profile: { ...profile, user_id: 910021, first_name: "Fixture <script>alert(1)</script>", last_name: "Very long member name" },
        statistics: { traffic_used_bytes: available(1073741824), traffic_limit_bytes: available(21474836480), device_count: available(2), device_limit: available(5) },
        avatar_path: `/api/admin/minishop-corp/contracts/${CONTRACT_ID}/members/${MEMBER_ID}/avatar`, operation: null },
      { id: "50000000-0000-4000-8000-000000000002", state: "active", joined_at: now,
        profile: { user_id: 910022, first_name: null, last_name: null, username: null, telegram_url: null },
        statistics: { traffic_used_bytes: unknown, traffic_limit_bytes: { value: 0, state: "stored" }, device_count: unknown, device_limit: available(0) }, avatar_path: null, operation: null },
    ],
  };
  const ok = (payload) => ({ status: 200, payload: { ok: true, ...payload } });
  const fail = (error, status = 400) => ({ status, payload: { ok: false, error } });
  function handle(path, method = "GET", body) {
    const url = new URL(path, "https://fixture.invalid");
    const p = url.pathname.replace("/api/admin/minishop-corp", "");
    state.calls.push({ path: p, method, body, search: url.search });
    if (p === "/options/context") return ok({ actor_user_id: state.actorUserId });
    if (p === "/options/tariffs") return ok({ tariffs: [
      { key: "personal", names: { ru: "Личный", en: "Personal" }, hidden: false },
      { key: "corp", names: { ru: "Корпоративный", en: "Corporate" }, hidden: true },
    ] });
    if (p === "/options/accounts") {
      const q = url.searchParams.get("q");
      const found = url.searchParams.get("user_id") === String(profile.user_id) || ["910011", String(TELEGRAM_ID), MINISHOP_ID, "@fixture_manager", "fixture_manager", "manager@example.invalid"].includes(q);
      return ok({ accounts: found ? [{ ...profile, minishop_id: MINISHOP_ID, telegram_id: TELEGRAM_ID }] : [], next_page: null });
    }
    if (p === "/options/squad") return url.searchParams.get("uuid") === SQUAD_ID ? ok({ squad: { uuid: SQUAD_ID, name: "Fixture external squad" } }) : fail("minishop_corp_squad_unavailable", 422);
    if (p === "/contracts") {
      if (method === "GET") {
        if (state.failList) return fail("minishop_corp_access_denied", 403);
        return ok({ contracts: state.contracts.filter((row) => !url.searchParams.get("after") || row.id > url.searchParams.get("after")).slice(0, Number(url.searchParams.get("limit") ?? 25)) });
      }
      const existing = state.contracts.find((row) => row.id === body.id);
      if (existing) return ok({ contract: existing });
      const row = { ...body, version: 1, expired: Date.parse(body.ends_at) <= Date.now(), member_count: 0 }; state.contracts.push(row);
      return ok({ contract: row });
    }
    const parts = p.split("/").filter(Boolean), current = state.contracts.find((row) => row.id === parts[1]);
    if (!current) return fail("minishop_corp_contract_missing", 404);
    if (parts.length === 2) {
      if (method === "PUT") {
        if (body.expected_version !== current.version) return fail("minishop_corp_version_conflict", 409);
        const { expected_version, ...data } = body; Object.assign(current, data, { version: current.version + 1 });
      }
      return ok({ contract: current });
    }
    if (parts[2] === "invitations") {
      if (method === "GET") return ok({ invitations: state.invitations.filter((row) => !url.searchParams.get("after") || row.id > url.searchParams.get("after")).slice(0, 25) });
      const old = state.invitations.find((row) => row.id === parts[3]);
      if (parts[4] === "revoke") { if (!old) return fail("minishop_corp_invitation_missing", 404); old.revoked_at = now; return ok({ invitation: old }); }
      const existing = state.invitations.find((row) => row.id === body.id);
      if (existing) return ok({ invitation: existing, code: null, link: null, created: false, notice: "minishop_corp_code_shown_once" });
      if (old) old.revoked_at = now;
      else if (body.kind === "reusable" && state.invitations.some((row) => row.kind === "reusable" && !row.revoked_at)) return fail("minishop_corp_invitation_exists", 409);
      const row = { id: body.id, contract_id: current.id, kind: old?.kind ?? body.kind, use_limit: old?.use_limit ?? body.use_limit,
        used_count: 0, reserved_count: 0, revoked_at: null, created_at: now, replaces_id: old?.id ?? null };
      state.invitations.push(row);
      return ok({ invitation: row, code: "CORP-00000000000000000000000000000000", link: "https://example.invalid/#corp_code=synthetic", created: true, notice: "minishop_corp_code_shown_once" });
    }
    if (parts[2] === "members") {
      if (parts.length === 3) return ok({ members: state.members, next_after: null });
      const row = state.members.find((row) => row.id === parts[3]);
      if (!row) return fail("minishop_corp_membership_missing", 404);
      if (parts[4] === "avatar") return ok({ avatar: { data_url: PNG, state: "available", updated_at: now } });
      if (parts[4] === "exclude") {
        state.pending = true; row.state = "leaving";
        row.operation = { id: body.request_id, membership_id: row.id, kind: "exclude", state: "pending", attempts: 0, error_code: null, next_attempt_at: now, updated_at: now };
        return { status: 202, payload: { ok: true, operation: row.operation } };
      }
    }
    if (parts[2] === "synchronization") {
      if (parts[3] === "operations") return ok({ operations: [{ id: "60000000-0000-4000-8000-000000000001", membership_id: MEMBER_ID,
        user_id: 910021, contract_version: current.version, kind: "reconcile", state: "retry", attempts: 2, error_code: "minishop_corp_panel_unconfirmed", next_attempt_at: now, updated_at: now }] });
      return ok({ synchronization: { version: current.version, current_members: state.members.length, confirmed: 0, awaiting_dispatch: 1,
        joining: 0, departing: state.pending ? 1 : 0, pending: 0, running: 0, retrying: 1, last_checked_at: null,
        next_sweep_at: now, sweep_running: false, sweep_completed_at: now } });
    }
    throw new Error(`Unexpected fixture route: ${method} ${p}`);
  }
  state.respond = (path, method = "GET", body) => {
    const response = handle(path, method, body);
    if (method !== "GET" && state.loseNextWrite) { state.loseNextWrite = false; throw new Error("synthetic_network_loss"); }
    // HTTP JSON also separates client values from the backing fixture.
    return structuredClone(response);
  };
  return state;
}
