import { describe, expect, it } from "vitest";

import { EMPTY_FILTER, type FilterDraft, toMessageFilter } from "@/components/FilterEditor";
import { type ExportOptions, exportQuery, formatSize, isOneOf, LOGICAL_OPERATORS } from "@/lib/api";

const options: ExportOptions = {
  folderId: "2",
  includeSubfolders: false,
  start: "2024-01-01",
  end: "",
  filter: { operator: "or", groups: [{ operator: "and", conditions: [{ field: "subject", operator: "contains", value: "budget" }] }] },
  onlyWithAttachments: true,
  extractAttachments: false,
};

describe("exportQuery", () => {
  it("construit les paramètres attendus par le backend", () => {
    const params = new URLSearchParams(exportQuery(options));

    expect(params.get("folder_id")).toBe("2");
    expect(params.get("include_subfolders")).toBe("false");
    expect(params.get("start")).toBe("2024-01-01");
    expect(params.has("end")).toBe(false);
    expect(params.get("only_with_attachments")).toBe("true");
    expect(params.has("extract_attachments")).toBe(false);
    expect(JSON.parse(params.get("filter")!)).toEqual(options.filter);
  });
});

describe("toMessageFilter", () => {
  it("ignore les conditions vides et les groupes sans condition", () => {
    const draft: FilterDraft = {
      operator: "and",
      groups: [
        { key: 1, operator: "or", conditions: [
          { key: 2, field: "from", operator: "contains", value: "  alice  " },
          { key: 3, field: "to", operator: "contains", value: " " },
        ] },
        { key: 4, operator: "and", conditions: [{ key: 5, field: "subject", operator: "contains", value: "" }] },
      ],
    };

    expect(toMessageFilter(draft)).toEqual({
      operator: "and",
      groups: [{ operator: "or", conditions: [{ field: "from", operator: "contains", value: "alice" }] }],
    });
    expect(toMessageFilter(EMPTY_FILTER)).toBeNull();
  });
});

describe("utilitaires", () => {
  it("vérifie les valeurs permises", () => {
    expect(isOneOf(LOGICAL_OPERATORS, "and")).toBe(true);
    expect(isOneOf(LOGICAL_OPERATORS, "xor")).toBe(false);
  });

  it("formate les tailles", () => {
    expect(formatSize(512)).toBe("512 o");
    expect(formatSize(1536)).toBe("1,5 Ko");
  });
});
