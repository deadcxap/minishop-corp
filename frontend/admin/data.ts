import { boolean, choice, list, nullable, number, object, string } from "../shared/api";
import type { OperationInfo } from "../shared/members";
import { operation } from "../shared/members-data";
import { account as profile } from "../shared/members-data";
export { operation, member, avatar } from "../shared/members-data";

export interface Contract {
  id: string; name: string; ends_at: string; version: number; expired: boolean;
  tariff_key: string; external_squad_uuid: string | null; manager_user_id: number | null; member_count: number;
}
export interface Terms {
  name: string; tariff_key: string; external_squad_uuid: string | null; ends_at: string; manager_user_id: number | null;
}
export interface Tariff { key: string; names: { ru: string; en: string }; hidden: boolean }
export interface Account { user_id: number; first_name: string | null; last_name: string | null; username: string | null; minishop_id: string; telegram_id: number | null }
export interface Squad { uuid: string; name: string }
export interface Invitation {
  id: string; kind: "single" | "reusable"; used_count: number; reserved_count: number;
  use_limit: number; revoked_at: string | null; created_at: string;
  code: string | null; link: string | null;
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
    expired: boolean(v.expired), tariff_key: string(v.tariff_key), external_squad_uuid: nullable(v.external_squad_uuid, string),
    manager_user_id: nullable(v.manager_user_id, number), member_count: number(v.member_count) };
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
  return { ...profile(v), telegram_id: nullable(v.telegram_id, number) };
}
export function squad(value: unknown): Squad {
  const v = object(value);
  return { uuid: string(v.uuid), name: string(v.name) };
}
export function invitation(value: unknown): Invitation {
  const v = object(value);
  return { id: string(v.id), kind: choice(v.kind, ["single", "reusable"]), used_count: number(v.used_count),
    reserved_count: number(v.reserved_count), use_limit: number(v.use_limit),
    revoked_at: nullable(v.revoked_at, string), created_at: string(v.created_at),
    code: nullable(v.code, string), link: nullable(v.link, string) };
}
export function issued(value: unknown): Issued {
  const v = object(value);
  return { invitation: invitation(v.invitation), code: nullable(v.code, string), link: nullable(v.link, string) };
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
