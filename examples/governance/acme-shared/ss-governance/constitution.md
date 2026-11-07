---
name: shared-services
version: 1.0.0
---
# Shared services constitution

Applies to every change in a acme-shared repo, whichever product's feature makes it.

- **SS-1** Shared services ship as versioned libraries, SDKs or deployable charts; each consuming product runs them in its own runtime. (Policy)
- **SS-2** Semantic versioning: a breaking change needs a new major version and a deprecation window; consumers adopt new versions through their own change sets. (Test)
- **SS-3** No product-specific logic in shared code; products vary behavior through configuration. (Rubric)
- **SS-4** Every shared service publishes contract tests that consuming products run in their pipelines. (Test)
- **SS-5** Audit framework events are append-only and tamper-evident, and record who, what, when and why; event schemas stay backward compatible. (Test)
- **SS-6** Security components (authentication, authorization, secrets, cryptography) use vetted libraries only, never custom cryptography, and every change goes through G5. (Policy)
- **SS-7** Communication services honor opt-outs and channel rules, and keep PHI out of message bodies unless the channel is approved for it. (Test)
