"""The laptop tier's AWS on moto, end to end through OpenChoreo (tracker CP-118): after
`valxel dev cluster up --aws`, check that

1. a Resource the tool creates and binds through OpenChoreo's API, with the tool's own client
   (OpenChoreoMcp.apply_resource and bind_resource, as the pipeline does after G4), becomes a
   DynamoDB table in moto: OpenChoreo -> ClusterResourceType -> Crossplane -> moto;
2. a component deployed through OpenChoreo reaches moto from its pod (the valxel-local-aws trait)
   and sees that table.

    python scripts/laptop_aws_e2e.py            # .github/workflows/laptop-aws.yml runs it

Needs the laptop cluster (`valxel dev cluster up --aws`), kubectl, and boto3."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import boto3

from valxel_sdlc.config import load_settings
from valxel_sdlc.laptop import cluster
from valxel_sdlc.services import openchoreo_client

PROJECT, ENV = "default", "development"  # what OpenChoreo's k3d install sets up
PIPELINE = ("development", "staging", "production")
TABLE, COMPONENT = "e2e-orders", "e2e-awscli"
MOTO = "http://localhost:5050"  # the bundle's moto, published for this machine


def kubectl(*args: str, input: str | None = None, check: bool = True) -> str:
    # kubectl from PATH, as `valxel dev cluster` uses it, with arguments this script writes.
    got = subprocess.run(["kubectl", "--context", cluster.CONTEXT, *args], input=input, text=True,  # noqa: S603, S607
                         capture_output=True, check=False)
    if check and got.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {got.stderr.strip()}")
    return got.stdout


def wait(what: str, probe, *, minutes: int = 15, every: int = 10):
    end = time.time() + minutes * 60
    last = None
    while time.time() < end:
        last = probe()
        if last:
            print(f"ok: {what}", flush=True)
            return last
        time.sleep(every)
    raise SystemExit(f"timed out after {minutes} min: {what}")


def resource_through_openchoreo() -> str:
    os.environ.update(cluster.SETTINGS)
    oc = openchoreo_client(load_settings())
    params = {"partition_key": "id", "sort_key": "", "ttl_attribute": "", "indexes": [], "consumers": [],
              "tags": {"valxel:product": "E2E", "valxel:feature": "E2E-FEAT-1", "valxel:data-class": "general"}}
    print(oc.apply_resource(name=TABLE, type="key-value-table", parameters=params, project=PROJECT), flush=True)
    # The Resource controller cuts its first release asynchronously.
    wait("the resource has a release",
         lambda: oc.apply_resource(name=TABLE, type="key-value-table", parameters=params, project=PROJECT)["release"])
    print(oc.bind_resource(name=TABLE, env=ENV, size="small", actor="ci", actor_type="human", project=PROJECT,
                           pipeline=PIPELINE), flush=True)

    def ready():
        st = oc.resource_status(name=TABLE, env=ENV, project=PROJECT)
        print(f"  {TABLE} in {ENV}: {st}", flush=True)
        if st["state"] == "failed":
            raise SystemExit(f"binding failed: {st['message']}")
        return st["state"] == "ready"

    wait(f"{TABLE} is ready in {ENV} (OpenChoreo says so)", ready, minutes=20, every=15)
    # Moto takes any credentials; these are placeholders, as in the laptop's .env.
    ddb = boto3.client("dynamodb", endpoint_url=MOTO, region_name="us-east-1",
                       aws_access_key_id="local", aws_secret_access_key="local")  # noqa: S106
    tables = ddb.list_tables()["TableNames"]
    mine = [t for t in tables if TABLE in t]
    if not mine:
        raise SystemExit(f"OpenChoreo says ready but moto has no {TABLE} table: {tables}")
    print(f"ok: moto has {mine[0]}", flush=True)
    return mine[0]


COMPONENT_YAML = f"""
apiVersion: openchoreo.dev/v1alpha1
kind: Component
metadata:
  name: {COMPONENT}
  namespace: default
spec:
  owner:
    projectName: {PROJECT}
  autoDeploy: true
  componentType:
    kind: ClusterComponentType
    name: deployment/worker
---
apiVersion: openchoreo.dev/v1alpha1
kind: Workload
metadata:
  name: {COMPONENT}-workload
  namespace: default
spec:
  owner:
    componentName: {COMPONENT}
    projectName: {PROJECT}
  container:
    image: public.ecr.aws/aws-cli/aws-cli:2.27.50
    command: ["sleep"]
    args: ["3600"]
"""


def pod_reaches_moto(table: str) -> None:
    kubectl("apply", "-f", "-", input=COMPONENT_YAML)

    def deployment():
        for d in json.loads(kubectl("get", "deployments", "-A", "-o", "json"))["items"]:
            if d["metadata"]["name"].startswith(COMPONENT) and not d["metadata"]["namespace"].startswith("openchoreo"):
                return d
        return None

    d = wait(f"OpenChoreo deployed {COMPONENT}", deployment)
    ns, name = d["metadata"]["namespace"], d["metadata"]["name"]
    main = next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "main")
    env = {e["name"]: e.get("value") for e in main.get("env") or []}
    if "AWS_ENDPOINT_URL" not in env:
        raise SystemExit(f"{ns}/{name}'s main container has no AWS_ENDPOINT_URL (the trait didn't apply): {env}")
    print(f"ok: {ns}/{name} gets AWS_ENDPOINT_URL={env['AWS_ENDPOINT_URL']}", flush=True)
    kubectl("-n", ns, "rollout", "status", f"deployment/{name}", "--timeout=10m")
    out = kubectl("-n", ns, "exec", f"deployment/{name}", "-c", "main", "--", "aws", "dynamodb", "list-tables",
                  "--output", "json")
    if table not in json.loads(out)["TableNames"]:
        raise SystemExit(f"the pod reached an AWS without {table}: {out}")
    print(f"ok: the pod lists {table} in moto", flush=True)


def main() -> int:
    table = resource_through_openchoreo()
    pod_reaches_moto(table)
    print("The tool -> OpenChoreo -> Crossplane -> moto, and a component's pod -> moto: all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
