import type { CustomerProps } from "../shared/host";
import { customerText } from "../shared/i18n";
import { Panel, button, date, el } from "../shared/ui";
import { CustomerApi } from "./api";
import { membership, nullable, type Membership } from "./data";

/** One compact entry in Settings; membership details belong to the hidden page. */
export class SettingsCard extends Panel {
  current: Membership | null = null;
  loaded = false;
  constructor(target: HTMLElement, public props: CustomerProps) {
    super(props.language, (signal) => new CustomerApi(() => this.props.host, signal));
    this.root.className = "minishop-corp corp-settings-tile"; target.append(this.root);
    void this.run(async () => {
      this.current = nullable((await this.api.request("/membership")).membership, membership);
      this.loaded = true;
    });
  }
  render(): void {
    const c = (key: Parameters<typeof customerText>[1]): string => customerText(this.language, key);
    const entry = button("", () => this.props.host.navigate("corporate-home"), "corp-settings-button");
    const icon = el("span", "▦", "corp-settings-icon"); icon.setAttribute("aria-hidden", "true");
    const text = el("span", undefined, "corp-settings-text");
    text.append(el("strong", this.current?.contract.name ?? c("title")));
    text.append(el("small", this.current ? `${c("ends_at")}: ${date(this.current.contract.ends_at, this.language)}`
      : this.error ? c("settings_unavailable") : this.loaded ? c("settings_join") : c("loading")));
    const arrow = el("span", "›", "corp-settings-arrow"); arrow.setAttribute("aria-hidden", "true");
    entry.append(icon, text, arrow); this.body.replaceChildren(entry);
    this.root.setAttribute("aria-busy", String(this.busy));
  }
  updateProps(props: CustomerProps): void { this.props = props; this.update(props.language); }
}
