"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import FolderTree from "@/components/FolderTree";
import SiteHeader from "@/components/SiteHeader";
import {
  ApiError,
  type ExportOptions,
  type FolderNode,
  type Summary,
  exportUrl,
  formatSize,
  getFolders,
  getMe,
  getSummary,
  logout,
} from "@/lib/api";

export default function FoldersPage() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [folders, setFolders] = useState<FolderNode[] | null>(null);
  const [selected, setSelected] = useState<FolderNode | null>(null);
  const [includeSubfolders, setIncludeSubfolders] = useState(true);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [busy, setBusy] = useState<"summary" | "download" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        router.replace("/");
        return;
      }
      setError(e instanceof ApiError ? e.message : "Erreur inattendue");
    },
    [router],
  );

  useEffect(() => {
    Promise.all([getMe(), getFolders()])
      .then(([user, tree]) => {
        setUsername(user.username);
        setFolders(tree);
      })
      .catch(handleError);
  }, [handleError]);

  // Toute modification des options invalide l'estimation précédente
  useEffect(() => {
    setSummary(null);
    setNotice(null);
  }, [selected, includeSubfolders, start, end]);

  const datesInvalid = Boolean(start && end && start > end);
  const options: ExportOptions | null = selected
    ? { folderId: selected.id, includeSubfolders, start, end }
    : null;

  async function handleSummary() {
    if (!options) return;
    setError(null);
    setBusy("summary");
    try {
      setSummary(await getSummary(options));
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  async function handleDownload() {
    if (!options) return;
    setError(null);
    setBusy("download");
    try {
      // Vérifie que la session est toujours valide avant de lancer le téléchargement
      await getMe();
      // La réponse étant une pièce jointe, le navigateur la télécharge sans quitter la page
      window.location.assign(exportUrl(options));
      setNotice(
        "Le téléchargement a démarré. L'archive est construite au fil de l'eau : " +
          "pour un gros dossier, cela peut prendre plusieurs minutes.",
      );
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
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
              selectedId={selected?.id ?? null}
              onSelect={setSelected}
            />
          </section>

          <section className="card">
            <h2>Export</h2>
            {!selected ? (
              <p className="muted">Sélectionnez un dossier à télécharger.</p>
            ) : (
              <div className="form">
                <p>
                  Dossier sélectionné : <strong>{selected.name}</strong>{" "}
                  <span className="muted">({selected.path})</span>
                </p>

                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={includeSubfolders}
                    onChange={(e) => setIncludeSubfolders(e.target.checked)}
                  />
                  Inclure les sous-dossiers
                </label>

                <fieldset className="dates">
                  <legend>Période (facultatif)</legend>
                  <label>
                    Du
                    <input
                      type="date"
                      value={start}
                      max={end || undefined}
                      onChange={(e) => setStart(e.target.value)}
                    />
                  </label>
                  <label>
                    Au
                    <input
                      type="date"
                      value={end}
                      min={start || undefined}
                      onChange={(e) => setEnd(e.target.value)}
                    />
                  </label>
                </fieldset>
                {datesInvalid && (
                  <p className="error">La date de début doit précéder la date de fin.</p>
                )}

                {summary && (
                  <p className="info">
                    {summary.message_count} message(s) dans {summary.folder_count} dossier(s),{" "}
                    {formatSize(summary.total_size)} environ.
                  </p>
                )}
                {notice && <p className="info">{notice}</p>}
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
                    disabled={busy !== null || datesInvalid}
                  >
                    Télécharger le ZIP
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      </main>
    </>
  );
}
