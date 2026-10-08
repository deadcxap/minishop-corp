/** Link contents only prefill a field; the server still checks code and consent. */
export function invitationFromLocation(): string {
  const params = new URLSearchParams(window.location.search);
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const fromFragment = fragment.get("corp_code");
  if (fromFragment && /^CORP-[a-f0-9]{32}$/i.test(fromFragment)) return fromFragment.toUpperCase();
  const telegram = (window as Window & { Telegram?: { WebApp?: { initDataUnsafe?: { start_param?: unknown } } } }).Telegram;
  for (const value of [telegram?.WebApp?.initDataUnsafe?.start_param,
    ...["tgWebAppStartParam", "startapp", "start_param"].map((key) => params.get(key))]) {
    if (typeof value === "string" && /^corp_[a-f0-9]{32}$/i.test(value)) return `CORP-${value.slice(5).toUpperCase()}`;
  }
  return "";
}
