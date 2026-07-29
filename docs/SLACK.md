# Slack approvals setup

Slack is the intended approval surface: interactive buttons, mobile push,
threads, and a searchable decision history that doubles as a business audit
trail.

**None of this is required to run ctswarm.** The local approval UI on
`http://localhost:8091` is always available and handles the identical decision
flow. Set Slack up when you want approvals to reach your phone. Until then,
nothing is blocked and nothing is silently dropped.

This takes about ten minutes and needs a Slack workspace you can install an app
into.

---

## 1. Create the app

1. Go to <https://api.slack.com/apps> and choose **Create New App** then
   **From scratch**.
2. Name it `ctswarm` and pick your workspace.

## 2. Bot token scopes

Under **OAuth & Permissions** add these **Bot Token Scopes**:

| Scope | Why |
|---|---|
| `chat:write` | Post approval cards |
| `chat:write.public` | Post to a channel the bot has not been invited to |

Nothing else. The bot never needs to read messages, and a token that can read
your workspace is a much larger thing to leak than one that can only post.

## 3. Install and collect credentials

1. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`).
2. From **Basic Information**, copy the **Signing Secret**.
3. Create a private channel, for example `#ai-factory-approvals`, and copy its
   channel ID (channel name, right-click, **View channel details**, bottom of the
   dialog).

Put all three in `.env`:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_APPROVAL_CHANNEL=C0123456789
```

`SLACK_SIGNING_SECRET` is **mandatory**, not optional. The interactivity endpoint
decides whether irreversible actions proceed, so ctswarm refuses to process an
unsigned callback and returns 503 rather than trusting it.

## 4. Interactivity endpoint

Slack must reach your approval service to deliver button clicks, which means a
publicly resolvable HTTPS URL. Two reasonable options:

**Tailscale Funnel** (preferred: no third-party tunnel, stable hostname)

```bash
tailscale funnel 8091
# -> https://<host>.<tailnet>.ts.net
```

**ngrok** (fine for a trial)

```bash
ngrok http 8091
```

Then in **Interactivity & Shortcuts**, turn interactivity **On** and set the
Request URL to:

```
https://<your-public-host>/slack/interactivity
```

Slack sends a verification request immediately. It will fail until the approval
service is running, so start it first:

```bash
docker compose -f vendor/SWE-AF/docker-compose.yml \
               -f infra/docker-compose.ctswarm.yml up -d ctswarm-approvals
```

## 5. Verify

```bash
curl localhost:8091/health
# {"ok":true,"slack_configured":true,"local_ui":true,"pending":0}
```

Send a test card:

```bash
curl -X POST localhost:8091/approval/request \
  -H 'content-type: application/json' \
  -d '{"action":"deploy","detail":"deploy to production",
       "build_id":"slack-test","repo":"you/your-repo",
       "recommendation":"Deny; this is only a delivery test."}'
```

A card should appear in your channel with Approve, Deny, Request changes, and
Pause build. Click **Deny**. The card should update to show the resolution, and:

```bash
curl localhost:8091/approval/status/<dedupe_key>
# {"resolved":true,"decision":"deny",...}
```

## Security notes

- The signing secret is the only thing standing between the public internet and
  your approval buttons. Treat it like a production credential and rotate it if
  a tunnel URL was ever shared.
- Tunnels expose port 8091 only. The router on 8090 stays bound to loopback and
  must never be tunnelled; it fronts local model endpoints.
- Approve buttons for irreversible actions carry a confirmation dialog, because a
  misclick on a production deploy has no undo.
- Decisions are append-only in both Slack and the local UI. A second click on a
  resolved card is rejected rather than silently overwriting the first decision.

## If Slack is unavailable

The build does not proceed. An approval that cannot be delivered is not an
approval that was granted, so an undelivered request stays pending and expires to
**pause**, never to approve. Resolve it in the local UI at
`http://localhost:8091`, which works with no Slack configuration at all.
