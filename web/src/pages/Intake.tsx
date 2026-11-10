import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, PROFILE_HELP, PROFILE_LABEL, type Draft, type IntakeSession, type Me, type Profile } from "../api";
import { SimilarList } from "../components/External";

const FIELD_LABEL: Record<string, string> = {
  product_id: "Product", problem: "Problem", users: "Users", success_metric: "Success measure",
};

export function Intake({ me }: { me: Me }) {
  const { sid } = useParams();
  const nav = useNavigate();
  const [s, setS] = useState<IntakeSession | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [pilot, setPilot] = useState(false);
  const [profile, setProfile] = useState<Profile | "">("");
  const [profiles, setProfiles] = useState<Record<string, { allowed: Profile[]; dflt: Profile }>>({});
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setErr("");
    if (sid) {
      api.intake(sid).then(setS).catch((e) => setErr(e.message));
    } else if (me.lanes.length) {
      api.intakeStart().then((x) => nav(`/intake/${x.id}`, { replace: true })).catch((e) => setErr(e.message));
    }
  }, [sid, me.lanes.length, nav]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [s?.messages.length]);
  useEffect(() => {
    api.products().then((ps) => setProfiles(Object.fromEntries(Object.entries(ps).map(([id, p]) =>
      [id, { allowed: p.profiles ?? ["feature"], dflt: p.default_profile ?? "feature" }])))).catch(() => undefined);
  }, []);

  async function run(p: Promise<IntakeSession>) {
    setBusy(true);
    setErr("");
    try { setS(await p); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  function say(e?: FormEvent) {
    e?.preventDefault();
    if (!s || !text.trim()) return;
    const t = text;
    setText("");
    setS({ ...s, messages: [...s.messages, { role: "user", text: t, at: new Date().toISOString() }] });
    run(api.intakeSay(s.id, t));
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); say(); }
  }

  function edit(field: keyof Draft, value: string) {
    if (!s) return;
    setS({ ...s, draft: { ...s.draft, [field]: value } });
  }

  function save(field: keyof Draft) {
    if (!s) return;
    run(api.intakeDraft(s.id, { [field]: s.draft[field] }));
  }

  if (!me.lanes.length) {
    return <div className="panel empty"><h2>You can't submit intents yet</h2><p>Ask an owner to add you to a product's GitHub organization.</p></div>;
  }
  if (!s) return err ? <p className="error-note" role="alert">{err}</p> : <p className="faint">Starting…</p>;

  const submitted = Boolean(s.feature_id);
  const names = Object.fromEntries(me.lanes.map((l) => [l.id, l.name]));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>New intent</h1>
          <p className="muted">Tell the intake agent what you need. It drafts the intent as you talk; you submit it when it's right.</p>
        </div>
        <button className="btn secondary" onClick={() => api.intakeStart().then((x) => nav(`/intake/${x.id}`))}>Start over</button>
      </div>
      <div className="intake">
        <section className="panel chat" aria-label="Conversation with the intake agent">
          <div className="messages" aria-live="polite">
            {s.messages.map((m, i) => <div key={i} className={`msg ${m.role}`}>{m.text}</div>)}
            {busy && <div className="msg agent faint">Thinking…</div>}
            <div ref={end} />
          </div>
          <form className="composer" onSubmit={say}>
            <label className="visually-hidden" htmlFor="say">Message</label>
            <textarea id="say" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
              placeholder={submitted ? "This intent has been submitted." : "Describe the problem, or answer the question above"}
              disabled={submitted} autoFocus />
            <button className="btn" type="submit" disabled={busy || submitted || !text.trim()}>Send</button>
          </form>
        </section>

        <section className="panel intent" aria-labelledby="h-intent">
          <h2 id="h-intent">The intent</h2>
          <label className="field">Product
            <select value={s.draft.product_id} disabled={submitted}
              onChange={(e) => { edit("product_id", e.target.value); run(api.intakeDraft(s.id, { product_id: e.target.value })); }}>
              <option value="">Not chosen yet</option>
              {s.lanes.map((l) => <option key={l} value={l}>{names[l] ?? l}</option>)}
            </select>
          </label>
          <label className="field">Title
            <input value={s.draft.title} disabled={submitted} onChange={(e) => edit("title", e.target.value)} onBlur={() => save("title")} />
          </label>
          <label className="field">Problem
            <textarea value={s.draft.problem} disabled={submitted} onChange={(e) => edit("problem", e.target.value)} onBlur={() => save("problem")} />
          </label>
          <label className="field">Users
            <input value={s.draft.users} disabled={submitted} onChange={(e) => edit("users", e.target.value)} onBlur={() => save("users")} />
          </label>
          <label className="field">Success measure
            <input value={s.draft.success_metric} disabled={submitted} onChange={(e) => edit("success_metric", e.target.value)} onBlur={() => save("success_metric")} />
          </label>
          {(s.draft.similar ?? []).length > 0 && (
            <div className={s.draft.similar.some((m) => m.duplicate) && !s.draft.not_duplicate_reason ? "need" : "note"}>
              <p>{s.draft.similar.some((m) => m.duplicate)
                ? s.draft.not_duplicate_reason
                  ? `Looks like an existing feature; you said why it's different: ${s.draft.not_duplicate_reason}`
                  : "Looks like an existing feature. Add to it, or tell the agent how this one is different before submitting."
                : "Related work the agents will build on:"}</p>
              <SimilarList items={s.draft.similar} />
            </div>
          )}
          {!submitted && s.missing.length > 0 && (
            <p className="need">Still needed: {s.missing.map((m) => FIELD_LABEL[m] ?? m).join(", ")}</p>
          )}
          {err && <p className="error-note" role="alert">{err}</p>}
          {submitted ? (
            <p className="note">Submitted as <Link to={`/features/${s.feature_id}`}>{s.feature_id}</Link>. Agents have started on it; you'll find its first gate in Decisions when it's ready.</p>
          ) : (
            <div className="head-actions">
              {s.draft.product_id && (profiles[s.draft.product_id]?.allowed.length ?? 0) > 1 && (
                <label className="field small">Workflow
                  <select value={profile || profiles[s.draft.product_id].dflt}
                    onChange={(e) => setProfile(e.target.value as Profile)}>
                    {profiles[s.draft.product_id].allowed.map((p) => <option key={p} value={p}>{PROFILE_LABEL[p]}</option>)}
                  </select>
                  <span className="faint">{PROFILE_HELP[(profile || profiles[s.draft.product_id].dflt) as Profile]}</span>
                </label>
              )}
              <label className="small">
                <input id="intake-pilot" type="checkbox" checked={pilot} onChange={(e) => setPilot(e.target.checked)} /> Pilot feature
                <span className="faint"> (measured on the Pilot page)</span>
              </label>
              <button className="btn" disabled={busy || !s.ready} onClick={() => run(api.intakeSubmit(s.id, pilot, profile))}>Submit intent</button>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
