---
id: ADR-EMR-0003
title: Every API call carries an OAuth 2.0 bearer token from the product's identity provider
status: accepted
date: 2026-11-02
repos: [vrixel-emr/emr-intake-api]
---
# ADR-EMR-0003: Every API call carries an OAuth 2.0 bearer token from the product's identity provider

## Context

All EMR data is PHI. Network location is not identity, and services are reached through the
OpenChoreo gateway and by agents.

## Decision

Every service is an OAuth 2.0 resource server: it accepts only JWTs from the product's identity
provider, checks audience and scopes, and leaves only health probes open. People use
authorization code with PKCE; services and agents use client credentials.

## Consequences

- No API keys or shared secrets between services.
- Contract tests run with security off; security tests run with it on.
