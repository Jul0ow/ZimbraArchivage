"use client";

import { useEffect, useState } from "react";

import { ApiError, getExportProgress } from "@/lib/api";

const POLL_INTERVAL_MS = 1000;
// Export toujours introuvable après ce délai : il est suivi par un autre réplica du backend,
// ou n'a pas pu démarrer. On se contente alors d'une attente simple.
const NOT_FOUND_TIMEOUT_MS = 15000;

// waiting : export pas encore démarré ; unknown : avancement impossible à suivre
export type ExportStatus = "waiting" | "running" | "finished" | "unknown";

type ProgressState = { exportId: string; done: number; status: ExportStatus };

/** Suit l'avancement d'un export en interrogeant le backend toutes les secondes. */
export function useExportProgress(exportId: string | null): { done: number; status: ExportStatus } {
  const [state, setState] = useState<ProgressState | null>(null);

  useEffect(() => {
    if (!exportId) return;
    const startedAt = Date.now();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll(id: string) {
      try {
        const progress = await getExportProgress(id);
        if (stopped) return;
        setState({
          exportId: id,
          done: progress.done,
          status: progress.finished ? "finished" : "running",
        });
        if (progress.finished) return;
      } catch (e) {
        if (stopped) return;
        const notFound = e instanceof ApiError && e.status === 404;
        const lost =
          (notFound && Date.now() - startedAt > NOT_FOUND_TIMEOUT_MS) ||
          (e instanceof ApiError && e.status === 401);
        if (lost) {
          setState((s) => ({
            exportId: id,
            done: s?.exportId === id ? s.done : 0,
            status: "unknown",
          }));
          return;
        }
        // Export pas encore enregistré ou erreur passagère : on réessaie
      }
      timer = setTimeout(() => poll(id), POLL_INTERVAL_MS);
    }

    timer = setTimeout(() => poll(exportId), POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [exportId]);

  // L'état d'un export précédent n'est jamais renvoyé pour un nouvel export
  return state && state.exportId === exportId ? state : { done: 0, status: "waiting" };
}

type Props = {
  done: number;
  // Nombre de messages attendus, null s'il n'est pas (encore) connu
  total: number | null;
  status: ExportStatus;
  onHide: () => void;
};

/** Écran d'attente affiché pendant la construction et le téléchargement de l'archive. */
export default function DownloadOverlay({ done, total, status, onHide }: Props) {
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : null;

  let detail: string;
  if (status === "waiting") {
    detail = "Démarrage de l'export…";
  } else if (total !== null) {
    detail = `${done.toLocaleString("fr-FR")} message(s) sur ${total.toLocaleString("fr-FR")}`;
  } else {
    detail = `${done.toLocaleString("fr-FR")} message(s) ajouté(s) à l'archive`;
  }

  return (
    <div className="overlay">
      <div
        className="overlay-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="download-title"
      >
        <div className="spinner" aria-hidden="true" />
        <h2 id="download-title">Téléchargement en cours</h2>
        {status === "unknown" ? (
          <p>
            L'avancement ne peut pas être affiché ici : suivez le téléchargement dans la liste des
            téléchargements de votre navigateur.
          </p>
        ) : (
          <>
            <p aria-live="polite">{detail}</p>
            <div
              className="progress-bar"
              role="progressbar"
              aria-label="Avancement de l'export"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent ?? undefined}
            >
              <div
                className={percent === null ? "progress-value indeterminate" : "progress-value"}
                style={percent === null ? undefined : { width: `${percent}%` }}
              />
            </div>
          </>
        )}
        <p className="hint muted">
          Les courriels sont récupérés un par un depuis Zimbra : pour un gros dossier, cela peut
          prendre plusieurs minutes. Le téléchargement continue si vous masquez cette fenêtre.
        </p>
        <button type="button" className="button" onClick={onHide} autoFocus>
          Masquer
        </button>
      </div>
    </div>
  );
}
