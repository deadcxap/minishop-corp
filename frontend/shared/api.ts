/** Admin extensions in the pinned host use same-origin cookies and its CSRF cookie. */
export class ApiError extends Error {
  constructor(readonly code: string, readonly status = 0) { super(code); }
  get uncertain(): boolean { return this.status === 0 || this.status >= 500; }
}

export function requestId(): string {
  // getRandomValues also works on local HTTP previews, unlike randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export type ObjectValue = Record<string, unknown>;
export function object(value: unknown): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_response");
  return value as ObjectValue;
}
export function string(value: unknown): string {
  if (typeof value !== "string") throw new ApiError("invalid_response");
  return value;
}
export function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) throw new ApiError("invalid_response");
  return value;
}
export function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new ApiError("invalid_response");
  return value;
}
export function nullable<T>(value: unknown, decode: (value: unknown) => T): T | null {
  return value === null ? null : decode(value);
}
export function list<T>(value: unknown, decode: (value: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new ApiError("invalid_response");
  return value.map(decode);
}
export function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  const result = choices.find((item) => item === value);
  if (result === undefined) throw new ApiError("invalid_response");
  return result;
}

export class AdminApi {
  constructor(readonly signal: AbortSignal) {}
  async request(path: string, method = "GET", body?: object): Promise<ObjectValue> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") {
      const cookie = document.cookie.match(/(?:^|;\s*)rw_webapp_csrf=([^;]*)/);
      headers["X-CSRF-Token"] = cookie ? decodeURIComponent(cookie[1] ?? "") : "";
    }
    try {
      const response = await fetch(`/api/admin/minishop-corp${path}`, {
        method, headers, credentials: "same-origin", signal: this.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let data: ObjectValue;
      try { data = object(await response.json()); }
      catch { throw new ApiError("request_failed", response.ok ? 0 : response.status); }
      if (!response.ok || data.ok !== true) {
        throw new ApiError(typeof data.error === "string" ? data.error : "request_failed",
          response.ok && data.ok !== false ? 0 : response.status);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError("request_failed");
    }
  }
}
