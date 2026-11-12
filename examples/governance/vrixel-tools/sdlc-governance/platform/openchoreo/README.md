# OpenChoreo platform pieces for capacity plans

Applied once by the platform owners to the shared control plane. Written against the OpenChoreo 1.x
documentation (ComponentType `parameters` and `environmentConfigs`, templates with `${...}`, per
environment values in each ReleaseBinding); dry-run them on your installation before applying,
since field names can differ between releases.

- `component-types.yaml`: service, bff, web app and event worker. Each takes `replicas` and
  `resources` per environment (set by the Vrixel SDLC from the product's runtime/capacity.yaml),
  spreads pods across zones, adds a disruption budget when 2 or more pods run, and sets health
  probes and graceful shutdown.
- `autoscaler.yaml`: the `autoscaler` ClusterTrait, a HorizontalPodAutoscaler on CPU. The tool adds
  an instance to each component whose plan has a range (`<component>-autoscaler`) and sets its
  minimum and maximum per environment.
- `clusters.yaml`: the template for a product's clusters (two data planes and its production
  observability plane), which the tool fills in at the product gate and proposes to the platform's
  infrastructure repo. Replace the `kind` with whatever your infrastructure code provisions.
