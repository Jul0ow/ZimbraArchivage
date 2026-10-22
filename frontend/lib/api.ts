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

export type ExportOptions = {
  folderId: string;
  includeSubfolders: boolean;
  // Dates au format AAAA-MM-JJ, vides si pas de borne
  start: string;
  end: string;
};

export type Summary = {
  folder_count: number;
  message_count: number;
  total_size: number;
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

export const getSummary = (options: ExportOptions) =>
  request<Summary>(`/api/export/summary?${exportQuery(options)}`);

export const exportUrl = (options: ExportOptions) => `/api/export?${exportQuery(options)}`;

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
