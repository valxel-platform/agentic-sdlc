"""A product's data planes on Google Cloud and Azure, built by the tool at the product gate.

On those clouds the product gate doesn't propose cluster files for a platform team to build: the tool
builds each data plane itself, in the customer's own project or subscription, with the roles the
customer's connect template granted there (constitution VX-20). Two planes per product, one per
account in product.yaml's runtime.cloud (cloud/onboarding.py):

    <pid>-nonprod   the non-production account's: dev, staging and change-set environments
    <pid>-prod      the production account's

    product gate  -> for each plane: the account's connection must be in full mode, else wait
                  -> a Job in the tool's cluster runs platform-foundation's plane-builder image
                     (`plane-build <cloud> apply`, tofu/stacks/product-plane-<cloud>) as the tool's own
                     service account, acting as the account's valxel-deploy identity; the state stays in
                     the customer's account
                  -> when it succeeds: the plane's agent CA, read from the new cluster as valxel-deploy,
                     and a pull request to the infrastructure repo registering the plane with
                     OpenChoreo's control plane (as platform-foundation's scripts/register-plane.sh does)
                  -> environments.environments() creates the project and environments once it's ready

Every step is safe to repeat: a plane's Job has a fixed name, so asking again reports its progress
instead of starting another. Nothing here holds a credential: the Job's pod gets the tool's own
workload identity, and the tool asks the cloud for a short-lived token when it reads the agent CA.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

from .cloud import onboarding

CLOUDS = ("gcp", "azure")
KINDS = {"non_production": "nonprod", "production": "prod"}
STATE_CONTAINER = "tfstate"
AKS_SERVER_APP = "6dae42f8-4368-4678-94ff-3960e28e3630"  # AKS's Entra server app: every AKS token's audience
AGENT_SECRET = ("openchoreo-data-plane", "cluster-agent-tls")


def plane_name(product_id: str, kind: str) -> str:
    return f"{product_id.lower()}-{KINDS[kind]}"


def settings(env: Any = None) -> dict:
    """What every plane shares, from the tool's settings."""
    env = env if env is not None else os.environ
    cidrs = [c.strip() for c in (env.get("VALXEL_PLANE_ADMIN_CIDRS") or "").split(",") if c.strip()]
    return {
        "image": env.get("VALXEL_PLANE_BUILDER_IMAGE", ""),
        "namespace": env.get("VALXEL_PLANE_BUILDER_NAMESPACE", "") or env.get("POD_NAMESPACE", "") or "valxel",
        "service_account": env.get("VALXEL_PLANE_BUILDER_SERVICE_ACCOUNT", "") or "valxel",
        "domain": env.get("VALXEL_PLANE_DOMAIN", ""),  # planes answer at <plane>.<this>
        "control_plane_domain": env.get("VALXEL_CONTROL_PLANE_DOMAIN", ""),
        "acme_email": env.get("VALXEL_PLANE_ACME_EMAIL", ""),
        # The tool's egress addresses: the only ones (with the customer's) that reach a plane's API.
        "admin_cidrs": {f"valxel-{i}": c for i, c in enumerate(cidrs)},
        "gitops_branch": env.get("VALXEL_PLANE_GITOPS_BRANCH", "") or "main",
        # The db-logins controller each plane runs, pinned by digest (platform-resources publishes it).
        "db_logins_image": env.get("VALXEL_DB_LOGINS_IMAGE", ""),
    }


def missing_settings(s: dict) -> list[str]:
    names = {"image": "VALXEL_PLANE_BUILDER_IMAGE", "domain": "VALXEL_PLANE_DOMAIN",
             "control_plane_domain": "VALXEL_CONTROL_PLANE_DOMAIN", "acme_email": "VALXEL_PLANE_ACME_EMAIL",
             "admin_cidrs": "VALXEL_PLANE_ADMIN_CIDRS", "db_logins_image": "VALXEL_DB_LOGINS_IMAGE"}
    return [env for key, env in names.items() if not s.get(key)]


def plane_vars(product_id: str, kind: str, cloud: dict, s: dict, *, zones: int = 3) -> dict:
    """terraform.tfvars.json for tofu/stacks/product-plane-<cloud>."""
    provider = cloud["provider"]
    name = plane_name(product_id, kind)
    prod = kind == "production"
    out: dict[str, Any] = {
        "name": name, "product": product_id.lower(), "env": KINDS[kind],
        "domain": f"{name}.{s['domain']}", "control_plane_domain": s["control_plane_domain"],
        "acme_email": s["acme_email"], "admin_cidrs": s["admin_cidrs"], "gitops_branch": s["gitops_branch"],
        # Non-production: spot, one zone; production: never spot, the region's zones.
        "nodes": {"min": 2 if prod else 1, "max": 10, "spot": not prod},
        "cluster_vars": {"db_logins_image": s["db_logins_image"]},
    }
    account = onboarding.plane_accounts(cloud)[kind]
    if provider == "gcp":
        out.update({"project": account, "region": cloud["region"], "zone_count": min(zones, 3) if prod else 1})
    elif provider == "azure":
        count = min(zones, 3) if prod else 1
        out.update({"location": cloud["region"], "zones": [str(z) for z in range(1, count + 1)]})
    else:
        raise ValueError(f"the tool builds data planes on {', '.join(CLOUDS)}, not {provider}")
    return out


def state_location(provider: str, account: str) -> dict:
    """Where the connect template put the plane's OpenTofu state, in the customer's account."""
    if provider == "gcp":
        return {"STATE_BUCKET": f"{account}-valxel-state"}
    return {"STATE_ACCOUNT": "vxst" + account.replace("-", "")[:20]}


# ------------------------------------------------------------------------------ the Job

class Runner(Protocol):
    def status(self, name: str) -> dict: ...  # {"state": absent|running|succeeded|failed, "output", "log"}
    def start(self, name: str, *, args: list[str], env: dict[str, str], files: dict[str, str]) -> None: ...


def job_name(plane: str, action: str = "apply") -> str:
    return f"plane-{action}-{plane}"[:63].rstrip("-")


class KubernetesJobs:
    """Jobs in the tool's namespace, through the cluster's API with this pod's service account (the
    chart's role lets it make Jobs and ConfigMaps and read pod logs there, nothing else)."""

    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, s: dict, http: Any = None) -> None:
        import httpx

        host, port = os.environ.get("KUBERNETES_SERVICE_HOST", ""), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base, self.ns, self.s = f"https://{host}:{port}", s["namespace"], s
        self.http = http or httpx.Client(verify=str(self.SA / "ca.crt"), timeout=30, trust_env=False)

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        token = (self.SA / "token").read_text().strip()
        resp = self.http.request(method, f"{self.base}{path}", json=body, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"the cluster refused {method} {path.rsplit('/', 2)[-2:]}: {resp.status_code}")
        return resp.text if raw else (resp.json() if resp.content else {})

    def status(self, name: str) -> dict:
        job = self._call("GET", f"/apis/batch/v1/namespaces/{self.ns}/jobs/{name}")
        if not job:
            return {"state": "absent"}
        st = job.get("status") or {}
        state = "succeeded" if st.get("succeeded") else "failed" if any(
            c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or []) else "running"
        out: dict[str, Any] = {"state": state, "started": st.get("startTime", "")}
        if state != "running":
            pods = self._call("GET", f"/api/v1/namespaces/{self.ns}/pods?labelSelector=job-name%3D{name}") or {}
            items = sorted(pods.get("items") or [], key=lambda p: p["metadata"].get("creationTimestamp", ""))
            pod = items[-1]["metadata"]["name"] if items else ""
            path = f"/api/v1/namespaces/{self.ns}/pods/{pod}/log?tailLines=200"
            log = self._call("GET", path, raw=True) if pod else ""
            out.update(parse_log(log or ""))
        return out

    def start(self, name: str, *, args: list[str], env: dict[str, str], files: dict[str, str]) -> None:
        labels = {"app.kubernetes.io/name": "plane-builder", "app.kubernetes.io/part-of": "valxel"}
        self._call("DELETE", f"/api/v1/namespaces/{self.ns}/configmaps/{name}")  # an earlier run's, if any
        self._call("POST", f"/api/v1/namespaces/{self.ns}/configmaps",
                   {"metadata": {"name": name, "labels": labels}, "data": files})
        container = {
            "name": "plane-build", "image": self.s["image"], "args": args,
            "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
            "volumeMounts": [{"name": "work", "mountPath": "/work", "readOnly": True}],
            "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True,
                                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
        }
        job = {
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "backoffLimit": 1, "activeDeadlineSeconds": 5400, "ttlSecondsAfterFinished": 7 * 86400,
                "template": {
                    "metadata": {"labels": {**labels, "azure.workload.identity/use": "true"}},
                    "spec": {"serviceAccountName": self.s["service_account"], "restartPolicy": "Never",
                             "containers": [container],
                             "volumes": [{"name": "work", "configMap": {"name": name}}]},
                },
            },
        }
        self._call("POST", f"/apis/batch/v1/namespaces/{self.ns}/jobs", job)


def parse_log(log: str) -> dict:
    """plane-build prints the stack's `plane` output as JSON on its last line."""
    lines = [ln for ln in log.strip().splitlines() if ln.strip()]
    if lines:
        try:
            out = json.loads(lines[-1])
            if isinstance(out, dict):
                return {"output": out}
        except json.JSONDecodeError:
            pass
    return {"log": "\n".join(lines[-15:])}


# ------------------------------------------------------------------------------ registration

def registration(plane: str, domain: str, agent_ca: str, observability: str = "") -> str:
    """The control plane's record of the plane: what scripts/register-plane.sh writes."""
    ca = "\n".join(f"        {ln}" for ln in agent_ca.strip().splitlines())
    obs = (f"  observabilityPlaneRef:\n    kind: ClusterObservabilityPlane\n    name: {observability}\n"
           if observability else "")
    return (f"# Written by the SDLC tool when it built {plane} (agentic-sdlc dataplanes.py): the plane and its\n"
            f"# agent's CA, a public certificate.\n"
            f"apiVersion: openchoreo.dev/v1alpha1\nkind: ClusterDataPlane\nmetadata:\n  name: {plane}\n"
            f"  labels:\n    valxel.io/product: \"{plane.rsplit('-', 1)[0]}\"\nspec:\n  planeID: {plane}\n"
            f"  clusterAgent:\n    clientCA:\n      value: |\n{ca}\n  secretStoreRef:\n    name: default\n{obs}"
            f"  gateway:\n    ingress:\n      external:\n        name: gateway-default\n"
            f"        namespace: openchoreo-data-plane\n        https:\n          host: apps.{domain}\n"
            f"          listenerName: https\n          port: 443\n")


def read_agent_ca(provider: str, output: dict, token: str, http: Any = None) -> str:
    """The plane's agent CA, read from its cluster with a short-lived token for valxel-deploy. ''
    until Flux has installed OpenChoreo's data plane there."""
    import httpx

    ns, secret = AGENT_SECRET
    host = output.get("endpoint", "")
    host = host if host.startswith("https://") else f"https://{host}"
    ca = output.get("ca_certificate", "")
    verify: Any = True
    if ca:
        import ssl

        verify = ssl.create_default_context(cadata=base64.b64decode(ca).decode())
    client = http or httpx.Client(verify=verify, timeout=20, trust_env=False)
    resp = client.get(f"{host}/api/v1/namespaces/{ns}/secrets/{secret}", headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    data = (resp.json().get("data") or {}).get("ca.crt", "")
    return base64.b64decode(data).decode() if data else ""


# ------------------------------------------------------------------------------ the step

def build(services, *, product_id: str, cloud: dict | None, approver: str, zones: int = 3, thread_id: str = "",
          runner: Runner | None = None, s: dict | None = None, infra_repo: str | None = None,
          token: Any = None) -> dict:
    """Build (or report on) the product's two data planes. Returns {"planes": {name: status},
    "waiting": [why]}; a plane's status is one of waiting, building, built, registered, failed."""
    provider = (cloud or {}).get("provider", "")
    if provider not in CLOUDS:
        return {"skipped": True, "why": f"the tool builds data planes on {', '.join(CLOUDS)}; "
                                        f"{provider or 'this cloud'} products get cluster files for the platform team"}
    accounts = onboarding.plane_accounts(cloud or {})
    if not all(accounts.values()) or not (cloud or {}).get("region"):
        n = onboarding.noun(provider)
        return {"waiting": [f"the product's {onboarding.name(provider)} {n}s: set runtime.cloud in product.yaml "
                            f"(a non-production and a production {n}, and the region)"], "planes": {}}
    s = s or settings()
    unset = missing_settings(s)
    if unset:
        return {"waiting": [f"this Valxel isn't set up to build data planes: set {', '.join(unset)}"], "planes": {}}
    runner = runner or KubernetesJobs(s)
    infra_repo = infra_repo if infra_repo is not None else os.environ.get("VALXEL_INFRA_REPO", "")
    conns = getattr(services, "connections", None)
    planes: dict[str, dict] = {}
    waiting: list[str] = []
    for kind, account in accounts.items():
        name = plane_name(product_id, kind)
        rec = _connection(conns, provider, account)
        if not rec or (rec.get("reach") or {}).get("mode") != "full":
            noun = onboarding.noun(provider)
            why = (f"connect {noun} {account} in full mode from the connections page" if not rec else
                   f"{noun} {account} is connected in lite mode: connect it again in full mode")
            planes[name] = {"state": "waiting", "account": account, "why": why}
            waiting.append(why)
            continue
        job = job_name(name)
        st = runner.status(job)
        if st["state"] == "absent":
            vars_ = plane_vars(product_id, kind, cloud or {}, s, zones=zones)
            runner.start(job, args=[provider, "apply"], env=_job_env(provider, account, name, rec),
                         files={"terraform.tfvars.json": json.dumps(vars_, indent=2)})
            planes[name] = {"state": "building", "account": account, "job": job}
            waiting.append(f"{name} is being built (about 30 minutes)")
            continue
        if st["state"] == "running":
            planes[name] = {"state": "building", "account": account, "job": job, "since": st.get("started", "")}
            waiting.append(f"{name} is being built")
            continue
        if st["state"] == "failed":
            planes[name] = {"state": "failed", "account": account, "job": job, "log": st.get("log", "")}
            waiting.append(f"building {name} failed: see the Job {job}'s log; delete the Job to try again")
            continue
        out = st.get("output") or {}
        planes[name] = {"state": "built", "account": account, "plane": out,
                        "delegate": {"domain": out.get("domain"), "name_servers": out.get("name_servers")}}
        planes[name].update(_register(services, provider, account, name, out, rec, infra_repo=infra_repo,
                                      thread_id=thread_id or product_id, approver=approver, token=token))
        if planes[name]["state"] != "registered":
            waiting.append(planes[name].get("why", f"registering {name}"))
    return {"planes": planes, "waiting": waiting}


def _connection(conns, provider: str, account: str) -> dict | None:
    if conns is None:
        return None
    for c in conns.store.list("connection"):
        if c.get("tool") == provider and str(c.get("account", "")).lower() == account.lower():
            return c
    return None


def _job_env(provider: str, account: str, plane: str, rec: dict) -> dict[str, str]:
    """The Job acts as the account's valxel-deploy identity, from the tool's own workload identity."""
    env = {"PLANE": plane, **state_location(provider, account)}
    if provider == "gcp":
        env["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"] = f"valxel-deploy@{account}.iam.gserviceaccount.com"
        env["GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT"] = env["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"]
    else:
        reach = rec.get("reach") or {}
        env.update({"ARM_USE_OIDC": "true",
                    "ARM_OIDC_TOKEN_FILE_PATH": "/var/run/secrets/azure/tokens/azure-identity-token",
                    "ARM_CLIENT_ID": str(reach.get("deploy_client_id", "")),
                    "ARM_TENANT_ID": str(reach.get("tenant", "")), "ARM_SUBSCRIPTION_ID": account})
    return env


def _register(services, provider: str, account: str, plane: str, out: dict, rec: dict, *, infra_repo: str,
              thread_id: str, approver: str, token: Any = None) -> dict:
    """A pull request registering the plane with the control plane, once its agent has a CA."""
    if not infra_repo:
        return {"state": "built", "why": f"VALXEL_INFRA_REPO isn't set: register {plane} with the control plane by "
                                         "hand (platform-foundation scripts/register-plane.sh)"}
    oc = getattr(services, "openchoreo", None)
    known = getattr(oc, "data_plane", None)
    if callable(known) and known(name=plane).get("state") == "ready":
        return {"state": "registered"}
    try:
        tok = token(provider, account, rec) if token else cluster_token(services, provider, account, rec)
        ca = read_agent_ca(provider, out, tok)
    except Exception as exc:  # noqa: BLE001 - token, network or TLS: all mean "not reachable yet", retried later
        return {"state": "built", "why": f"couldn't read {plane}'s agent CA yet ({type(exc).__name__}): its platform "
                                         "may still be installing"}
    if not ca:
        return {"state": "built", "why": f"{plane}'s platform is still installing (OpenChoreo's agent has no CA yet)"}
    gh = services.github
    path = f"gitops/clusters/tools/planes/{plane}.yaml"
    branch = f"valxel/plane-{plane}"
    text = registration(plane, str(out.get("domain", "")), ca,
                        observability=os.environ.get("VALXEL_PLANE_OBSERVABILITY", "") or "nonprod")
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    gh.put_file(repo=infra_repo, branch=branch, path=path, content=text, message=f"{thread_id}: register {plane}")
    pr = gh.open_pr(repo=infra_repo, branch=branch, title=f"Register data plane {plane}", files=[path],
                    labels=["valxel", "inception"],
                    body=(f"{plane} was built in {onboarding.name(provider)} {onboarding.noun(provider)} {account} for "
                          f"the product gate {approver} approved. This registers it with the control plane (its "
                          f"agent's CA, {digest}). Add it to gitops/clusters/tools/planes/kustomization.yaml if your "
                          "repo lists planes there."))
    return {"state": "registering", "pr": pr.get("url"), "why": f"waiting for the pull request registering {plane}"}


def cluster_token(services, provider: str, account: str, rec: dict) -> str:
    """A short-lived token for valxel-deploy, good for the plane's Kubernetes API."""
    conns = services.connections
    if provider == "gcp":
        return str(conns.gcp.token(account, "deploy"))
    return str(conns.azure.token(account, "deploy", rec.get("reach"), scope=f"{AKS_SERVER_APP}/.default"))
