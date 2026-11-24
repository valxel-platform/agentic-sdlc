# AWS on your machine (moto)

The laptop tier can run with AWS without an AWS account. Every AWS call goes to
[moto](https://github.com/getmoto/moto), a local AWS emulator: the tool's own calls (archives,
connecting a customer's account, costs, inventory) and, with a local OpenChoreo, the AWS resources
a feature asks for, which go OpenChoreo → Crossplane → moto exactly as they go to AWS on the
platform. Tracker CP-116 (the tool) and CP-118 (the cluster); the cluster side is
platform-resources' `local/aws`, which its `local-aws` workflow checks on every change (CP-117).

```
        the tool (laptop compose)                     k3d cluster (valxel dev cluster up)
  api / worker ── AWS_ENDPOINT_URL ──┐          OpenChoreo ── ClusterResourceType
                                     ▼                              │ renders an XR
                              moto (valxel-moto) ◄── Crossplane AWS providers
                          127.0.0.1:5050 for you        (platform-resources local/aws)
```

## The tool on moto

```sh
valxel dev aws on          # AWS settings into .env: VALXEL_CLOUD=aws, the endpoint http://moto:5000, ...
valxel dev up              # starts moto beside the tool and seeds it (the archive buckets)
valxel dev aws connect acme          # connect workspace acme to moto's account (123456789012)
aws --endpoint-url http://localhost:5050 s3 ls       # look inside moto (any credentials)
```

What you can try:

- **Archives.** Checkpoints and audit anchors go to Object Lock buckets in moto
  (`valxel-local-checkpoints`, `valxel-local-anchors`).
- **Connecting an AWS account.** `valxel dev aws connect <workspace> [lite|full]` does what a
  customer does in the console (deploys `deploy/customer-stack.yaml` with the workspace's external
  ID) and then what the tool does (assumes the deploy and agents roles). The workspace then shows
  AWS as connected.
- **Inventory and costs.** The tagging API reads whatever is tagged `valxel:product` in moto, such
  as what Crossplane made. Cost Explorer answers with no data unless you script its rows (moto's
  `/moto-api/static/ce/cost-and-usage-results`; tests/test_aws_emulated.py shows how).
- **Signals.** Moto doesn't deliver CloudWatch, Inspector or budget events. Post a recorded payload
  to `/api/signals/cloudwatch` (or `inspector`, `aws-budgets`) with the signals token instead.

`valxel dev aws off`, then `valxel dev up`, goes back to no AWS.

## The tool, OpenChoreo and Crossplane on moto

```sh
valxel dev aws on
valxel dev cluster up --aws   # OpenChoreo, then Crossplane and the platform's AWS catalog, pointed at moto
valxel dev up                 # the tool, on http://localhost:8090
```

`cluster up --aws` starts moto, joins it to the cluster's Docker network, and runs
platform-resources' `local/aws/install.py` at a pinned commit (`VALXEL_PLATFORM_RESOURCES=<a
checkout>` uses yours instead): the Crossplane functions and AWS providers, a ProviderConfig whose
endpoint is moto, the local data plane's settings, the XRDs and Compositions, and OpenChoreo's
ClusterResourceTypes. A feature whose design asks for a table, bucket, queue, topic or stream then
gets it in moto, with its components' IAM roles and policies and its alarms, and the tool's
inventory sees it there.

Components' pods reach the same moto: the `valxel-local-aws` trait, embedded in every component type,
adds `AWS_ENDPOINT_URL` (moto's Service), placeholder credentials and the region to each
component's `main` container. A product's AWS SDK calls go to moto with no change to its code or
config, as on EKS they go to AWS through Pod Identity.

## What differs from AWS

Moto answers AWS's APIs; it doesn't enforce what AWS enforces. Passing here shows the flows and the
Compositions work end to end, not that the access they grant is right.

| On AWS | On moto |
| --- | --- |
| Object Lock COMPLIANCE can't be shortened | recorded, not enforced |
| The connect roles refuse a wrong external ID | any external ID assumes them |
| IAM and KMS key policies decide every call | any credentials do anything |
| The GitHub Apps' keys are imported into KMS (BYOK) | not possible: GitHub calls sign with the PEM, as on any laptop |
| Pods get credentials from EKS Pod Identity | workload identity makes the role only (no EKS); the `valxel-local-aws` trait gives pods moto's endpoint and placeholder credentials |
| Kafka topics and users on MSK | not installed |
| PostgreSQL and cache through OpenChoreo's own templates (RDS, ElastiCache) | not installed |
| A stack update changes it in place | a second `connect` replaces the stack |
| Everything persists | moto keeps everything in memory: `valxel dev up` seeds again, Crossplane makes its resources again |

Those are checked against a real AWS sandbox account (CP-119). Strict mode refuses an AWS endpoint
override, so none of this reaches a shared installation.

## The checks

- `tests/test_aws_emulated.py` runs the AWS adapters through the same conformance checks
  (`valxel_sdlc.cloud.conformance`) the GCP nightly job runs against a real project, against an
  in-process moto, on every pull request.
- platform-resources' `local-aws` workflow builds k3d, Crossplane and moto, installs the AWS
  catalog with `local/aws/install.py`, applies a feature's resources as OpenChoreo renders them, and
  checks moto holds what they built.
- This repo's `laptop-aws` workflow runs what a developer runs (`valxel dev init`, `aws on`,
  `cluster up --aws`) and then `scripts/laptop_aws_e2e.py`: a Resource the tool creates and binds
  through OpenChoreo's API with its own client becomes a table in moto, and a component deployed
  through OpenChoreo lists that table from its pod. On changes to the laptop tier, and weekly.
