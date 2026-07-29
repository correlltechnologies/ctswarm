"""Periodic build status to Slack, with pause and stop controls.

Distinct from approval cards on purpose. An approval *blocks* and demands a
decision; a status update informs and demands nothing. Mixing them trains the
reader to skim, and a skimmed approval card is a rubber stamp.

So status posts carry no Approve/Deny. They carry Pause and Stop, which are
always-safe actions the owner may take at any time for any reason, plus what the
team is doing and what it has cost so far.

Update cadence follows the plan's "no general progress spam" rule: posts happen
on phase transitions and terminal states, and otherwise only on a timer when
something has actually changed.
"""

from __future__ import annotations

import os

import httpx

from ..ledger import Ledger
from .slack import SLACK_API

STATE_EMOJI = {
    "queued": ":hourglass_flowing_sand:",
    "planning": ":memo:",
    "executing": ":hammer_and_wrench:",
    "verifying": ":mag:",
    "gating": ":shield:",
    "complete": ":white_check_mark:",
    "failed": ":x:",
    "paused": ":double_vertical_bar:",
    "stopped": ":octagonal_sign:",
    "blocked": ":no_entry:",
}


def _humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def build_status_blocks(record, usage: dict, capacity: dict) -> list[dict]:
    """Render a status update.

    Answers the three questions an owner actually has: what is it doing, what has
    it cost, and how do I stop it.
    """
    state = record.state.value
    emoji = STATE_EMOJI.get(state, ":gear:")

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{state.upper()}*  `{record.build_id}`\n"
                f"_{record.goal[:220]}_",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Runtime*\n{record.runtime.value}"},
                {"type": "mrkdwn", "text": f"*Elapsed*\n{_humanize(record.elapsed_s)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Model calls*\n{usage.get('total_calls', 0)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Local share*\n{usage.get('local_fraction', 0):.0%}",
                },
            ],
        },
    ]

    spend = usage.get("total_cost_usd", 0.0)
    subscription_lines = [
        f"- *{name}*: {info['fraction_remaining']:.0%} of window left"
        for name, info in capacity.items()
        if name != "open_code" and info.get("available") is not None
    ]
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Spend this build*: ${spend:.2f}\n"
                + ("\n".join(subscription_lines) or "_local inference only_"),
            },
        }
    )

    if record.phase_detail:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": record.phase_detail[:280]}
                ],
            }
        )

    if record.pr_url:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Pull request*: {record.pr_url}"},
            }
        )

    if record.error:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":warning: `{record.error[:300]}`"},
            }
        )

    gates = record.gate_results or {}
    if gates:
        scanners = gates.get("scanners") or {}
        committee = gates.get("committee") or {}
        lines = []
        if scanners:
            verdict = "passed" if scanners.get("passed") else "FAILED"
            lines.append(f"- scanners: {verdict}")
            for name in scanners.get("failed", [])[:4]:
                lines.append(f"    - failed: `{name}`")
            for name in scanners.get("unavailable", [])[:4]:
                lines.append(f"    - could not run: `{name}`")
        if committee:
            if committee.get("skipped"):
                lines.append(f"- committee: skipped ({committee['skipped']})")
            else:
                verdict = "approved" if committee.get("approved") else "BLOCKED"
                families = ", ".join(committee.get("families_represented", []))
                lines.append(f"- committee: {verdict} ({families})")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Gates*\n" + "\n".join(lines)},
            }
        )

    # Controls only on a live build. Offering Pause on a finished build invites a
    # click that does nothing, which erodes trust in every other button.
    if not record.state.terminal and state != "paused":
        blocks.append(
            {
                "type": "actions",
                "block_id": f"ctswarm_build:{record.build_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Pause"},
                        "action_id": "build_pause",
                        "value": record.build_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Stop"},
                        "style": "danger",
                        "action_id": "build_stop",
                        "value": record.build_id,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Stop this build?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": "Work completed so far is kept on its branch. "
                                "The build will not resume.",
                            },
                            "confirm": {"type": "plain_text", "text": "Stop"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                ],
            }
        )
    elif state == "paused":
        blocks.append(
            {
                "type": "actions",
                "block_id": f"ctswarm_build:{record.build_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Resume"},
                        "style": "primary",
                        "action_id": "build_resume",
                        "value": record.build_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Stop"},
                        "style": "danger",
                        "action_id": "build_stop",
                        "value": record.build_id,
                    },
                ],
            }
        )

    return blocks


class StatusNotifier:
    """Posts build status. Degrades to logging when Slack is unconfigured."""

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        channel: str | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        self.channel = channel or os.environ.get(
            "SLACK_STATUS_CHANNEL", os.environ.get("SLACK_APPROVAL_CHANNEL", "")
        )
        self.ledger = ledger or Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
        # Slack thread timestamp, so a build's updates stay in one thread instead
        # of flooding the channel with a dozen top-level posts.
        self._threads: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.channel)

    async def post(self, record) -> bool:
        """Post or update a build status. Never raises."""
        usage = self.ledger.usage_summary(record.build_id)
        try:
            from ..capacity import CapacityManager

            capacity = CapacityManager(ledger=self.ledger).report()
        except Exception:  # noqa: BLE001 - status must not break the build loop
            capacity = {}

        # Always record locally, so status history survives with or without
        # Slack and `ctswarm status` works offline.
        self.ledger.record_event(
            "build_status",
            {
                "state": record.state.value,
                "detail": record.phase_detail,
                "usage": usage,
                "pr_url": record.pr_url,
            },
            build_id=record.build_id,
        )

        if not self.configured:
            return False

        blocks = build_status_blocks(record, usage, capacity)
        payload: dict = {
            "channel": self.channel,
            "text": f"{record.build_id}: {record.state.value}",
            "blocks": blocks,
        }
        thread_ts = self._threads.get(record.build_id)
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{SLACK_API}/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                    json=payload,
                )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return False

        if not body.get("ok"):
            return False
        if not thread_ts:
            self._threads[record.build_id] = str(body.get("ts", ""))
        return True
