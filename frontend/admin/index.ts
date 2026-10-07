import type { AdminProps } from "../shared/host";
import { mountStatus, type ViewInstance } from "../shared/view";

export function mountView(view: string, target: HTMLElement, props: AdminProps): ViewInstance {
  if (view !== "corporate-contracts") throw new Error("unsupported_view");
  return mountStatus(target, props.currentLang ?? "en", "admin", async (signal) => {
    const response = await fetch("/api/admin/minishop-corp/status", {
      credentials: "same-origin", signal,
    });
    const result: unknown = await response.json();
    if (!response.ok || typeof result !== "object" || result === null
      || !("ok" in result) || result.ok !== true) {
      throw new Error("status_request_failed");
    }
    return result;
  });
}

export function updateView(instance: ViewInstance, props: AdminProps): void {
  instance.update(props.currentLang ?? "en");
}

export function unmountView(instance: ViewInstance): void { instance.destroy(); }
