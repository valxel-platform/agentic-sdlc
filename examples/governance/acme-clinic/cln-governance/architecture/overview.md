# Clinic records: architecture overview

The current design, kept by the product's architects. Agents read this before every design; it
changes through pull requests like any governance file. Decisions are in `decisions/`.

## Context

Patient records for outpatient clinics. Users: clinicians, front-desk
staff and patients. Every system boundary speaks FHIR R4. Regulation: HIPAA. All patient data
is PHI.

## Building blocks

| Repository | Role | Owns |
| --- | --- | --- |
| acme-clinic/cln-intake-api | provider | Patient intake: registration, referrals, demographics (FHIR Patient, ServiceRequest) |
| acme-clinic/cln-web | consumer | The clinic web app for front-desk staff and clinicians |

Shared services (`SS` lane) provide platform packages, including `ss-llm-gateway`, which this
product deploys in its own OpenChoreo project `cln`. Payments go through PAY (`PAY` lane).

## Key flows

- A front-desk user registers a patient in cln-web; cln-web calls cln-intake-api with the user's
  OAuth token; cln-intake-api stores the FHIR Patient resource.
- Product agents reach models only through the CLN deployment of the LLM gateway.

## Change log
