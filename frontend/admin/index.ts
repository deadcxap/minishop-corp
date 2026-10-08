import type { AdminProps } from "../shared/host";
import { AdminView } from "./app";

export function mountView(view: string, target: HTMLElement, props: AdminProps): AdminView {
  if (view !== "corporate-contracts") throw new Error("unsupported_view");
  return new AdminView(target, props.currentLang ?? "en");
}
export function updateView(instance: AdminView, props: AdminProps): void { instance.update(props.currentLang ?? "en"); }
export function unmountView(instance: AdminView): void { instance.destroy(); }
