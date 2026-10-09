import { Panel, button, el, field, input } from "./ui";

export function invitationCode(panel: Panel, row: { id: string; code: string | null; link: string | null }): HTMLElement {
  const box = el("div", undefined, "corp-secret");
  if (!row.code) { box.append(el("p", panel.t("legacy_code"), "corp-notice")); return box; }
  for (const [key, value] of [["code", row.code], ["link", row.link]] as const) {
    if (!value) continue;
    const text = input(key, value, () => {}); text.readOnly = true; text.autocomplete = "off"; text.spellcheck = false;
    text.dataset.focus = `${key}-${row.id}`; text.addEventListener("focus", () => text.select());
    box.append(field(panel.t(key), text), button(panel.t("copy"), () => { void panel.run(async () => {
      try { await navigator.clipboard.writeText(value); panel.notice = "admin_minishop_corp_copied"; }
      catch { panel.notice = "admin_minishop_corp_copy_failed"; }
    }); }));
  }
  if (!row.link) box.append(el("p", panel.t("link_missing")));
  return box;
}
