import os
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from classifier import Classifier
from cleanup import CleanupManager
from models import ClassifiedTask, TaskAction, TaskQuery, TaskType
from notion_service import NotionTaskCreator, _get_title, _get_status, _get_action_date
from scanner import DailyScanner

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]

classifier = Classifier(api_key=ANTHROPIC_API_KEY)
notion_creator = NotionTaskCreator(api_key=NOTION_API_KEY)

# Phase 2 & 3 modules — initialized in main() after app is built
daily_scanner: DailyScanner | None = None
cleanup_manager: CleanupManager | None = None


def _format_confirmation(task: ClassifiedTask) -> str:
    """Format a human-readable confirmation message."""
    parts = [f"✅ Task created: '{task.name}'"]

    if task.action_date:
        parts.append(f"📅 Due: {task.action_date.isoformat()}")
    if task.urgency:
        parts.append(f"🔥 Urgency: {task.urgency.value}")
    if task.importance:
        parts.append(f"⭐ Importance: {task.importance.value}")
    if task.product:
        parts.append(f"📦 Product: {', '.join(task.product)}")
    if task.tags:
        parts.append(f"🏷️ Tags: {', '.join(task.tags)}")

    return "\n".join(parts)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "🐻 Jammanbo bot here!\n"
        "Send a message and I'll auto-create a Notion task.\n"
        "Daily summary at 09:00 KST."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Authorization: only respond to the owner
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    message_text = update.message.text
    if not message_text:
        return

    # Step 1: Classify with Claude
    result: ClassifiedTask | TaskAction | TaskQuery | None = None
    classification_failed = False
    try:
        result = await classifier.classify(message_text)
        logger.info("Classified as: type=%s", result.type.value)
    except Exception as e:
        logger.error("Classification failed: %s", e)
        classification_failed = True

    # Step 2: Handle query — search and display results
    if isinstance(result, TaskQuery):
        await _handle_query(update, result)
        return

    # Step 3: Handle action — search and update existing tasks
    if isinstance(result, TaskAction):
        await _handle_action(update, result)
        return

    # Step 4: Handle memo — acknowledge only, don't save to Notion
    if result and result.type == TaskType.MEMO:
        await update.message.reply_text(f"📝 Noted as memo: '{result.name}'")
        return

    # Step 4: Create in Notion
    try:
        if classification_failed:
            await notion_creator.create_raw_task(message_text)
            await update.message.reply_text(
                "⚠️ Auto-classification failed. Task created with raw message.\n"
                "Please organize it in Notion."
            )
        else:
            await notion_creator.create_task(result)
            reply = _format_confirmation(result)
            await update.message.reply_text(reply)
    except Exception as e:
        logger.error("Notion API failed: %s", e)
        await update.message.reply_text(
            f"❌ Notion save failed: {str(e)[:100]}\n"
            "Please try again later."
        )


async def _handle_query(update: Update, query: TaskQuery) -> None:
    """Search Notion and display results (read-only)."""
    try:
        pages = await notion_creator.search_tasks_by_title(
            query.search_query, active_only=False
        )
    except Exception as e:
        logger.error("Notion search failed: %s", e)
        await update.message.reply_text(f"❌ Search failed: {str(e)[:100]}")
        return

    if not pages:
        await update.message.reply_text(
            f"🔍 No tasks found matching '{query.search_query}'."
        )
        return

    lines = [f"🔍 *{len(pages)} task(s) matching '{query.search_query}':*\n"]
    for i, page in enumerate(pages[:15], 1):
        title = _get_title(page)
        status = _get_status(page)
        date = _get_action_date(page)
        date_str = f" | {date}" if date else ""
        lines.append(f"{i}. {title}  \\[{status}]{date_str}")

    if len(pages) > 15:
        lines.append(f"\n… and {len(pages) - 15} more")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "…"

    await update.message.reply_text(text, parse_mode="Markdown")


# Pending action store: short key → {page_id, new_status}
_pending_actions: dict[str, dict] = {}
_action_counter = 0


async def _handle_action(update: Update, action: TaskAction) -> None:
    """Search Notion for matching tasks, send each as individual card."""
    global _action_counter

    try:
        pages = await notion_creator.search_tasks_by_title(action.search_query)
    except Exception as e:
        logger.error("Notion search failed: %s", e)
        await update.message.reply_text(
            f"❌ Search failed: {str(e)[:100]}"
        )
        return

    if not pages:
        await update.message.reply_text(
            f"🔍 No active tasks found matching '{action.search_query}'."
        )
        return

    status_label = f" → {action.new_status}" if action.new_status else ""
    await update.message.reply_text(
        f"🔍 Found {len(pages)} task(s) matching '{action.search_query}'{status_label}:"
    )

    # Send each match as its own message with per-task buttons
    for page in pages[:10]:
        page_id = page["id"]
        title = _get_title(page)
        status = _get_status(page)

        _action_counter += 1
        key = str(_action_counter)
        _pending_actions[key] = {
            "page_id": page_id,
            "new_status": action.new_status,
            "title": title,
        }

        text = f"*{title}*\nStatus: {status}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{action.new_status} ✓" if action.new_status else "Update ✓",
                    callback_data=f"action_yes:{key}",
                ),
                InlineKeyboardButton("Skip", callback_data=f"action_no:{key}"),
            ]
        ])

        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=keyboard
        )


async def handle_action_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle per-task action button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if ":" not in data:
        return

    action_type, key = data.split(":", 1)
    pending = _pending_actions.pop(key, None)

    if not pending:
        await query.edit_message_text("⏳ Already handled.")
        return

    title = pending.get("title", "task")

    if action_type == "action_no":
        await query.edit_message_text(f"⏭ Skipped: {title}")
        return

    new_status = pending["new_status"]
    page_id = pending["page_id"]

    if not new_status:
        await query.edit_message_text("❌ No target status specified.")
        return

    try:
        await notion_creator.update_task_status(page_id, new_status)
        await query.edit_message_text(f"✅ {title} → {new_status}")
    except Exception:
        logger.exception("Failed to update task %s", page_id)
        await query.edit_message_text(f"❌ Failed to update: {title}")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug command to manually trigger the daily job."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🔄 Running manual scan...")
    await scheduled_daily_job()


async def handle_cleanup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route cleanup inline button presses."""
    if cleanup_manager:
        await cleanup_manager.handle_callback(update, context)


async def scheduled_daily_job() -> None:
    """Runs scanner then cleanup sequentially."""
    logger.info("Starting scheduled daily job")
    if daily_scanner:
        await daily_scanner.run_daily_scan()
    if cleanup_manager:
        await cleanup_manager.run_daily_cleanup()
    logger.info("Finished scheduled daily job")


async def post_init(application: Application) -> None:
    """Called after the event loop is running — safe to start AsyncIOScheduler."""
    global daily_scanner, cleanup_manager

    daily_scanner = DailyScanner(
        bot=application.bot, chat_id=TELEGRAM_CHAT_ID, notion=notion_creator
    )
    cleanup_manager = CleanupManager(
        bot=application.bot, chat_id=TELEGRAM_CHAT_ID, notion=notion_creator
    )

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        scheduled_daily_job,
        trigger=CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"),
        id="daily_scan",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — daily job at 09:00 KST")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(
        CallbackQueryHandler(handle_cleanup_callback, pattern=r"^cleanup_")
    )
    app.add_handler(
        CallbackQueryHandler(handle_action_callback, pattern=r"^action_")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jammanbo bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
