---
name: platform
version: 1.0.0
---
# Vrixel platform constitution

Applies to every product. Product constitutions may tighten these articles, never loosen them.
Precedence: platform constitution, then product constitution, then ADRs, then PRD and task instructions.

- **VX-1** Humans own intent, architecture and production: G1, G4 and G6 are never automated away. (Human)
- **VX-2** Every artifact has an ID, its parents and the constitution version it was made under. (Policy)
- **VX-3** No agent holds production credentials or promotes to production. (Policy)
- **VX-4** Contracts change before code and stay backward compatible: expand, then contract. (Test)
- **VX-5** Every acceptance criterion maps to at least one automated test. (Policy)
- **VX-6** Reuse before build: check the capability catalog first; a new capability states why. (Rubric)
- **VX-7** PHI and payment data reach only approved model providers, stores and regions. (Policy)
- **VX-8** Every service ships with SLOs, structured logs and traces. (Policy)
- **VX-9** User-facing UI meets WCAG 2.1 AA. (Test, Rubric)
- **VX-10** Stay within the per-feature budget; escalate rather than overspend. (Policy)
