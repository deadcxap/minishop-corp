import { Panel, el, iconButton } from "./ui";

export type InvitationSecret = "code" | "link";

/** Clipboard access stays inside the user's click; a denied clipboard opens a selectable field. */
export function invitationCopy(panel: Panel, row: { id: string; code: string | null; link: string | null },
  fallback: (key: InvitationSecret) => void): HTMLElement {
  const box = el("div", undefined, "corp-row-actions");
  for (const key of ["code", "link"] as const) {
    const value = row[key], missing = panel.t(!row.code ? "legacy_code" : "link_missing");
    const copy = iconButton(panel.t(key === "code" ? "copy_code" : "copy_link"), key === "code" ? "copy" : "link", () => {
      if (!value || panel.busy || panel.retry) return;
      void (async () => {
        try {
          await navigator.clipboard.writeText(value);
          if (panel.alive) { panel.notice = "admin_minishop_corp_copied"; panel.render(); }
        } catch { if (panel.alive) fallback(key); }
      })();
    }, "corp-ghost");
    copy.dataset.focus = `copy-${key}-${row.id}`;
    copy.disabled = !value;
    if (!value) {
      const unavailable = el("span", undefined, "corp-copy-unavailable");
      unavailable.title = missing; unavailable.tabIndex = 0;
      unavailable.setAttribute("aria-label", `${copy.getAttribute("aria-label")}. ${missing}`);
      unavailable.append(copy, el("span", missing, "corp-sr-only")); box.append(unavailable);
    } else box.append(copy);
  }
  return box;
}
