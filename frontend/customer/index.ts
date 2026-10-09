import type { CustomerProps } from "../shared/host";
import { CustomerView } from "./app";
import { ManagerView } from "./manager";
import { SettingsCard } from "./settings";
type View = CustomerView | ManagerView | SettingsCard;

export function mountView(view: string, target: HTMLElement, props: CustomerProps): View {
  if (!["corporate-home", "corporate-card", "corporate-manager"].includes(view) || props.host.version !== 1) {
    throw new Error("unsupported_view_or_host");
  }
  return view === "corporate-manager" ? new ManagerView(target, props)
    : view === "corporate-card" ? new SettingsCard(target, props) : new CustomerView(target, props);
}

export function updateView(instance: View, props: CustomerProps): void {
  instance.updateProps(props);
}

export function unmountView(instance: View): void { instance.destroy(); }
