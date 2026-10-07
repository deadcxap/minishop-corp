import { translate } from "./i18n";
import "./styles.css";

type Status = "loading" | "ready" | "error";

export interface ViewInstance {
  update(language: string): void;
  destroy(): void;
}

export function mountStatus(
  target: HTMLElement,
  language: string,
  audience: "customer" | "admin",
  load: (signal: AbortSignal) => Promise<unknown>,
): ViewInstance {
  const controller = new AbortController();
  const root = document.createElement("section");
  root.className = "minishop-corp";
  const heading = document.createElement("h2");
  const message = document.createElement("p");
  message.setAttribute("role", "status");
  root.append(heading, message);
  target.append(root);
  let currentLanguage = language;
  let status: Status = "loading";

  function render(): void {
    heading.textContent = translate(currentLanguage,
      audience === "admin" ? "admin_minishop_corp_title" : "wa_minishop_corp_title");
    message.textContent = translate(currentLanguage, status === "ready"
      ? audience === "admin" ? "admin_minishop_corp_pending" : "wa_minishop_corp_pending"
      : status === "error" ? "wa_minishop_corp_error" : "wa_minishop_corp_loading");
    root.setAttribute("aria-busy", String(status === "loading"));
  }

  render();
  void load(controller.signal).then((result: unknown) => {
    if (typeof result !== "object" || result === null || !("stage" in result)
      || result.stage !== "scaffold") {
      throw new Error("invalid_status_response");
    }
    status = "ready";
  }).catch(() => { status = "error"; }).finally(() => {
    if (!controller.signal.aborted) render();
  });

  return {
    update(nextLanguage) { currentLanguage = nextLanguage; render(); },
    destroy() { controller.abort(); root.remove(); },
  };
}
