import { useEffect, useId, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/** Agent-written Markdown. Raw HTML is never rendered; ```mermaid blocks are drawn as diagrams. */
export function Md({ children }: { children: string }) {
  return <Markdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>{children}</Markdown>;
}

const COMPONENTS: Components = {
  pre({ node, children, ...rest }) {
    const code = node?.children[0];
    if (code && code.type === "element" && code.tagName === "code") {
      const cls = code.properties?.className;
      if (Array.isArray(cls) && cls.includes("language-mermaid")) {
        const text = code.children.map((c) => (c.type === "text" ? c.value : "")).join("");
        return <MermaidDiagram source={text.replace(/\n$/, "")} />;
      }
    }
    return <pre {...rest}>{children}</pre>;
  },
};

// Mermaid renders through shared DOM scratch space, so one diagram is drawn at a time.
let queue: Promise<unknown> = Promise.resolve();

function dark(): boolean {
  const set = document.documentElement.dataset.theme;
  if (set) return set === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

async function draw(id: string, source: string): Promise<string> {
  const { default: mermaid } = await import("mermaid");
  // strict: no scripts or click handlers, labels sanitized; the source is agent-written.
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark() ? "dark" : "neutral",
                       fontFamily: "IBM Plex Sans, sans-serif" });
  const { svg } = await mermaid.render(id, source);
  return svg;
}

export function MermaidDiagram({ source }: { source: string }) {
  const id = "mmd-" + useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    setSvg("");
    setErr("");
    const job = queue.then(() => draw(id, source));
    queue = job.catch(() => undefined);
    job.then((s) => live && setSvg(s)).catch((e) => live && setErr(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [id, source]);
  if (err) {
    return (
      <figure className="diagram">
        <p className="faint small">This diagram didn't draw ({err.split("\n")[0]}). Its source:</p>
        <pre><code>{source}</code></pre>
      </figure>
    );
  }
  return (
    <figure className="diagram">
      {svg ? <div className="diagram-svg" dangerouslySetInnerHTML={{ __html: svg }} /> : <p className="faint small">Drawing the diagram…</p>}
      <details>
        <summary className="small muted">Source</summary>
        <pre><code>{source}</code></pre>
      </details>
    </figure>
  );
}
