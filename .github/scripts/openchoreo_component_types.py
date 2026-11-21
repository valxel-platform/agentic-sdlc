"""Builds the platform's ClusterComponentTypes on OpenChoreo's own (samples/getting-started),
keeping the platform's additions: hardened pods, zone spread, disruption budgets, zero-downtime
rollouts, role labels for network policies, and each role's own resources (T-72).

When OpenChoreo is upgraded, rebuild from the new release's types and review the difference:

    git clone --depth 1 --branch v1.3.3 https://github.com/openchoreo/openchoreo /tmp/openchoreo
    python .github/scripts/openchoreo_component_types.py /tmp/openchoreo \
        examples/governance/valxel-platform/sdlc-governance/platform/openchoreo/component-types.yaml
"""

import copy
import sys
from pathlib import Path

import yaml

OC = Path(sys.argv[1])  # the openchoreo checkout at v1.3.3
OUT = Path(sys.argv[2])
base = {n: yaml.safe_load((OC / f"samples/getting-started/component-types/{f}.yaml").read_text())
        for n, f in (("service", "service"), ("worker", "worker"), ("web", "webapp"), ("task", "scheduled-task"))}

CONFIG_IDS = ("env-config", "file-config", "secret-env-external", "secret-file-external")


def res(b, *ids):
    return [copy.deepcopy(r) for r in b["spec"]["resources"] if r["id"] in ids]


def schema(replicas=1, cpu="250m", mem="256Mi", extra=None, replicas_min=1):
    props = {
        "replicas": {"type": "integer", "minimum": replicas_min, "default": replicas},
        "resources": {"type": "object", "default": {}, "properties": {
            "requests": {"type": "object", "default": {}, "properties": {
                "cpu": {"type": "string", "default": cpu}, "memory": {"type": "string", "default": mem}}},
            # No CPU limit, deliberately: throttling hurts latency more than sharing hurts neighbours.
            "limits": {"type": "object", "default": {}, "properties": {
                "memory": {"type": "string", "default": mem}}}}},
        "imagePullPolicy": {"type": "string", "default": "IfNotPresent", "enum": ["Always", "IfNotPresent", "Never"]},
    }
    if replicas is None:
        props.pop("replicas")
    props.update(extra or {})
    return {"openAPIV3Schema": {"type": "object", "properties": props}}


ROLE_PARAM = {"role": {"type": "string", "default": "", "description":
                       "The repo's role in repos.yaml, set by the Valxel SDLC when it registers the component; "
                       "pods carry it as valxel.io/role for the network policies"}}

SPREAD = [{"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone", "whenUnsatisfiable": "ScheduleAnyway",
           "labelSelector": {"matchLabels": "${metadata.podSelectors}"}}]
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
CONTAINER_SECURITY = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                      "capabilities": {"drop": ["ALL"]}}
# Every other type: non-root (the image's own numeric user), nothing to gain, the default seccomp
# profile. Not a read-only root: a web server writes its caches.
POD_BASELINE = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
CONTAINER_BASELINE = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
LABELS = '${oc_merge(metadata.podSelectors, {"valxel.io/role": parameters.role})}'


def container(*, env_extra="", mounts_extra="", probes=None, hardened=False):
    c = {
        "name": "main",
        "image": "${workload.container.image}",
        "imagePullPolicy": "${environmentConfigs.imagePullPolicy}",
        "command": "${has(workload.container.command) ? workload.container.command : oc_omit()}\n",
        "args": "${has(workload.container.args) ? workload.container.args : oc_omit()}\n",
        "resources": "${environmentConfigs.resources}",
        "env": "${dependencies.toContainerEnvs()" + (f" + {env_extra}" if env_extra else "") + "}",
        "envFrom": "${configurations.toContainerEnvFrom()}",
        "volumeMounts": "${configurations.toContainerVolumeMounts() + dependencies.toContainerVolumeMounts()"
                        + (f" + {mounts_extra}" if mounts_extra else "") + "}",
    }
    c["securityContext"] = CONTAINER_SECURITY if hardened else CONTAINER_BASELINE
    if probes:
        ready, live = probes
        c["readinessProbe"] = {"httpGet": {"path": ready, "port": 8080}, "periodSeconds": 5}
        c["livenessProbe"] = {"httpGet": {"path": live, "port": 8080}, "periodSeconds": 10, "failureThreshold": 6}
    return c


def deployment(*, c, volumes_extra="", grace=30, hardened=False, token=True, runtime=None, sa=None, prestop=True):
    pod = {}
    if not token:
        pod["automountServiceAccountToken"] = False
    if sa:
        pod["serviceAccountName"] = sa
    if runtime:
        pod["runtimeClassName"] = runtime
    pod["securityContext"] = POD_SECURITY if hardened else POD_BASELINE
    pod["terminationGracePeriodSeconds"] = grace
    pod["topologySpreadConstraints"] = SPREAD
    if prestop:
        c = {**c, "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}}}
    pod["containers"] = [c]
    pod["volumes"] = "${configurations.toVolumes() + dependencies.toVolumes()" + (
        f" + {volumes_extra}" if volumes_extra else "") + "}"
    return {"id": "deployment", "template": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "${metadata.name}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
        "spec": {"replicas": "${environmentConfigs.replicas}",
                 "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                 "selector": {"matchLabels": "${metadata.podSelectors}"},
                 "template": {"metadata": {"labels": LABELS}, "spec": pod}}}}


PDB = {"id": "pdb", "includeWhen": "${environmentConfigs.replicas >= 2}", "template": {
    "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
    "metadata": {"name": "${metadata.name}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
    "spec": {"maxUnavailable": 1, "selector": {"matchLabels": "${metadata.podSelectors}"}}}}

WORKFLOWS = [{"kind": "ClusterWorkflow", "name": "docker"}]
TRAITS = [{"kind": "ClusterTrait", "name": "autoscaler"}, {"kind": "ClusterTrait", "name": "observability-alert-rule"}]
GATEWAY_RULES = [v for v in base["service"]["spec"]["validations"] if "gateway" in v["rule"]]


def ctype(name, description, *, workload="deployment", env, resources, validations=(), traits=TRAITS,
          params=None):
    spec = {"workloadType": workload, "allowedWorkflows": WORKFLOWS, "allowedTraits": traits}
    if validations:
        spec["validations"] = list(validations)
    spec["parameters"] = {"openAPIV3Schema": {"type": "object", "properties": {**ROLE_PARAM, **(params or {})}}}
    spec["environmentConfigs"] = env
    spec["resources"] = resources
    return {"apiVersion": "openchoreo.dev/v1alpha1", "kind": "ClusterComponentType",
            "metadata": {"name": name, "annotations": {"openchoreo.dev/description": description}}, "spec": spec}


svc = base["service"]
web = base["web"]
TMP = '[{"name": "tmp", "mountPath": "/tmp"}]'


def tmp_volume(size):
    return f'[{{"name": "tmp", "emptyDir": {{"sizeLimit": "{size}"}}}}]'


DNS = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
               "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
       "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
OTEL = {"to": [{"namespaceSelector": {"matchLabels": {"valxel.io/plane": "observability"}}}],
        "ports": [{"protocol": "TCP", "port": 4317}, {"protocol": "TCP", "port": 4318}]}

types = []

# --- service and bff: request-serving, endpoints required --------------------------------------
types.append(ctype(
    "service", "A long-running request-serving component (repos.yaml roles service, bff, mcp-server, llm-gateway)",
    env=schema(),
    validations=[v for v in svc["spec"]["validations"]],
    resources=[deployment(c=container(probes=("/health/ready", "/health/live")))]
    + res(svc, "service", "httproute-external", "httproute-internal") + [PDB] + res(svc, *CONFIG_IDS)))

# --- web-app: the 1.3 web application, with the platform's rollout and spread -----------------
types.append(ctype(
    "web-app", "A web front end served to browsers (repos.yaml role web)",
    env=schema(cpu="100m", mem="128Mi"),
    validations=[v for v in web["spec"].get("validations", [])],
    resources=[deployment(c=container())] + res(web, "service", "httproute-external", "httproute-internal")
    + [PDB] + res(web, *CONFIG_IDS)))

# --- worker: no endpoints --------------------------------------------------------------------
types.append(ctype(
    "worker", "A long-running background or event-driven component with no endpoints (repos.yaml roles "
              "event-worker, reporting)",
    env=schema(),
    validations=list(base["worker"]["spec"]["validations"]),
    resources=[deployment(c=container())] + [PDB] + res(base["worker"], *CONFIG_IDS)))

# --- agent -----------------------------------------------------------------------------------
agent_env = schema(cpu="500m", mem="512Mi", extra={
    "allowedTools": {"type": "string", "default": "[]", "description":
                     "The MCP tools the agent may call, as a JSON list, from the repo's agent.yaml on every deploy"},
    "readOnlyTools": {"type": "string", "default": "[]", "description":
                      "The tools it calls without a person, as a JSON list"},
    "egressCIDRs": {"type": "array", "items": {"type": "string"}, "default": [], "description":
                    "The database and event brokers outside the cluster, per environment"}})
types.append(ctype(
    "agent", "A product's agent: locked down, it reaches only the product's LLM gateway, MCP servers, code "
             "sandbox, database and event brokers; PHI and payments agents run under gVisor",
    params={"isolation": {"type": "string", "enum": ["standard", "gvisor"], "default": "standard"}},
    env=agent_env, validations=GATEWAY_RULES,
    resources=[deployment(
        c=container(hardened=True, probes=("/readyz", "/healthz"), mounts_extra=TMP,
                    env_extra='[{"name": "AGENT_ALLOWED_TOOLS", "value": environmentConfigs.allowedTools}, '
                              '{"name": "AGENT_READ_ONLY_TOOLS", "value": environmentConfigs.readOnlyTools}]'),
        volumes_extra=tmp_volume("256Mi"), grace=60, hardened=True, token=False,
        runtime='${parameters.isolation == "gvisor" ? "gvisor" : oc_omit()}')]
    + res(svc, "service", "httproute-external", "httproute-internal")
    + [{"id": "egress", "template": {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "${metadata.name}-egress", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
        "spec": {"podSelector": {"matchLabels": "${metadata.podSelectors}"}, "policyTypes": ["Egress"], "egress": [
            DNS,
            {"to": [{"namespaceSelector": {}, "podSelector": {"matchExpressions": [
                {"key": "valxel.io/role", "operator": "In", "values": ["mcp-server", "llm-gateway", "code-sandbox"]}]}}]},
            OTEL,
            {"to": '${environmentConfigs.egressCIDRs.map(c, {"ipBlock": {"cidr": c}})}'}]}}},
       PDB] + res(svc, *CONFIG_IDS)))

# --- code-sandbox ----------------------------------------------------------------------------
types.append(ctype(
    "code-sandbox", "A product's code sandbox (the run_code MCP server): claims a fresh sandbox per run; only the "
                    "product's agents reach it",
    env=schema(replicas=2, extra={"apiServerCIDRs": {"type": "array", "items": {"type": "string"}, "default": []}}),
    resources=[
        {"id": "serviceaccount", "template": {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
            "name": "${metadata.name}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"}}},
        {"id": "role", "template": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {
            "name": "${metadata.name}-claims", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "rules": [{"apiGroups": ["extensions.agents.x-k8s.io"], "resources": ["sandboxclaims"],
                       "verbs": ["create", "get", "delete"]}]}},
        {"id": "rolebinding", "template": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": "${metadata.name}-claims", "namespace": "${metadata.namespace}",
                         "labels": "${metadata.labels}"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "${metadata.name}-claims"},
            "subjects": [{"kind": "ServiceAccount", "name": "${metadata.name}", "namespace": "${metadata.namespace}"}]}},
        deployment(c={**container(hardened=True, probes=("/healthz", "/healthz"),
                                  env_extra='[{"name": "SANDBOX_EXECUTOR", "value": "kubernetes"}, '
                                            '{"name": "SANDBOX_SANDBOX_TEMPLATE", "value": "code-runner"}]')},
                   hardened=True, sa="${metadata.name}"),
        *res(svc, "service"),
        {"id": "network", "template": {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "${metadata.name}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "spec": {"podSelector": {"matchLabels": "${metadata.podSelectors}"}, "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [
                         {"namespaceSelector": {}, "podSelector": {"matchLabels": {"valxel.io/role": "agent"}}},
                         {"namespaceSelector": {"matchLabels": {"valxel.io/plane": "observability"}}}],
                         "ports": [{"protocol": "TCP", "port": 8080}]}],
                     "egress": [DNS,
                                {"to": [{"podSelector": {"matchLabels": {"valxel.io/sandbox": "code-runner"}}}],
                                 "ports": [{"protocol": "TCP", "port": 8888}]},
                                OTEL,
                                {"to": '${environmentConfigs.apiServerCIDRs.map(c, {"ipBlock": {"cidr": c}})}',
                                 "ports": [{"protocol": "TCP", "port": 443}]}]}}},
        PDB] + res(svc, *CONFIG_IDS)))

# --- connector -------------------------------------------------------------------------------
conn_env = schema(replicas=2, extra={
    "egressCIDRs": {"type": "array", "items": {"type": "string"}, "default": []},
    "webhookPaths": {"type": "array", "items": {"type": "string", "pattern": "^/webhooks/[a-z0-9-]+$"}, "default": [],
                     "description": "From integration.yaml at the deployed commit (valxel_sdlc.connectors)"},
    "egressHosts": {"type": "array", "items": {"type": "string"}, "default": []},
    "dataClass": {"type": "string", "enum": ["general", "payments", "phi"], "default": "general"},
    "callerRoles": {"type": "array", "items": {"type": "string"}, "default": [], "description":
                    "Roles inside the product that may call it: none for a connector, the BFF for a channel"}})
hosts = base["service"]["spec"]["resources"][2]["template"]["spec"]["hostnames"]
types.append(ctype(
    "connector", "A product's connector to a third party, or its channel service: only the gateway reaches its "
                 "webhook paths, and it reaches only its vendors' hosts by name",
    env=conn_env, validations=GATEWAY_RULES,
    resources=[
        deployment(c=container(hardened=True, probes=("/readyz", "/healthz"),
                               mounts_extra=TMP[:-1] + ', {"name": "vendor-secrets", "mountPath": '
                                                       '"/var/run/secrets/valxel", "readOnly": true}]'),
                   volumes_extra=tmp_volume("64Mi")[:-1] + ', {"name": "vendor-secrets", "secret": {"secretName": '
                                 'metadata.name + "-vendor", "defaultMode": 256, "optional": true}}]',
                   hardened=True, token=False,
                   runtime='${environmentConfigs.dataClass == "general" ? oc_omit() : "gvisor"}'),
        {"id": "service", "template": {"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": "${metadata.componentName}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "spec": {"type": "ClusterIP", "selector": "${metadata.podSelectors}",
                     "ports": [{"name": "http", "port": 8080, "targetPort": 8080}]}}},
        {"id": "route", "includeWhen": "${size(environmentConfigs.webhookPaths) > 0 && has(gateway.ingress) && "
                                       "has(gateway.ingress.external)}",
         "template": {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
                      "metadata": {"name": "${metadata.name}-webhooks", "namespace": "${metadata.namespace}",
                                   "labels": "${metadata.labels}"},
                      "spec": {"parentRefs": [{"name": "${gateway.ingress.external.name}",
                                               "namespace": "${gateway.ingress.external.namespace}"}],
                               "hostnames": hosts,
                               "rules": [{"matches": '${environmentConfigs.webhookPaths.map(p, {"path": {"type": '
                                                     '"Exact", "value": p}, "method": "POST"})}',
                                          "backendRefs": [{"name": "${metadata.componentName}", "port": 8080}]}]}}},
        {"id": "ingress", "template": {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "${metadata.name}-ingress", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "spec": {"podSelector": {"matchLabels": "${metadata.podSelectors}"}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [
                         {"namespaceSelector": {"matchLabels": {
                             "kubernetes.io/metadata.name": "${gateway.ingress.external.namespace}"}}},
                         {"namespaceSelector": {"matchLabels": {"valxel.io/plane": "observability"}}}],
                         "ports": [{"protocol": "TCP", "port": 8080}]}]}}},
        {"id": "callers", "includeWhen": "${size(environmentConfigs.callerRoles) > 0}", "template": {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "${metadata.name}-callers", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "spec": {"podSelector": {"matchLabels": "${metadata.podSelectors}"}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {}, "podSelector": {"matchExpressions": [
                         {"key": "valxel.io/role", "operator": "In", "values": "${environmentConfigs.callerRoles}"}]}}],
                         "ports": [{"protocol": "TCP", "port": 8080}]}]}}},
        {"id": "egress", "template": {
            "apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "${metadata.name}-egress", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
            "spec": {"endpointSelector": {"matchLabels": "${metadata.podSelectors}"}, "egress": [
                {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}],
                 "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": [{"matchPattern": "*"}]}}]},
                {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "observability"}}],
                 "toPorts": [{"ports": [{"port": "4317", "protocol": "TCP"}, {"port": "4318", "protocol": "TCP"}]}]},
                {"toCIDR": "${environmentConfigs.egressCIDRs}"},
                {"toFQDNs": '${environmentConfigs.egressHosts.map(h, {"matchName": h})}',
                 "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}]}}},
        PDB] + res(svc, *CONFIG_IDS)))

# --- scheduled-task --------------------------------------------------------------------------
task = base["task"]
task_env = schema(replicas=None, extra={
    "environment": {"type": "string", "enum": ["dev", "staging", "production"], "default": "dev",
                    "description": "The environment's name, for job.yaml's per-environment schedule"},
    "schedule": {"type": "string", "default": "15 3 * * *"},
    "timeZone": {"type": "string", "default": "UTC"},
    "suspend": {"type": "boolean", "default": False},
    "activeDeadlineSeconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 1800}})
job_c = container(hardened=True, mounts_extra=TMP,
                  env_extra='[{"name": "JOB_ENVIRONMENT", "value": environmentConfigs.environment}]')
types.append(ctype(
    "scheduled-task", "A product's scheduled job: runs once and exits; runs never overlap, a failed run isn't "
                      "retried, and one that couldn't start within 10 minutes is skipped (repos.yaml role job)",
    workload="cronjob", traits=[{"kind": "ClusterTrait", "name": "observability-alert-rule"}],
    validations=list(task["spec"].get("validations") or []),
    env=task_env,
    resources=[{"id": "cronjob", "template": {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "${metadata.name}", "namespace": "${metadata.namespace}", "labels": "${metadata.labels}"},
        "spec": {"schedule": "${environmentConfigs.schedule}", "timeZone": "${environmentConfigs.timeZone}",
                 "suspend": "${environmentConfigs.suspend}", "concurrencyPolicy": "Forbid",
                 "startingDeadlineSeconds": 600, "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
                 "jobTemplate": {"spec": {
                     "backoffLimit": 0, "activeDeadlineSeconds": "${environmentConfigs.activeDeadlineSeconds}",
                     "ttlSecondsAfterFinished": 86400,
                     "template": {"metadata": {"labels": LABELS}, "spec": {
                         "restartPolicy": "Never", "automountServiceAccountToken": False,
                         "securityContext": POD_SECURITY, "terminationGracePeriodSeconds": 45,
                         "containers": [job_c],
                         "volumes": "${configurations.toVolumes() + dependencies.toVolumes() + "
                                    + tmp_volume("128Mi") + "}"}}}}}}}]
    + res(task, *CONFIG_IDS)))

HEADER = """\
# The platform's component types, one per kind of repo (repos.yaml `openchoreo.component_type`).
# Built on OpenChoreo 1.3.3's own types (samples/getting-started/component-types): the image from
# the workload, a Service and HTTPRoutes per endpoint, and the component's configurations, secrets
# and dependencies injected as 1.3 does. The platform adds, everywhere: zero-downtime rollouts, zone
# spread, a disruption budget from 2 pods, graceful shutdown, health probes, and the repo's role as
# the pods' `valxel.io/role` label (the `role` parameter, set by the Valxel SDLC at registration),
# which the network policies select on. Each type takes `replicas` and `resources` per environment,
# set by the Valxel SDLC from the product's runtime/capacity.yaml; no CPU limit, deliberately.
#
# agent, code-sandbox, connector and scheduled-task are also locked down: non-root, read-only root
# file system (tmp only), no privileges to gain, no Kubernetes token unless they need the API, and
# egress only where they must go. PHI and payments agents and connectors run under gVisor.
#
# Builds use the platform's `docker` workflow only (build-workflow.yaml): signed, scanned, with
# provenance. Checked against OpenChoreo 1.3.3's CRD schemas; run `valxel dev cluster check` and a
# dry run against your installation before applying.
"""
OUT.write_text(HEADER + "\n" + yaml.safe_dump_all(types, sort_keys=False, width=110, allow_unicode=True))
print(f"{len(types)} types -> {OUT}")
