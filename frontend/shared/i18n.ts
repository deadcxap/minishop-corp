import en from "../../locales/en.json";
import ru from "../../locales/ru.json";
import { ApiError } from "./api";

type LocaleKey = keyof typeof en;
const dictionaries: Record<"ru" | "en", Record<LocaleKey, string>> = { ru, en };

export function translate(language: string, key: LocaleKey): string {
  return dictionaries[language.split("-")[0] === "ru" ? "ru" : "en"][key];
}

export type { LocaleKey };
export type AdminKey = { [K in LocaleKey]: K extends `admin_minishop_corp_${infer S}` ? S : never }[LocaleKey];
export function adminText(language: string, key: AdminKey): string {
  return translate(language, `admin_minishop_corp_${key}`);
}
export type CustomerKey = { [K in LocaleKey]: K extends `wa_minishop_corp_${infer S}` ? S : never }[LocaleKey];
export function customerText(language: string, key: CustomerKey): string {
  return translate(language, `wa_minishop_corp_${key}`);
}
export function errorText(language: string, error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return adminText(language, "session_error");
  if (error instanceof ApiError && error.status === 403) return translate(language, "minishop_corp_access_denied");
  const code = error instanceof Error ? error.message : "";
  if (["csrf_failed", "unauthorized", "authentication_required"].includes(code)) {
    return adminText(language, "session_error");
  }
  if (Object.prototype.hasOwnProperty.call(en, code)) return translate(language, code as LocaleKey);
  return translate(language, "wa_minishop_corp_error");
}
