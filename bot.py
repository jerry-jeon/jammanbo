import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import Agent, AgentResponse, get_conversation_messages, save_conversation_turn
from cleanup import CleanupManager
from interaction_logger import InteractionLog, LOG_FILE
from notion_service import NotionTaskCreator, validate_page_id
from scanner import ProactiveManager

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

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
STATE_FILE = STATE_DIR / "state.json"

notion_creator = NotionTaskCreator(api_key=NOTION_API_KEY)
agent = Agent(api_key=ANTHROPIC_API_KEY, notion=notion_creator)

# Phase 2 & 3 modules — initialized in post_init() after app is built
proactive_manager: ProactiveManager | None = None
cleanup_manager: CleanupManager | None = None
scheduler: AsyncIOScheduler | None = None  # must be global to prevent GC

# Pending action store for inline buttons: short key → {page_id, new_status, title}
_pending_actions: dict[str, dict] = {}
_action_counter = 0


# ── State helpers ────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _update_last_interaction() -> None:
    """Track user interaction time for proactive message read-detection."""
    state = _load_state()
    state["last_user_interaction_time"] = datetime.now(KST).isoformat()
    _save_state(state)


# ── Commands ─────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text(
        "🐻 Jammanbo bot here!\n"
        "Send a message and I'll help manage your Notion tasks.\n"
        "Hourly check-ins from 9am to 11pm KST."
    )


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger for proactive check + cleanup."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🔄 Running manual scan...")
    try:
        await asyncio.wait_for(scheduled_daily_job(), timeout=120.0)
        await update.message.reply_text("✅ Scan complete.")
    except asyncio.TimeoutError:
        logger.error("Manual scan timed out after 120s")
        await update.message.reply_text("⏰ Scan timed out. Check logs for details.")
    except Exception:
        logger.exception("Manual scan failed")
        await update.message.reply_text("❌ Scan failed. Check logs for details.")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send recent agent log entries for debugging."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    # Parse optional arguments: /logs [errors] [count]
    # Examples: /logs, /logs 20, /logs errors, /logs errors 30
    count = 10
    errors_only = False
    if context.args:
        for arg in context.args:
            if arg.lower() in ("error", "errors"):
                errors_only = True
            else:
                try:
                    count = min(int(arg), 50)
                except ValueError:
                    pass

    if not LOG_FILE.exists():
        await update.message.reply_text("📋 No log file found yet.")
        return

    try:
        lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        await update.message.reply_text("❌ Failed to read log file.")
        return

    if not lines:
        await update.message.reply_text("📋 Log file is empty.")
        return

    if errors_only:
        lines = [ln for ln in lines if '"error":' in ln or '"response_sent": false' in ln]
        if not lines:
            await update.message.reply_text("📋 No error entries found.")
            return

    recent = lines[-count:]
    # Format each line compactly for readability
    label = "error" if errors_only else "log"
    output_parts = []
    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("ts", "?")[:19]
            mode = entry.get("mode", "?")
            sent = "✅" if entry.get("response_sent") else "❌"
            err = entry.get("error", "")
            user_msg = entry.get("user_message", "")[:60]
            duration = entry.get("duration_ms", 0)
            steps = len(entry.get("steps", []))
            summary = f"[{ts}] {mode} {sent} {duration}ms {steps}steps"
            if err:
                summary += f" ERR:{err[:80]}"
            summary += f"\n  → {user_msg}"
            output_parts.append(summary)
        except json.JSONDecodeError:
            output_parts.append(line[:120])

    text = f"📋 Last {len(recent)} {label} entries:\n\n" + "\n\n".join(output_parts)
    if len(text) > 4000:
        text = text[:3997] + "…"
    await update.message.reply_text(text)


# ── Main message handler ────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    text = update.message.text
    if not text:
        return

    _update_last_interaction()

    await context.bot.send_chat_action(chat_id=TELEGRAM_CHAT_ID, action="typing")

    messages = get_conversation_messages(TELEGRAM_CHAT_ID, text)
    ilog = InteractionLog(user_message=text, mode="chat")

    try:
        result = await agent.run(messages, mode="chat", interaction_log=ilog)
    except Exception:
        logger.exception("Agent run failed")
        ilog.finalize(response_text="", response_sent=False, error=f"agent.run failed: {logger.name}")
        try:
            await notion_creator.create_raw_task(text)
            await update.message.reply_text(
                "⚠️ Agent failed. Task created with raw message."
            )
        except Exception:
            logger.exception("Raw task creation also failed")
            await update.message.reply_text("❌ Something went wrong. Please try again.")
        return

    save_conversation_turn(TELEGRAM_CHAT_ID, text, result.text or "")

    response_sent = False
    send_error = None
    try:
        if result.confirmation_request:
            await _render_confirmation_buttons(update, result)
            response_sent = True
        elif result.text:
            await _safe_reply(update, result.text)
            response_sent = True
    except Exception as e:
        logger.exception("Failed to send reply to Telegram")
        send_error = str(e)

    ilog.finalize(
        response_text=result.text or "",
        response_sent=response_sent,
        error=send_error,
    )


async def _safe_reply(update: Update, text: str) -> None:
    """Send a reply with Markdown, falling back to plain text on parse failure."""
    reply_text = text
    if len(reply_text) > 4000:
        reply_text = reply_text[:3997] + "…"
    try:
        await update.message.reply_text(reply_text, parse_mode="Markdown")
    except Exception:
        logger.warning("Markdown parse failed, falling back to plain text")
        await update.message.reply_text(reply_text)


async def _render_confirmation_buttons(update: Update, result: AgentResponse) -> None:
    """Render inline keyboard buttons for task status confirmation."""
    global _action_counter

    req = result.confirmation_request
    tasks = req.get("tasks", [])
    new_status = req.get("new_status", "Done")
    header = req.get("header_message", "")

    if header:
        await update.message.reply_text(header)

    for task_info in tasks[:10]:
        _action_counter += 1
        key = str(_action_counter)
        _pending_actions[key] = {
            "page_id": task_info["page_id"],
            "new_status": new_status,
            "title": task_info["title"],
        }

        text = f"*{task_info['title']}*\nStatus: {task_info.get('current_status', '?')}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{new_status} ✓",
                    callback_data=f"action_yes:{key}",
                ),
                InlineKeyboardButton("Skip", callback_data=f"action_no:{key}"),
            ]
        ])
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=keyboard
        )

    # Also send the agent's text response if present
    if result.text:
        await _safe_reply(update, result.text)


# ── Inline button callbacks ─────────────────────────────────────

async def handle_action_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle per-task action button presses."""
    query = update.callback_query
    await query.answer()

    _update_last_interaction()

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

    try:
        validate_page_id(page_id)
    except ValueError:
        logger.warning("Invalid page_id in action callback: %s", page_id)
        await query.edit_message_text("❌ Invalid task reference.")
        return

    if not new_status:
        await query.edit_message_text("❌ No target status specified.")
        return

    try:
        await notion_creator.update_task_status(page_id, new_status)
        await query.edit_message_text(f"✅ {title} → {new_status}")
    except Exception:
        logger.exception("Failed to update task %s", page_id)
        await query.edit_message_text(f"❌ Failed to update: {title}")


async def handle_cleanup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route cleanup inline button presses."""
    _update_last_interaction()
    if cleanup_manager:
        await cleanup_manager.handle_callback(update, context)


# ── Scheduled jobs ───────────────────────────────────────────────

async def scheduled_daily_job() -> None:
    """Runs proactive check then cleanup sequentially."""
    logger.info("Starting scheduled daily job")
    if proactive_manager:
        await proactive_manager.run_proactive_check()
    if cleanup_manager:
        await cleanup_manager.run_daily_cleanup()
    logger.info("Finished scheduled daily job")


async def scheduled_hourly_proactive() -> None:
    """Hourly proactive check (non-cleanup hours)."""
    logger.info("Starting hourly proactive check")
    if proactive_manager:
        await proactive_manager.run_proactive_check()
    logger.info("Finished hourly proactive check")


# ── App lifecycle ────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Called after the event loop is running — safe to start AsyncIOScheduler."""
    global proactive_manager, cleanup_manager, scheduler

    proactive_manager = ProactiveManager(
        bot=application.bot, chat_id=TELEGRAM_CHAT_ID, agent=agent
    )
    cleanup_manager = CleanupManager(
        bot=application.bot, chat_id=TELEGRAM_CHAT_ID, notion=notion_creator
    )

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # Daily at 09:00: proactive check + cleanup
    scheduler.add_job(
        scheduled_daily_job,
        trigger=CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"),
        id="daily_scan",
        replace_existing=True,
    )

    # Hourly proactive check 10am-11pm (9am handled by daily job)
    scheduler.add_job(
        scheduled_hourly_proactive,
        trigger=CronTrigger(hour="10-23", minute=0, timezone="Asia/Seoul"),
        id="hourly_proactive",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started — daily job at 09:00, hourly proactive 10:00-23:00 KST")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("logs", logs_command))
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
