export type User = {
  username: string;
};

export type FolderNode = {
  id: string;
  name: string;
  path: string;
  message_count: number;
  // Couleur définie dans Zimbra (#RRGGBB), null pour la couleur par défaut
  color: string | null;
  children: FolderNode[];
};

// Dossier racine Zimbra : le choisir revient à exporter toute la boîte aux lettres.
// L'entrée correspondante, avec son nombre de messages, est fournie par le backend.
export const ROOT_FOLDER_ID = "1";

// Limites d'un filtre, les mêmes que MessageFilter côté backend (backend/app/filters.py)
export const MAX_FILTER_GROUPS = 10;
export const MAX_GROUP_CONDITIONS = 20;
export const MAX_FILTER_VALUE_LENGTH = 200;

export const LOGICAL_OPERATORS = ["and", "or"] as const;
export type LogicalOperator = (typeof LOGICAL_OPERATORS)[number];

export const FILTER_FIELDS = ["subject", "from", "to"] as const;
export const CONDITION_OPERATORS = ["contains", "not_contains"] as const;

export type FilterCondition = {
  field: (typeof FILTER_FIELDS)[number];
  operator: (typeof CONDITION_OPERATORS)[number];
  value: string;
};

/** Vérifie qu'une valeur saisie (ex. option d'un select) fait partie des valeurs permises. */
export function isOneOf<T extends string>(values: readonly T[], value: string): value is T {
  return (values as readonly string[]).includes(value);
}

export type FilterGroup = {
  operator: LogicalOperator;
  conditions: FilterCondition[];
};

// Groupes de conditions : même format que MessageFilter côté backend
export type MessageFilter = {
  operator: LogicalOperator;
  groups: FilterGroup[];
};

// Adresse suggérée pour un filtre sur l'expéditeur ou le destinataire
export type AddressSuggestion = {
  address: string;
  name: string;
};

export type ExportOptions = {
  folderId: string;
  includeSubfolders: boolean;
  // Dates au format AAAA-MM-JJ, vides si pas de borne
  start: string;
  end: string;
  filter: MessageFilter | null;
  onlyWithAttachments: boolean;
  extractAttachments: boolean;
};

export type Summary = {
  folder_count: number;
  message_count: number;
  total_size: number;
};

export type ExportProgress = {
  // Nombre de courriels traités (ajoutés à l'archive ou en erreur)
  done: number;
  // Nombre de courriels à traiter, null tant que les dossiers sont parcourus
  total: number | null;
  finished: boolean;
  // Raison d'un export interrompu avant la fin (session expirée, Zimbra indisponible…)
  error: string | null;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    let message = `Erreur inattendue (HTTP ${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body.detail)) {
        message = "Paramètres invalides";
      }
    } catch {
      // Corps de réponse non JSON : on garde le message générique
    }
    throw new ApiError(response.status, message);
  }
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // Le contenu est celui décrit par l'API du backend : il n'est pas revérifié ici
  return (await send(path, init)).json() as Promise<T>;
}

async function requestNoContent(path: string, init?: RequestInit): Promise<void> {
  await send(path, init);
}

export function exportQuery(options: ExportOptions): string {
  const params = new URLSearchParams({
    folder_id: options.folderId,
    include_subfolders: String(options.includeSubfolders),
  });
  if (options.start) params.set("start", options.start);
  if (options.end) params.set("end", options.end);
  if (options.filter) params.set("filter", JSON.stringify(options.filter));
  if (options.onlyWithAttachments) params.set("only_with_attachments", "true");
  if (options.extractAttachments) params.set("extract_attachments", "true");
  return params.toString();
}

export const login = (username: string, password: string) =>
  request<User>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

export const logout = () => requestNoContent("/api/auth/logout", { method: "POST" });

export const getMe = () => request<User>("/api/auth/me");

export const getFolders = () => request<FolderNode[]>("/api/folders");

export const getAddressSuggestions = (query: string, field: "from" | "to", signal?: AbortSignal) =>
  request<AddressSuggestion[]>(`/api/addresses?${new URLSearchParams({ q: query, field })}`, {
    signal,
  });

export const getSummary = (options: ExportOptions) =>
  request<Summary>(`/api/export/summary?${exportQuery(options)}`);

// exportId permet de suivre l'avancement de l'export avec getExportProgress
export const exportUrl = (options: ExportOptions, exportId: string) =>
  `/api/export?${exportQuery(options)}&${new URLSearchParams({ export_id: exportId })}`;

export const getExportProgress = (exportId: string) =>
  request<ExportProgress>(`/api/export/progress?${new URLSearchParams({ export_id: exportId })}`);

/** Identifiant aléatoire d'un export (crypto.randomUUID n'existe qu'en HTTPS). */
export function newExportId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function formatSize(bytes: number): string {
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toLocaleString("fr-FR", { maximumFractionDigits: unit === 0 ? 0 : 1 })} ${units[unit]}`;
}
