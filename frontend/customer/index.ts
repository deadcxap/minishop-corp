import type { CustomerProps } from "../shared/host";
import { mountStatus, type ViewInstance } from "../shared/view";

export function mountView(view: string, target: HTMLElement, props: CustomerProps): ViewInstance {
  if (view !== "corporate-home" || props.host.version !== 1) {
    throw new Error("unsupported_view_or_host");
  }
  return mountStatus(target, props.language, "customer",
    (signal) => props.host.request("/status", { signal }));
}

export function updateView(instance: ViewInstance, props: CustomerProps): void {
  instance.update(props.language);
}

export function unmountView(instance: ViewInstance): void { instance.destroy(); }
