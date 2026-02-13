import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot

from agent import save_conversation_turn
from interaction_logger import InteractionLog
from notion_service import NotionTaskCreator, _get_title, _get_status, _get_action_date

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
STATE_FILE = STATE_DIR / "state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class ProactiveManager:
    def __init__(self, bot: Bot, chat_id: int, agent):
        self.bot = bot
        self.chat_id = chat_id
        self.agent = agent

    async def _safe_send(self, text: str):
        """Send a message with Markdown, falling back to plain text on parse failure."""
        if len(text) > 4000:
            text = text[:3997] + "…"
        try:
            return await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            logger.warning("Markdown parse failed in proactive message, falling back to plain text")
            return await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
            )

    async def _fetch_workspace_summary(self) -> str:
        """Fetch real workspace data from Notion to give the agent accurate context."""
        notion: NotionTaskCreator = self.agent.notion
        now = datetime.now(KST)
        today = now.date()
        today_iso = today.isoformat()

        # Calculate this week's remaining range (tomorrow through Sunday)
        days_until_sunday = 6 - today.weekday()
        if days_until_sunday < 0:
            days_until_sunday = 0
        end_of_week = today + timedelta(days=days_until_sunday)

        # Calculate stale cutoff (2 weeks ago)
        stale_cutoff = (now - timedelta(weeks=2)).isoformat()

        try:
            overdue, today_tasks, week_tasks, stale_tasks, (in_progress, todo) = (
                await asyncio.wait_for(
                    asyncio.gather(
                        notion.query_overdue_tasks(today_iso),
                        notion.query_today_tasks(today_iso),
                        notion.query_this_week_tasks(today_iso, end_of_week.isoformat()),
                        notion.query_stale_tasks(stale_cutoff),
                        notion.query_active_task_count(),
                    ),
                    timeout=30.0,
                )
            )
        except asyncio.TimeoutError:
            logger.error("Workspace summary fetch timed out after 30s")
            return ""
        except Exception:
            logger.exception("Failed to fetch workspace summary")
            return ""

        def _format_tasks(pages: list[dict], max_items: int = 10) -> str:
            if not pages:
                return "  (none)"
            lines = []
            for p in pages[:max_items]:
                title = _get_title(p)
                status = _get_status(p)
                date = _get_action_date(p) or "no date"
                page_id = p.get("id", "")
                lines.append(f"  - {title} [{status}] (due: {date}) [id:{page_id}]")
            if len(pages) > max_items:
                lines.append(f"  ... and {len(pages) - max_items} more")
            return "\n".join(lines)

        parts = [
            "## Current workspace snapshot (live from Notion)",
            "(본문 내용 미포함 — 내용 확인 필요 시 get_task_detail 호출 필요)",
            "",
            f"📊 Active tasks: {in_progress} in progress, {todo} TODO",
            "",
            f"🔴 Overdue ({len(overdue)}):",
            _format_tasks(overdue),
            "",
            f"🟡 Due today ({len(today_tasks)}):",
            _format_tasks(today_tasks),
            "",
            f"🔵 Rest of this week ({len(week_tasks)}):",
            _format_tasks(week_tasks),
            "",
            f"⚪ Stale (no update for 2+ weeks) ({len(stale_tasks)}):",
            _format_tasks(stale_tasks, max_items=5),
        ]

        return "\n".join(parts)

    async def run_proactive_check(self) -> None:
        """Called every hour 9am-11pm. Agent queries Notion and decides what to say."""
        logger.info("Running proactive check")
        ilog = InteractionLog(user_message="[proactive check-in]", mode="proactive")

        workspace_summary = await self._fetch_workspace_summary()

        prompt_parts = ["Do an hourly check-in."]
        if workspace_summary:
            prompt_parts.append(
                "Here is the current workspace snapshot:\n\n"
                + workspace_summary
                + "\n\nBased on this data and the time of day, send ONE helpful message."
            )
        else:
            prompt_parts.append(
                "Look at the current task state and send something helpful."
            )

        messages = [
            {
                "role": "user",
                "content": "\n".join(prompt_parts),
            }
        ]

        try:
            result = await self.agent.run(messages, mode="proactive", interaction_log=ilog)
        except Exception:
            logger.exception("Proactive agent run failed")
            ilog.finalize(response_text="", response_sent=False, error="agent.run failed")
            return

        if not result.text or result.text.strip() == "SKIP":
            logger.info("Proactive check: nothing to send (SKIP)")
            ilog.finalize(response_text="SKIP", response_sent=False)
            return

        state = _load_state()
        user_read = self._has_user_read(state)

        response_sent = False
        send_error = None
        try:
            if user_read:
                msg = await self._safe_send(result.text)
                state["proactive_message_id"] = msg.message_id
            else:
                prev_id = state.get("proactive_message_id")
                if prev_id:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=prev_id,
                            text=result.text,
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.warning("Failed to edit proactive message, sending new one")
                        msg = await self._safe_send(result.text)
                        state["proactive_message_id"] = msg.message_id
                else:
                    msg = await self._safe_send(result.text)
                    state["proactive_message_id"] = msg.message_id
            response_sent = True
        except Exception as e:
            logger.exception("Failed to send proactive message to Telegram")
            send_error = str(e)

        ilog.finalize(
            response_text=result.text or "",
            response_sent=response_sent,
            error=send_error,
        )

        if not response_sent:
            return

        state["proactive_message_time"] = datetime.now(KST).isoformat()
        _save_state(state)

        # Save to conversation history so the agent has context when user replies.
        # Include the workspace snapshot so the chat agent can reference
        # task names and page IDs from the check-in.
        history_user_msg = "[hourly check-in]"
        if workspace_summary:
            history_user_msg += "\n\n" + workspace_summary
        save_conversation_turn(self.chat_id, history_user_msg, result.text)

        logger.info("Proactive check: message sent/edited")

    def _has_user_read(self, state: dict) -> bool:
        """User 'read' = sent a message or tapped a button after last proactive message."""
        last_proactive = state.get("proactive_message_time")
        last_interaction = state.get("last_user_interaction_time")
        if not last_proactive or not last_interaction:
            return True  # Default: treat as read
        return last_interaction > last_proactive
