/** The subset of the v1 host used by this plugin, with no imports of core components. */
export interface CustomerProps {
  host: {
    version: 1;
    request(path: string, options?: RequestInit): Promise<unknown>;
  };
  language: string;
}

export interface AdminProps {
  currentLang?: string;
}
