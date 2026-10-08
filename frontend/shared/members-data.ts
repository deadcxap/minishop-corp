import { choice, nullable, number, object, string } from "./api";
import type { CorporateMember, MemberAvatar, MemberStatistics, OperationInfo, Statistic } from "./members";
export type { OperationInfo } from "./members";

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

export function account(value: unknown) {
  const v = object(value);
  return { user_id: number(v.user_id), first_name: nullable(v.first_name, string),
    last_name: nullable(v.last_name, string), username: nullable(v.username, string) };
}
