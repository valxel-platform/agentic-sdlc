---
id: ADR-CLN-0002
title: Services in Java 21 and Spring Boot; Rust only for measured hot paths
status: accepted
date: 2026-11-02
repos: [acme-clinic/cln-intake-api]
---
# ADR-CLN-0002: Services in Java 21 and Spring Boot; Rust only for measured hot paths

## Context

The team's experience, FHIR libraries (HAPI FHIR), security and observability tooling are
strongest on the JVM. Rust gives lower latency and memory use, at a higher cost to build and hire for.

## Decision

New services start from valxel-tools/template-service (Java 21, Spring Boot). A component moves to
Rust only when a measured hot path misses its SLO on the JVM, and an ADR records the measurement.

## Consequences

- One stack for services: shared templates, libraries and on-call knowledge.
- Agentic components are the exception: they use the Python templates (template-agent, template-mcp-server).
