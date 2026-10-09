import type { CustomerProps } from "../shared/host";
import { customerText } from "../shared/i18n";
import { Panel, button, date, el } from "../shared/ui";
import { CustomerApi } from "./api";
import { membership, nullable, type Membership } from "./data";

function icon(size: number, paths: string[], className: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({ width: String(size), height: String(size), viewBox: "0 0 24 24",
    fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: className })) svg.setAttribute(name, value);
  for (const d of paths) { const path = document.createElementNS(svg.namespaceURI, "path"); path.setAttribute("d", d); svg.append(path); }
  return svg;
}

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
    entry.className = "corp-settings-button";
    const symbol = icon(21, ["M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2", "M4 6h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z", "M2 11a20 20 0 0 0 20 0", "M12 12v2"], "corp-settings-icon");
    const text = el("span", undefined, "corp-settings-text");
    text.append(el("strong", this.current?.contract.name ?? c("title")));
    text.append(el("small", this.current ? `${c("ends_at")}: ${date(this.current.contract.ends_at, this.language)}`
      : this.error ? c("settings_unavailable") : this.loaded ? c("settings_join") : c("loading")));
    const arrow = icon(17, ["M5 12h14", "m12 5 7 7-7 7"], "corp-settings-arrow");
    entry.append(symbol, text, arrow); this.body.replaceChildren(entry);
    this.root.setAttribute("aria-busy", String(this.busy));
  }
  updateProps(props: CustomerProps): void { this.props = props; this.update(props.language); }
}
