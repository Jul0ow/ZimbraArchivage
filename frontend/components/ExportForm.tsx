"use client";

import FilterEditor from "@/components/FilterEditor";
import type { ExportFormState } from "@/lib/useExportOptions";

type Props = {
  state: ExportFormState;
};

/** Options d'un export : sous-dossiers, période, filtres et pièces jointes. */
export default function ExportForm({ state }: Props) {
  const { form, update, isMailbox, datesInvalid } = state;
  if (!form.folder) return null;

  return (
    <>
      {isMailbox ? (
        <p>
          Export de <strong>toute la boîte aux lettres</strong>{" "}
          <span className="muted">(hors Corbeille et Spam)</span>
        </p>
      ) : (
        <>
          <p>
            Dossier sélectionné : <strong>{form.folder.name}</strong>{" "}
            <span className="muted">({form.folder.path})</span>
          </p>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={form.includeSubfolders}
              onChange={(e) => update({ includeSubfolders: e.target.checked })}
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
            value={form.start}
            max={form.end || undefined}
            onChange={(e) => update({ start: e.target.value })}
          />
        </label>
        <label>
          Au
          <input
            type="date"
            value={form.end}
            min={form.start || undefined}
            onChange={(e) => update({ end: e.target.value })}
          />
        </label>
      </fieldset>
      {datesInvalid && <p className="error">La date de début doit précéder la date de fin.</p>}

      <FilterEditor
        value={form.filterDraft}
        filter={state.options?.filter ?? null}
        onChange={(filterDraft) => update({ filterDraft })}
      />

      <fieldset className="options">
        <legend>Pièces jointes</legend>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.onlyWithAttachments}
            onChange={(e) => update({ onlyWithAttachments: e.target.checked })}
          />
          Uniquement les messages avec pièces jointes
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.extractAttachments}
            onChange={(e) => update({ extractAttachments: e.target.checked })}
          />
          Extraire aussi les pièces jointes dans des fichiers séparés
        </label>
        <p className="hint muted">
          Le fichier .eml contient toujours le message complet avec ses pièces jointes.
        </p>
      </fieldset>
    </>
  );
}
