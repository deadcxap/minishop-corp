import en from "../../locales/en.json";
import ru from "../../locales/ru.json";

type LocaleKey = keyof typeof en;
const dictionaries: Record<"ru" | "en", Record<LocaleKey, string>> = { ru, en };

export function translate(language: string, key: LocaleKey): string {
  return dictionaries[language.split("-")[0] === "ru" ? "ru" : "en"][key];
}
