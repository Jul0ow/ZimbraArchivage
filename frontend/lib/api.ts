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

// Dossier racine Zimbra : le choisir revient à exporter toute la boîte aux lettres
export const ROOT_FOLDER_ID = "1";

export type LogicalOperator = "and" | "or";

export type FilterCondition = {
  field: "subject" | "from" | "to";
  operator: "contains" | "not_contains";
  value: string;
};

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
  finished: boolean;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

function exportQuery(options: ExportOptions): string {
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

export const logout = () => request<void>("/api/auth/logout", { method: "POST" });

export const getMe = () => request<User>("/api/auth/me");

export const getFolders = () => request<FolderNode[]>("/api/folders");

export const getAddressSuggestions = (query: string, field: "from" | "to") =>
  request<AddressSuggestion[]>(`/api/addresses?${new URLSearchParams({ q: query, field })}`);

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
