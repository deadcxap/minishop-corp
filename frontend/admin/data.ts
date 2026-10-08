import { boolean, choice, list, nullable, number, object, string } from "../shared/api";
import type { CorporateMember, MemberAvatar, MemberStatistics, OperationInfo, Statistic } from "../shared/members";

export interface Contract {
  id: string; name: string; ends_at: string; version: number; expired: boolean;
  tariff_key: string; external_squad_uuid: string; manager_user_id: number; member_count: number;
}
export interface Terms {
  name: string; tariff_key: string; external_squad_uuid: string; ends_at: string; manager_user_id: number;
}
export interface Tariff { key: string; names: { ru: string; en: string }; hidden: boolean }
export interface Account { user_id: number; first_name: string | null; last_name: string | null; username: string | null }
export interface Squad { uuid: string; name: string }
export interface Invitation {
  id: string; kind: "single" | "reusable"; used_count: number; reserved_count: number;
  use_limit: number; revoked_at: string | null; created_at: string;
}
export interface Issued { invitation: Invitation; code: string | null; link: string | null }
export interface Sync {
  version: number; current_members: number; confirmed: number; awaiting_dispatch: number;
  joining: number; departing: number; pending: number; running: number; retrying: number;
  last_checked_at: string | null; next_sweep_at: string; sweep_running: boolean;
  sweep_completed_at: string | null;
}
export interface SyncOperation extends OperationInfo { user_id: number; contract_version: number }

export function contract(value: unknown): Contract {
  const v = object(value);
  return { id: string(v.id), name: string(v.name), ends_at: string(v.ends_at), version: number(v.version),
    expired: boolean(v.expired), tariff_key: string(v.tariff_key), external_squad_uuid: string(v.external_squad_uuid),
    manager_user_id: number(v.manager_user_id), member_count: number(v.member_count) };
}
export function terms(v: Contract): Terms {
  return { name: v.name, tariff_key: v.tariff_key, external_squad_uuid: v.external_squad_uuid,
    ends_at: v.ends_at, manager_user_id: v.manager_user_id };
}
export function sameTerms(a: Terms, b: Terms): boolean {
  return a.name === b.name && a.tariff_key === b.tariff_key && a.external_squad_uuid === b.external_squad_uuid
    && a.manager_user_id === b.manager_user_id && Date.parse(a.ends_at) === Date.parse(b.ends_at);
}
export function tariff(value: unknown): Tariff {
  const v = object(value), names = object(v.names);
  return { key: string(v.key), names: { ru: string(names.ru), en: string(names.en) }, hidden: boolean(v.hidden) };
}
export function account(value: unknown): Account {
  const v = object(value);
  return { user_id: number(v.user_id), first_name: nullable(v.first_name, string),
    last_name: nullable(v.last_name, string), username: nullable(v.username, string) };
}
export function squad(value: unknown): Squad {
  const v = object(value);
  return { uuid: string(v.uuid), name: string(v.name) };
}
export function invitation(value: unknown): Invitation {
  const v = object(value);
  return { id: string(v.id), kind: choice(v.kind, ["single", "reusable"]), used_count: number(v.used_count),
    reserved_count: number(v.reserved_count), use_limit: number(v.use_limit),
    revoked_at: nullable(v.revoked_at, string), created_at: string(v.created_at) };
}
export function issued(value: unknown): Issued {
  const v = object(value);
  return { invitation: invitation(v.invitation), code: nullable(v.code, string), link: nullable(v.link, string) };
}
export function operation(value: unknown): OperationInfo {
  const v = object(value);
  return { id: string(v.id), membership_id: string(v.membership_id),
    kind: choice(v.kind, ["join", "leave", "exclude", "reconcile"]),
    state: choice(v.state, ["pending", "running", "retry", "succeeded", "failed", "cancelled"]),
    attempts: number(v.attempts), error_code: nullable(v.error_code, string),
    next_attempt_at: string(v.next_attempt_at), updated_at: string(v.updated_at) };
}
function statistic(value: unknown): Statistic {
  const v = object(value), state = choice(v.state, ["available", "stored", "stale", "unavailable"]);
  if (state === "unavailable") return { value: null, state };
  return { value: number(v.value), state };
}
export function member(value: unknown): CorporateMember {
  const v = object(value), p = object(v.profile), s = object(v.statistics);
  const statistics: MemberStatistics = { traffic_used_bytes: statistic(s.traffic_used_bytes),
    traffic_limit_bytes: statistic(s.traffic_limit_bytes), device_count: statistic(s.device_count), device_limit: statistic(s.device_limit) };
  return { id: string(v.id), state: choice(v.state, ["pending", "active", "leaving", "left", "excluded", "failed"]),
    joined_at: nullable(v.joined_at, string), profile: { ...account(p), telegram_url: nullable(p.telegram_url, string) },
    statistics, avatar_path: nullable(v.avatar_path, string), operation: nullable(v.operation, operation) };
}
export function avatar(value: unknown): MemberAvatar {
  const v = object(value);
  return { data_url: nullable(v.data_url, string), state: choice(v.state, ["available", "stale", "missing", "unavailable"]),
    updated_at: nullable(v.updated_at, string) };
}
export function synchronization(value: unknown): Sync {
  const v = object(value);
  return { version: number(v.version), current_members: number(v.current_members), confirmed: number(v.confirmed),
    awaiting_dispatch: number(v.awaiting_dispatch), joining: number(v.joining), departing: number(v.departing),
    pending: number(v.pending), running: number(v.running), retrying: number(v.retrying),
    last_checked_at: nullable(v.last_checked_at, string), next_sweep_at: string(v.next_sweep_at),
    sweep_running: boolean(v.sweep_running), sweep_completed_at: nullable(v.sweep_completed_at, string) };
}
export function syncOperation(value: unknown): SyncOperation {
  const v = object(value);
  return { ...operation(v), user_id: number(v.user_id), contract_version: number(v.contract_version) };
}
export { list, nullable, number, string };
