"""The release rehearsal: a release against a copy of the tool's database, before it's deployed.

One pod, its steps in order (platform-foundation gitops/apps/release-rehearsal runs it):

  serve           (sidecar, new image) copies the database through the cloud's DatabaseCopies, sets
                  the copy's password, and hands the URL out on the pod's loopback only; when the pod
                  ends, deletes the copy and writes the report through ReportStore
  install         (new image) puts this file in the work directory, for the old image to run
  sql CURRENT|NEW the SQL each image runs, to report what changed
  copy            waits for the copy, and records it
  smoke current   the running release against the copy: the API starts (its schema steps), reads
                  every product and feature, then the worker runs the copy's queue
  smoke candidate the new release: its schema changes run here, on real data; its worker writes
  smoke current-again  the running release again, against what the new one left: the rollback
  done            exits 0 if every step passed

The old image predates whatever this file adds, so the steps it runs (sql, smoke) use only the
standard library and the tool's own API, imported when they run. Only `serve` reaches a cloud, in
the new image. Every image runs offline (VALXEL_MODE=stub, VALXEL_DELIVERY=stub, no keys); the
pod's network limits it to the copy and the cloud's APIs. No step writes a password down.

REHEARSAL_DATABASE_URL instead of a copy runs the steps against any Postgres (no cloud), and with
no REHEARSAL_BUCKET the report is written to the work directory: how to try it on a laptop.
"""

from __future__ import annotations

import ast
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

WORK = Path(os.environ.get("REHEARSAL_WORK", "/work"))
FILES = Path(os.environ.get("REHEARSAL_FILES", "/rehearsal"))
COPY = os.environ.get("REHEARSAL_COPY", "valxel-rehearsal-sdlc")
PORT = int(os.environ.get("REHEARSAL_PORT", "8711"))
STEPS = ["sql-current", "sql-candidate", "copy", "smoke-current", "smoke-candidate", "smoke-current-again"]
STATEMENT = re.compile(r"^(CREATE|ALTER|DROP|TRUNCATE)\s+(OR\s+REPLACE\s+)?(UNIQUE\s+)?"
                       r"(TABLE|INDEX|TRIGGER|FUNCTION|VIEW|TYPE|SCHEMA|EXTENSION|SEQUENCE)\b", re.I)


# ---------------------------------------------------------------- what is rehearsed


def images(text: str) -> dict[str, str]:
    """The `  key: value` lines under `data:` of a releases file."""
    values, in_data = {}, False
    for line in text.splitlines():
        if line.startswith("data:"):
            in_data = True
        elif in_data and re.match(r"^  [a-z_]+:", line):
            k, v = line.strip().split(":", 1)
            values[k] = v.strip()
        elif in_data and line and not line.startswith(" "):
            in_data = False
    return values


def key(current: str, candidate: str) -> str:
    """The report's name: the images running and the images proposed, whatever the files' comments."""
    def canon(text: str) -> str:
        return "\n".join(f"{k}={v}" for k, v in sorted(images(text).items()))

    return hashlib.sha256(f"{canon(current)}\n--\n{canon(candidate)}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------- steps


def record(step: str, ok: bool, started: float, **detail) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    row = {"step": step, "ok": ok, "seconds": round(time.time() - started, 1), **detail}
    with open(WORK / "steps.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def statements(package: Path) -> list[str]:
    """Every DDL statement in an installed valxel_sdlc, one per line (ADR 0012's scan)."""
    out = set()
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), str(path))
        except SyntaxError:
            continue
        parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        for node in ast.walk(tree):
            if id(node) in parts:  # a piece of an f-string, read with it
                continue
            if isinstance(node, ast.JoinedStr):
                text = "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            else:
                continue
            for stmt in text.split(";"):
                flat = " ".join(stmt.split())
                if STATEMENT.match(flat) and len(flat) < 2000:
                    out.add(flat)
    return sorted(out)


def sql(label: str) -> int:
    started = time.time()
    import valxel_sdlc

    found = statements(Path(valxel_sdlc.__file__).parent)
    (WORK / f"sql-{label}.txt").write_text("\n".join(found) + "\n")
    record(f"sql-{label}", True, started, statements=len(found))
    return 0


def install() -> int:
    """This file into the work directory, where the old image runs it."""
    WORK.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(__file__, WORK / "rehearsal.py")
    return 0


def _sidecar(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def copy() -> int:
    """Wait for the sidecar's copy, then record it (it has no password to give away here)."""
    started = time.time()
    while time.time() - started < float(os.environ.get("REHEARSAL_COPY_WAIT_S", "7200")):
        try:
            _, state = _sidecar("/status")
        except OSError:
            time.sleep(5)
            continue
        if state.get("state") == "ready":
            record("copy", True, started, **{k: v for k, v in state.items() if k != "state"})
            return 0
        if state.get("state") == "failed":
            record("copy", False, started, error=state.get("error", ""))
            return 1
        time.sleep(20)
    record("copy", False, started, error="no copy within the wait")
    return 1


def _expect(ok: object, problem: str) -> None:
    """A check that fails the step (an assert would vanish under python -O)."""
    if not ok:
        raise AssertionError(problem)


def _env() -> dict[str, str]:
    code, got = _sidecar("/database")
    if code != 200:
        raise RuntimeError(f"no database from the sidecar ({code}): {got.get('error') or got.get('state')}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VALXEL_")}
    env.update({
        "DATABASE_URL": got["url"],
        # Offline: stand-ins for every integration, no keys, dev sign-in (ADR 0004).
        "VALXEL_MODE": "stub", "VALXEL_DELIVERY": "stub", "VALXEL_STRICT": "0", "VALXEL_AUTH": "dev",
        "VALXEL_RUN_QUEUE": "postgres", "VALXEL_GOVERNANCE_SOURCE": "local",
        "VALXEL_TRACE_ENVIRONMENT": "test", "VALXEL_ARTIFACT_DIR": str(WORK / "artifacts"),
        "VALXEL_WATCH_INTERVAL_S": "3600", "VALXEL_WORKER_CONCURRENCY": "2",
    })
    return env


def _api_checks(env: dict[str, str]) -> dict:
    """The API checks in a process of their own, with exactly this environment."""
    out = subprocess.run([sys.executable, __file__, "api-checks"], env=env, capture_output=True, text=True,  # noqa: S603
                         timeout=900, check=False)
    if out.returncode != 0:
        errors = [ln for ln in out.stderr.splitlines() if re.match(r"^[\w.]+(Error|Exception|Timeout|Exit)\b", ln)]
        raise AssertionError((errors or out.stderr.strip().splitlines() or ["no output"])[-1][:400])
    return dict(json.loads(out.stdout.strip().splitlines()[-1]))


def api_checks() -> dict:
    """Start the API (its schema steps run as it starts) and read what's stored. A failure names the
    path and status, never a response's body: the report holds no data from the copy."""
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app

    t0 = time.time()
    with TestClient(create_app()) as c:
        started_s = round(time.time() - t0, 1)
        _expect(c.get("/api/healthz").status_code == 200, "healthz")
        teams = [t for t in os.environ.get("REHEARSAL_TEAMS", "valxel-platform/platform").split(",") if t]
        r = c.post("/api/auth/dev-login", json={"login": "release-rehearsal", "teams": teams})
        _expect(r.status_code == 200, f"dev sign-in: {r.status_code}")
        # Then as a member of every product's organization, so it reads every feature.
        r = c.get("/api/products")
        _expect(r.status_code == 200, f"/api/products: {r.status_code}")
        products = r.json() if isinstance(r.json(), dict) else {}
        orgs = sorted({str(p.get("governance_repo", "")).split("/")[0] for p in products.values()
                       if isinstance(p, dict)} - {""})
        r = c.post("/api/auth/dev-login", json={"login": "release-rehearsal", "teams": teams, "orgs": orgs})
        _expect(r.status_code == 200, f"dev sign-in: {r.status_code}")
        r = c.get("/api/features")
        _expect(r.status_code == 200, f"/api/features: {r.status_code}")
        body = r.json()
        listed = body if isinstance(body, list) else body.get("features", [])
        read = 0
        for f in listed[: int(os.environ.get("REHEARSAL_READ_FEATURES", "40"))]:
            fid = f.get("id") or f.get("feature_id")
            r = c.get(f"/api/features/{quote(str(fid), safe='')}")
            _expect(r.status_code == 200, f"/api/features/{fid}: {r.status_code}")
            read += 1
        for path in ("/api/inbox", "/api/products"):
            r = c.get(path)
            _expect(r.status_code == 200, f"{path}: {r.status_code}")
    return {"api_start_s": started_s, "features": len(listed), "features_read": read}


def _worker_checks(env: dict[str, str], seconds: int) -> dict:
    """Run the worker on the copy's queue for a while: it claims and runs work, offline, and writes."""
    import socket

    with socket.socket() as probe:  # a free port: nothing else in the pod (or a test run) has it
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])
    p = subprocess.Popen(["valxel", "worker", "--port", port], env=env)  # noqa: S603,S607 - the image's own CLI
    try:
        deadline, healthy = time.time() + 180, False
        while time.time() < deadline and not healthy:
            if p.poll() is not None:
                raise AssertionError(f"worker exited with {p.returncode} as it started")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                    healthy = r.status == 200
            except OSError:
                time.sleep(3)
        _expect(healthy, "worker never became healthy")
        time.sleep(seconds)
        _expect(p.poll() is None, f"worker exited with {p.returncode}")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            state = json.load(r)
        _expect(state.get("ok"), "worker unhealthy")
        return {"worker_queue": state.get("queue"), "worker_ran_s": seconds}
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(60)
        except subprocess.TimeoutExpired:
            p.kill()


def smoke(label: str) -> int:
    started = time.time()
    step = f"smoke-{label}"
    try:
        env = _env()
        detail = _api_checks(env)
        detail.update(_worker_checks(env, int(os.environ.get("REHEARSAL_WORKER_S", "120"))))
        import valxel_sdlc

        detail["version"] = getattr(valxel_sdlc, "__version__", "")
        record(step, True, started, **detail)
        return 0
    except Exception as e:  # noqa: BLE001 - the step's result is the record
        record(step, False, started, error=f"{type(e).__name__}: {e}"[:500])
        return 1


# ---------------------------------------------------------------- the report


def report() -> dict:
    current = (FILES / "current.yaml").read_text() if (FILES / "current.yaml").exists() else ""
    candidate = (FILES / "candidate.yaml").read_text() if (FILES / "candidate.yaml").exists() else ""
    steps = []
    if (WORK / "steps.jsonl").exists():
        steps = [json.loads(line) for line in (WORK / "steps.jsonl").read_text().splitlines() if line.strip()]
    done_ = {s["step"]: s["ok"] for s in steps}

    def lines(name: str) -> set[str]:
        path = WORK / f"sql-{name}.txt"
        return set(path.read_text().splitlines()) - {""} if path.exists() else set()

    before, after = lines("current"), lines("candidate")
    return {
        "key": key(current, candidate),
        "passed": all(done_.get(s) for s in STEPS),
        "current": images(current),
        "candidate": images(candidate),
        "schema_changed": before != after,
        "sql_added": sorted(after - before)[:50],
        "sql_removed": sorted(before - after)[:50],
        "steps": steps,
        "missing": [s for s in STEPS if s not in done_],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def done() -> int:
    result = report()
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["passed"] else 1


# ---------------------------------------------------------------- the sidecar


class _Copy:
    """The copy, made in the background; its URL, password included, only ever in memory."""

    def __init__(self) -> None:
        self.state, self.url, self.error, self.made = "copying", "", "", False
        self.detail: dict[str, object] = {}

    def make(self) -> None:
        started = time.time()
        try:
            if os.environ.get("REHEARSAL_DATABASE_URL"):
                self.url, self.state = os.environ["REHEARSAL_DATABASE_URL"], "ready"
                self.detail = {"copy": "REHEARSAL_DATABASE_URL"}
                return
            from valxel_sdlc.cloud import database_copies, provider

            copies, source = database_copies(), os.environ["REHEARSAL_SOURCE"]
            copies.delete(COPY)  # a rehearsal stopped half way leaves one behind
            got = None
            while got is None:  # until a copy that's still being deleted is gone
                try:
                    got = copies.copy(source, COPY)
                except Exception as exc:  # only "it's still there" is retried
                    gone_soon = re.search(r"exist|being deleted|conflict|409", str(exc), re.I)
                    if not gone_soon or time.time() - started > 1800:
                        raise
                    time.sleep(30)
            self.made = True
            restored = time.time()
            password = copies.set_password(COPY, got["user"])
            name = os.environ.get("REHEARSAL_DATABASE", "valxel_sdlc")
            self.url = (f"postgresql://{quote(got['user'], safe='')}:{quote(password, safe='')}@{got['host']}:"
                        f"{got.get('port', 5432)}/{name}?sslmode=require")
            self.detail = {"cloud": provider(), "source": source, "restore_s": round(restored - started),
                           "password_s": round(time.time() - restored)}
            self.state = "ready"
        except Exception as exc:  # noqa: BLE001 - reported to the copy step
            self.error, self.state = f"{type(exc).__name__}: {exc}"[:500], "failed"


def serve(stop: threading.Event | None = None) -> int:
    """Until SIGTERM (the pod ending), or `stop` in a test."""
    copy_ = _Copy()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body: dict[str, object]
            if self.path == "/database" and copy_.state == "ready":
                code, body = 200, {"url": copy_.url}
            elif self.path == "/status":
                code, body = 200, {"state": copy_.state, "error": copy_.error, **copy_.detail}
            else:
                code, body = (500 if copy_.state == "failed" else 503), {"state": copy_.state, "error": copy_.error}
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=copy_.make, daemon=True).start()
    if stop is None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())  # type: ignore[union-attr]
    stop.wait()
    server.shutdown()
    server.server_close()
    result = report()
    if copy_.made:
        try:
            from valxel_sdlc.cloud import database_copies

            database_copies().delete(COPY)
            result["copy_deleted"] = True
        except Exception as exc:  # noqa: BLE001 - the next rehearsal deletes it first; say so
            result["copy_deleted"] = f"failed: {str(exc)[:200]}"
    body = json.dumps(result, indent=2).encode()
    bucket = os.environ.get("REHEARSAL_BUCKET", "")
    if bucket:
        from valxel_sdlc.cloud import report_store

        report_store(bucket).put(f"rehearsals/{result['key']}.json", body)
    else:
        (WORK / "report.json").write_bytes(body)
    print(json.dumps({"published": result["key"], "passed": result["passed"]}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    cmd, *rest = argv or ["done"]
    steps = {"serve": serve, "install": install, "copy": copy, "done": done}
    if cmd in steps:
        return steps[cmd]()
    if cmd == "api-checks":  # smoke's API checks, in a process of their own
        print(json.dumps(api_checks()), flush=True)
        return 0
    if cmd == "sql" and rest:
        return sql(rest[0])
    if cmd == "smoke" and rest:
        return smoke(rest[0])
    print("valxel rehearse serve | install | sql LABEL | copy | smoke LABEL | done", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
