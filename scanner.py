import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot

from notion_service import NotionTaskCreator, _get_title, _get_status, _get_action_date

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATE_FILE = Path("state.json")
OVERLOAD_THRESHOLD = 10
SEVERE_OVERDUE_THRESHOLD = 3
MAX_ITEMS_PER_SECTION = 8


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class DailyScanner:
    def __init__(self, bot: Bot, chat_id: int, notion: NotionTaskCreator):
        self.bot = bot
        self.chat_id = chat_id
        self.notion = notion

    async def run_daily_scan(self) -> None:
        """Main entry point called by APScheduler."""
        now = datetime.now(KST)
        today = now.date()
        today_iso = today.isoformat()

        # Calculate date ranges
        days_until_sunday = (6 - today.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        sunday = today + timedelta(days=days_until_sunday)
        sunday_iso = sunday.isoformat()

        two_weeks_ago = (today - timedelta(days=14)).isoformat()

        try:
            overdue = await self.notion.query_overdue_tasks(today_iso)
            today_tasks = await self.notion.query_today_tasks(today_iso)
            week_tasks = await self.notion.query_this_week_tasks(today_iso, sunday_iso)
            stale = await self.notion.query_stale_tasks(two_weeks_ago)
            in_progress, todo = await self.notion.query_active_task_count()
        except Exception:
            logger.exception("Failed to query Notion for daily scan")
            await self.bot.send_message(
                chat_id=self.chat_id,
                text="❌ 데일리 스캔 중 Notion 조회 실패. 로그를 확인하세요.",
            )
            return

        summary = self._format_daily_summary(
            overdue, today_tasks, week_tasks, stale, in_progress, todo, today_iso
        )
        await self._send_or_edit_summary(summary, today_iso)
        await self._send_p0_alerts(overdue, in_progress, todo)

    def _format_daily_summary(
        self,
        overdue: list[dict],
        today_tasks: list[dict],
        week_tasks: list[dict],
        stale: list[dict],
        in_progress: int,
        todo: int,
        today_iso: str,
    ) -> str:
        lines = [f"📋 *Daily Summary — {today_iso}*\n"]

        # Overdue
        if overdue:
            lines.append(f"🔴 *Overdue ({len(overdue)})*")
            for page in overdue[:MAX_ITEMS_PER_SECTION]:
                title = _get_title(page)
                date = _get_action_date(page) or "no date"
                lines.append(f"  • {title}  _{date}_")
            if len(overdue) > MAX_ITEMS_PER_SECTION:
                lines.append(f"  … and {len(overdue) - MAX_ITEMS_PER_SECTION} more")
            lines.append("")

        # Today
        if today_tasks:
            lines.append(f"📌 *Today ({len(today_tasks)})*")
            for page in today_tasks[:MAX_ITEMS_PER_SECTION]:
                title = _get_title(page)
                status = _get_status(page)
                lines.append(f"  • {title}  \\[{status}]")
            if len(today_tasks) > MAX_ITEMS_PER_SECTION:
                lines.append(f"  … and {len(today_tasks) - MAX_ITEMS_PER_SECTION} more")
            lines.append("")

        # This week
        if week_tasks:
            lines.append(f"📅 *This Week ({len(week_tasks)})*")
            for page in week_tasks[:MAX_ITEMS_PER_SECTION]:
                title = _get_title(page)
                date = _get_action_date(page) or "no date"
                lines.append(f"  • {title}  _{date}_")
            if len(week_tasks) > MAX_ITEMS_PER_SECTION:
                lines.append(f"  … and {len(week_tasks) - MAX_ITEMS_PER_SECTION} more")
            lines.append("")

        # Stale
        if stale:
            lines.append(f"🧊 *Stale ({len(stale)})*")
            for page in stale[:5]:
                title = _get_title(page)
                status = _get_status(page)
                lines.append(f"  • {title}  \\[{status}]")
            if len(stale) > 5:
                lines.append(f"  … and {len(stale) - 5} more")
            lines.append("")

        # No items at all
        if not overdue and not today_tasks and not week_tasks and not stale:
            lines.append("✨ 모든 게 깔끔해요! 할 일이 없습니다.")
            lines.append("")

        # Stats footer
        total_active = in_progress + todo
        lines.append(f"📊 Active: {total_active} (In progress: {in_progress}, TODO: {todo})")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3997] + "…"
        return text

    async def _send_or_edit_summary(self, text: str, today_iso: str) -> None:
        state = _load_state()
        saved_date = state.get("daily_summary_date")
        saved_msg_id = state.get("daily_summary_message_id")

        if saved_date == today_iso and saved_msg_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=saved_msg_id,
                    text=text,
                    parse_mode="Markdown",
                )
                logger.info("Edited daily summary message %s", saved_msg_id)
                return
            except Exception:
                logger.warning("Failed to edit summary, sending new one")

        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="Markdown",
        )
        state["daily_summary_date"] = today_iso
        state["daily_summary_message_id"] = msg.message_id
        _save_state(state)
        logger.info("Sent new daily summary message %s", msg.message_id)

    async def _send_p0_alerts(
        self, overdue: list[dict], in_progress: int, todo: int
    ) -> None:
        total_active = in_progress + todo

        if total_active > OVERLOAD_THRESHOLD:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=(
                    f"⚠️ *Overload Alert*\n"
                    f"Active tasks: {total_active} (threshold: {OVERLOAD_THRESHOLD})\n"
                    f"정리가 필요해 보여요!"
                ),
                parse_mode="Markdown",
            )

        if len(overdue) >= SEVERE_OVERDUE_THRESHOLD:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=(
                    f"🚨 *Overdue Alert*\n"
                    f"밀린 작업 {len(overdue)}개!\n"
                    f"오늘 하나라도 처리해볼까요?"
                ),
                parse_mode="Markdown",
            )
