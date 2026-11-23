# 0010. One cloud per installation, reached through ports in the core

Date: 2026-11-23
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The tool runs on AWS, and the products it builds run on AWS too. Customers on Google Cloud and
Azure want the same: the tool on their cloud, and their products built on that cloud's services.
Nobody has asked for one installation that plans across clouds.

AWS reaches into the core in nine places: the secret store and OAuth vault (Secrets Manager),
GitHub App signing (KMS), the audit anchors and checkpoint archive (S3 Object Lock), sign-in
(Cognito), customer accounts (STS AssumeRole), costs (Cost Explorer), inventory (the tagging API),
signals (CloudWatch, EventBridge, Budgets, Inspector) and the resource catalog. Some sit behind an
interface already (`SecretStore`, `Signer`, `AnchorStore`, `CostSource`); others call boto3 from
the middle of the code. Adding a second cloud by `if cloud == ...` in those places would spread
three SDKs through the core.

## Decision

- **One cloud per installation.** `VALXEL_CLOUD` (`aws`, `gcp` or `azure`; `aws` by default)
  names it, and every product the installation builds runs on that cloud. A product's
  `runtime.cloud` (`provider`, `region`, `accounts`) must name the installation's cloud. The older
  `runtime.aws` still reads, as `provider: aws`.
- **Ports in the core.** `valxel_sdlc.cloud.ports` defines what the core needs from a cloud:
  `SecretStore`, `Signer`, `AnchorStore`, `ImmutableStore`, `IdentityProvider`, `CloudAccounts`,
  `CostSource`, `ResourceInventory` and `SignalSource`. The core uses only these.
- **Adapters per cloud.** Each cloud's adapters live in `valxel_sdlc/cloud/<provider>/`, installed
  with that cloud's extra. A cloud's SDK is imported nowhere else: config/seams.yaml's `cloud_sdk`
  rule fails CI on a new import, and lists today's as debt to move into `cloud/aws/`.
- **Conformance, not trust.** `cloud/fakes.py` has an in-memory implementation of every port and
  `cloud/conformance.py` the behavior each must show (a rotation keeps the previous secret; an
  immutable object can't be rewritten; a foreign token is refused). The fakes pass in every test
  run; each adapter passes the same checks, recorded in CI and live against its cloud nightly.
- **The resource catalog becomes cloud-neutral.** Types such as `key-value-table` and `queue`
  keep one contract, with a block per cloud saying how it is built and priced. This lands with the
  second cloud's compositions.
- **Strict mode** refuses an unknown cloud, and a cloud that has no adapters yet.

## Alternatives

- **Branch on the cloud where it's used.** Cheapest for the second cloud, worst for the third: every
  feature then knows three SDKs, and nothing stops a fourth branch from creeping in.
- **A multi-cloud abstraction library (Libcloud, Pulumi's or Crossplane's providers) in the core.**
  They cover provisioning, not what the core needs (signing, immutable archives, identity, cost
  data), and add a large dependency. Crossplane stays where it is: in the platform's compositions.
- **Run the tool on Kubernetes primitives only, with no cloud services.** Possible for secrets
  (mounted files) and signing (a PEM key), and those stay as options. It gives up what the cloud
  does better: keys that never leave a KMS, Object Lock archives, cost data.

## Consequences

- The core gets smaller and testable without a cloud. Each new cloud is adapters, compositions and
  a connect template, with no change to the core.
- Every port change needs every adapter and the fakes updated, and the conformance checks with them.
- Existing AWS installations change nothing: `VALXEL_CLOUD` defaults to `aws`, `runtime.aws` and the
  chart's `awsRegion` still read.
- The order is Google Cloud second, then Azure. Revisit if a customer needs a cloud outside the three.
