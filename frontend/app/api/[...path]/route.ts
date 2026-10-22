import type { NextRequest } from "next/server";

// Relaie les appels /api/* vers le backend FastAPI. L'adresse du backend est lue
// à l'exécution, ce qui permet de la changer sans reconstruire l'image.
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

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

function filterHeaders(source: Headers): Headers {
  const headers = new Headers(source);
  for (const name of EXCLUDED_HEADERS) {
    headers.delete(name);
  }
  return headers;
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  const target = new URL(
    `/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`,
    BACKEND_URL,
  );
  const hasBody = !["GET", "HEAD"].includes(request.method);

  let response: Response;
  try {
    response = await fetch(target, {
      method: request.method,
      headers: filterHeaders(request.headers),
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
      // Interrompt l'export côté backend si l'utilisateur annule le téléchargement
      signal: request.signal,
    });
  } catch {
    return Response.json({ detail: "Service d'archivage injoignable" }, { status: 502 });
  }

  // Le corps est retransmis en flux, sans attendre la fin de l'archive ZIP
  return new Response(response.body, {
    status: response.status,
    headers: filterHeaders(response.headers),
  });
}

export { proxy as GET, proxy as POST };
