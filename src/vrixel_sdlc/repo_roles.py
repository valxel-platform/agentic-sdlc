"""Repository roles. A role says what a repo is for, which template creates it, which checks
guard it and in which order a feature's changes to it merge and deploy (dependencies first).

  library     packages other repos use; no runtime
  provider    a service with a versioned API; changes records by publishing events to Kafka first
  event-worker  applies a product's events to a data store (Postgres, a search index), once and in order
  mcp-server  product capabilities as MCP tools (often over providers' APIs), for agents
  agent       a workflow agent: uses MCP tools, models through the LLM gateway, people approve changes
  consumer    a web app or other client of the APIs and agents
  bff         the product's backend-for-frontend: browser sign-in, server-side tokens, its only
              external endpoint for the web app
  acceptance  the product's end-to-end tests across repos (one per product); runs after each deploy
"""

from __future__ import annotations

ROLES = ("library", "provider", "event-worker", "mcp-server", "agent", "consumer", "bff", "acceptance")
REPO_ORDER = {role: i for i, role in enumerate(ROLES)}
ROLE_PATTERN = "|".join(ROLES)
