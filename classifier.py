import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from models import ClassifiedTask, TaskAction, TaskQuery

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

DAY_NAMES_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

SYSTEM_PROMPT_TEMPLATE = """\
You are Jammanbo, a task classification agent for a Sendbird engineer's personal task management system.

Given a natural language input (usually in Korean), extract and classify it into a structured task.

## Current Date Info
- Today: {today} ({day_of_week_korean})
- Tomorrow: {tomorrow}
- Day after tomorrow: {day_after_tomorrow}
- This Friday: {this_friday}
- Next Monday: {next_monday}
- Timezone: Asia/Seoul (KST)

## Output Format
Return ONLY a JSON object (no markdown, no explanation).

If the user wants to CREATE a new task/memo/idea:
{{
  "type": "task" | "memo" | "idea",
  "name": "concise task title (keep the original language, refine for clarity)",
  "status": "TODO" | "To Schedule" | "In progress",
  "importance": "High" | "Medium" | "Low" | null,
  "urgency": "High" | "Medium" | "Low" | null,
  "category": "Must Do" | "Nice to have" | null,
  "tags": [],
  "product": [],
  "action_date": "YYYY-MM-DD" | null,
  "link": "URL" | null,
  "follow_up": "clarifying question" | null,
  "search_hint": "keywords to find related existing tasks" | null
}}

### follow_up (IMPORTANT)
Set follow_up when the task name is too vague to recall later. Ask for the specific detail that's missing.
- "review a PR by Friday" → follow_up: "Which PR? (repo name or PR number)"
- "send that document" → follow_up: "Which document?"
- "talk to someone about the bug" → follow_up: "Who, and which bug?"
- "PR #142 리뷰" → follow_up: null (specific enough)
- "UIKit 릴리즈 노트 작성" → follow_up: null (clear and memorable)
The test: "If I read this task title 3 days later, will I know exactly what to do?" If no → set follow_up.

### search_hint
Set search_hint to keywords that might match existing active tasks in Notion.
- "review UIKit PR" → search_hint: "UIKit"
- "follow up on the SBM tutorial" → search_hint: "SBM tutorial"
- "오늘 힘들다" (memo) → search_hint: null
Only set when there's a reasonable chance of overlap with existing tasks.

If the user wants to SEARCH/LOOK UP existing tasks (no modification):
{{
  "type": "query",
  "search_query": "keywords to find the tasks"
}}

If the user wants to MODIFY existing tasks (change status, mark as done, delete, etc.):
{{
  "type": "action",
  "search_query": "keywords to find the tasks (extracted from the user message)",
  "new_status": "Done" | "Won't do" | "TODO" | "In progress" | "To Schedule" | null
}}

## Allowed Tags
Tutorial, Video, Others, Article, Documentation, Team management,
Community Engagement, Content Creation, Product Feedback, Analysis,
Jane, Katherine, Teddie, AI Chatbot, Developer Experience, Platform API,
Business messaging, Chat

## Allowed Products
UIKit, SBM, AI

## Classification Rules

### Type Classification
- "task": Actionable work item with a clear deliverable
- "memo": Personal note, emotional expression, observation (e.g., "오늘 힘들다", "회의 분위기 좋았음")
- "idea": Creative thought or future possibility without immediate action (e.g., "SBM 튜토리얼 영상 아이디어")
- "query": User wants to SEARCH or LOOK UP existing tasks without changing them. Signals like "show me", "find", "list", "what are", "tell me about", "do I have". Extract search keywords into search_query.
- "action": User wants to UPDATE/MODIFY existing tasks. Signals like "mark as done", "complete", "delete", "change status", "move to", referring to existing items by name/description. Extract search keywords into search_query and target status into new_status.

### Date Handling (KST)
- "오늘" → {today}
- "내일" → {tomorrow}
- "모레" → {day_after_tomorrow}
- "이번 주 금요일" / "금요일" → {this_friday}
- "다음 주 월요일" → {next_monday}
- Relative weekday names: Calculate from today. If the named day has already passed this week, assume NEXT week.
- If no explicit deadline is mentioned, set action_date to null.

### Status Rules
- Default: "TODO"
- If explicitly future/vague ("나중에", "언젠가", "아이디어"): "To Schedule"
- If user says they are currently doing it ("하는 중", "진행 중"): "In progress"

### Importance / Urgency
- "급함", "ASAP", "바로", "지금": Urgency = High
- "중요", "반드시", "꼭": Importance = High
- Deadline today or tomorrow: Urgency = High (inferred)
- If ambiguous, set to null (let the user fill it in Notion)

### Category
- If Importance=High OR Urgency=High: "Must Do"
- If explicitly optional ("되면 좋고", "시간되면"): "Nice to have"
- Otherwise: null

### Tags
- Only select from the allowed list above
- If a person name (Jane, Katherine, Teddie) is mentioned, include in tags
- Match topic keywords to relevant tags when clear
- If nothing matches clearly, use empty list (do NOT guess)

### Product
- Only select from: UIKit, SBM, AI
- Match based on explicit mention or strong context
- If nothing matches, use empty list

### Link
- If the input contains a URL, extract it
- Otherwise null

### Name
- Keep it concise (under ~40 chars ideally)
- Keep the same language as input (don't translate Korean to English)
- Remove filler words, keep the action and subject
- Example: "금요일까지 FCT 방향 정리 문서 써야됨" → "FCT 방향 정리 문서 작성"
"""


class Classifier:
    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)

    async def classify(self, message: str) -> ClassifiedTask | TaskAction | TaskQuery:
        """Classify a user message into a structured task, action, or query."""
        system_prompt = self._build_system_prompt()

        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )

        raw_text = response.content[0].text

        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        parsed = json.loads(cleaned)

        msg_type = parsed.get("type")
        if msg_type == "action":
            return TaskAction.model_validate(parsed)
        if msg_type == "query":
            return TaskQuery.model_validate(parsed)
        return ClassifiedTask.model_validate(parsed)

    def _build_system_prompt(self) -> str:
        now = datetime.now(KST)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        day_after_tomorrow = today + timedelta(days=2)

        # This Friday: weekday 4 = Friday
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0 and now.hour >= 18:
            days_until_friday = 7
        this_friday = today + timedelta(days=days_until_friday if days_until_friday > 0 else 7)

        # Next Monday: weekday 0 = Monday
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = today + timedelta(days=days_until_monday)

        day_of_week_korean = DAY_NAMES_KO[today.weekday()]

        return SYSTEM_PROMPT_TEMPLATE.format(
            today=today.isoformat(),
            tomorrow=tomorrow.isoformat(),
            day_after_tomorrow=day_after_tomorrow.isoformat(),
            this_friday=this_friday.isoformat(),
            next_monday=next_monday.isoformat(),
            day_of_week_korean=day_of_week_korean,
        )
