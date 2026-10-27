"use client";

import { useEffect, useState } from "react";

import { ApiError, getExportProgress } from "@/lib/api";

const POLL_INTERVAL_MS = 1000;
// Au-delà de cette durée, l'avancement est demandé moins souvent
const SLOW_POLL_AFTER_MS = 30000;
const SLOW_POLL_INTERVAL_MS = 3000;
// Export toujours introuvable après ce délai : il est suivi par un autre réplica du backend,
// ou n'a pas pu démarrer. On se contente alors d'une attente simple.
const NOT_FOUND_TIMEOUT_MS = 15000;

// waiting : export pas encore démarré ; unknown : avancement impossible à suivre ;
// failed : export interrompu avant la fin, l'archive est incomplète
export type ExportStatus = "waiting" | "running" | "finished" | "failed" | "unknown";

export type ExportProgressState = {
  done: number;
  // Nombre de courriels attendus, null tant qu'il n'est pas connu
  total: number | null;
  status: ExportStatus;
  error: string | null;
};

const INITIAL_STATE: ExportProgressState = { done: 0, total: null, status: "waiting", error: null };

/** Suit l'avancement d'un export en interrogeant régulièrement le backend. */
export function useExportProgress(exportId: string | null): ExportProgressState {
  const [state, setState] = useState<(ExportProgressState & { exportId: string }) | null>(null);

  useEffect(() => {
    if (!exportId) return;
    const startedAt = Date.now();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll(id: string) {
      try {
        const progress = await getExportProgress(id);
        if (stopped) return;
        let status: ExportStatus = progress.finished ? "finished" : "running";
        if (progress.error) status = "failed";
        setState({ exportId: id, done: progress.done, total: progress.total, status, error: progress.error });
        if (progress.finished) return;
      } catch (e) {
        if (stopped) return;
        const notFound = e instanceof ApiError && e.status === 404;
        const lost =
          (notFound && Date.now() - startedAt > NOT_FOUND_TIMEOUT_MS) ||
          (e instanceof ApiError && e.status === 401);
        if (lost) {
          setState((s) => ({
            ...(s?.exportId === id ? s : INITIAL_STATE),
            exportId: id,
            status: "unknown",
          }));
          return;
        }
        // Export pas encore enregistré ou erreur passagère : on réessaie
      }
      const elapsed = Date.now() - startedAt;
      timer = setTimeout(() => poll(id), elapsed > SLOW_POLL_AFTER_MS ? SLOW_POLL_INTERVAL_MS : POLL_INTERVAL_MS);
    }

    timer = setTimeout(() => poll(exportId), POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [exportId]);

  // L'état d'un export précédent n'est jamais renvoyé pour un nouvel export
  if (!state || state.exportId !== exportId) return INITIAL_STATE;
  return { done: state.done, total: state.total, status: state.status, error: state.error };
}
