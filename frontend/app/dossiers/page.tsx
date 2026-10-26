"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import FilterEditor, { EMPTY_FILTER, type FilterDraft, toMessageFilter } from "@/components/FilterEditor";
import FolderTree from "@/components/FolderTree";
import SiteHeader from "@/components/SiteHeader";
import {
  ApiError,
  type ExportOptions,
  type FolderNode,
  ROOT_FOLDER_ID,
  type Summary,
  exportUrl,
  formatSize,
  getFolders,
  getMe,
  getSummary,
  logout,
} from "@/lib/api";

// Corbeille et Spam, exclues de l'export de toute la boîte (comme côté backend)
const EXCLUDED_FROM_MAILBOX = new Set(["3", "4"]);

function countMessages(folder: FolderNode): number {
  return folder.message_count + folder.children.reduce((sum, c) => sum + countMessages(c), 0);
}

/** Entrée « Toute la boîte aux lettres », placée avant les dossiers. */
function mailboxNode(folders: FolderNode[]): FolderNode {
  const included = folders.filter((f) => !EXCLUDED_FROM_MAILBOX.has(f.id));
  return {
    id: ROOT_FOLDER_ID,
    name: "Toute la boîte aux lettres",
    path: "/",
    message_count: included.reduce((sum, f) => sum + countMessages(f), 0),
    color: null,
    children: [],
  };
}

export default function FoldersPage() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [folders, setFolders] = useState<FolderNode[] | null>(null);
  const [selected, setSelected] = useState<FolderNode | null>(null);
  const [includeSubfolders, setIncludeSubfolders] = useState(true);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [filterDraft, setFilterDraft] = useState<FilterDraft>(EMPTY_FILTER);
  const [onlyWithAttachments, setOnlyWithAttachments] = useState(false);
  const [extractAttachments, setExtractAttachments] = useState(false);
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

  const filter = useMemo(() => toMessageFilter(filterDraft), [filterDraft]);
  const filterKey = JSON.stringify(filter);
  const treeFolders = useMemo(() => folders && [mailboxNode(folders), ...folders], [folders]);
  const isMailbox = selected?.id === ROOT_FOLDER_ID;

  // Toute modification des options invalide l'estimation précédente
  useEffect(() => {
    setSummary(null);
    setNotice(null);
  }, [selected, includeSubfolders, start, end, filterKey, onlyWithAttachments, extractAttachments]);

  const datesInvalid = Boolean(start && end && start > end);
  const options: ExportOptions | null = selected
    ? {
        folderId: selected.id,
        // Toute la boîte : les dossiers sont forcément inclus
        includeSubfolders: isMailbox || includeSubfolders,
        start,
        end,
        filter,
        onlyWithAttachments,
        extractAttachments,
      }
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

  if (!treeFolders) {
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
              folders={treeFolders}
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
                {isMailbox ? (
                  <p>
                    Export de <strong>toute la boîte aux lettres</strong>{" "}
                    <span className="muted">(hors Corbeille et Spam)</span>
                  </p>
                ) : (
                  <>
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
                  </>
                )}

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

                <FilterEditor value={filterDraft} onChange={setFilterDraft} />

                <fieldset className="options">
                  <legend>Pièces jointes</legend>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={onlyWithAttachments}
                      onChange={(e) => setOnlyWithAttachments(e.target.checked)}
                    />
                    Uniquement les messages avec pièces jointes
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={extractAttachments}
                      onChange={(e) => setExtractAttachments(e.target.checked)}
                    />
                    Extraire aussi les pièces jointes dans des fichiers séparés
                  </label>
                  <p className="hint muted">
                    Le fichier .eml contient toujours le message complet avec ses pièces jointes.
                  </p>
                </fieldset>

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
