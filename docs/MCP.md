# MCP servers

ctswarm keeps its own registry of MCP servers, renders it into the two
configuration formats the CLI harnesses actually read, and mounts the result
into the agent containers. The registry is the authority; the host's own
`~/.claude.json` and `~/.codex/config.toml` are read exactly once, to seed it.

## Why it is not just inheritance

It used to be. The compose file bind-mounted the host's Claude and Codex
configuration into the agent containers wholesale, which had three consequences
worth naming:

- every build got every MCP server on the box, whether it needed one or not
- the per-build selection in the launch API was **decoration**. It changed a
  sentence in the prompt and nothing else, so an operator who deselected a
  server still had agents able to call it
- there was no way to add, edit, or remove a server without editing files on
  the host, which is not a thing you do from a phone

## The two formats are not equivalent

Verified against the shipped CLIs rather than taken from documentation, because
this is the part most likely to be wrong.

`claude mcp add --scope project` writes:

```json
{
  "mcpServers": {
    "demo": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@some/server"],
      "env": {"FOO": "bar"}
    },
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer x"}
    }
  }
}
```

`codex mcp add` writes:

```toml
[mcp_servers.demo]
command = "npx"
args = ["-y", "@some/server"]

[mcp_servers.demo.env]
FOO = "bar"

[mcp_servers.remote]
url = "https://example.com/mcp"
bearer_token_env_var = "MY_TOKEN"
```

Two differences matter in practice:

| | Claude Code | Codex |
|---|---|---|
| stdio | yes | yes |
| streamable HTTP | yes | yes |
| SSE | yes | **no** |
| per-server headers | yes | **no**, bearer token from an environment variable only |

So a server that authenticates with a custom header, or one that speaks SSE,
can be given to Claude Code and cannot be given to Codex. ctswarm does not
quietly drop those: `GET /api/mcp-servers/materialized` returns a `skipped`
list naming each one and why. A tool that is simply absent from one harness is
a build failure nobody can diagnose.

## Secrets

Secret values never enter the ledger and never leave the API.

- The registry entry stores the *names* of secret values (`secret_env`,
  `secret_headers`), never the values.
- Values live in `var/mcp-secrets.json`, created `0600`, beside the database.
  The ledger is world-readable by design and copied by every backup; a
  credential in there would be a credential in both.
- `PUT /api/mcp-servers/{id}/secrets` is write-only. No route reads one back.
  The settings screen is told *which* declared secrets have a value, so it can
  say that a server will fail on its first authenticated call.
- Deleting a server forgets its secrets, so re-adding the same id does not
  silently inherit an old credential.

Values are resolved at render time into the two config files, which are also
written `0600`.

## What happens on first start

`ensure_seeded` imports the host's existing servers into the registry once,
then records that it has done so. This matters because the rendered
configuration *replaces* the inherited one: without the import, upgrading would
silently take away every MCP server the operator already had. It never repeats,
so a server you deliberately removed does not come back on the next restart,
and an entry you edited is never reverted.

Import is additive and never overwrites. An id already in the registry is
reported as skipped.

## When a change takes effect

| Harness | Picks up a registry change |
|---|---|
| Codex | immediately; it re-reads `config.toml` on every invocation |
| Claude Code | when the agent containers restart |

The Claude CLI rewrites its own config during startup, so it cannot be given a
read-only mount directly. The container copies a read-only seed to ephemeral
storage at start instead, which means the copy is as old as the container.

```bash
./stack.sh restart      # restarts swe-agent and swe-fast only
```

## API

| Route | Purpose |
|---|---|
| `GET /api/mcp-servers` | the registry, with per-server secret status |
| `POST /api/mcp-servers` | add one |
| `PUT /api/mcp-servers/{id}` | replace one; the id in the path wins |
| `DELETE /api/mcp-servers/{id}` | remove it and forget its secrets |
| `PUT /api/mcp-servers/{id}/secrets` | write-only credential values; `""` clears one |
| `POST /api/mcp-servers/import` | adopt anything on the host not already registered |
| `GET /api/mcp-servers/discovered` | what the host has, flagged with `in_registry` |
| `GET /api/mcp-servers/materialized` | what each harness will actually see, plus `skipped` |

## Google Drive on a headless host

The Drive MCP server needs an OAuth consent flow, which needs a browser. On a
Pi reached only over SSH, run the consent once on a machine that has one, then
copy the resulting token into the registry as a secret value. Re-consent is
manual when the token is revoked; there is no way around that from a box with
no display.

## Files

| Path | Contents |
|---|---|
| ledger `mcp_registry_v1` | the registry, without secret values |
| `var/mcp-secrets.json` | secret values, `0600` |
| `var/mcp/claude.json` | rendered; mounted read-only into both agent services |
| `var/mcp/codex.toml` | rendered; mounted read-only into both agent services |

`var/` is gitignored, so neither the rendered configuration nor the secrets can
be committed.
