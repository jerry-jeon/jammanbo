import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot

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

    async def run_proactive_check(self) -> None:
        """Called every hour 9am-11pm. Agent queries Notion and decides what to say."""
        logger.info("Running proactive check")

        messages = [
            {
                "role": "user",
                "content": (
                    "Do an hourly check-in. Look at the current task state and "
                    "send something helpful."
                ),
            }
        ]

        try:
            result = await self.agent.run(messages, mode="proactive")
        except Exception:
            logger.exception("Proactive agent run failed")
            return

        if not result.text or result.text.strip() == "SKIP":
            logger.info("Proactive check: nothing to send (SKIP)")
            return

        state = _load_state()
        user_read = self._has_user_read(state)

        if user_read:
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=result.text,
                parse_mode="Markdown",
            )
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
                    msg = await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=result.text,
                        parse_mode="Markdown",
                    )
                    state["proactive_message_id"] = msg.message_id
            else:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=result.text,
                    parse_mode="Markdown",
                )
                state["proactive_message_id"] = msg.message_id

        state["proactive_message_time"] = datetime.now(KST).isoformat()
        _save_state(state)
        logger.info("Proactive check: message sent/edited")

    def _has_user_read(self, state: dict) -> bool:
        """User 'read' = sent a message or tapped a button after last proactive message."""
        last_proactive = state.get("proactive_message_time")
        last_interaction = state.get("last_user_interaction_time")
        if not last_proactive or not last_interaction:
            return True  # Default: treat as read
        return last_interaction > last_proactive
