# OpenChoreo platform pieces for capacity plans

Applied once by the platform owners to the shared control plane. Written for OpenChoreo 1.3.3: the
component types and the project type are built on 1.3.3's own (samples/getting-started), and the
files pass 1.3.3's CRD schemas. Dry-run them on your installation before applying.

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
  - **Telemetry, the same on every cloud.** Every container sends OTLP/HTTP to
    `otel-collector.observability` in its data plane (platform-foundation
    `gitops/apps/product-telemetry`), with `OTEL_SERVICE_NAME` and resource attributes naming the
    image it runs (`service.version`), the environment, the product and its data class. Products
    can't point their traces elsewhere. Long-running pods carry `prometheus.io/scrape`, `port` and
    `path` annotations for the data plane's metrics collector. Every template serves metrics on the
    management port, 8081, the only port the mesh lets the collector reach. The Valxel SDLC sets the
    path from repos.yaml `openchoreo.metrics`: Spring's actuator for the Java templates, `/metrics`
    otherwise. A cluster without that collector just drops the spans.
  - Each builds only with the platform's `docker` workflow.
  - **Mesh callers.** Every component runs as its own service account, named for the component, so
    the mesh knows who is calling. Each type takes `allowedCallers` (`[{component, ports}]`) and
    `fromIngress`, and renders them as an Istio AuthorizationPolicy: only the listed components, in
    the same namespace (so dev can't call staging when both share a cluster), on those ports; the
    gateway reaches only a component with `fromIngress`, and never its metrics port. With neither
    set there is no rule, and the project type's deny-all holds.
  - **The rules are the product's, not the platform's.** Each product's security repo (`<prefix>security`
    in the product's org, made at the product gate from `template-security`) declares `calls` in
    `security.yaml`. Its security owners review it, and its CI runs platform-security's checker.
    When it merges, the Valxel SDLC sets each component's `allowedCallers` and `fromIngress`
    (valxel_sdlc.product_security), and the next release takes them through dev, staging and
    production with the code.
- `project-type.yaml`: the `valxel` ClusterProjectType. The Valxel SDLC creates every product
  project with it. Each environment's namespace joins the ambient mesh, with mTLS STRICT, a deny-all
  AuthorizationPolicy, and one allowance: the data plane's metrics collector (namespace
  `observability`) may reach port 8081. Everything else is a component's own `allowedCallers`.
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
