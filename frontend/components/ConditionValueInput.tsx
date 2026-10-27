"use client";

import { type KeyboardEvent, useEffect, useId, useState } from "react";

import {
  type AddressSuggestion,
  type FilterCondition,
  MAX_FILTER_VALUE_LENGTH,
  getAddressSuggestions,
} from "@/lib/api";

// Suggestions demandées à partir de 2 caractères (minimum du backend), une fois la saisie interrompue
const MIN_QUERY_LENGTH = 2;
const QUERY_DELAY_MS = 250;

type Props = {
  field: FilterCondition["field"];
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
};

/** Valeur d'une condition, avec suggestions d'adresses pour l'expéditeur et le destinataire. */
export default function ConditionValueInput({ field, value, placeholder, onChange }: Props) {
  const listId = useId();
  // Suggestions reçues, associées à la saisie pour laquelle elles ont été demandées
  const [found, setFound] = useState<{ query: string; items: AddressSuggestion[] } | null>(null);
  // Liste ouverte pendant la saisie, refermée au choix d'une adresse ou en quittant le champ
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);

  const query = value.trim();
  const addressField = field === "subject" ? null : field;

  const enabled = open && addressField !== null && query.length >= MIN_QUERY_LENGTH;
  const suggestions = enabled && found?.query === query ? found.items : [];

  useEffect(() => {
    if (!open || !addressField || query.length < MIN_QUERY_LENGTH) return;
    // La requête devenue inutile (nouvelle saisie) est annulée, et non simplement ignorée
    const controller = new AbortController();
    const timer = setTimeout(() => {
      getAddressSuggestions(query, addressField, controller.signal)
        .then((items) => {
          setFound({ query, items });
          setActive(-1);
        })
        // Les suggestions sont facultatives : une erreur n'empêche pas la saisie
        .catch(() => {
          if (!controller.signal.aborted) setFound({ query, items: [] });
        });
    }, QUERY_DELAY_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [open, addressField, query]);

  const expanded = open && suggestions.length > 0;

  function select(suggestion: AddressSuggestion) {
    onChange(suggestion.address);
    setOpen(false);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!expanded) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((index) => (index + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((index) => (index <= 0 ? suggestions.length : index) - 1);
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      select(suggestions[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="combobox">
      <input
        type="text"
        aria-label="Valeur"
        maxLength={MAX_FILTER_VALUE_LENGTH}
        placeholder={placeholder}
        value={value}
        autoComplete="off"
        role={addressField ? "combobox" : undefined}
        aria-autocomplete={addressField ? "list" : undefined}
        aria-expanded={addressField ? expanded : undefined}
        aria-controls={expanded ? listId : undefined}
        aria-activedescendant={expanded && active >= 0 ? `${listId}-${active}` : undefined}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onKeyDown={handleKeyDown}
        onBlur={() => setOpen(false)}
      />
      {expanded && (
        <ul id={listId} role="listbox" className="suggestions">
          {suggestions.map((suggestion, index) => (
            <li
              key={suggestion.address}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "suggestion active" : "suggestion"}
              // mousedown plutôt que click : le champ ne perd pas le focus avant le choix
              onMouseDown={(e) => {
                e.preventDefault();
                select(suggestion);
              }}
              onMouseEnter={() => setActive(index)}
            >
              <span>{suggestion.name || suggestion.address}</span>
              {suggestion.name && <span className="suggestion-address">{suggestion.address}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
