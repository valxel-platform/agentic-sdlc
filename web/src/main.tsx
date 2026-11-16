import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes } from "react-router-dom";
import "./styles.css";
import { api, ApiError, type Me } from "./api";
import { closeLive, useLive, useLiveStatus } from "./live";
import { SignIn } from "./pages/SignIn";
import { Lockup } from "./components/Mark";
import { Inbox } from "./pages/Inbox";
import { Board } from "./pages/Board";
import { FeaturePage } from "./pages/Feature";
import { Intake } from "./pages/Intake";
import { RoadmapPage } from "./pages/Roadmap";
import { InitiativePage, ReleasePage, ReviewPage } from "./pages/Thread";
import { IncidentPage, IncidentsPage } from "./pages/Incidents";
import { CostsPage } from "./pages/Costs";
import { ChangesPage } from "./pages/Changes";
import { InventoryPage } from "./pages/Inventory";
import { PilotPage } from "./pages/Pilot";
import { LessonsPage } from "./pages/Lessons";
import { InceptionPage, ProductsPage } from "./pages/Products";
import { JoinPage, WorkspacePage, WorkspacesPage } from "./pages/Workspace";
import { StartPage } from "./pages/Start";

function Shell({ me, onSignOut, onChanged }: { me: Me; onSignOut: () => void; onChanged: () => void }) {
  const live = useLiveStatus();
  const [mine, setMine] = useState(0);
  const refresh = useCallback(() => { api.inbox(true).then((i) => setMine(i.length)).catch(() => {}); }, []);
  useEffect(refresh, [refresh]);
  useLive(refresh, (e) => e.type === "feature.updated");

  return (
    <div className="shell">
      <header className="side">
        <Link to="/" className="brand" aria-label="Valxel Agentic SDLC: home"><Lockup /></Link>
        <nav className="nav" aria-label="Main">
          <NavLink to="/inbox">Decisions {mine > 0 && <span className="count" aria-label={`${mine} waiting on you`}>{mine}</span>}</NavLink>
          <NavLink to="/roadmap">Roadmap</NavLink>
          <NavLink to="/products">Products</NavLink>
          <NavLink to="/board">Features</NavLink>
          <NavLink to="/incidents">Incidents</NavLink>
          <NavLink to="/changes">Changes</NavLink>
          <NavLink to="/inventory">Inventory</NavLink>
          <NavLink to="/costs">Costs</NavLink>
          <NavLink to="/lessons">Lessons</NavLink>
          <NavLink to="/pilot">Pilot</NavLink>
          <NavLink to="/intake">New request</NavLink>
        </nav>
        <div className="side-foot">
          <div className={`live ${live ? "on" : ""}`} title={live ? "Live updates on" : "Reconnecting…"}>
            <i aria-hidden="true" /><span>{live ? "Live" : "Reconnecting…"}</span>
          </div>
          <Account me={me} onSignOut={onSignOut} />
        </div>
      </header>
      <main>
        <Routes>
          {/* Someone with no workspace and no product yet starts with the sign-up wizard. */}
          <Route path="/" element={<Navigate to={newcomer(me) ? "/start" : "/inbox"} replace />} />
          <Route path="/start" element={<StartPage me={me} onChanged={onChanged} />} />
          <Route path="/start/:ws" element={<StartPage me={me} onChanged={onChanged} />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/board" element={<Board me={me} />} />
          <Route path="/features/:id" element={<FeaturePage canSync={(pid) => me.lanes.some((l) => l.id === pid)} />} />
          <Route path="/roadmap" element={<RoadmapPage me={me} />} />
          <Route path="/products" element={<ProductsPage me={me} />} />
          <Route path="/products/:id" element={<InceptionPage />} />
          <Route path="/initiatives/:id" element={<InitiativePage />} />
          <Route path="/releases/:id" element={<ReleasePage />} />
          <Route path="/reviews/:id" element={<ReviewPage />} />
          <Route path="/incidents" element={<IncidentsPage me={me} />} />
          <Route path="/costs" element={<CostsPage me={me} />} />
          <Route path="/lessons" element={<LessonsPage me={me} />} />
          <Route path="/pilot" element={<PilotPage me={me} />} />
          <Route path="/changes" element={<ChangesPage me={me} />} />
          <Route path="/inventory" element={<InventoryPage me={me} />} />
          <Route path="/incidents/:id" element={<IncidentPage />} />
          <Route path="/intake" element={<Intake me={me} />} />
          <Route path="/intake/:sid" element={<Intake me={me} />} />
          <Route path="/workspaces" element={<WorkspacesPage />} />
          <Route path="/workspaces/:id" element={<WorkspacePage me={me} onChanged={onChanged} />} />
          <Route path="/join/:ws/:token" element={<JoinPage onJoined={onChanged} />} />
          <Route path="*" element={<Navigate to="/inbox" replace />} />
        </Routes>
      </main>
    </div>
  );
}

/** Who's signed in, with their workspaces and sign out, in one menu so the bar keeps room for pages. */
function Account({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const ref = useRef<HTMLDetailsElement>(null);
  const close = () => { if (ref.current) ref.current.open = false; };
  useEffect(() => {
    const away = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("click", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("click", away); document.removeEventListener("keydown", esc); };
  }, []);
  const many = Object.keys(me.roles ?? {}).length !== 1;
  return (
    <details className="account" ref={ref}>
      <summary aria-label={`Account: ${me.name || me.login}`}>
        <span className="who">
          {me.avatar_url ? <img src={me.avatar_url} alt="" /> : <span className="initial" aria-hidden="true">{me.login[0]?.toUpperCase()}</span>}
          <span>{me.name || me.login}</span>
        </span>
      </summary>
      <div className="account-menu">
        <div className="who-line">Signed in as <strong>{me.login}</strong></div>
        <Link to="/workspaces" onClick={close}>{many ? "Workspaces" : "Workspace"}</Link>
        <Link to="/start" onClick={close}>Set up a new workspace</Link>
        <button type="button" onClick={() => { close(); onSignOut(); }}>Sign out</button>
      </div>
    </details>
  );
}

function newcomer(me: Me): boolean {
  return Object.keys(me.roles ?? {}).length === 0 && me.lanes.length === 0;
}

function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const load = useCallback(() => {
    api.me().then(setMe).catch((e) => { if (e instanceof ApiError && e.status === 401) setMe(null); else setMe(null); });
  }, []);
  useEffect(load, [load]);
  if (me === undefined) return null;
  if (me === null) return <SignIn onSignedIn={load} />;
  return <Shell me={me} onChanged={load} onSignOut={() => { api.logout().finally(() => { closeLive(); setMe(null); }); }} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter><App /></BrowserRouter>
  </StrictMode>,
);
