"use client";

import { useMemo, useReducer } from "react";

import { EMPTY_FILTER, type FilterDraft, toMessageFilter } from "@/components/FilterEditor";
import { type ExportOptions, type FolderNode, ROOT_FOLDER_ID } from "@/lib/api";

/** Options saisies dans le formulaire d'export, avant leur conversion pour l'API. */
export type ExportForm = {
  folder: FolderNode | null;
  includeSubfolders: boolean;
  // Dates au format AAAA-MM-JJ, vides si pas de borne
  start: string;
  end: string;
  filterDraft: FilterDraft;
  onlyWithAttachments: boolean;
  extractAttachments: boolean;
};

const INITIAL_FORM: ExportForm = {
  folder: null,
  includeSubfolders: true,
  start: "",
  end: "",
  filterDraft: EMPTY_FILTER,
  onlyWithAttachments: false,
  extractAttachments: false,
};

function reducer(form: ExportForm, changes: Partial<ExportForm>): ExportForm {
  return { ...form, ...changes };
}

export type ExportFormState = {
  form: ExportForm;
  update: (changes: Partial<ExportForm>) => void;
  // Options envoyées à l'API, null tant qu'aucun dossier n'est choisi
  options: ExportOptions | null;
  // Identifie les options : une estimation n'est valable que pour la même clé
  optionsKey: string | null;
  isMailbox: boolean;
  datesInvalid: boolean;
};

export function useExportOptions(): ExportFormState {
  const [form, update] = useReducer(reducer, INITIAL_FORM);

  const filter = useMemo(() => toMessageFilter(form.filterDraft), [form.filterDraft]);
  const isMailbox = form.folder?.id === ROOT_FOLDER_ID;
  const options = useMemo<ExportOptions | null>(
    () =>
      form.folder && {
        folderId: form.folder.id,
        // Toute la boîte : les dossiers sont toujours inclus (règle appliquée aussi par le backend)
        includeSubfolders: isMailbox || form.includeSubfolders,
        start: form.start,
        end: form.end,
        filter,
        onlyWithAttachments: form.onlyWithAttachments,
        extractAttachments: form.extractAttachments,
      },
    [form, filter, isMailbox],
  );

  return {
    form,
    update,
    options,
    optionsKey: options && JSON.stringify(options),
    isMailbox,
    datesInvalid: Boolean(form.start && form.end && form.start > form.end),
  };
}
