import { boolean, choice, list, nullable, number, object, string } from "../shared/api";
import { operation, type OperationInfo } from "../shared/members-data";

export interface Summary { id: string; name: string; ends_at: string; version: number; expired: boolean }
export interface Tariff { key: string; names: { ru: string; en: string }; traffic_limit_bytes: number | null; hwid_device_limit: number | null }
export interface CustomerOperation extends OperationInfo { departure: { kind: "trial" | "disabled"; ends_at: string } | null }
export interface Membership { id: string; state: "pending" | "active" | "leaving" | "left" | "excluded" | "failed"; contract: Summary; tariff: Tariff | null; can_leave: boolean; operation: CustomerOperation | null }
export interface Offer { invitation_id: string; contract: Summary; tariff: Tariff }
export function summary(value: unknown): Summary {
  const v = object(value);
  return { id: string(v.id), name: string(v.name), ends_at: string(v.ends_at), version: number(v.version), expired: boolean(v.expired) };
}
export function tariff(value: unknown): Tariff {
  const v = object(value), names = object(v.names);
  return { key: string(v.key), names: { ru: string(names.ru), en: string(names.en) },
    traffic_limit_bytes: nullable(v.traffic_limit_bytes, number), hwid_device_limit: nullable(v.hwid_device_limit, number) };
}
export function customerOperation(value: unknown): CustomerOperation {
  const v = object(value);
  return { ...operation(v), departure: nullable(v.departure, (raw) => {
    const d = object(raw); return { kind: choice(d.kind, ["trial", "disabled"]), ends_at: string(d.ends_at) };
  }) };
}
export function membership(value: unknown): Membership {
  const v = object(value);
  return { id: string(v.id), state: choice(v.state, ["pending", "active", "leaving", "left", "excluded", "failed"]),
    contract: summary(v.contract), tariff: nullable(v.tariff, tariff), can_leave: boolean(v.can_leave),
    operation: nullable(v.operation, customerOperation) };
}
export function offer(value: unknown): Offer {
  const v = object(value), o = object(v.offer);
  return { invitation_id: string(o.invitation_id), contract: summary(o.contract), tariff: tariff(v.tariff) };
}
export { list, nullable, number, object, string };
