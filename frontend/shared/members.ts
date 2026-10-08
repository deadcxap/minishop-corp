/** Scoped JSON payloads returned by S07; the customer host unwraps the ok envelope. */
export type DataState = "available" | "stored" | "stale" | "unavailable";
export type Statistic =
  | { value: number; state: Exclude<DataState, "unavailable"> }
  | { value: null; state: "unavailable" };

export interface MemberStatistics {
  traffic_used_bytes: Statistic;
  traffic_limit_bytes: Statistic;
  device_count: Statistic;
  device_limit: Statistic;
}

export interface OperationInfo {
  id: string;
  membership_id: string;
  kind: "join" | "leave" | "exclude" | "reconcile";
  state: "pending" | "running" | "retry" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  error_code: string | null;
  next_attempt_at: string;
  updated_at: string;
}

export interface CorporateMember {
  id: string;
  state: "pending" | "active" | "leaving" | "left" | "excluded" | "failed";
  joined_at: string | null;
  profile: {
    user_id: number;
    first_name: string | null;
    last_name: string | null;
    username: string | null;
    telegram_url: string | null;
  };
  statistics: MemberStatistics;
  avatar_path: string | null;
  operation: OperationInfo | null;
}

export interface MembersPage {
  members: CorporateMember[];
  next_after: string | null;
}

export interface MemberAvatar {
  data_url: string | null;
  state: "available" | "stale" | "missing" | "unavailable";
  updated_at: string | null;
}
