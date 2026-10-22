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
          <span>{folder.name}</span>
          <span className="badge">{folder.message_count}</span>
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
