"""Slack delivery for approval cards.

Slack is the primary surface because it gives interactive buttons, mobile push,
threads, and a searchable decision history that doubles as a business audit
trail. It is not, however, allowed to be a single point of failure: if Slack is
unconfigured or unreachable, the approval still exists and is still actionable
through the local UI. A notification channel that can silently swallow a
CRITICAL request is worse than no channel.

Signature verification is mandatory rather than optional. The interactivity
endpoint decides whether irreversible actions proceed, so an unauthenticated
POST to it is a direct path to approving a production deploy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

import httpx

from .rules import ApprovalRequest, Risk

SLACK_API = "https://slack.com/api"

# Slack rejects signed requests older than five minutes; matching that bound
# locally is what makes replaying a captured approval payload useless.
MAX_SIGNATURE_AGE_S = 300

RISK_COLOR = {
    Risk.LOW: "#4a5568",
    Risk.MEDIUM: "#975a16",
    Risk.HIGH: "#9b2c2c",
    Risk.CRITICAL: "#742a2a",
}

RISK_EMOJI = {
    Risk.LOW: ":information_source:",
    Risk.MEDIUM: ":warning:",
    Risk.HIGH: ":rotating_light:",
    Risk.CRITICAL: ":no_entry:",
}


def verify_signature(
    *, signing_secret: str, timestamp: str, signature: str, body: bytes
) -> bool:
    """Verify Slack's request signature.

    Returns False rather than raising on any malformed input, so a hostile
    payload cannot distinguish rejection reasons through error behavior.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_SIGNATURE_AGE_S:
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"), basestring, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def build_blocks(request: ApprovalRequest, *, dedupe_key: str) -> list[dict]:
    """Render an approval card as Slack Block Kit.

    Content follows the plan's required card contents: what is requested and why
    it cannot proceed, exact blast radius, risk and reversibility, evidence
    already collected, a recommendation, and the action buttons.
    """
    emoji = RISK_EMOJI.get(request.risk, ":question:")
    reversibility = "reversible" if request.reversible else "*NOT reversible*"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Approval needed: {request.action}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Why this cannot proceed*\n{request.why_blocked}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Risk*\n{request.risk.value}"},
                {"type": "mrkdwn", "text": f"*Reversibility*\n{reversibility}"},
                {"type": "mrkdwn", "text": f"*Build*\n`{request.build_id or 'n/a'}`"},
                {"type": "mrkdwn", "text": f"*Rule*\n`{request.rule_name}`"},
            ],
        },
    ]

    scope_bits = []
    if request.repo:
        scope_bits.append(f"*Repo* `{request.repo}`")
    if request.branch:
        scope_bits.append(f"*Branch* `{request.branch}`")
    if request.pull_request:
        scope_bits.append(f"*PR* {request.pull_request}")
    if request.environment:
        scope_bits.append(f"*Env* `{request.environment}`")
    if request.files_affected:
        shown = ", ".join(f"`{f}`" for f in request.files_affected[:8])
        more = (
            f" and {len(request.files_affected) - 8} more"
            if len(request.files_affected) > 8
            else ""
        )
        scope_bits.append(f"*Files* {shown}{more}")
    if scope_bits:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(scope_bits)},
            }
        )

    if request.detail:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Requested action*\n```{request.detail[:2500]}```",
                },
            }
        )

    if request.evidence:
        lines = [f"- *{k}*: {v}" for k, v in list(request.evidence.items())[:8]]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Evidence collected*\n" + "\n".join(lines),
                },
            }
        )

    if request.retry_history:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Retries already attempted: "
                        + " -> ".join(request.retry_history[:6]),
                    }
                ],
            }
        )

    if request.alternatives:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Alternatives considered*\n"
                    + "\n".join(f"- {a}" for a in request.alternatives[:5]),
                },
            }
        )

    if request.recommendation:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Recommendation*\n{request.recommendation}",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"ctswarm_approval:{dedupe_key}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve",
                    "value": dedupe_key,
                    # Irreversible actions get a confirmation dialog. A misclick
                    # on a production deploy has no undo.
                    **(
                        {}
                        if request.reversible
                        else {
                            "confirm": {
                                "title": {
                                    "type": "plain_text",
                                    "text": "This cannot be undone",
                                },
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"`{request.action}` is not reversible. "
                                    "Approve anyway?",
                                },
                                "confirm": {
                                    "type": "plain_text",
                                    "text": "Approve",
                                },
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            }
                        }
                    ),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "deny",
                    "value": dedupe_key,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Request changes"},
                    "action_id": "modify",
                    "value": dedupe_key,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Pause build"},
                    "action_id": "pause",
                    "value": dedupe_key,
                },
            ],
        }
    )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "No response pauses the build safely. "
                    "Silence is never treated as approval.",
                }
            ],
        }
    )

    return blocks


class SlackNotifier:
    """Posts cards to Slack. Degrades to unavailable rather than raising."""

    def __init__(
        self,
        *,
        bot_token: Optional[str],
        channel: Optional[str],
        signing_secret: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token
        self.channel = channel
        self.signing_secret = signing_secret

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.channel)

    async def post(self, request: ApprovalRequest, dedupe_key: str) -> tuple[bool, str]:
        """Post a card. Returns (delivered, message_ref_or_error)."""
        if not self.configured:
            return False, "slack not configured"

        payload = {
            "channel": self.channel,
            "text": f"Approval needed: {request.action} ({request.risk.value} risk)",
            "blocks": build_blocks(request, dedupe_key=dedupe_key),
            "attachments": [
                {"color": RISK_COLOR.get(request.risk, "#4a5568"), "blocks": []}
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{SLACK_API}/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    content=json.dumps(payload),
                )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"slack transport error: {exc}"

        if not body.get("ok"):
            return False, f"slack error: {body.get('error', 'unknown')}"
        return True, str(body.get("ts", ""))

    async def update_resolved(
        self, message_ref: str, *, decision: str, decided_by: str
    ) -> bool:
        """Replace the buttons on a resolved card.

        Leaving live buttons on a decided request invites a second, conflicting
        click and makes the audit trail ambiguous.
        """
        if not self.configured or not message_ref:
            return False
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Resolved: {decision}*"
                    + (f" by {decided_by}" if decided_by else ""),
                },
            }
        ]
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{SLACK_API}/chat.update",
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                    json={
                        "channel": self.channel,
                        "ts": message_ref,
                        "text": f"Resolved: {decision}",
                        "blocks": blocks,
                    },
                )
            return bool(response.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False
