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

from classifier import Classifier
from cleanup import CleanupManager
from models import ClassifiedTask, TaskType
from notion_service import NotionTaskCreator
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
    parts = [f"✅ Task 생성: '{task.name}'"]

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
        "🐻 잠만보 봇이에요!\n"
        "메시지를 보내면 자동으로 Notion Task를 만들어 드려요.\n"
        "매일 09:00에 데일리 요약을 보내드려요."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Authorization: only respond to the owner
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    message_text = update.message.text
    if not message_text:
        return

    # Step 1: Classify with Claude
    task: ClassifiedTask | None = None
    classification_failed = False
    try:
        task = await classifier.classify(message_text)
        logger.info("Classified as: type=%s name='%s'", task.type.value, task.name)
    except Exception as e:
        logger.error("Classification failed: %s", e)
        classification_failed = True

    # Step 2: Handle memo — acknowledge only, don't save to Notion
    if task and task.type == TaskType.MEMO:
        await update.message.reply_text(f"📝 메모로 기록했어요: '{task.name}'")
        return

    # Step 3: Create in Notion
    try:
        if classification_failed:
            await notion_creator.create_raw_task(message_text)
            await update.message.reply_text(
                "⚠️ 자동 분류 실패, 원본 메시지로 Task 생성했어요.\n"
                "Notion에서 직접 정리해주세요."
            )
        else:
            await notion_creator.create_task(task)
            reply = _format_confirmation(task)
            await update.message.reply_text(reply)
    except Exception as e:
        logger.error("Notion API failed: %s", e)
        await update.message.reply_text(
            f"❌ Notion 저장 실패: {str(e)[:100]}\n"
            "잠시 후 다시 시도해주세요."
        )


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug command to manually trigger the daily job."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🔄 수동 스캔 시작...")
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


def main():
    global daily_scanner, cleanup_manager

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Initialize Phase 2 & 3 modules
    daily_scanner = DailyScanner(
        bot=app.bot, chat_id=TELEGRAM_CHAT_ID, notion=notion_creator
    )
    cleanup_manager = CleanupManager(
        bot=app.bot, chat_id=TELEGRAM_CHAT_ID, notion=notion_creator
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(
        CallbackQueryHandler(handle_cleanup_callback, pattern=r"^cleanup_")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # APScheduler — daily at 09:00 KST
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        scheduled_daily_job,
        trigger=CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"),
        id="daily_scan",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — daily job at 09:00 KST")

    logger.info("Jammanbo bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
