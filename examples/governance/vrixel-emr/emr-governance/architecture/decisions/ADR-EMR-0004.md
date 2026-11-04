---
id: ADR-EMR-0004
title: Product agents use models only through the EMR's LLM gateway
status: accepted
date: 2026-11-02
repos: []
---
# ADR-EMR-0004: Product agents use models only through the EMR's LLM gateway

## Context

Agents in the EMR handle PHI. Only model providers under a Business Associate Agreement may
receive it, and spend must be metered per agent.

## Decision

Agentic features call models only through the EMR's deployment of ss-llm-gateway, which allows
PHI only to BAA providers, holds the provider keys, redacts and meters spend per caller. Agents
reach product data only through MCP servers on an allowlist (platform articles VX-12 to VX-14).

## Consequences

- No provider API keys in agent repos.
- Adding a model provider is a gateway policy change, reviewed by security.
