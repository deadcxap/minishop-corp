import { ApiError, object, type ApiClient, type ObjectValue } from "../shared/api";
import type { CustomerProps } from "../shared/host";

/** Native host unwraps successes; 429/5xx errors carry status and payload. */
export class CustomerApi implements ApiClient {
  constructor(private host: () => CustomerProps["host"], readonly signal: AbortSignal) {}
  async request(path: string, method = "GET", body?: object): Promise<ObjectValue> {
    try {
      return object(await this.host().request(path, {
        method, signal: this.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }));
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error && typeof error === "object") {
        const outer = error as Record<string, unknown>;
        const payload = outer.payload && typeof outer.payload === "object"
          ? outer.payload as Record<string, unknown> : outer;
        const code = typeof payload.error === "string" ? payload.error : "request_failed";
        const status = typeof outer.status === "number" ? outer.status
          : payload.ok === false ? ["unauthorized", "authentication_required"].includes(code) ? 401
            : ["minishop_corp_access_denied", "forbidden"].includes(code) ? 403
              : ["minishop_corp_contract_missing", "minishop_corp_membership_missing", "minishop_corp_invitation_missing"].includes(code) ? 404 : 400 : 0;
        const wait = typeof payload.retry_after === "number" && Number.isFinite(payload.retry_after)
          ? Math.max(0, payload.retry_after) : null;
        throw new ApiError(code, status, wait);
      }
      throw new ApiError("request_failed");
    }
  }
}
