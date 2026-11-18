# Live smoke tests

The offline suite runs the tool with stand-ins. These tests run the **live clients**, the code that
talks to GitHub, the LLM gateway and the model API in a deployment, as they are, and swap only the
wire for a cassette of recorded exchanges (`valxel_sdlc/cassettes.py`). CI's `smoke` job runs them
on every pull request with the SDK versions the image ships (`requirements.lock`).

| What | Test |
| --- | --- |
| GitHub: a file on a branch, a labelled pull request, its checks, the merge | `test_github_*` |
| The gateway: a session token minted with its cap and models, then revoked | `test_gateway_*` |
| The model through the gateway: the call's labels and data class, usage read back | `test_model_*` |

A replayed request must be the one recorded: method, URL, body and the `x-valxel-*` headers. A
client that sends something else, skips a call or adds one fails.

## Recording

The first cassettes are **seeded** from the services' documented responses (`fakes.py`), and say so
in `_meta`. Replace them with real exchanges from the dev services:

```bash
VALXEL_SMOKE_RECORD=1 \
VALXEL_SMOKE_GITHUB_REPO=valxel-platform/sdlc-smoke VALXEL_SMOKE_GITHUB_TOKEN=... \
VALXEL_LLM_GATEWAY_URL=https://llm-gateway.dev... VALXEL_LLM_GATEWAY_API_KEY=... \
pytest tests/smoke
```

Re-record after a client change that changes what it sends (the test says which exchange differs).
Recording merges a pull request in the smoke repository; use a repository that holds nothing else.
Cassettes keep no secrets (headers are dropped, token-like fields masked) and only the tests' own
synthetic data.
