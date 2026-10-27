"use client";

import type { ExportProgressState } from "@/lib/useExportProgress";

type Props = {
  progress: ExportProgressState;
  onHide: () => void;
};

/** Écran d'attente affiché pendant la construction et le téléchargement de l'archive. */
export default function DownloadOverlay({ progress, onHide }: Props) {
  const { done, total, status } = progress;
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : null;

  let detail: string;
  if (status === "waiting") {
    detail = "Démarrage de l'export…";
  } else if (total === null) {
    detail = "Recherche des courriels à archiver…";
  } else {
    detail = `${done.toLocaleString("fr-FR")} message(s) sur ${total.toLocaleString("fr-FR")}`;
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
            L’avancement ne peut pas être affiché ici : suivez le téléchargement dans la liste des
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
          Les courriels sont récupérés depuis Zimbra : pour un gros dossier, cela peut prendre
          plusieurs minutes. Le téléchargement continue si vous masquez cette fenêtre.
        </p>
        <button type="button" className="button" onClick={onHide} autoFocus>
          Masquer
        </button>
      </div>
    </div>
  );
}
