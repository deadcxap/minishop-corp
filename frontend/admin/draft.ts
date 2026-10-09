import { boolean, choice, nullable, number, object, string } from "../shared/api";
import { contract, type Contract, type Terms } from "./data";

export type Tab = "terms" | "members" | "invitations" | "sync";
export interface Submission { terms: Terms; previous: Contract | null }
export interface EditorDraft {
  id: string; original: Contract | null; name: string; tariffKey: string; end: string;
  initialEnd: string; endTouched: boolean; managerId: number | null; squadId: string;
  query: string; pending: Submission | null;
}
export interface DraftState { contractId: string | null; tab: Tab; editor: EditorDraft | null }
const KEY = "minishop-corp.admin-draft.v1";
const TTL = 24 * 60 * 60 * 1000;
const memory = new WeakMap<Document, string>();
const active = new WeakMap<Document, DraftStore>();

function decodeSubmission(value: unknown): Submission {
  const v = object(value), t = object(v.terms);
  return { previous: nullable(v.previous, contract), terms: {
    name: string(t.name), tariff_key: string(t.tariff_key),
    external_squad_uuid: nullable(t.external_squad_uuid, string),
    ends_at: string(t.ends_at), manager_user_id: nullable(t.manager_user_id, number),
  } };
}
function decodeEditor(value: unknown): EditorDraft {
  const v = object(value);
  return { id: string(v.id), original: nullable(v.original, contract), name: string(v.name),
    tariffKey: string(v.tariffKey), end: string(v.end), initialEnd: string(v.initialEnd),
    endTouched: boolean(v.endTouched), managerId: nullable(v.managerId, number),
    squadId: string(v.squadId), query: string(v.query), pending: nullable(v.pending, decodeSubmission) };
}

/** One tab-local draft, restored only after the server confirms the current administrator. */
export class DraftStore {
  constructor(readonly owner: number, private doc = document) { active.set(doc, this); }
  clear(): void {
    if (active.get(this.doc) !== this) return;
    memory.delete(this.doc);
    try { this.doc.defaultView?.sessionStorage.removeItem(KEY); } catch { /* Storage may be disabled. */ }
  }
  write(state: DraftState): void {
    // Host transitions can mount the replacement before disposing the old view.
    if (active.get(this.doc) !== this) return;
    const raw = JSON.stringify({ version: 1, owner: this.owner, updated: Date.now(), ...state });
    memory.set(this.doc, raw);
    try { this.doc.defaultView?.sessionStorage.setItem(KEY, raw); } catch { /* Remounts still use memory. */ }
  }
  read(): DraftState | null {
    try {
      let raw = memory.get(this.doc);
      try { raw = this.doc.defaultView?.sessionStorage.getItem(KEY) ?? raw; } catch { /* Use memory. */ }
      if (!raw) return null;
      if (raw.length > 64_000) throw new Error("invalid_draft");
      const v = object(JSON.parse(raw));
      const age = Date.now() - number(v.updated);
      // A clock correction must not discard edits saved a few moments earlier.
      if (v.version !== 1 || number(v.owner) !== this.owner || age > TTL) throw new Error("expired_draft");
      return { contractId: nullable(v.contractId, string), tab: choice(v.tab, ["terms", "members", "invitations", "sync"]),
        editor: nullable(v.editor, decodeEditor) };
    } catch { this.clear(); return null; }
  }
}
