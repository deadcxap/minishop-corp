import { boolean, type ApiClient } from "../shared/api";
import { button, el, person } from "../shared/ui";
import { account, list, type Account } from "./data";

export interface AccountMatches {
  accounts: Account[];
  truncated: boolean;
}

export async function searchAccounts(api: ApiClient, query: string): Promise<AccountMatches> {
  const result = await api.request(`/options/accounts?q=${encodeURIComponent(query.trim())}`);
  return { accounts: list(result.accounts, account), truncated: boolean(result.truncated ?? false) };
}

export function accountCard(candidate: Account, action: string, choose: () => void, telegramLabel: string, style = ""): HTMLElement {
  const card = el("div", undefined, "corp-card");
  card.dataset.account = candidate.minishop_id;
  const labels = [person(candidate), candidate.minishop_id];
  if (candidate.username) labels.push(`@${candidate.username}`);
  if (candidate.telegram_id) labels.push(`${telegramLabel}: ${candidate.telegram_id}`);
  card.append(el("p", labels.join(" · ")), button(action, choose, style));
  return card;
}
