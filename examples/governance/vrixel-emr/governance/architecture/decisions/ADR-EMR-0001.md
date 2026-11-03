---
id: ADR-EMR-0001
title: FHIR R4 at every system boundary
status: accepted
date: 2026-07-01
repos: [vrixel-emr/emr-intake-api, vrixel-emr/emr-web]
---
# ADR-EMR-0001: FHIR R4 at every system boundary

## Context

The EMR exchanges clinical data with clinics, labs, payers and national systems (ABDM in India),
all of which speak FHIR. Internal formats at the edges would need translation everywhere.

## Decision

Every API that crosses a system boundary uses FHIR R4 resources and their standard operations.
Internal service-to-service calls may use simpler shapes, but anything exposed outside a service
boundary is FHIR.

## Consequences

- New APIs model data as FHIR resources first (Patient, ServiceRequest, Consent, ...).
- Contract tests check FHIR conformance of the resources a provider returns.
