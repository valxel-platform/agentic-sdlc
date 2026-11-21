# OpenChoreo platform pieces for capacity plans

Applied once by the platform owners to the shared control plane. Written for OpenChoreo 1.3.3: the
component types are built on 1.3.3's own (samples/getting-started), and both files pass 1.3.3's CRD
schemas. Dry-run them on your installation before applying.

- `component-types.yaml`: one type per kind of repo that repos.yaml names: `service` (services, BFFs,
  MCP servers, LLM gateways), `web-app`, `worker` (no endpoints), `agent`, `code-sandbox`,
  `connector` and `scheduled-task`.
  - **As 1.3 does:** each reads the image from the workload (`workload.container.image`), serves its
    endpoints through a Service and HTTPRoutes, and gets its configurations, secrets and
    dependencies injected.
  - **What the platform adds:**
    - zero-downtime rollouts, zone spread, a disruption budget from 2 pods, graceful shutdown and
      health probes;
    - no CPU limits;
    - the repo's role as the pods' `valxel.io/role` label, from the `role` parameter the Valxel SDLC
      sets when it registers the component. The network policies select on it.
  - **Agents** hold no Kubernetes token, write only to /tmp, run non-root with no added privileges,
    and reach only the product's LLM gateway, MCP servers, code sandbox, database and event brokers.
    PHI and payments agents run under gVisor (`isolation` per data class in repos.yaml), and so do
    PHI and payments connectors. Data planes running them need the gVisor RuntimeClass (`gvisor`).
  - Each type takes `replicas` and `resources` per environment, set by the Valxel SDLC from the
    product's runtime/capacity.yaml.
  - Each builds only with the platform's `docker` workflow.
- `autoscaler.yaml`: the `autoscaler` ClusterTrait, a HorizontalPodAutoscaler on CPU. The tool adds
  an instance to each component whose plan has a range (`<component>-autoscaler`) and sets its
  minimum and maximum per environment.
- `build-workflow.yaml`: the `docker` ClusterWorkflow every component builds with.
  - **Steps:** build, push by digest, scan (fails on high or critical CVEs), CycloneDX SBOM, cosign
    signature and SLSA provenance, all signed with a KMS key only the workflow plane can use.
  - **Last step:** OpenChoreo's own `generate-workload`, which makes the built digest deployable.
  - **Parameters:** OpenChoreo 1.3's `repository` {url, secretRef, revision {branch, commit},
    appPath}, plus `registry`. Set `registry`'s default to the platform's registry on install.
  - The tool reads the steps by name (suites `container` and `provenance`, both required).
- Logs and metrics: OpenChoreo 1.3 serves them from the observability plane's own MCP server
  (`openchoreo-observer`). Set `VALXEL_OPENCHOREO_OBSERVER_MCP_URL` (same credentials) so incident
  triage reads a component's logs, resource metrics and HTTP metrics. `valxel dev cluster check`
  checks its tools.
- `admission-policy.yaml`: Kyverno on every data plane: only images signed by that key, with
  provenance naming one of the product's own repositories and an SBOM; pods run the verified digest.
  The chain: the pipeline builds only merge commits that passed the gates, the build signs what it
  built, the data planes run only what it signed.
- `clusters.yaml`: the template for a product's clusters (two data planes and its production
  observability plane), which the tool fills in at the product gate and proposes to the platform's
  infrastructure repo. Replace the `kind` with whatever your infrastructure code provisions.
- Image rescans (not a manifest): turn on ECR enhanced scanning with continuous rescans for the
  product registries, and an EventBridge rule on `{"source": ["aws.inspector2"], "detail-type":
  ["Inspector2 Finding"]}` with an API destination to the tool's `/api/signals/inspector` (Bearer
  `VALXEL_SIGNALS_TOKEN`). Running images' high and critical vulnerabilities then open incidents.
- The images inside `build-workflow.yaml` are pinned by tag with `REPLACE_WITH_DIGEST` markers: run
  the tool's `.github/scripts/pin_images.py` approach (or `docker buildx imagetools inspect`) once
  where the registries are reachable, and keep them pinned.
