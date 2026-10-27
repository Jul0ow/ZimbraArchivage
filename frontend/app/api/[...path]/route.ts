import { proxy } from "@/lib/proxy";

// Relaie les appels /api/* vers le backend FastAPI, à chaque requête
export const dynamic = "force-dynamic";

export { proxy as GET, proxy as POST };
