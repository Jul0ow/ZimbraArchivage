"use client";

import { Fragment } from "react";

import ConditionValueInput from "@/components/ConditionValueInput";
import {
  CONDITION_OPERATORS,
  FILTER_FIELDS,
  type FilterCondition,
  LOGICAL_OPERATORS,
  type LogicalOperator,
  MAX_FILTER_GROUPS,
  MAX_GROUP_CONDITIONS,
  type MessageFilter,
  isOneOf,
} from "@/lib/api";

// Brouillon édité dans le formulaire : chaque élément porte une clé stable pour React
export type ConditionDraft = FilterCondition & { key: number };
export type GroupDraft = { key: number; operator: LogicalOperator; conditions: ConditionDraft[] };
export type FilterDraft = { operator: LogicalOperator; groups: GroupDraft[] };

export const EMPTY_FILTER: FilterDraft = { operator: "or", groups: [] };

const FIELD_LABELS: Record<FilterCondition["field"], string> = {
  subject: "Objet",
  from: "Expéditeur",
  to: "Destinataire",
};

const FIELD_PLACEHOLDERS: Record<FilterCondition["field"], string> = {
  subject: "ex. facture",
  from: "ex. jean.dupont@domaine.fr",
  to: "ex. service@domaine.fr",
};

const OPERATOR_LABELS: Record<FilterCondition["operator"], string> = {
  contains: "contient",
  not_contains: "ne contient pas",
};

const LOGICAL_LABELS: Record<LogicalOperator, string> = { and: "ET", or: "OU" };

// Clés React des groupes et conditions, uniques pour la page
let lastKey = 0;
const newKey = () => ++lastKey;

const newCondition = (): ConditionDraft => ({
  key: newKey(),
  field: "subject",
  operator: "contains",
  value: "",
});

const newGroup = (): GroupDraft => ({
  key: newKey(),
  operator: "and",
  conditions: [newCondition()],
});

/** Filtre envoyé à l'API : conditions sans valeur et groupes vides ignorés, null si rien à filtrer. */
export function toMessageFilter(draft: FilterDraft): MessageFilter | null {
  const groups = draft.groups
    .map((group) => ({
      operator: group.operator,
      conditions: group.conditions
        .map(({ field, operator, value }) => ({ field, operator, value: value.trim() }))
        .filter((condition) => condition.value),
    }))
    .filter((group) => group.conditions.length > 0);
  return groups.length > 0 ? { operator: draft.operator, groups } : null;
}

/** Description en français du filtre, ex. (Objet contient « a » ET Expéditeur contient « b ») OU … */
export function describeFilter(filter: MessageFilter): string {
  const groups = filter.groups.map((group) => {
    const text = group.conditions
      .map((c) => `${FIELD_LABELS[c.field]} ${OPERATOR_LABELS[c.operator]} « ${c.value} »`)
      .join(` ${LOGICAL_LABELS[group.operator]} `);
    return group.conditions.length > 1 && filter.groups.length > 1 ? `(${text})` : text;
  });
  return groups.join(` ${LOGICAL_LABELS[filter.operator]} `);
}

type Props = {
  value: FilterDraft;
  // Filtre envoyé à l'API, calculé à partir de value par toMessageFilter
  filter: MessageFilter | null;
  onChange: (value: FilterDraft) => void;
};

export default function FilterEditor({ value, filter, onChange }: Props) {
  const canAddGroup = value.groups.length < MAX_FILTER_GROUPS;

  function updateGroup(key: number, update: (group: GroupDraft) => GroupDraft | null) {
    const groups = value.groups.flatMap((group) => {
      if (group.key !== key) return [group];
      const updated = update(group);
      // Un groupe sans condition est supprimé
      return updated && updated.conditions.length > 0 ? [updated] : [];
    });
    onChange({ ...value, groups });
  }

  function updateCondition(groupKey: number, key: number, changes: Partial<FilterCondition>) {
    updateGroup(groupKey, (group) => ({
      ...group,
      conditions: group.conditions.map((c) => (c.key === key ? { ...c, ...changes } : c)),
    }));
  }

  return (
    <fieldset className="filters">
      <legend>Filtres (facultatif)</legend>
      <p className="hint muted">
        Recherche par mots, comme dans Zimbra. Ajoutez * pour chercher un début de mot (ex. budg*).
        Le destinataire couvre les champs À et Cc. Pour l’expéditeur et le destinataire, des
        adresses sont proposées dès les premières lettres.
      </p>

      {value.groups.length > 1 && (
        <label className="inline-field">
          Les messages doivent respecter
          <select
            value={value.operator}
            onChange={(e) => {
              const operator = e.target.value;
              if (isOneOf(LOGICAL_OPERATORS, operator)) onChange({ ...value, operator });
            }}
          >
            <option value="or">au moins un des groupes (OU)</option>
            <option value="and">tous les groupes (ET)</option>
          </select>
        </label>
      )}

      {value.groups.length > 0 && (
        <div className="filter-groups">
          {value.groups.map((group, index) => (
            <Fragment key={group.key}>
              {index > 0 && <div className="connector">{LOGICAL_LABELS[value.operator]}</div>}
              <div className="filter-group">
                <div className="filter-group-header">
                  <strong>Groupe {index + 1}</strong>
                  {group.conditions.length > 1 && (
                    <select
                      aria-label={`Opérateur du groupe ${index + 1}`}
                      value={group.operator}
                      onChange={(e) => {
                        const operator = e.target.value;
                        if (isOneOf(LOGICAL_OPERATORS, operator)) {
                          updateGroup(group.key, (g) => ({ ...g, operator }));
                        }
                      }}
                    >
                      <option value="and">toutes les conditions (ET)</option>
                      <option value="or">au moins une condition (OU)</option>
                    </select>
                  )}
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Supprimer le groupe ${index + 1}`}
                    title="Supprimer le groupe"
                    onClick={() => updateGroup(group.key, () => null)}
                  >
                    ×
                  </button>
                </div>

                {group.conditions.map((condition, conditionIndex) => (
                  <Fragment key={condition.key}>
                    {conditionIndex > 0 && (
                      <div className="connector small">{LOGICAL_LABELS[group.operator]}</div>
                    )}
                    <div className="filter-row">
                      <select
                        aria-label="Champ"
                        value={condition.field}
                        onChange={(e) => {
                          const field = e.target.value;
                          if (isOneOf(FILTER_FIELDS, field)) {
                            updateCondition(group.key, condition.key, { field });
                          }
                        }}
                      >
                        {Object.entries(FIELD_LABELS).map(([field, label]) => (
                          <option key={field} value={field}>
                            {label}
                          </option>
                        ))}
                      </select>
                      <select
                        aria-label="Opérateur"
                        value={condition.operator}
                        onChange={(e) => {
                          const operator = e.target.value;
                          if (isOneOf(CONDITION_OPERATORS, operator)) {
                            updateCondition(group.key, condition.key, { operator });
                          }
                        }}
                      >
                        {Object.entries(OPERATOR_LABELS).map(([operator, label]) => (
                          <option key={operator} value={operator}>
                            {label}
                          </option>
                        ))}
                      </select>
                      <ConditionValueInput
                        field={condition.field}
                        placeholder={FIELD_PLACEHOLDERS[condition.field]}
                        value={condition.value}
                        onChange={(value) => updateCondition(group.key, condition.key, { value })}
                      />
                      <button
                        type="button"
                        className="icon-button"
                        aria-label="Supprimer la condition"
                        title="Supprimer la condition"
                        onClick={() =>
                          updateGroup(group.key, (g) => ({
                            ...g,
                            conditions: g.conditions.filter((c) => c.key !== condition.key),
                          }))
                        }
                      >
                        ×
                      </button>
                    </div>
                  </Fragment>
                ))}

                <div>
                  <button
                    type="button"
                    className="button small subtle"
                    disabled={group.conditions.length >= MAX_GROUP_CONDITIONS}
                    title={
                      group.conditions.length >= MAX_GROUP_CONDITIONS
                        ? `${MAX_GROUP_CONDITIONS} conditions au plus par groupe`
                        : undefined
                    }
                    onClick={() =>
                      updateGroup(group.key, (g) => ({
                        ...g,
                        conditions: [...g.conditions, newCondition()],
                      }))
                    }
                  >
                    + Ajouter une condition
                  </button>
                </div>
              </div>
            </Fragment>
          ))}
        </div>
      )}

      <div>
        <button
          type="button"
          className="button small"
          disabled={!canAddGroup}
          title={canAddGroup ? undefined : `${MAX_FILTER_GROUPS} groupes au plus`}
          onClick={() => onChange({ ...value, groups: [...value.groups, newGroup()] })}
        >
          {value.groups.length === 0 ? "+ Ajouter un filtre" : "+ Ajouter un groupe de conditions"}
        </button>
      </div>

      {filter && (
        <p className="filter-preview">
          <span className="muted">Filtre appliqué :</span> {describeFilter(filter)}
        </p>
      )}
    </fieldset>
  );
}
