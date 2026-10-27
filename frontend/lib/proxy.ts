import { Agent, fetch as undiciFetch } from "undici";

// Relais des appels /api/* vers le backend FastAPI (voir app/api/[...path]/route.ts).
// L'adresse du backend est lue à l'exécution, ce qui permet de la changer sans reconstruire l'image.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Sans délai d'attente : une estimation peut durer plusieurs minutes avant la réponse,
// et un export long ne doit pas être coupé (les délais par défaut sont de 300 s)
const dispatcher = new Agent({ headersTimeout: 0, bodyTimeout: 0, keepAliveTimeout: 60_000 });

// En-têtes propres à chaque connexion HTTP, qu'il ne faut pas retransmettre
const EXCLUDED_HEADERS = [
  "connection",
  "keep-alive",
  "transfer-encoding",
  "host",
  "content-length",
  "content-encoding",
  "accept-encoding",
];

export function filterHeaders(source: Headers): Headers {
  const headers = new Headers(source);
  for (const name of EXCLUDED_HEADERS) {
    headers.delete(name);
  }
  return headers;
}

/** URL du backend pour un chemin relayé, null si un segment tente de sortir de /api. */
export function backendUrl(path: string[], search: string, base: string = BACKEND_URL): URL | null {
  if (path.some((segment) => segment === "." || segment === ".." || segment === "")) {
    return null;
  }
  const url = new URL(base);
  url.pathname = `${url.pathname.replace(/\/$/, "")}/api/${path.map(encodeURIComponent).join("/")}`;
  url.search = search;
  return url;
}

// fetch d'undici, pour pouvoir lui passer le dispatcher ci-dessus ; remplaçable dans les tests
export const backend = {
  fetch: (url: URL, init: Parameters<typeof undiciFetch>[1]) => undiciFetch(url, { ...init, dispatcher }),
};

export async function proxy(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  const target = backendUrl(path, new URL(request.url).search);
  if (!target) {
    return Response.json({ detail: "Chemin invalide" }, { status: 400 });
  }
  const hasBody = !["GET", "HEAD"].includes(request.method);

  let response: Awaited<ReturnType<typeof undiciFetch>>;
  try {
    response = await backend.fetch(target, {
      method: request.method,
      headers: filterHeaders(request.headers),
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      // Interrompt l'export côté backend si l'utilisateur annule le téléchargement
      signal: request.signal,
    });
  } catch {
    return Response.json({ detail: "Service d'archivage injoignable" }, { status: 502 });
  }

  // Le corps est retransmis en flux, sans attendre la fin de l'archive ZIP
  return new Response(response.body as ReadableStream<Uint8Array> | null, {
    status: response.status,
    headers: filterHeaders(response.headers as unknown as Headers),
  });
}
