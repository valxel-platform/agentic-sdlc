---
name: cln
version: 1.0.0
---
# Clinic records constitution

Starter draft. Regulatory articles need compliance counsel review before they are treated as complete.

- **CLN-1** PHI gets minimum-necessary access, encryption in transit and at rest, and an audit log of every read and write (HIPAA). (Policy, Test)
- **CLN-2** FHIR R4 is the contract at every external boundary; no proprietary formats leave the system. (Test)
- **CLN-3** AI output never enters the legal medical record without clinician confirmation. (Rubric, Test)
- **CLN-4** Patient data stays in the market and region its rules require (HIPAA, and state and national data residency rules). (Policy)
- **CLN-5** Changes to clinical workflows need a clinician reviewer at G3. (Human)
- **CLN-6** Audit trails are append-only and tamper-evident. (Test)
- **CLN-7** Certification criteria (ONC) are tracked as capabilities; touching one re-runs its conformance tests. (Test)
- **CLN-8** Every PR that touches PHI paths goes through G5. (Policy)
