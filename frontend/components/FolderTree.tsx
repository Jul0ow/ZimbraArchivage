"use client";

import { useState } from "react";

import type { FolderNode } from "@/lib/api";

type Props = {
  folders: FolderNode[];
  selectedId: string | null;
  onSelect: (folder: FolderNode) => void;
};

export default function FolderTree({ folders, selectedId, onSelect }: Props) {
  return (
    <ul className="tree" role="tree">
      {folders.map((folder) => (
        <FolderItem key={folder.id} folder={folder} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </ul>
  );
}

function FolderItem({
  folder,
  selectedId,
  onSelect,
}: {
  folder: FolderNode;
  selectedId: string | null;
  onSelect: (folder: FolderNode) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasChildren = folder.children.length > 0;
  const selected = folder.id === selectedId;

  return (
    <li role="treeitem" aria-selected={selected} aria-expanded={hasChildren ? expanded : undefined}>
      <div className={`tree-row${selected ? " selected" : ""}`}>
        {hasChildren ? (
          <button
            type="button"
            className="tree-toggle"
            aria-label={expanded ? `Replier ${folder.name}` : `Déplier ${folder.name}`}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "▾" : "▸"}
          </button>
        ) : (
          <span className="tree-toggle" />
        )}
        <button type="button" className="tree-label" onClick={() => onSelect(folder)}>
          <FolderIcon />
          <span className="tree-name">{folder.name}</span>
          <span className="badge" title="Nombre de messages">
            {folder.message_count}
          </span>
        </button>
      </div>
      {hasChildren && expanded && (
        <ul className="tree" role="group">
          {folder.children.map((child) => (
            <FolderItem key={child.id} folder={child} selectedId={selectedId} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </li>
  );
}

function FolderIcon() {
  return (
    <svg className="tree-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.38a1.5 1.5 0 0 1 1.06.44L11.5 7h8A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"
        fill="currentColor"
      />
    </svg>
  );
}
