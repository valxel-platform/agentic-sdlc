import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import "./styles.css";
import { api, ApiError, type Me } from "./api";
import { closeLive, useLive, useLiveStatus } from "./live";
import { SignIn } from "./pages/SignIn";
import { Lockup } from "./components/Mark";
import { CommandPalette } from "./components/CommandPalette";
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
import { HealthPage } from "./pages/Health";
import { LessonsPage } from "./pages/Lessons";
import { InceptionPage, ProductsPage } from "./pages/Products";
import { JoinPage, WorkspacePage, WorkspacesPage } from "./pages/Workspace";
import { StartPage } from "./pages/Start";
import { TodayPage } from "./pages/Today";
import { ActivityPage } from "./pages/Activity";
import { ConnectedAppsPage } from "./pages/ConnectedApps";
import { SupportPage } from "./pages/Support";
import { startTelemetry } from "./telemetry";

// Tracing fails open: a missing endpoint or a collector that can't be
// reached never breaks app boot (startTelemetry returns null when off).
// A build-time endpoint (VITE_OTEL_EXPORTER_OTLP_ENDPOINT) starts it now;
// otherwise it starts once the API says it takes spans (see App below).
let tracing = startTelemetry() !== null;

/** Start tracing through the API's own /otlp/v1/traces, once, when the API takes spans. */
export function traceThroughApi(me: Me): void {
  if (tracing || !me.tracing) return;
  tracing = startTelemetry({ endpoint: new URL("/otlp/v1/traces", window.location.origin).href }) !== null;
}

function Shell({ me, onSignOut, onChanged }: { me: Me; onSignOut: () => void; onChanged: () => void }) {
  const live = useLiveStatus();
  const [mine, setMine] = useState(0);
  const [palette, setPalette] = useState(false);
  const [apps, setApps] = useState<{ connected: number; total: number } | null>(null);
  const refresh = useCallback(() => {
    api
      .inbox(true)
      .then((i) => setMine(i.length))
      .catch(() => {});
  }, []);
  useEffect(refresh, [refresh]);
  useLive(refresh, (e) => e.type === "feature.updated");
  useEffect(() => {
    const ws = Object.keys(me.roles ?? {})[0];
    if (ws)
      api
        .connections(ws)
        .then((rows) =>
          setApps({ connected: rows.filter((r) => r.status === "connected").length, total: rows.length }),
        )
        .catch(() => {});
  }, [me.roles]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((p) => !p);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const loc = useLocation();
  const bare = loc.pathname.startsWith("/start");
  const routes = (
    <Routes>
      {/* Someone with no workspace and no product yet starts with the sign-up wizard. */}
      <Route path="/" element={<Navigate to={newcomer(me) ? "/start" : "/today"} replace />} />
      <Route path="/today" element={<TodayPage me={me} />} />
      <Route path="/start" element={<StartPage me={me} onChanged={onChanged} />} />
      <Route path="/start/:ws" element={<StartPage me={me} onChanged={onChanged} />} />
      <Route path="/inbox" element={<Inbox />} />
      <Route path="/board" element={<Board me={me} />} />
      <Route
        path="/features/:id"
        element={<FeaturePage canSync={(pid) => me.lanes.some((l) => l.id === pid)} />}
      />
      <Route path="/roadmap" element={<RoadmapPage me={me} />} />
      <Route path="/products" element={<ProductsPage me={me} />} />
      <Route path="/products/:id" element={<InceptionPage />} />
      <Route path="/initiatives/:id" element={<InitiativePage />} />
      <Route path="/releases/:id" element={<ReleasePage />} />
      <Route path="/reviews/:id" element={<ReviewPage />} />
      <Route path="/incidents" element={<IncidentsPage me={me} />} />
      <Route path="/costs" element={<CostsPage me={me} />} />
      <Route path="/health" element={<HealthPage me={me} />} />
      <Route path="/support" element={<SupportPage />} />
      <Route path="/lessons" element={<LessonsPage me={me} />} />
      <Route path="/pilot" element={<PilotPage me={me} />} />
      <Route path="/changes" element={<ChangesPage me={me} />} />
      <Route path="/inventory" element={<InventoryPage me={me} />} />
      <Route path="/incidents/:id" element={<IncidentPage />} />
      <Route path="/intake" element={<Intake me={me} />} />
      <Route path="/intake/:sid" element={<Intake me={me} />} />
      <Route path="/connected-apps" element={<ConnectedAppsPage me={me} />} />
      <Route path="/activity" element={<ActivityPage me={me} />} />
      <Route path="/more" element={<More me={me} onSignOut={onSignOut} />} />
      <Route path="/workspaces" element={<WorkspacesPage />} />
      <Route path="/workspaces/:id" element={<WorkspacePage me={me} onChanged={onChanged} />} />
      <Route path="/join/:ws/:token" element={<JoinPage onJoined={onChanged} />} />
      <Route path="*" element={<Navigate to="/inbox" replace />} />
    </Routes>
  );

  // The sign-up wizard is a bare, decluttered page: no rail, top bar or tab bar.
  if (bare) {
    return (
      <div className="shell">
        <main>{routes}</main>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="side">
        <Link to="/" className="brand" aria-label="Valxel Agentic SDLC: home">
          <Lockup />
        </Link>
        <nav className="nav" aria-label="Main">
          <div className="nav-group">
            <span>Work</span>
            <NavLink to="/today">Today</NavLink>
            <NavLink to="/board">Features</NavLink>
            <NavLink to="/products">Products</NavLink>
            <NavLink to="/roadmap">Roadmap</NavLink>
            <NavLink to="/intake">New request</NavLink>
          </div>
          <div className="nav-group">
            <span>Decisions</span>
            <NavLink to="/inbox">
              Approvals{" "}
              {mine > 0 && (
                <span className="count" aria-label={`${mine} waiting on you`}>
                  {mine}
                </span>
              )}
            </NavLink>
            <NavLink to="/changes">Changes</NavLink>
          </div>
          <div className="nav-group">
            <span>Operate</span>
            <NavLink to="/incidents">Incidents</NavLink>
            <NavLink to="/pilot">Pilot</NavLink>
            <NavLink to="/costs">Costs</NavLink>
            <NavLink to="/health">Agent health</NavLink>
            <NavLink to="/support">Support</NavLink>
            <NavLink to="/inventory">Inventory</NavLink>
            <NavLink to="/lessons">Lessons</NavLink>
          </div>
          <div className="nav-group">
            <span>Workspace</span>
            <NavLink to="/connected-apps">Connected apps</NavLink>
            <NavLink to="/activity">Activity</NavLink>
            <NavLink to="/workspaces">Workspace admin</NavLink>
            <NavLink to="/start">Start page</NavLink>
          </div>
        </nav>
        <div className="side-foot">
          {apps && (
            <Link className="apps-note" to="/connected-apps">
              {apps.connected} of {apps.total} apps connected
            </Link>
          )}
          <div className={`live ${live ? "on" : ""}`} title={live ? "Live updates on" : "Reconnecting…"}>
            <i aria-hidden="true" />
            <span>{live ? "Live" : "Reconnecting…"}</span>
          </div>
          <Account me={me} onSignOut={onSignOut} />
        </div>
      </aside>
      <div className="shell-main">
        <div className="topbar">
          <button
            type="button"
            className="search-btn"
            onClick={() => setPalette(true)}
            aria-label="Search (Command K)"
          >
            <span>Search projects, approvals, apps…</span>
            <kbd>⌘K</kbd>
          </button>
          <Link className="btn new-btn" to="/intake">
            New project
          </Link>
        </div>
        <main>{routes}</main>
        <nav className="tabbar" aria-label="Main">
          <NavLink to="/today">Today</NavLink>
          <NavLink to="/board">Features</NavLink>
          <NavLink to="/inbox">
            Approvals{" "}
            {mine > 0 && (
              <span className="count" aria-label={`${mine} waiting on you`}>
                {mine}
              </span>
            )}
          </NavLink>
          <NavLink to="/more">More</NavLink>
        </nav>
      </div>
      <CommandPalette open={palette} onClose={() => setPalette(false)} me={me} />
    </div>
  );
}

/** Mobile "More": every section that doesn't fit the bottom tab bar. */
function More({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const links: [string, string][] = [
    ["Start a new product", "/products"],
    ["Products", "/products"],
    ["Roadmap", "/roadmap"],
    ["Changes", "/changes"],
    ["Incidents", "/incidents"],
    ["Pilot", "/pilot"],
    ["Costs", "/costs"],
    ["Agent health", "/health"],
    ["Support", "/support"],
    ["Inventory", "/inventory"],
    ["Lessons", "/lessons"],
    ["Connected apps", "/connected-apps"],
    ["Activity", "/activity"],
    ["Workspace admin", "/workspaces"],
    ["Start page", "/start"],
  ];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>More</h1>
          <p className="muted">Everything else, in one place.</p>
        </div>
      </div>
      <div className="panel">
        <nav className="more-list pad" aria-label="More sections">
          {links.map(([label, to]) => (
            <Link key={label + to} to={to}>
              {label}
            </Link>
          ))}
          <button type="button" onClick={onSignOut}>
            Sign out ({me.name || me.login})
          </button>
        </nav>
      </div>
    </>
  );
}

/** Who's signed in, with their workspaces and sign out, in one menu so the rail keeps room for pages. */
function Account({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const ref = useRef<HTMLDetailsElement>(null);
  const close = () => {
    if (ref.current) ref.current.open = false;
  };
  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("click", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("click", away);
      document.removeEventListener("keydown", esc);
    };
  }, []);
  const many = Object.keys(me.roles ?? {}).length !== 1;
  return (
    <details className="account" ref={ref}>
      <summary aria-label={`Account: ${me.name || me.login}`}>
        <span className="who">
          {me.avatar_url ? (
            <img src={me.avatar_url} alt="" />
          ) : (
            <span className="initial" aria-hidden="true">
              {me.login[0]?.toUpperCase()}
            </span>
          )}
          <span>{me.name || me.login}</span>
        </span>
      </summary>
      <div className="account-menu">
        <div className="who-line">
          Signed in as <strong>{me.login}</strong>
        </div>
        <Link to="/workspaces" onClick={close}>
          {many ? "Workspaces" : "Workspace"}
        </Link>
        <Link to="/connected-apps" onClick={close}>
          Connected apps
        </Link>
        <Link to="/activity" onClick={close}>
          Activity
        </Link>
        <Link to="/start" onClick={close}>
          Set up a new workspace
        </Link>
        <button
          type="button"
          onClick={() => {
            close();
            onSignOut();
          }}
        >
          Sign out
        </button>
        <button
          type="button"
          onClick={() => {
            close();
            api
              .logoutEverywhere()
              .catch(() => {})
              .finally(onSignOut);
          }}
        >
          Sign out on every device
        </button>
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
    api
      .me()
      .then((m) => {
        traceThroughApi(m);
        setMe(m);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setMe(null);
        else setMe(null);
      });
  }, []);
  useEffect(load, [load]);
  if (me === undefined) return null;
  if (me === null) return <SignIn onSignedIn={load} />;
  return (
    <Shell
      me={me}
      onChanged={load}
      onSignOut={() => {
        api
          .logout()
          .catch(() => {})
          .finally(() => {
            closeLive();
            setMe(null);
          });
      }}
    />
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
