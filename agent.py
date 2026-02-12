import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from interaction_logger import InteractionLog
from models import ClassifiedTask, Status, ALLOWED_TAGS, ALLOWED_PRODUCTS, ALL_STATUSES
from notion_service import NotionTaskCreator, _get_title, _get_status, _get_action_date

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
STATE_FILE = STATE_DIR / "state.json"
MAX_HISTORY_MESSAGES = 10
DAY_NAMES_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

TOOL_DEFINITIONS = [
    {
        "name": "create_task",
        "description": "Create a new task in the Notion database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Concise task title (keep the original language, refine for clarity)",
                },
                "status": {
                    "type": "string",
                    "enum": ["TODO", "To Schedule", "In progress"],
                    "description": "Task status. Default: TODO",
                },
                "importance": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": "Task importance level",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": "Task urgency level",
                },
                "category": {
                    "type": "string",
                    "enum": ["Must Do", "Nice to have"],
                    "description": "Task category",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Tags from allowed list: {', '.join(ALLOWED_TAGS)}",
                },
                "product": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Products from allowed list: {', '.join(ALLOWED_PRODUCTS)}",
                },
                "action_date": {
                    "type": "string",
                    "description": "Due date in YYYY-MM-DD format",
                },
                "link": {
                    "type": "string",
                    "description": "URL if the input contains one",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_tasks",
        "description": "Search existing tasks by title keywords. Returns up to 10 results with body content included.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for in task titles",
                },
                "active_only": {
                    "type": "boolean",
                    "description": "Only search active tasks (not Done/Won't do). Default: true",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "update_task_status",
        "description": "Update a task's status in Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID of the task to update",
                },
                "new_status": {
                    "type": "string",
                    "enum": ALL_STATUSES,
                    "description": "The new status to set",
                },
            },
            "required": ["page_id", "new_status"],
        },
    },
    {
        "name": "get_task_detail",
        "description": (
            "Get full details and body content of a specific task by page ID. "
            "Use this when the user wants to know what's inside a task — "
            "its properties and body/description content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID of the task",
                },
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "Update one or more properties of an existing task in Notion. "
            "Use this to rename a task, change its status, date, importance, urgency, "
            "category, tags, product, or link. You can update multiple fields at once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID of the task to update",
                },
                "name": {
                    "type": "string",
                    "description": "New title for the task",
                },
                "status": {
                    "type": "string",
                    "enum": ALL_STATUSES,
                    "description": "New status",
                },
                "importance": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": "New importance level",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                    "description": "New urgency level",
                },
                "category": {
                    "type": "string",
                    "enum": ["Must Do", "Nice to have"],
                    "description": "New category",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"New tags from allowed list: {', '.join(ALLOWED_TAGS)}",
                },
                "product": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"New products from allowed list: {', '.join(ALLOWED_PRODUCTS)}",
                },
                "action_date": {
                    "type": "string",
                    "description": "New due date in YYYY-MM-DD format",
                },
                "link": {
                    "type": "string",
                    "description": "New URL",
                },
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "append_page_content",
        "description": (
            "Append text blocks (headings, paragraphs, dividers) to an existing Notion page body. "
            "Use this to add notes, merge content from other pages, or build structured documents. "
            "Call get_task_detail first to read source content, then append it to the target page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID to append content to",
                },
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["heading_2", "heading_3", "paragraph", "divider"],
                                "description": "Block type",
                            },
                            "text": {
                                "type": "string",
                                "description": "Text content (not needed for divider)",
                            },
                        },
                        "required": ["type"],
                    },
                    "description": "Content blocks to append",
                },
            },
            "required": ["page_id", "blocks"],
        },
    },
    {
        "name": "request_user_confirmation",
        "description": (
            "Present tasks with inline buttons for the user to confirm a status change. "
            "Use this when the user asks to update task status — always confirm before changing. "
            "The bot will render Telegram inline keyboard buttons and wait for the user's tap."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "page_id": {"type": "string"},
                            "title": {"type": "string"},
                            "current_status": {"type": "string"},
                        },
                        "required": ["page_id", "title", "current_status"],
                    },
                    "description": "Tasks to present for confirmation",
                },
                "new_status": {
                    "type": "string",
                    "description": "The target status to change to",
                },
                "header_message": {
                    "type": "string",
                    "description": "Message to show above the buttons",
                },
            },
            "required": ["tasks", "new_status"],
        },
    },
]


SYSTEM_PROMPT_TEMPLATE = """\
You are Jammanbo, a personal task management assistant for a Sendbird engineer.
You help manage tasks in a Notion database via Telegram.

## Your capabilities
- Create tasks when the user describes work to do, ideas, or actionable items
- Search existing tasks when the user asks about them
- View task details and body content when the user asks what's inside a task (search first, then get_task_detail)
- Update task status when asked (always confirm with the user first via request_user_confirmation)
- Update task properties (title, date, importance, urgency, category, tags, etc.) using update_task
- Append content to a page body (headings, paragraphs, dividers) using append_page_content
- Merge pages: read content from source pages (get_task_detail), append to a target page, then mark sources as Done
- Acknowledge memos/emotions warmly without creating tasks
- Ask clarifying questions when a message is too vague to create a useful task

## Multi-step requests
When a user asks for multiple changes in one message, always do as much as you can.
Never refuse the entire request because one part is impossible — complete the parts you can
and clearly explain which parts you could not do and why.

## Current date context
- Today: {today} ({day_of_week_korean})
- Tomorrow: {tomorrow}
- Day after tomorrow: {day_after_tomorrow}
- This Friday: {this_friday}
- Next Monday: {next_monday}
- Timezone: Asia/Seoul (KST)

## Task field guidelines

### Date handling (KST)
- "오늘" → {today}
- "내일" → {tomorrow}
- "모레" → {day_after_tomorrow}
- "이번 주 금요일" / "금요일" → {this_friday}
- "다음 주 월요일" → {next_monday}
- Relative weekday names: calculate from today. If the named day has already passed this week, assume NEXT week.
- If no explicit deadline is mentioned, don't set action_date.

### Status rules
- Default: "TODO"
- If explicitly future/vague ("나중에", "언젠가", "아이디어"): "To Schedule"
- If user says they are currently doing it ("하는 중", "진행 중"): "In progress"

### Importance / Urgency
- "급함", "ASAP", "바로", "지금": Urgency = High
- "중요", "반드시", "꼭": Importance = High
- Deadline today or tomorrow: Urgency = High (inferred)
- If ambiguous, don't set importance/urgency.

### Category
- If Importance=High OR Urgency=High: "Must Do"
- If explicitly optional ("되면 좋고", "시간되면"): "Nice to have"
- Otherwise don't set category.

### Tags & Products
- Only select from the allowed lists defined in the tool schemas
- If a person name (Jane, Katherine, Teddie) is mentioned, include in tags
- Match topic keywords to relevant tags when clear
- If nothing matches clearly, use empty list (do NOT guess)

### Task name
- Keep it concise (under ~40 chars ideally)
- Keep the same language as input (don't translate Korean to English)
- Remove filler words, keep the action and subject
- Example: "금요일까지 FCT 방향 정리 문서 써야됨" → "FCT 방향 정리 문서 작성"

## Clarification
If a task is too vague to recall 3 days later (e.g., "review a PR", "send that document"), ask a clarifying question instead of creating a vague task.
Clear examples that DON'T need clarification: "PR #142 리뷰", "UIKit 릴리즈 노트 작성"

## After creating a task
If the task might overlap with existing work, call search_tasks to find related active tasks and mention them in your response.

## Content fetching rules
- Task lists and workspace snapshots only include metadata (title, status, date) — NOT body content.
- NEVER claim a page is empty or has no content unless you have actually fetched it via get_task_detail or search_tasks (which includes body_content in results).
- If body_content shows "(빈 페이지)", the page truly has no content blocks.
- When evaluating, reviewing, or discussing a task's content (e.g., ambiguous tasks, cleanup, asking "what's in this task"), proactively call get_task_detail FIRST before responding — don't wait for the user to ask.

## Page ID usage (IMPORTANT)
- Conversation history may contain workspace snapshots with page IDs in [id:PAGE_ID] format.
- When referencing a task from prior conversation context, ALWAYS look for its page ID first.
- Prefer get_task_detail(page_id) over search_tasks(title) when a page ID is available — title search can miss due to formatting differences.
- If search_tasks returns 0 results for a task you know exists, check conversation history for its page ID and try get_task_detail instead.
- NEVER say a page "cannot be found" without first trying get_task_detail with its page ID from conversation context.

## Response style
- Reply in the same language the user uses
- Keep responses concise — this is Telegram
- Use emoji sparingly for visual structure
"""

PROACTIVE_PROMPT_SUFFIX = """
## Proactive check-in mode
You are doing an hourly check-in. The user message includes a live workspace snapshot
with overdue tasks, today's tasks, this week's tasks, stale tasks, and active task counts
fetched directly from Notion. Use this data as your primary source of truth.

IMPORTANT: The workspace snapshot only includes metadata (title, status, date).
It does NOT include page body content. Before claiming any task is empty, has no content,
or is "title-only", you MUST call get_task_detail to verify.
When you encounter ambiguous or unclear tasks, proactively fetch their content with
get_task_detail before discussing them with the user.

You may also use Notion tools (search_tasks, get_task_detail) if you need more detail
on a specific task from the snapshot.

## Page ID preservation (CRITICAL)
When mentioning a specific task in your response, ALWAYS include its page ID in the format
[id:PAGE_ID] right after the task name. This is essential because when the user replies
to your message, the follow-up chat agent needs the page ID to look up the task directly.
Without it, title-based search may fail.
Example: "Coupang eats" [id:abc123-def456] (TODO, 2/10 마감)

Based on the snapshot and the time of day, send ONE helpful message. Examples:
- Ask about progress on a specific in-progress task
- Remind about an approaching deadline
- Flag overdue tasks that need attention
- Suggest tackling a specific task if the schedule is light
- Note overload and suggest cutting scope
- If nothing notable, respond with exactly "SKIP" (the bot will not send anything)

Be specific — reference actual task names from the snapshot. Don't send generic motivational messages.
IMPORTANT: Do NOT say the task list is clear unless the snapshot shows 0 active tasks.
Current time: {now_kst}
"""


@dataclass
class AgentResponse:
    text: str = ""
    confirmation_request: dict | None = None


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_conversation_messages(chat_id: int, new_user_message: str) -> list[dict]:
    """Build messages list from persisted conversation history + new message."""
    state = _load_state()
    history = state.get("conversation_history", {}).get(str(chat_id), [])
    messages = list(history)
    messages.append({"role": "user", "content": new_user_message})
    return messages


def save_conversation_turn(chat_id: int, user_text: str, assistant_text: str) -> None:
    """Save a user+assistant turn to persistent conversation history."""
    state = _load_state()
    if "conversation_history" not in state:
        state["conversation_history"] = {}
    key = str(chat_id)
    history = state["conversation_history"].get(key, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    state["conversation_history"][key] = history[-MAX_HISTORY_MESSAGES:]
    _save_state(state)


class Agent:
    def __init__(self, api_key: str, notion: NotionTaskCreator):
        self.client = AsyncAnthropic(api_key=api_key)
        self.notion = notion

    async def run(
        self,
        messages: list[dict],
        mode: str = "chat",
        interaction_log: InteractionLog | None = None,
    ) -> AgentResponse:
        """Run the agentic loop. mode: 'chat' or 'proactive'."""
        system_prompt = self._build_system_prompt(mode)
        max_iterations = 5
        confirmation_request = None

        for _ in range(max_iterations):
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=1024,
                    temperature=0.0,
                    system=system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                ),
                timeout=30.0,
            )

            # Collect text and tool_use blocks
            text_parts = []
            tool_use_blocks = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)

            if not tool_use_blocks:
                return AgentResponse(
                    text="\n".join(text_parts),
                    confirmation_request=confirmation_request,
                )

            # Execute tools, collect results
            tool_results = []
            for block in tool_use_blocks:
                result = await self._execute_tool(block.name, block.input)
                if interaction_log:
                    interaction_log.add_step(block.name, block.input, result)
                if block.name == "request_user_confirmation":
                    confirmation_request = block.input
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        # Safety: max iterations reached
        final_text = "\n".join(text_parts) if text_parts else "처리 중 반복 한도에 도달했습니다."
        return AgentResponse(text=final_text, confirmation_request=confirmation_request)

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool and return the result dict."""
        try:
            if name == "create_task":
                return await self._tool_create_task(input_data)
            elif name == "search_tasks":
                return await self._tool_search_tasks(input_data)
            elif name == "update_task_status":
                return await self._tool_update_task_status(input_data)
            elif name == "update_task":
                return await self._tool_update_task(input_data)
            elif name == "get_task_detail":
                return await self._tool_get_task_detail(input_data)
            elif name == "append_page_content":
                return await self._tool_append_page_content(input_data)
            elif name == "request_user_confirmation":
                return {"status": "confirmation_sent"}
            else:
                return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return {"error": f"Tool '{name}' failed: {type(e).__name__}"}

    async def _tool_create_task(self, input_data: dict) -> dict:
        """Create a task in Notion via the existing NotionTaskCreator."""
        from datetime import date as date_type

        task = ClassifiedTask(
            type="task",
            name=input_data["name"],
            status=Status(input_data.get("status", "TODO")),
            importance=input_data.get("importance"),
            urgency=input_data.get("urgency"),
            category=input_data.get("category"),
            tags=input_data.get("tags", []),
            product=input_data.get("product", []),
            action_date=(
                date_type.fromisoformat(input_data["action_date"])
                if input_data.get("action_date")
                else None
            ),
            link=input_data.get("link"),
        )
        page = await self.notion.create_task(task)
        return {"success": True, "page_id": page["id"], "name": task.name}

    async def _tool_search_tasks(self, input_data: dict) -> dict:
        """Search tasks by title, including body content for up to 10 results."""
        query = input_data["query"]
        active_only = input_data.get("active_only", True)
        pages = await self.notion.search_tasks_by_title(query, active_only=active_only)
        tasks = await self.notion.fetch_pages_with_content(pages[:10])
        return {"count": len(pages), "tasks": tasks}

    async def _tool_update_task_status(self, input_data: dict) -> dict:
        """Update a task's status."""
        page_id = input_data["page_id"]
        new_status = input_data["new_status"]
        await self.notion.update_task_status(page_id, new_status)
        return {"success": True, "page_id": page_id}

    async def _tool_update_task(self, input_data: dict) -> dict:
        """Update one or more properties of a task."""
        page_id = input_data["page_id"]
        updates = {k: v for k, v in input_data.items() if k != "page_id"}
        await self.notion.update_task(page_id, updates)
        return {"success": True, "page_id": page_id, "updated_fields": list(updates.keys())}

    async def _tool_append_page_content(self, input_data: dict) -> dict:
        """Append content blocks to a Notion page body."""
        page_id = input_data["page_id"]
        blocks = input_data["blocks"]
        count = await self.notion.append_page_content(page_id, blocks)
        return {"success": True, "page_id": page_id, "blocks_appended": count}

    async def _tool_get_task_detail(self, input_data: dict) -> dict:
        """Get full task details including body content."""
        from notion_client.errors import APIResponseError

        page_id = input_data["page_id"]
        try:
            page = await self.notion.get_page(page_id)
        except APIResponseError as e:
            if e.status == 404:
                return {"error": "page_not_found", "page_id": page_id,
                        "message": "이 페이지가 삭제되었거나 존재하지 않습니다."}
            raise
        content = await self.notion.get_page_content(page_id)

        props = page.get("properties", {})

        def _get_select(name: str) -> str | None:
            sel = props.get(name, {}).get("select")
            return sel["name"] if sel else None

        def _get_multi_select(name: str) -> list[str]:
            return [s["name"] for s in props.get(name, {}).get("multi_select", [])]

        return {
            "page_id": page_id,
            "title": _get_title(page),
            "status": _get_status(page),
            "action_date": _get_action_date(page) or None,
            "importance": _get_select("Importance"),
            "urgency": _get_select("Urgency"),
            "category": _get_select("Category"),
            "tags": _get_multi_select("Tags"),
            "product": _get_multi_select("Product"),
            "link": props.get("Link", {}).get("url"),
            "body_content": content if content else "(빈 페이지)",
        }

    def _build_system_prompt(self, mode: str = "chat") -> str:
        now = datetime.now(KST)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        day_after_tomorrow = today + timedelta(days=2)

        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0 and now.hour >= 18:
            days_until_friday = 7
        this_friday = today + timedelta(days=days_until_friday if days_until_friday > 0 else 7)

        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = today + timedelta(days=days_until_monday)

        day_of_week_korean = DAY_NAMES_KO[today.weekday()]

        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            today=today.isoformat(),
            tomorrow=tomorrow.isoformat(),
            day_after_tomorrow=day_after_tomorrow.isoformat(),
            this_friday=this_friday.isoformat(),
            next_monday=next_monday.isoformat(),
            day_of_week_korean=day_of_week_korean,
        )

        if mode == "proactive":
            prompt += PROACTIVE_PROMPT_SUFFIX.format(
                now_kst=now.strftime("%Y-%m-%d %H:%M KST"),
            )

        return prompt
