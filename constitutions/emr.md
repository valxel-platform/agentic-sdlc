---
name: emr
version: 1.0.0
---
# Healthcare EMR constitution

Starter draft. Regulatory articles need compliance counsel review before they are treated as complete.

- **EMR-1** PHI gets minimum-necessary access, encryption in transit and at rest, and an audit log of every read and write (HIPAA). (Policy, Test)
- **EMR-2** FHIR R4 is the contract at every external boundary; no proprietary formats leave the system. (Test)
- **EMR-3** AI output never enters the legal medical record without clinician confirmation. (Rubric, Test)
- **EMR-4** Patient data stays in the market and region its rules require (HIPAA in the US; ABDM and Indian data rules in India). (Policy)
- **EMR-5** Changes to clinical workflows need a clinician reviewer at G3. (Human)
- **EMR-6** Audit trails are append-only and tamper-evident. (Test)
- **EMR-7** Certification criteria (ONC, ABDM) are tracked as capabilities; touching one re-runs its conformance tests. (Test)
- **EMR-8** Every PR that touches PHI paths goes through G5. (Policy)
