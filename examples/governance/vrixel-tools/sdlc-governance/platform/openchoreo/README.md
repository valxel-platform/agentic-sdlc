# OpenChoreo platform pieces for capacity plans

Applied once by the platform owners to the shared control plane. Written against the OpenChoreo 1.x
documentation (ComponentType `parameters` and `environmentConfigs`, templates with `${...}`, per
environment values in each ReleaseBinding); dry-run them on your installation before applying,
since field names can differ between releases.

- `component-types.yaml`: service, bff, web app and event worker, and `agent`: an agent's pods hold no Kubernetes token, write only to /tmp, run non-root with no added privileges, reach only the product's LLM gateway, MCP servers, database and event brokers, and (PHI and payments) run under gVisor, set per data class by `isolation` in repos.yaml. Data planes running PHI or payments agents need the gVisor RuntimeClass installed (`gvisor`). Each takes `replicas` and
  `resources` per environment (set by the Vrixel SDLC from the product's runtime/capacity.yaml),
  spreads pods across zones, adds a disruption budget when 2 or more pods run, and sets health
  probes and graceful shutdown.
- `autoscaler.yaml`: the `autoscaler` ClusterTrait, a HorizontalPodAutoscaler on CPU. The tool adds
  an instance to each component whose plan has a range (`<component>-autoscaler`) and sets its
  minimum and maximum per environment.
- `build-workflow.yaml`: the image build every component uses: build, push by digest, scan (fails on
  high or critical CVEs), CycloneDX SBOM, cosign signature and SLSA provenance, signed with a KMS key
  only the workflow plane can use. The tool reads its steps by name (suites `container` and
  `provenance`, both required).
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
  `VRIXEL_SIGNALS_TOKEN`). Running images' high and critical vulnerabilities then open incidents.
- The images inside `build-workflow.yaml` are pinned by tag with `REPLACE_WITH_DIGEST` markers: run
  the tool's `.github/scripts/pin_images.py` approach (or `docker buildx imagetools inspect`) once
  where the registries are reachable, and keep them pinned.
