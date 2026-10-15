import type { AdminProps } from "../shared/host";
import { AdminView } from "./app";
import { BroadcastAudienceView } from "./broadcasts";

type AdminInstance = AdminView | BroadcastAudienceView;
export function mountView(view: string, target: HTMLElement, props: AdminProps): AdminInstance {
  if (view === "corporate-broadcast-audience") return new BroadcastAudienceView(target, props.currentLang ?? "en", props.routePrefix);
  if (view !== "corporate-contracts") throw new Error("unsupported_view");
  return new AdminView(target, props.currentLang ?? "en");
}
export function updateView(instance: AdminInstance, props: AdminProps): void { instance.update(props.currentLang ?? "en"); }
export function unmountView(instance: AdminInstance): void { instance.destroy(); }
