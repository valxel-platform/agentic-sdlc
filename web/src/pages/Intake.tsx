import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AGENT_PROPOSES, api, ATTACH_ACCEPT, EXPERIENCE_DELEGATED, PROFILE_HELP, PROFILE_LABEL, type Attachment, type Draft, type ExperienceKey, type IntakeSession, type Me, type Profile, type RequestSize } from "../api";
import { threadPath } from "../vocab";
import { AttachmentList } from "../components/Attachments";
import { SimilarList } from "../components/External";
import { Mark } from "../components/Mark";

/** What G1 needs before the intent can leave the yard: one signal each. */
const NEEDS: { key: keyof Draft; label: string; hint: string }[] = [
  { key: "product_id", label: "Product", hint: "Which product this changes" },
  { key: "problem", label: "Problem", hint: "What goes wrong today, and for whom" },
  { key: "users", label: "Users", hint: "Who uses it, and in what situation" },
  { key: "success_metric", label: "Success measure", hint: "A number or a change you could observe" },
];
/** The experience brief: asked only when the intent changes what people see. */
const EXPERIENCE: { key: ExperienceKey; label: string; hint: string }[] = [
  { key: "where", label: "Screen or flow", hint: "Which screen changes, or a new one" },
  { key: "main_task", label: "Main task", hint: "What a person must do or see first" },
  { key: "context", label: "Where they use it", hint: "On a phone in the field, at a desk…" },
  { key: "look", label: "Look", hint: "Follow the current design, or what to mirror or avoid" },
];
/** What an initiative or a new product needs, in the order the agent asks. */
const SIZED_NEEDS: Record<"initiative" | "product", { key: keyof Draft; label: string }[]> = {
  initiative: [{ key: "product_id", label: "Product" }, { key: "problem", label: "Outcome" },
    { key: "success_metric", label: "Measure" }, { key: "horizon", label: "When" }, { key: "research", label: "Research" }],
  product: [{ key: "name", label: "Name" }, { key: "org", label: "GitHub org" }, { key: "problem", label: "The idea" },
    { key: "users", label: "Users" }, { key: "success_metric", label: "First goal" }, { key: "research", label: "Research" }],
};
const FIELD_LABEL: Record<string, string> = {
  ...Object.fromEntries(NEEDS.map((n) => [n.key, n.label])), experience: "Experience", size: "Size",
  horizon: "When", research: "Research", name: "Name", org: "GitHub org",
};
const SIZE: Record<RequestSize, { label: string; what: string; submit: string }> = {
  feature: { label: "Feature", what: "One change. Goes straight to a brief and G1.", submit: "Submit intent" },
  initiative: { label: "Initiative", what: "Several features toward one goal you can measure. Researched, then drafted for G0.", submit: "Draft the initiative" },
  product: { label: "New product", what: "New users or a new business. Researched, then a brief for the product council.", submit: "Start the product" },
};
const RESEARCH: Record<string, string> = { quick: "Quick web research", thorough: "Thorough research", own: "I've done my own" };

/** Examples people can start from; they only fill the message box. */
const STARTERS: { size: RequestSize; text: string }[] = [
  { size: "feature", text: "Add a CSV export to the visits list" },
  { size: "initiative", text: "Cut no-shows by a quarter this year" },
  { size: "initiative", text: "Make onboarding a new clinic take a day, not a week" },
  { size: "product", text: "A booking app for small vet practices" },
];

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
  const [lit, setLit] = useState<Set<string>>(new Set());  // fields the agent just filled
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    setErr("");
    if (sid) {
      api.intake(sid).then(setS).catch((e) => setErr(e.message));
    } else if (me.lanes.length) {
      api.intakeStart().then((x) => nav(`/intake/${x.id}`, { replace: true })).catch((e) => setErr(e.message));
    }
  }, [sid, me.lanes.length, nav]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [s?.messages.length, busy]);
  useEffect(() => {
    api.products().then((ps) => setProfiles(Object.fromEntries(Object.entries(ps).map(([id, p]) =>
      [id, { allowed: p.profiles ?? ["feature"], dflt: p.default_profile ?? "feature" }])))).catch(() => undefined);
  }, []);
  // The message box grows with what's typed, up to a limit.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  async function run(p: Promise<IntakeSession>, fromAgent = false) {
    setBusy(true);
    setErr("");
    try {
      const next = await p;
      if (fromAgent && s) {
        const changed: string[] = NEEDS.map((n) => n.key).filter((k) => next.draft[k] && next.draft[k] !== s.draft[k]);
        if (next.draft.title !== s.draft.title) changed.push("title");
        EXPERIENCE.forEach((e) => {
          if (next.draft.experience?.[e.key] && next.draft.experience[e.key] !== s.draft.experience?.[e.key]) changed.push(`x:${e.key}`);
        });
        if (next.draft.ui && EXPERIENCE.every((e) => next.draft.experience?.[e.key]) &&
            !EXPERIENCE.every((e) => s.draft.experience?.[e.key])) changed.push("experience");
        if (changed.length) {
          setLit(new Set(changed));
          window.setTimeout(() => setLit(new Set()), 1800);
        }
      }
      setS(next);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  function say(e?: FormEvent) {
    e?.preventDefault();
    if (!s || !text.trim() || busy) return;
    const t = text.trim();
    setText("");
    const optimistic = { ...s, messages: [...s.messages, { role: "user" as const, text: t, at: new Date().toISOString() }] };
    setS(optimistic);
    run(api.intakeSay(s.id, t), true);
  }

  async function attach(files: FileList | File[] | null) {
    if (!s || !files || s.feature_id) return;
    setErr("");
    for (const file of Array.from(files)) {
      setUploading((u) => [...u, file.name]);
      try {
        const next = await api.intakeAttach(s.id, file);
        setS((cur) => (cur ? { ...cur, attachments: next.attachments, messages: next.messages } : next));
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setUploading((u) => u.filter((n) => n !== file.name));
      }
    }
    if (picker.current) picker.current.value = "";
  }

  function detach(a: Attachment) {
    if (!s) return;
    run(api.intakeDetach(s.id, a.id));
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); say(); }
  }

  function resize(size: RequestSize) {
    if (!s || s.draft.size === size) return;
    run(api.intakeDraft(s.id, { size }));
  }

  function edit(field: keyof Draft, value: string) {
    if (!s) return;
    setS({ ...s, draft: { ...s.draft, [field]: value } });
  }

  function save(field: keyof Draft) {
    if (!s) return;
    run(api.intakeDraft(s.id, { [field]: s.draft[field] }));
  }

  function editExperience(key: ExperienceKey, value: string) {
    if (!s) return;
    setS({ ...s, draft: { ...s.draft, experience: { ...s.draft.experience, [key]: value } } });
  }

  function saveExperience(key: ExperienceKey) {
    if (!s) return;
    run(api.intakeDraft(s.id, { experience: { [key]: s.draft.experience?.[key] ?? "" } }));
  }

  function quick(text: string) {
    if (!s || busy) return;
    setS({ ...s, messages: [...s.messages, { role: "user", text, at: new Date().toISOString() }] });
    run(api.intakeSay(s.id, text), true);
  }

  if (!me.lanes.length) {
    return <div className="panel empty"><h2>You can't submit intents yet</h2><p>Ask an owner to add you to a product's GitHub organization.</p></div>;
  }
  if (!s) return err ? <p className="error-note" role="alert">{err}</p> : <p className="faint">Starting…</p>;

  const submitted = Boolean(s.feature_id);
  const names = Object.fromEntries(me.lanes.map((l) => [l.id, l.name]));
  const size = s.draft.size;
  const sized = size === "initiative" || size === "product";
  const needs: { key: string; label: string }[] = sized ? SIZED_NEEDS[size]
    : s.draft.ui ? [...NEEDS, { key: "experience", label: "Experience" }] : NEEDS;
  const have = needs.filter((n) => !s.missing.includes(n.key)).length;
  const last = s.messages[s.messages.length - 1];
  const options = !submitted && !busy && last?.role === "agent" ? last.options ?? [] : [];
  const fresh = s.messages.filter((m) => m.role === "user").length === 0;
  const dup = (s.draft.similar ?? []).some((m) => m.duplicate) && !s.draft.not_duplicate_reason;
  const chosen = s.draft.product_id ? profiles[s.draft.product_id] : undefined;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>What do you want to do?</h1>
          <p className="muted">Describe it in your own words. You don't need to know whether it's a feature, an initiative or
            a new product: the agent suggests one, and you can change it.</p>
        </div>
        <button className="btn secondary" onClick={() => api.intakeStart().then((x) => nav(`/intake/${x.id}`))}>Start over</button>
      </div>

      <div className="intake">
        <section className={`chat${dragging ? " dragging" : ""}`} aria-label="Conversation with the intake agent"
          onDragOver={(e) => { if (!submitted && e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragging(true); } }}
          onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false); }}
          onDrop={(e) => { e.preventDefault(); setDragging(false); attach(e.dataTransfer.files); }}>
          {dragging && <div className="drop-hint" aria-hidden="true">Drop files to attach them to the intent</div>}
          <ol className="messages" aria-live="polite">
            {s.messages.map((m, i) => {
              const first = i === 0 || s.messages[i - 1].role !== m.role;
              return (
                <li key={i} className={`turn ${m.role}${first ? " first" : ""}`}>
                  {first && (
                    <div className="who-said">
                      {m.role === "agent" ? <><Mark size={18} /> Intake agent</> : "You"}
                    </div>
                  )}
                  {m.attachment ? (() => {
                    const a = (s.attachments ?? []).find((x) => x.id === m.attachment);
                    return (
                      <div className="said attached-note">
                        <span className={`file-kind ${a?.kind ?? ""}`} aria-hidden="true">{a?.kind === "image" ? "IMG" : a?.kind === "pdf" ? "PDF" : "TXT"}</span>
                        {m.text}
                      </div>
                    );
                  })() : <div className="said">{m.text}</div>}
                </li>
              );
            })}
            {options.length > 0 && (
              <li className="turn quick" aria-label="Quick replies">
                {options.map((o) => <button key={o} type="button" className="starter" onClick={() => quick(o)}>{o}</button>)}
              </li>
            )}
            {busy && (
              <li className="turn agent first thinking">
                <div className="who-said"><Mark size={18} /> Intake agent</div>
                <div className="said"><span className="lamps" aria-hidden="true"><i /><i /><i /></span>
                  <span className="visually-hidden">The agent is replying</span></div>
              </li>
            )}
            {submitted && (
              <li className="turn system">
                <div className="said">
                  Started <Link to={threadPath(s.feature_id)}>{s.feature_id}</Link>.{" "}
                  {s.kind === "initiative" ? "An agent is researching it, then drafting it for G0." :
                    s.kind === "inception" ? "An agent is researching it, then agents write the brief for the product council." :
                      "Agents have started discovery."} Its first decision will appear in <Link to="/inbox">Decisions</Link>.
                </div>
              </li>
            )}
            <div ref={end} />
          </ol>

          {fresh && !submitted && (
            <div className="starters">
              <span className="small faint">Not sure how to start? Pick an example and make it yours.</span>
              <div>
                {STARTERS.map((t) => (
                  <button key={t.text} type="button" className="starter" onClick={() => { setText(t.text); box.current?.focus(); }}>
                    <span className="starter-size">{SIZE[t.size].label}</span> {t.text}
                  </button>
                ))}
              </div>
            </div>
          )}

          <form className="composer" onSubmit={say}>
            <label className="visually-hidden" htmlFor="say">Message to the intake agent</label>
            <div className="composer-box">
              <input ref={picker} type="file" multiple accept={ATTACH_ACCEPT} hidden onChange={(e) => attach(e.target.files)} />
              <button type="button" className="clip" onClick={() => picker.current?.click()} disabled={submitted}
                aria-label="Attach files" title="Attach screenshots, PDFs or text files">
                <svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true"><path d="M13.5 6.5l-6 6a2 2 0 0 0 2.8 2.8l6.4-6.4a3.5 3.5 0 0 0-5-5L5.3 10.3a5 5 0 0 0 7 7l5.2-5.2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
              </button>
              <textarea id="say" ref={box} rows={1} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
                placeholder={submitted ? "This request has been submitted." : fresh ? "Describe what you want, in your own words" : "Answer, or say what to change"}
                disabled={submitted} autoFocus />
              <button className="send" type="submit" disabled={busy || submitted || !text.trim()} aria-label="Send">
                <svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h12M10 4l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </button>
            </div>
            {uploading.length > 0 && <p className="composer-hint small" role="status">Attaching {uploading.join(", ")}…</p>}
            {!submitted && !uploading.length && (
              <p className="composer-hint small faint">Enter sends; Shift+Enter starts a new line. Attach or drop screenshots, PDFs and text files (up to 5).</p>
            )}
          </form>
          <p className="mobile-ready">
            <span className={s.ready || submitted ? "faint" : "need"}>
              {submitted ? "Submitted" : s.ready ? "Ready to submit" : `${have} of ${needs.length} in place`}
            </span>
            <a href="#h-intent">Review the intent</a>
          </p>
        </section>

        <section className="panel intent" aria-labelledby="h-intent">
          <header className="intent-head">
            <h2 id="h-intent">{size === "initiative" ? "The initiative" : size === "product" ? "The new product" : "The intent"}</h2>
            <p className={`readiness ${submitted ? "done" : s.ready ? "ready" : "waiting"}`}>
              {submitted ? "Submitted" : s.ready ? "Ready to submit" : `${have} of ${needs.length} in place`}
            </p>
            <ol className={`needs n${needs.length}`} aria-label="What G1 needs">
              {needs.map((n) => {
                const ok = !s.missing.includes(n.key);
                return <li key={n.key} className={`${ok ? "ok" : ""}${lit.has(n.key) ? " lit" : ""}`}>
                  <i aria-hidden="true" />{n.label}<span className="visually-hidden">{ok ? ": in place" : ": still needed"}</span>
                </li>;
              })}
            </ol>
          </header>

          <div className="intent-body">
            {(s.draft.size || s.draft.size_suggested) && (
              <fieldset className="size-pick" disabled={submitted}>
                <legend>Size{s.draft.size_suggested && !s.draft.size ? ": the agent suggests " + SIZE[s.draft.size_suggested].label.toLowerCase() : ""}</legend>
                <div className="segmented" role="radiogroup">
                  {(Object.keys(SIZE) as RequestSize[]).map((k) => (
                    <button key={k} type="button" role="radio" aria-checked={size === k}
                      className={`${size === k ? "on" : ""}${!size && s.draft.size_suggested === k ? " suggested" : ""}`}
                      onClick={() => resize(k)}>{SIZE[k].label}</button>
                  ))}
                </div>
                <p className="small faint">{size ? SIZE[size].what : s.draft.size_why}</p>
                {size === "product" && !s.starter && (
                  <p className="small error-note">Only the inception starters can start a new product. Plan it as an initiative of a product you work on instead.</p>
                )}
              </fieldset>
            )}
            {sized ? (
              <>
                {size === "product" ? (
                  <div className="split">
                    <label className={`ifield${s.missing.includes("name") ? " missing" : ""}`}><span>Name</span>
                      <input value={s.draft.name} disabled={submitted} placeholder="A working name is fine"
                        onChange={(e) => edit("name", e.target.value)} onBlur={() => save("name")} /></label>
                    <label className={`ifield${s.missing.includes("org") ? " missing" : ""}`}><span>GitHub org</span>
                      <input value={s.draft.org} disabled={submitted} placeholder="It must already exist"
                        onChange={(e) => edit("org", e.target.value)} onBlur={() => save("org")} /></label>
                  </div>
                ) : (
                  <>
                    <label className={`ifield title${lit.has("title") ? " lit" : ""}`}><span>Title</span>
                      <input value={s.draft.title} disabled={submitted} onChange={(e) => edit("title", e.target.value)} onBlur={() => save("title")} /></label>
                    <label className={`ifield${s.missing.includes("product_id") ? " missing" : ""}`}><span>Product</span>
                      <select value={s.draft.product_id} disabled={submitted}
                        onChange={(e) => { edit("product_id", e.target.value); run(api.intakeDraft(s.id, { product_id: e.target.value })); }}>
                        <option value="">Not chosen yet</option>
                        {s.lanes.map((l) => <option key={l} value={l}>{names[l] ?? l}</option>)}
                      </select></label>
                  </>
                )}
                <label className={`ifield${s.missing.includes("problem") ? " missing" : ""}`}>
                  <span>{size === "product" ? "The idea" : "Outcome"}</span>
                  <textarea rows={3} value={s.draft.problem} disabled={submitted} onChange={(e) => edit("problem", e.target.value)} onBlur={() => save("problem")} /></label>
                <label className={`ifield${s.missing.includes("users") ? " missing" : ""}`}>
                  <span>Users{size === "initiative" ? " (optional)" : ""}</span>
                  <input value={s.draft.users} disabled={submitted} placeholder="Who, and in what situation"
                    onChange={(e) => edit("users", e.target.value)} onBlur={() => save("users")} /></label>
                <label className={`ifield${lit.has("success_metric") ? " lit" : ""}${s.missing.includes("success_metric") ? " missing" : ""}${s.draft.success_metric === AGENT_PROPOSES ? " delegated" : ""}`}>
                  <span>{size === "product" ? "First goal" : "Measure"}</span>
                  <input value={s.draft.success_metric} disabled={submitted} placeholder="A number, where it should get to, by when"
                    onChange={(e) => edit("success_metric", e.target.value)} onBlur={() => save("success_metric")} /></label>
                <div className="split">
                  {size === "initiative" ? (
                    <label className={`ifield${s.missing.includes("horizon") ? " missing" : ""}`}><span>When</span>
                      <select value={s.draft.horizon} disabled={submitted} onChange={(e) => run(api.intakeDraft(s.id, { horizon: e.target.value }))}>
                        <option value="">Not chosen yet</option>
                        <option value="now">Now</option><option value="next">Next</option><option value="later">Later</option>
                        <option value={AGENT_PROPOSES}>Let the agent suggest</option>
                      </select></label>
                  ) : (
                    <label className="ifield"><span>First-year budget (USD, optional)</span>
                      <input type="number" min={0} step={100} value={s.draft.budget_usd || ""} disabled={submitted}
                        onChange={(e) => setS({ ...s, draft: { ...s.draft, budget_usd: Number(e.target.value) || 0 } })}
                        onBlur={() => run(api.intakeDraft(s.id, { budget_usd: s.draft.budget_usd }))} /></label>
                  )}
                  <label className={`ifield${s.missing.includes("research") ? " missing" : ""}`}><span>Research first</span>
                    <select value={s.draft.research} disabled={submitted}
                      onChange={(e) => run(api.intakeDraft(s.id, { research: e.target.value as Draft["research"] }))}>
                      <option value="">Not chosen yet</option>
                      {Object.entries(RESEARCH).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                    </select></label>
                </div>
                {!submitted && s.missing.some((m) => ["success_metric", "horizon", "research", "users"].includes(m)) && (
                  <button type="button" className="linkish leave" onClick={() => quick("Leave the rest to the agent")}>Leave the rest to the agent</button>
                )}
              </>
            ) : (<>
            <label className={`ifield title${lit.has("title") ? " lit" : ""}`}>
              <span>Title</span>
              <input value={s.draft.title} disabled={submitted} placeholder="The agent suggests one from the problem"
                onChange={(e) => edit("title", e.target.value)} onBlur={() => save("title")} />
            </label>
            <label className={`ifield${lit.has("product_id") ? " lit" : ""}${s.missing.includes("product_id") ? " missing" : ""}`}>
              <span>Product</span>
              <select value={s.draft.product_id} disabled={submitted}
                onChange={(e) => { edit("product_id", e.target.value); run(api.intakeDraft(s.id, { product_id: e.target.value })); }}>
                <option value="">Not chosen yet</option>
                {s.lanes.map((l) => <option key={l} value={l}>{names[l] ?? l}</option>)}
              </select>
            </label>
            {NEEDS.filter((n) => n.key !== "product_id").map((n) => {
              const long = n.key === "problem";
              const props = {
                value: String(s.draft[n.key] ?? ""), disabled: submitted, placeholder: n.hint,
                onChange: (e: { target: { value: string } }) => edit(n.key, e.target.value), onBlur: () => save(n.key),
              };
              return (
                <label key={n.key} className={`ifield${lit.has(n.key) ? " lit" : ""}${s.missing.includes(n.key) ? " missing" : ""}`}>
                  <span>{n.label}</span>
                  {long ? <textarea rows={3} {...props} /> : <input {...props} />}
                </label>
              );
            })}

            {s.draft.product_id && (
              <div className={`experience${s.draft.ui ? "" : " off"}`}>
                <div className="experience-head">
                  <span>Experience</span>
                  <label className="check small">
                    <input type="checkbox" checked={s.draft.ui} disabled={submitted}
                      onChange={(e) => run(api.intakeDraft(s.id, { ui: e.target.checked }))} />
                    Changes what people see
                  </label>
                </div>
                {s.draft.ui && EXPERIENCE.map((e) => {
                  const v = s.draft.experience?.[e.key] ?? "";
                  const delegated = v === EXPERIENCE_DELEGATED;
                  return (
                    <label key={e.key} className={`ifield${lit.has(`x:${e.key}`) ? " lit" : ""}${!v ? " missing" : ""}${delegated ? " delegated" : ""}`}>
                      <span>{e.label}</span>
                      <input value={v} disabled={submitted} placeholder={e.hint}
                        onChange={(ev) => editExperience(e.key, ev.target.value)} onBlur={() => saveExperience(e.key)} />
                    </label>
                  );
                })}
                {s.draft.ui && !submitted && !EXPERIENCE.every((e) => s.draft.experience?.[e.key]) && (
                  <button type="button" className="linkish leave" onClick={() => run(api.intakeDraft(s.id, {
                    experience: Object.fromEntries(EXPERIENCE.filter((e) => !s.draft.experience?.[e.key]).map((e) => [e.key, EXPERIENCE_DELEGATED])),
                  }), true)}>Leave the rest to the design agent</button>
                )}
              </div>
            )}

            </>)}

            {(s.attachments ?? []).length > 0 && (
              <div className="ifield attached">
                <span>Attachments</span>
                <AttachmentList items={s.attachments}
                  href={(a) => `/api/intake/${encodeURIComponent(s.id)}/attachments/${a.id}`}
                  onRemove={submitted ? undefined : detach} />
                <p className="small faint">Kept private until you submit. Then they're stored with the feature, and only the
                  agents its product allows ever read them.</p>
              </div>
            )}

            {(s.draft.similar ?? []).length > 0 && (
              <div className={`related ${dup ? "dup" : ""}`}>
                <p>{(s.draft.similar ?? []).some((m) => m.duplicate)
                  ? s.draft.not_duplicate_reason
                    ? `Looks like existing work. You said why it's different: ${s.draft.not_duplicate_reason}`
                    : "Looks like existing work. Add to it instead, or tell the agent how this one is different."
                  : "Related work the agents will build on"}</p>
                <SimilarList items={s.draft.similar} />
              </div>
            )}
          </div>

          {!submitted && (
            <footer className="intent-foot">
              {!sized && chosen && chosen.allowed.length > 1 && (
                <label className="field">Workflow
                  <select value={profile || chosen.dflt} onChange={(e) => setProfile(e.target.value as Profile)}>
                    {chosen.allowed.map((p) => <option key={p} value={p}>{PROFILE_LABEL[p]}</option>)}
                  </select>
                  <span className="faint">{PROFILE_HELP[(profile || chosen.dflt) as Profile]}</span>
                </label>
              )}
              {!sized && <label className="check small">
                <input type="checkbox" checked={pilot} onChange={(e) => setPilot(e.target.checked)} />
                <span>Pilot feature <span className="faint">(its cost and people's time are measured on the Pilot page)</span></span>
              </label>}
              {err && <p className="error-note" role="alert">{err}</p>}
              <div className="submit-row">
                <button className="btn" disabled={busy || !s.ready} onClick={() => run(api.intakeSubmit(s.id, pilot, profile))}>{SIZE[size || "feature"].submit}</button>
                {!s.ready && (
                  <span className="small faint">
                    {s.missing.length ? `Still needed: ${s.missing.map((m) => FIELD_LABEL[m] ?? m).join(", ")}` : dup ? "Say how this differs from the existing work" : ""}
                  </span>
                )}
              </div>
            </footer>
          )}
        </section>
      </div>
    </>
  );
}
