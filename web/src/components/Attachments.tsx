import type { Attachment } from "../api";

export function size(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const KIND: Record<string, string> = { image: "Image", pdf: "PDF", text: "Text" };

/** Files attached to an intent: who reads each (agents read text; images and PDFs are for people). */
export function AttachmentList({ items, href, onRemove }: {
  items: Attachment[]; href: (a: Attachment) => string; onRemove?: (a: Attachment) => void;
}) {
  if (!items.length) return null;
  return (
    <ul className="attachments">
      {items.map((a) => (
        <li key={a.id}>
          <span className={`file-kind ${a.kind}`} aria-hidden="true">{a.kind === "image" ? "IMG" : a.kind === "pdf" ? "PDF" : "TXT"}</span>
          <span className="file-main">
            <a href={href(a)} download={a.name}>{a.name}</a>
            <span className="small faint">
              {KIND[a.kind] ?? a.kind}, {size(a.size)}. {a.read_by_agents ? "Agents read it at discovery and spec." : "For people; agents see only its name."}
            </span>
          </span>
          {onRemove && <button type="button" className="linkish remove" onClick={() => onRemove(a)} aria-label={`Remove ${a.name}`}>Remove</button>}
        </li>
      ))}
    </ul>
  );
}
