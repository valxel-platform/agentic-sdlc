# Healthcare EMR: architecture overview

The current design, kept by the product's architects. Agents read this before every design; it
changes through pull requests like any governance file. Decisions are in `decisions/`.

## Context

AI-native electronic medical records for US and India clinics. Users: clinicians, front-desk
staff and patients. Every system boundary speaks FHIR R4. Regulation: HIPAA in the US, ABDM in
India. All patient data is PHI.

## Building blocks

| Repository | Role | Owns |
| --- | --- | --- |
| vrixel-emr/emr-intake-api | provider | Patient intake: registration, referrals, demographics (FHIR Patient, ServiceRequest) |
| vrixel-emr/emr-web | consumer | The clinic web app for front-desk staff and clinicians |

Shared services (`SS` lane) provide platform packages, including `ss-llm-gateway`, which this
product deploys in its own OpenChoreo project `emr`. Payments go through APX (`APX` lane).

## Key flows

- A front-desk user registers a patient in emr-web; emr-web calls emr-intake-api with the user's
  OAuth token; emr-intake-api stores the FHIR Patient resource.
- Product agents reach models only through the EMR deployment of the LLM gateway.

## Change log
