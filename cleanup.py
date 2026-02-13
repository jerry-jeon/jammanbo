import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from notion_service import NotionTaskCreator, _get_title, _get_status, _get_created_time

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
STATE_FILE = STATE_DIR / "state.json"
DAILY_CLEANUP_COUNT = 3
QUEUE_STALE_DAYS = 7


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class CleanupManager:
    def __init__(self, bot: Bot, chat_id: int, notion: NotionTaskCreator):
        self.bot = bot
        self.chat_id = chat_id
        self.notion = notion

    async def run_daily_cleanup(self) -> None:
        """Called after daily scan. Rebuilds queue if empty/stale, sends items."""
        state = _load_state()
        queue = state.get("cleanup_queue", [])
        index = state.get("cleanup_index", 0)
        last_scan = state.get("cleanup_last_scan", "")

        # Rebuild if empty or stale
        needs_rebuild = (
            not queue
            or index >= len(queue)
            or self._is_stale(last_scan)
        )

        if needs_rebuild:
            queue = await self._build_queue()
            index = 0
            state["cleanup_queue"] = queue
            state["cleanup_index"] = index
            state["cleanup_last_scan"] = datetime.now(KST).isoformat()
            _save_state(state)

        if not queue:
            logger.info("No cleanup candidates found")
            return

        # Send up to DAILY_CLEANUP_COUNT items
        sent = 0
        while sent < DAILY_CLEANUP_COUNT and index < len(queue):
            page_id = queue[index]
            try:
                await self._send_cleanup_item(page_id)
                sent += 1
            except Exception:
                logger.exception("Failed to send cleanup item %s", page_id)
            index += 1

        state["cleanup_index"] = index
        _save_state(state)

        if sent > 0:
            logger.info("Sent %d cleanup items", sent)

    def _is_stale(self, last_scan_iso: str) -> bool:
        if not last_scan_iso:
            return True
        try:
            last = datetime.fromisoformat(last_scan_iso)
            return (datetime.now(KST) - last).days >= QUEUE_STALE_DAYS
        except ValueError:
            return True

    async def _build_queue(self) -> list[str]:
        """Query Notion for cleanup candidates, return page ID list."""
        six_months_ago = (datetime.now(KST).date() - timedelta(days=180)).isoformat()
        try:
            pages = await self.notion.query_cleanup_candidates(six_months_ago)
            return [p["id"] for p in pages]
        except Exception:
            logger.exception("Failed to build cleanup queue")
            return []

    async def _send_cleanup_item(self, page_id: str) -> None:
        """Fetch fresh page data, send message with inline buttons."""
        page = await self.notion.get_page(page_id)
        title = _get_title(page)
        status = _get_status(page)
        created = _get_created_time(page)[:10]

        # Fetch page content for summary
        try:
            content = await self.notion.get_page_content(page_id)
        except Exception:
            logger.exception("Failed to fetch content for %s", page_id)
            content = ""

        summary = self._make_summary(content)

        text = (
            f"🧹 *Cleanup candidate*\n"
            f"*{title}*\n"
            f"Status: {status} | Created: {created}"
        )
        if summary:
            text += f"\n\n{summary}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Keep ✓", callback_data=f"cleanup_keep:{page_id}"),
                InlineKeyboardButton("Done ✔", callback_data=f"cleanup_done:{page_id}"),
                InlineKeyboardButton("Delete ✗", callback_data=f"cleanup_delete:{page_id}"),
            ]
        ])

        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Route inline button presses."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if ":" not in data:
            return

        action, page_id = data.split(":", 1)

        if action == "cleanup_keep":
            await self._handle_keep(query, page_id)
        elif action == "cleanup_done":
            await self._handle_done(query, page_id)
        elif action == "cleanup_delete":
            await self._handle_delete(query, page_id)

    async def _handle_keep(self, query, page_id: str) -> None:
        """Mark as valid — remove from queue."""
        self._remove_from_queue(page_id)
        title = self._get_title_from_message(query.message.text)
        await query.edit_message_text(f"✅ Kept: {title}")

    async def _handle_delete(self, query, page_id: str) -> None:
        """Update Notion to 'Won't do', remove from queue."""
        try:
            await self.notion.update_task_status(page_id, "Won't do")
            self._remove_from_queue(page_id)
            title = self._get_title_from_message(query.message.text)
            await query.edit_message_text(f"🗑 Deleted: {title}\nChanged to Won't do in Notion")
        except Exception:
            logger.exception("Failed to update Notion for %s", page_id)
            await query.edit_message_text("❌ Notion update failed. Please try again.")

    async def _handle_done(self, query, page_id: str) -> None:
        """Update Notion to 'Done', remove from queue."""
        try:
            await self.notion.update_task_status(page_id, "Done")
            self._remove_from_queue(page_id)
            title = self._get_title_from_message(query.message.text)
            await query.edit_message_text(f"✅ Done: {title}\nMarked as Done in Notion")
        except Exception:
            logger.exception("Failed to update Notion for %s", page_id)
            await query.edit_message_text("❌ Notion update failed. Please try again.")

    @staticmethod
    def _make_summary(content: str, max_len: int = 200) -> str:
        """Truncate page content into a short summary for the cleanup message."""
        if not content:
            return "(내용 없음)"
        content = content.strip()
        if len(content) > max_len:
            content = content[:max_len].rstrip() + "…"
        # Escape Markdown special chars to avoid Telegram parse errors
        for ch in ("*", "_", "`", "["):
            content = content.replace(ch, "\\" + ch)
        return content

    def _remove_from_queue(self, page_id: str) -> None:
        state = _load_state()
        queue = state.get("cleanup_queue", [])
        if page_id in queue:
            queue.remove(page_id)
            state["cleanup_queue"] = queue
            _save_state(state)

    def _get_title_from_message(self, text: str) -> str:
        """Extract the title from the cleanup message text."""
        lines = text.split("\n")
        if len(lines) >= 2:
            return lines[1].strip("*")
        return "task"
