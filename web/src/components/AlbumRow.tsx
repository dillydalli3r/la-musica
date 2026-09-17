import type { MouseEvent, ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight } from "lucide-react";
import CoverImg from "./CoverImg";

/** One extra `<td>` of an album row, in display order. `cls` defaults to the
 * standard table cell class; `title` sets the cell's `title` attribute. */
export interface AlbumRowCell {
  id: string;
  cls?: string;
  /** Optional `title` attribute for the `<td>` itself (tooltip). */
  title?: string;
  node: ReactNode;
}

/** The library's album row shell, shared by the Library and Trash views so
 * both render the same cells, classes and expand behaviour. The library keeps
 * its album link and per-track table; the trash passes its own cells, actions
 * and expanded content. */
export default function AlbumRow({
  title,
  titleHref = null,
  titleExtra,
  coverPath,
  coverFile,
  coverTitle,
  cells = [],
  actions,
  expandedContent,
  colSpan,
  selected = false,
  selectMode = false,
  onToggleSel,
  onRowClick,
  showExpand = false,
  expanded = false,
  onToggle,
}: {
  title: ReactNode;
  titleHref?: string | null;
  titleExtra?: ReactNode;
  coverPath: string;
  coverFile?: string | null;
  coverTitle?: string;
  cells?: AlbumRowCell[];
  actions?: ReactNode;
  onRowClick?: () => void;
  selected?: boolean;
  selectMode?: boolean;
  onToggleSel?: () => void;
  showExpand?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  expandedContent?: ReactNode;
  colSpan?: number;
}) {
  // In select mode links select instead of navigating; otherwise they must not
  // bubble up to the row click (which toggles expansion).
  const linkClick = (e: MouseEvent) => {
    if (selectMode) {
      e.preventDefault();
      onToggleSel?.();
    } else e.stopPropagation();
  };
  return (
    <>
      <tr
        className={`table-row group ${selected ? "bg-accent/15" : ""}`}
        onClick={selectMode ? onToggleSel : onRowClick}
      >
        {selectMode && (
          <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
            <input type="checkbox" className="" checked={selected} onChange={onToggleSel} />
          </td>
        )}
        {showExpand && (
          <td className="td pr-0">
            <button
              className="p-1 text-zinc-500 hover:text-white"
              onClick={(e) => {
                e.stopPropagation();
                onToggle?.();
              }}
            >
              {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </button>
          </td>
        )}
        <td className="td">
          {titleHref ? (
            <Link to={titleHref} onClick={linkClick} title={coverTitle} className="inline-block">
              <CoverImg albumPath={coverPath} coverFile={coverFile} />
            </Link>
          ) : (
            <CoverImg albumPath={coverPath} coverFile={coverFile} />
          )}
        </td>
        {/* `title === null` only when the caller hides its name column — the
            whole cell (and its extra badge) disappears with it. */}
        {title != null && (
          <td className="td">
            <div className="flex items-center gap-1.5 min-w-0">
              {titleHref ? (
                <Link
                  to={titleHref}
                  onClick={linkClick}
                  className="font-medium hover:text-accent-soft break-words flex-1 min-w-0"
                >
                  {title}
                </Link>
              ) : (
                <span className="font-medium hover:text-accent-soft break-words flex-1 min-w-0">{title}</span>
              )}
              {titleExtra}
            </div>
          </td>
        )}
        {cells.map((c) => (
          <td key={c.id} className={c.cls ?? "td"} title={c.title}>
            {c.node}
          </td>
        ))}
        <td className="td text-right">
          <div
            className="flex justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={(e) => e.stopPropagation()}
          >
            {actions}
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-panel/30">
          <td colSpan={colSpan} className="p-0">
            {expandedContent}
          </td>
        </tr>
      )}
    </>
  );
}
