"""Strict mode (VALXEL_STRICT=1): the server refuses to start while anything that would quietly fall
back to an offline stand-in in production still does.

Most switches already fail loudly when misconfigured (VALXEL_MODE=live without a provider,
VALXEL_DELIVERY=live without the GitHub App). Some don't: a Jira or Lucid server without
credentials runs as a stand-in, a product's flags run in memory, a missing database keeps
everything in memory. Strict mode turns each of those into a startup error, so a typo in a
token can't make G6 reconcile the matrix against a fake tracker.

`stand_ins` returns what blocks; `known_gaps` returns what runs but can't be checked, and only warns."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class StrictModeError(RuntimeError):
    pass


def enabled(env=None) -> bool:
    return (env if env is not None else os.environ).get("VALXEL_STRICT", "").lower() in ("1", "true", "yes", "on")


def stand_ins(services, *, auth_mode: str, env=None) -> list[str]:
    """Each setting or product tool that would run as an offline stand-in."""
    from .services import flag_modes, integration_modes

    env = env if env is not None else os.environ
    s = services.settings
    out = []
    if s.mode != "live":
        out.append("VALXEL_MODE is not live: agents return canned output")
    if env.get("VALXEL_DELIVERY", "stub").lower() != "live":
        out.append("VALXEL_DELIVERY is not live: pull requests, builds and deployments stay in memory")
    if auth_mode == "dev":
        out.append("VALXEL_AUTH is dev: anyone can sign in as anyone")
    if s.governance_source != "github":
        out.append("VALXEL_GOVERNANCE_SOURCE is not github: governance is read from local files")
    if not s.database_url:
        out.append("DATABASE_URL is not set: features, the catalog and the audit trail are lost on restart")
    from .tenancy import enforced

    executor = env.get("VALXEL_SESSION_EXECUTOR", "local").lower()
    tier = env.get("VALXEL_TIER", "cluster").lower()
    out += tier_checks(env, tier=tier, live=s.mode == "live")
    from .cloud import strict_problems as cloud_problems

    out += cloud_problems(env)
    if s.mode == "live" and executor not in ("kubernetes", "docker"):
        out.append("VALXEL_SESSION_EXECUTOR is local: agent sessions run in the tool's own container, beside its "
                   "keys and every workspace's work; use kubernetes (cluster) or docker (laptop, VM) "
                   "(valxel_sdlc.session_runner, valxel_sdlc.session_launcher)")
    elif s.mode == "live" and executor == "docker" and tier == "cluster":
        out.append("VALXEL_SESSION_EXECUTOR is docker on the cluster tier: the cluster runs sessions in Agent "
                   "Sandbox pods (kubernetes)")
    if s.mode == "live" and executor in ("kubernetes", "docker") and tier in ("vm", "cluster"):
        from .git_broker import enabled as broker_on

        try:
            on = broker_on(env)
        except RuntimeError as exc:
            out.append(str(exc))
            on = True
        if not on:
            out.append("VALXEL_GIT_BROKER_URL and VALXEL_GIT_BROKER_KEY are not both set: agent sessions hold GitHub "
                       "tokens and can reach all of github.com; send their git through the egress proxy's broker "
                       "(valxel_sdlc.git_broker)")
    if env.get("VALXEL_LLM_GATEWAY_URL") and any(env.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
        out.append("The tool holds a model provider's key while its model calls go through the LLM gateway: "
                   "the key belongs to the gateway alone")
    if s.mode == "live" and not env.get("VALXEL_LLM_GATEWAY_URL"):
        out.append("VALXEL_LLM_GATEWAY_URL is not set: agent sessions hold the tool's own model key rather than a "
                   "short-lived gateway token (valxel_sdlc.gateway_sessions)")

    if not env.get("VALXEL_AUDIT_ANCHOR_BUCKET"):
        out.append("VALXEL_AUDIT_ANCHOR_BUCKET is not set: the audit and change logs' hash chains aren't anchored "
                   "outside the database, so whoever can change it can rewrite them undetected (audit_anchors.py)")
    if str(env.get("VALXEL_AUDIT_SINK", "log")).lower() != "kafka":
        out.append("VALXEL_AUDIT_SINK is not kafka: the tool's own audit records stay in its database, which it "
                   "can write to, instead of reaching the audit sink (valxel_sdlc.audit_events)")
    if s.database_url and not enforced(env):
        out.append("VALXEL_TENANCY is not rls: every workspace's rows are open to any query that forgets to "
                   "filter (valxel_sdlc.tenancy)")
    if s.mode == "live" and s.database_url and str(env.get("VALXEL_RUN_QUEUE", "")).lower() != "postgres":
        out.append("VALXEL_RUN_QUEUE is not postgres: agents run inside the process that serves the API, so a "
                   "deploy or a crash of it stops them and they share its memory and keys; queue the work and run "
                   "`valxel worker` (valxel_sdlc.run_queue)")
    if s.database_url and not env.get("VALXEL_CHECKPOINT_ARCHIVE_BUCKET"):
        regulated = [pid for pid in s.registry if _data_class(services, pid) in ("phi", "payments")]
        if regulated:
            out.append(f"VALXEL_CHECKPOINT_ARCHIVE_BUCKET is not set: {', '.join(regulated)} are regulated, so their "
                       "checkpoint history is never pruned rather than archived (valxel_sdlc.checkpoint_archive)")
    from .ops.signal_tokens import scoped_only

    if not scoped_only(env):
        out.append("VALXEL_SIGNALS_TOKENS is not scoped: the platform's signals token is accepted for every "
                   "product's alerts; give each sender its own (`valxel signals-token`) and set it to scoped")
    out += weak_secrets(env, database_url=s.database_url)
    out += unpinned_models(s, env)
    out += secrets_in_environment(env, live=s.mode == "live")
    out += app_key_in_kms(env) if tier != "vm" else []  # a VM has no KMS: the key is a file secret there
    out += provisioning_split(env)
    secret_env = _secret_env(env)
    modes = integration_modes(services)
    for pid in sorted(s.registry):
        try:
            product = services.snapshot(pid).product
        except Exception as exc:  # governance errors are reported elsewhere; a broken product isn't a stand-in
            out.append(f"{pid}: governance doesn't load ({exc})")
            continue
        tools = product.tools or {}
        for cap in ("diagrams", "work_tracking"):
            provider = (tools.get(cap) or {}).get("provider")
            if provider and modes.get(provider, "stub") == "stub":
                out.append(f"{pid}: tools.{cap} names {provider}, which has no server URL or credentials")
        if product.runtime and tools.get("flags"):
            if flag_modes(services, [pid]).get(pid) != "flipt":
                out.append(f"{pid}: tools.flags names Flipt, but its flags run in memory "
                           f"(set VALXEL_FLIPT_TOKEN_{pid.upper()} and VALXEL_DELIVERY=live)")
        cm = tools.get("change_management") or {}
        if cm.get("provider") == "servicenow" and not secret_env.get(f"VALXEL_SERVICENOW_AUTH_{pid.upper()}"):
            out.append(f"{pid}: tools.change_management names ServiceNow, but VALXEL_SERVICENOW_AUTH_{pid.upper()} is not set")
    return out


TIERS = ("laptop", "vm", "cluster")  # ADR 0007


def tier_checks(env, *, tier: str, live: bool, docker_socket: str = "/var/run/docker.sock") -> list[str]:
    """What each tier requires beyond the common checks (ADR 0007, T-69). The laptop isn't for strict
    mode; the VM needs TLS, real sign-in, sessions under gVisor, no Docker socket in the tool and
    backups; the cluster what it always has."""
    import os as _os

    if tier not in TIERS:
        return [f"VALXEL_TIER is one of {', '.join(TIERS)}, not {tier!r}"]
    if tier == "laptop":
        return [("VALXEL_TIER is laptop: the laptop tier is for one developer's machine (dev sign-in, local builds, "
                 "the Docker socket in the worker) and doesn't run in strict mode; use the VM tier (`valxel vm init`)")]
    out = []
    if tier == "vm":
        if not (env.get("VALXEL_PUBLIC_URL") or "").startswith("https://"):
            out.append("VALXEL_PUBLIC_URL is not https: the VM tier serves the tool over TLS only (Caddy in the VM "
                       "bundle)")
        if live and env.get("VALXEL_SESSION_RUNTIME", "").lower() != "runsc":
            out.append("VALXEL_SESSION_RUNTIME is not runsc: on the VM tier each agent session runs under gVisor; "
                       "install it in Docker (`valxel vm doctor` says how)")
        if _os.path.exists(docker_socket):
            out.append(f"{docker_socket} is in the tool's container: on the VM tier only the session launcher "
                       "reaches Docker")
        if not env.get("VALXEL_BACKUP_MARKER"):
            out.append("VALXEL_BACKUP_MARKER is not set: the VM tier backs up its databases and artifacts every night "
                       "and the tool checks the last one (the VM bundle's backup service)")
    return out


def unpinned_models(settings, env) -> list[str]:
    """In live mode every role's model is pinned, so a provider can't swap it silently (valxel_sdlc.pinning)."""
    from .models import available_roles, optional_roles
    from .pinning import why_unpinned

    if env.get("VALXEL_MODE", "stub").lower() != "live":
        return []
    optional, on = optional_roles(settings), available_roles(settings)
    out = []
    for name, rc in sorted(settings.roles.items()):
        if name in optional and name not in on:  # a fallback that can't run here isn't used
            continue
        why = why_unpinned(rc.model, settings.providers)
        if why:
            out.append(f"role {name}: {why} (a name a provider can repoint is the most common cause of silent drift)")
    return out


def _data_class(services, pid: str) -> str:
    try:
        return services.snapshot(pid).product.data_class
    except Exception:  # noqa: BLE001 - unreadable governance counts as regulated
        return "phi"


def _secret_env(env):
    from .secret_ring import environ

    return environ(env)


def secrets_in_environment(env, *, live: bool = False) -> list[str]:
    """In live mode the secrets come from a store (aws or files), not the environment, which anything
    that can read the process's environment can read; and none is left in the environment as well
    (valxel_sdlc.secret_ring)."""
    from .secret_ring import in_environment, store_kind

    try:
        kind = store_kind(env)
    except ValueError as exc:
        return [str(exc)]
    if kind == "env":
        return ["VALXEL_SECRET_STORE is env: secrets come from environment variables, which anything that can "
                "read the process's environment can read, and which pass to whatever it starts; use aws or "
                "files"] if live else []
    names = in_environment(env)
    return [f"VALXEL_SECRET_STORE is {kind}, but these secrets are also in the environment: {', '.join(names)}; "
            "keep them only in the store"] if names else []


def app_key_in_kms(env) -> list[str]:
    """The delivery GitHub App's private key acts for the tool in every product org: in live delivery it
    is a sign-only KMS key the process can ask to sign, never key material it holds (a file or a secret
    it could read, copy or leak), and none is left beside the KMS key (valxel_sdlc.signing)."""
    from .secret_ring import current
    from .signing import _names

    if env.get("VALXEL_DELIVERY", "stub").lower() != "live":
        return []
    n = _names("delivery")
    held = [n["file"]] if env.get(n["file"]) else []
    try:
        if current(n["pem"], env):
            held.append(n["pem"])
    except ValueError:
        held.append(n["pem"] + " (unreadable)")
    if not env.get(n["kms"]):
        where = f" ({', '.join(held)})" if held else ""
        return [(f"{n['kms']} is not set: the GitHub App's private key is held by this process{where}; "
                 "import it into KMS as a sign-only key and set the key's id (docs: GitHub App key in KMS)")]
    if held:
        return [f"{n['kms']} is set, but the key is still held here too ({', '.join(held)}): remove it"]
    return []


def provisioning_split(env) -> list[str]:
    """Repo administration belongs to the provisioning worker and its own GitHub App (security S4):
    the API process must queue provisioning and must not be able to sign for that App."""
    from .provision_queue import split
    from .signing import provisioner_key_reachable

    out = []
    if env.get("VALXEL_DELIVERY", "stub").lower() == "live" and not split(env):
        out.append("VALXEL_PROVISIONING is not queue: this process creates repos itself, with administration rights "
                   "in every product org; run `valxel provisioner` with the Provisioning App instead")
    reachable = provisioner_key_reachable(env)
    if reachable:
        out.append(f"this process can sign for the Provisioning App ({', '.join(reachable)}): only `valxel "
                   "provisioner` may")
    return out


def weak_secrets(env, *, database_url: str = "") -> list[str]:
    """Secrets set to published examples or too weak to protect anything (valxel_sdlc.secret_ring)."""
    from urllib.parse import urlsplit

    from .secret_ring import values, weak, weak_password

    out = []
    for i, key in enumerate(values("VALXEL_SESSION_SECRET", env)):
        why = weak(key)
        if why:
            which = "VALXEL_SESSION_SECRET" if i == 0 else "VALXEL_SESSION_SECRET's previous value"
            out.append(f"{which} is {why}: anyone who knows it can sign in as anyone and approve any gate")
    for name in ("VALXEL_GITHUB_WEBHOOK_SECRET", "VALXEL_SIGNALS_TOKEN", "VALXEL_SENTRY_CLIENT_SECRET"):
        for key in values(name, env):
            why = weak(key, min_length=20)
            if why:
                out.append(f"{name} is {why}")
    if database_url:
        try:
            password = urlsplit(database_url).password or ""
        except ValueError:
            password = ""
        why = weak_password(password) if password else ""  # none: IAM auth, a .pgpass or a socket
        if why:
            out.append(f"DATABASE_URL's password is {why}")
    return out


def env_value(name: str) -> str:
    return os.environ.get(name, "")


def known_gaps(services) -> list[str]:
    """What runs, but can't be checked: strict mode warns about these and starts anyway."""
    out = []
    if not env_value("VALXEL_CONFIG_REPO"):
        out.append("VALXEL_CONFIG_REPO is not set: changes to models and settings aren't evaluated against past "
                   "work before they merge")
    s = services.settings
    from .models import available_roles, optional_roles

    optional, on = optional_roles(s), (available_roles(s) if s.mode == "live" else set())
    unknown = sorted({r.model for n, r in s.roles.items()  # fallbacks count only when they can run
                      if (n not in optional or n in on) and not s.context_window(r.model)})
    if unknown:
        out.append("models.yaml has no context window for " + ", ".join(unknown) + ": how full their sessions' "
                   "context gets can't be measured (valxel usage context shows it as unknown)")
    pinned = getattr(getattr(s, "plugins", None), "ref", "")
    if pinned and s.mode == "live":
        from .plugins import installed_ref

        have = installed_ref(s.skills_dir or env_value("VALXEL_SKILLS_DIR"))
        if have and have != pinned:  # unmarked: a local checkout's plugins, not an image's
            out.append(f"config/plugins.yaml pins knowledge-work-plugins at {pinned[:12]}, but the installed plugins "
                       f"are at {have[:12]}: the agents follow the installed ones until the image is rebuilt from "
                       "the config's pin")
    tel = getattr(services, "telemetry", None)
    if tel is None or not tel.enabled:
        out.append("VALXEL_OTEL_ENDPOINT is not set: agent sessions aren't traced, so a session can't be "
                   "looked into from the feature page or G5/G6")
    for pid, entry in sorted(services.settings.registry.items()):
        if entry.kind != "shared":
            continue
        try:
            repos = services.snapshot(pid).product.repos
        except Exception:
            continue
        unchecked = [r.name for r in repos if r.role == "library" and not r.package]
        if unchecked:
            out.append(f"{pid}: {', '.join(unchecked)} declare no package, so G6 can't check that a release "
                       "candidate was published to GitHub Packages")
    return out


def check(services, *, auth_mode: str, env=None) -> list[str]:
    """Raise StrictModeError listing every stand-in; return the known gaps (already logged)."""
    problems = stand_ins(services, auth_mode=auth_mode, env=env)
    if problems:
        raise StrictModeError("VALXEL_STRICT is on and these would run as offline stand-ins:\n- "
                              + "\n- ".join(problems))
    gaps = known_gaps(services)
    for gap in gaps:
        log.warning("strict mode: %s", gap)
    return gaps
