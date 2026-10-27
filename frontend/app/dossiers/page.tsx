"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import DownloadOverlay from "@/components/DownloadOverlay";
import ExportForm from "@/components/ExportForm";
import FolderTree from "@/components/FolderTree";
import SiteHeader from "@/components/SiteHeader";
import {
  type FolderNode,
  type Summary,
  exportUrl,
  formatSize,
  getFolders,
  getMe,
  getSummary,
  logout,
  newExportId,
} from "@/lib/api";
import { useApiErrorHandler } from "@/lib/useApiErrorHandler";
import { useExportOptions } from "@/lib/useExportOptions";
import { useExportProgress } from "@/lib/useExportProgress";

// Message associé aux options pour lesquelles il a été affiché
type Keyed<T> = { key: string | null; value: T };

export default function FoldersPage() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [folders, setFolders] = useState<FolderNode[] | null>(null);
  const exportState = useExportOptions();
  const { form, update, options, optionsKey, datesInvalid } = exportState;
  // Estimation et avis valables seulement pour les options avec lesquelles ils ont été obtenus
  const [summary, setSummary] = useState<Keyed<Summary> | null>(null);
  const [notice, setNotice] = useState<Keyed<string> | null>(null);
  const [busy, setBusy] = useState<"summary" | "download" | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Export en cours de téléchargement, suivi par l'écran d'attente
  const [downloadId, setDownloadId] = useState<string | null>(null);
  const exportProgress = useExportProgress(downloadId);
  const handleError = useApiErrorHandler(setError);

  useEffect(() => {
    Promise.all([getMe(), getFolders()])
      .then(([user, tree]) => {
        setUsername(user.username);
        setFolders(tree);
      })
      .catch(handleError);
  }, [handleError]);

  // Fin de l'export : l'écran d'attente laisse place à un message
  if (downloadId && (exportProgress.status === "finished" || exportProgress.status === "failed")) {
    setDownloadId(null);
    if (exportProgress.status === "failed") {
      setError(`Export interrompu : ${exportProgress.error}. Le détail est dans le fichier erreurs.txt de l'archive.`);
    } else {
      setNotice({
        key: optionsKey,
        value: "Archive terminée : le fichier ZIP est dans les téléchargements de votre navigateur.",
      });
    }
  }

  const currentSummary = summary?.key === optionsKey ? summary.value : null;
  const currentNotice = notice?.key === optionsKey ? notice.value : null;

  async function handleSummary() {
    if (!options) return;
    const key = optionsKey;
    setError(null);
    setBusy("summary");
    try {
      setSummary({ key, value: await getSummary(options) });
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  async function handleDownload() {
    if (!options) return;
    setError(null);
    setNotice(null);
    setBusy("download");
    try {
      // Vérifie que la session est toujours valide avant de lancer le téléchargement
      await getMe();
      const id = newExportId();
      // La réponse étant une pièce jointe, le navigateur la télécharge sans quitter la page.
      // Le nombre de messages attendus est donné par l'avancement de l'export lui-même.
      window.location.assign(exportUrl(options, id));
      setDownloadId(id);
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  function handleHideDownload() {
    setDownloadId(null);
    setNotice({
      key: optionsKey,
      value:
        "Le téléchargement continue : suivez son avancement dans la liste des téléchargements " +
        "de votre navigateur.",
    });
  }

  async function handleLogout() {
    await logout().catch(() => undefined);
    router.replace("/");
  }

  if (!folders) {
    return (
      <>
        <SiteHeader username={username} onLogout={handleLogout} />
        <main className="site-main">
          <section className="card">
            {error ? (
              <p className="error" role="alert">
                {error}
              </p>
            ) : (
              <p className="muted">Chargement des dossiers…</p>
            )}
          </section>
        </main>
      </>
    );
  }

  return (
    <>
      <SiteHeader username={username} onLogout={handleLogout} />
      <main className="site-main">
        <div className="layout">
          <section className="card">
            <h2>Dossiers</h2>
            <FolderTree
              folders={folders}
              selectedId={form.folder?.id ?? null}
              onSelect={(folder) => update({ folder })}
            />
          </section>

          <section className="card">
            <h2>Export</h2>
            {!form.folder ? (
              <p className="muted">Sélectionnez un dossier à télécharger.</p>
            ) : (
              <div className="form">
                <ExportForm state={exportState} />

                {currentSummary && (
                  <p className="info">
                    {currentSummary.message_count} message(s) dans {currentSummary.folder_count}{" "}
                    dossier(s), {formatSize(currentSummary.total_size)} environ.
                  </p>
                )}
                {currentNotice && <p className="info">{currentNotice}</p>}
                {error && (
                  <p className="error" role="alert">
                    {error}
                  </p>
                )}

                <div className="actions">
                  <button
                    type="button"
                    className="button"
                    onClick={handleSummary}
                    disabled={busy !== null || datesInvalid}
                  >
                    {busy === "summary" ? "Estimation…" : "Estimer la taille"}
                  </button>
                  <button
                    type="button"
                    className="button primary"
                    onClick={handleDownload}
                    disabled={busy !== null || datesInvalid || downloadId !== null}
                  >
                    Télécharger le ZIP
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      </main>
      {downloadId && <DownloadOverlay progress={exportProgress} onHide={handleHideDownload} />}
    </>
  );
}
