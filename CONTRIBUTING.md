# Contributing to talyx

Bug reports, documentation fixes and small improvements are welcome. For a new metric
or a larger change, [open an issue](https://github.com/alan66603/talyx-mcp/issues) first
so we can agree on the scope.

## Set up

You need Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Fork the repository,
clone your fork, and from the checkout run:

```bash
uv sync --locked
uv run talyx -- python -m talyx.mock.server
```

The mock server is the main test target. Few real servers speak the `2026-07-28`
protocol yet, so the mock is the only reliable way to produce Multi Round-Trip cycles.
Its `cycle` tool takes `kind` (`input`, `poll` or `reject`) and `rounds`. For an
abandoned cycle, call the tool and never send the follow-up. If you add a metric the
mock cannot trigger, extend the mock in the same pull request.

## Check your work

Run these before you commit. CI runs the first two on every pull request.

```bash
uv run ruff check .
uv run pytest
promtool test rules alerts/mcp-rules.test.yaml   # if you changed alerts/
```

Tests set `TALYX_HUB=off` so nothing starts a real hub. Tests that touch client
settings, such as `talyx wrap`, must use a temporary home directory, never your
real `~/.claude.json` or `~/.talyx`.

## What fits the project

talyx is a monitoring layer for MCP servers: metrics and alerts. It is not a gateway.
These are out of scope, and pull requests that add them will be closed:

- authentication, RBAC or credential handling
- rate limiting, policy or tool filtering
- CSP rewriting
- routing to several upstream servers

These belong in a gateway. talyx is designed to run behind one (see
[Behind a gateway](README.md#behind-a-gateway)).

## Rules for metrics

- **Check that talyx can see the signal.** talyx only sees the JSON-RPC traffic between
  client and server. Something the client raises in its own process, such as hitting
  `input_required_max_rounds`, never reaches the pipe, so talyx cannot count it. A
  dedicated error type in the SDK does not mean the error shows up on the wire.
- **Use the wire values, not the blog posts.** A cycle step has
  `resultType == "input_required"`, with an underscore. Field names are camelCase:
  `resultType`, `inputRequests`, `requestState`. If `talyx_input_required_total` stays
  at 0 while the mock produces cycles, check this first.
- **Never record argument contents.** talyx reads the method and tool name from the
  JSON-RPC body, and nothing else from `params`.
- **Hash correlation keys in memory.** `requestState` is used only as a hash. Its raw
  value must not reach a metric label, a log line or a trace attribute.
- **Keep label values bounded.** Do not put IDs, hashes or free text in labels.

When you write about privacy, say that talyx "does not record tool argument contents".
Do not say it reads no message bodies: on stdio it has to read the method name from
the body.

## Open a pull request

Keep each pull request to one change. Explain the problem, what changes for the user
and how you checked it. If you add or change a metric or an alert, update
[docs/metrics.md](docs/metrics.md) and the README, and their `zh-TW` versions where they
exist.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
