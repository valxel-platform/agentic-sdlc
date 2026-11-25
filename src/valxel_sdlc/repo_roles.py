"""Repository roles. A role says what a repo is for, which template creates it, which checks
guard it and in which order a feature's changes to it merge and deploy (dependencies first).

  library     packages other repos use; no runtime
  provider    a service with a versioned API; changes records by publishing events to Kafka first
  event-worker  applies a product's events to a data store (Postgres, a search index), once and in order
  knowledge   retrieval over the product's own records for its agents: an index kept from the
              product's events, searched as MCP tools, filtered by who is asking (valxel_sdlc.retrieval)
  reporting   the product's data contracts into reporting tables and filtered topics (template-reporting)
  transform   dbt models over reporting's tables into marts, run on a schedule and published only when
              every test, control total and data-class check passes; after the reporting repos whose
              tables it reads (template-transform, valxel_sdlc.pipelines)
  job         work that runs on a schedule and exits: a CronJob with a run lock, run history and
              catch-up after missed runs; job.yaml holds its schedule (valxel_sdlc.jobs)
  document-intake  takes documents from people: checked uploads (size, format by content, malware),
              fields read through the LLM gateway, a review queue (valxel_sdlc.documents)
  channel     people talk to the product over Slack, Teams, SMS or email: provider signatures,
              identity linking, consent and opt-out (valxel_sdlc.channels)
  connector   the only component that talks to a third party: signed webhooks in, allowlisted calls
              out, as data contracts on Kafka; integration.yaml holds what's allowed (valxel_sdlc.connectors)
  mcp-server  product capabilities as MCP tools (often over providers' APIs), for agents
  code-sandbox  runs code agents write, one fresh isolated sandbox per run, as the MCP tool run_code
              (valxel_sdlc.sandboxes)
  agent       a workflow agent: uses MCP tools, models through the LLM gateway, people approve changes
  consumer    a web app or other client of the APIs and agents
  bff         the product's backend-for-frontend: browser sign-in, server-side tokens, its only
              external endpoint for the web app and its mobile apps
  mobile      an iOS and Android app (React Native, Expo): released through the stores in stages,
              not deployed (valxel_sdlc.store)
  acceptance  the product's end-to-end tests across repos (one per product); runs after each deploy
"""

from __future__ import annotations

ROLES = ("library", "provider", "event-worker", "knowledge", "reporting", "transform", "job", "connector", "document-intake", "channel", "mcp-server", "code-sandbox", "agent", "consumer", "bff", "mobile", "acceptance")
REPO_ORDER = {role: i for i, role in enumerate(ROLES)}
ROLE_PATTERN = "|".join(ROLES)
