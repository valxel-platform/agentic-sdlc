import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";

/** An agent-written artifact, rendered as Markdown (raw HTML is never rendered). */
export function ArtifactBody({ featureId, kind }: { featureId: string; kind: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    setText(null);
    setErr("");
    api.artifact(featureId, kind).then((a) => live && setText(a.content)).catch((e) => live && setErr(e.message));
    return () => { live = false; };
  }, [featureId, kind]);
  if (err) return <p className="error-note">{err}</p>;
  if (text === null) return <p className="faint small">Loading…</p>;
  return <div className="md"><Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown></div>;
}
