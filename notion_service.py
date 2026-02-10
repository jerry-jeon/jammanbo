import asyncio
import logging
from typing import Callable, TypeVar

from notion_client import AsyncClient
from notion_client.errors import APIResponseError

from models import ClassifiedTask

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RETRIES = 3


async def _retry_on_rate_limit(make_coro: Callable[[], T], max_retries: int = MAX_RETRIES) -> T:
    """Await a coroutine with retry on Notion 429 rate limit.

    ``make_coro`` must be a zero-arg callable that returns a new coroutine
    each time (e.g. ``lambda: client.pages.create(...)``), because a coroutine
    object can only be awaited once.
    """
    for attempt in range(max_retries + 1):
        try:
            return await make_coro()
        except APIResponseError as e:
            if e.status != 429 or attempt == max_retries:
                raise
            retry_after = e.headers.get("retry-after") if e.headers else None
            wait = float(retry_after) if retry_after else 2 ** attempt
            logger.warning(
                "Notion rate limited (429), retrying in %.1fs (attempt %d/%d)",
                wait,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(wait)

DATABASE_ID = "8c494555019043ebb83fe1afb5280467"
DATA_SOURCE_ID = "eca92760-91c2-4dff-ae10-ff5a080e8df0"
SOURCE_VALUE = "jammanbo-input"

ACTIVE_STATUSES = ["TODO", "In progress", "To Schedule"]
DONE_STATUSES = ["Done", "Won't do"]


def _get_status(page: dict) -> str:
    prop = page.get("properties", {}).get("Status", {})
    sel = prop.get("select")
    return sel["name"] if sel else ""


def _get_title(page: dict) -> str:
    prop = page.get("properties", {}).get("Name", {})
    title_list = prop.get("title", [])
    if title_list:
        return title_list[0].get("plain_text", "")
    return ""


def _get_action_date(page: dict) -> str:
    prop = page.get("properties", {}).get("Action Date", {})
    date_obj = prop.get("date")
    if date_obj:
        return date_obj.get("start", "")
    return ""


def _get_created_time(page: dict) -> str:
    return page.get("created_time", "")


class NotionTaskCreator:
    def __init__(self, api_key: str):
        self.client = AsyncClient(auth=api_key)

    async def create_task(self, task: ClassifiedTask) -> dict:
        """Create a Notion page from a ClassifiedTask."""
        properties = self._build_properties(task)
        page = await _retry_on_rate_limit(
            lambda: self.client.pages.create(
                parent={"database_id": DATABASE_ID},
                properties=properties,
            )
        )
        logger.info("Created Notion task: %s (id=%s)", task.name, page["id"])
        return page

    async def create_raw_task(self, raw_message: str) -> dict:
        """Fallback: create a task with just the raw message as title."""
        properties = {
            "Name": {"title": [{"text": {"content": raw_message[:2000]}}]},
            "Status": {"select": {"name": "TODO"}},
            "Source": {"select": {"name": SOURCE_VALUE}},
        }
        page = await _retry_on_rate_limit(
            lambda: self.client.pages.create(
                parent={"database_id": DATABASE_ID},
                properties=properties,
            )
        )
        logger.info("Created raw Notion task (id=%s)", page["id"])
        return page

    def _build_properties(self, task: ClassifiedTask) -> dict:
        """Build Notion properties dict, only setting non-null fields."""
        props: dict = {
            "Name": {"title": [{"text": {"content": task.name}}]},
            "Status": {"select": {"name": task.status.value}},
            "Source": {"select": {"name": SOURCE_VALUE}},
        }

        if task.importance is not None:
            props["Importance"] = {"select": {"name": task.importance.value}}
        if task.urgency is not None:
            props["Urgency"] = {"select": {"name": task.urgency.value}}
        if task.category is not None:
            props["Category"] = {"select": {"name": task.category.value}}
        if task.tags:
            props["Tags"] = {"multi_select": [{"name": t} for t in task.tags]}
        if task.product:
            props["Product"] = {"multi_select": [{"name": p} for p in task.product]}
        if task.action_date is not None:
            props["Action Date"] = {"date": {"start": task.action_date.isoformat()}}
        if task.link is not None:
            props["Link"] = {"url": task.link}

        return props

    # ── Query Methods (Phase 2 & 3) ──────────────────────────────────

    async def query_overdue_tasks(self, today_iso: str) -> list[dict]:
        """Action Date < today, Status not Done/Won't do."""
        response = await _retry_on_rate_limit(
            lambda: self.client.data_sources.query(
                data_source_id=DATA_SOURCE_ID,
                filter={
                    "and": [
                        {"property": "Action Date", "date": {"before": today_iso}},
                        {"property": "Status", "select": {"does_not_equal": "Done"}},
                        {"property": "Status", "select": {"does_not_equal": "Won't do"}},
                    ]
                },
                sorts=[{"property": "Action Date", "direction": "ascending"}],
                page_size=50,
            )
        )
        return response["results"]

    async def query_today_tasks(self, today_iso: str) -> list[dict]:
        """Action Date = today, active statuses."""
        response = await _retry_on_rate_limit(
            lambda: self.client.data_sources.query(
                data_source_id=DATA_SOURCE_ID,
                filter={
                    "and": [
                        {"property": "Action Date", "date": {"equals": today_iso}},
                        {"property": "Status", "select": {"does_not_equal": "Done"}},
                        {"property": "Status", "select": {"does_not_equal": "Won't do"}},
                    ]
                },
                sorts=[{"property": "Status", "direction": "ascending"}],
                page_size=50,
            )
        )
        return response["results"]

    async def query_this_week_tasks(self, start_iso: str, end_iso: str) -> list[dict]:
        """Action Date between start (exclusive today) and end (Sunday)."""
        response = await _retry_on_rate_limit(
            lambda: self.client.data_sources.query(
                data_source_id=DATA_SOURCE_ID,
                filter={
                    "and": [
                        {"property": "Action Date", "date": {"after": start_iso}},
                        {"property": "Action Date", "date": {"on_or_before": end_iso}},
                        {"property": "Status", "select": {"does_not_equal": "Done"}},
                        {"property": "Status", "select": {"does_not_equal": "Won't do"}},
                    ]
                },
                sorts=[{"property": "Action Date", "direction": "ascending"}],
                page_size=50,
            )
        )
        return response["results"]

    async def query_stale_tasks(self, cutoff_iso: str) -> list[dict]:
        """Last edited > 2 weeks ago, Status = TODO or In progress."""
        response = await _retry_on_rate_limit(
            lambda: self.client.data_sources.query(
                data_source_id=DATA_SOURCE_ID,
                filter={
                    "and": [
                        {"property": "Status", "select": {"does_not_equal": "Done"}},
                        {"property": "Status", "select": {"does_not_equal": "Won't do"}},
                        {"property": "Status", "select": {"does_not_equal": "To Schedule"}},
                        {
                            "timestamp": "last_edited_time",
                            "last_edited_time": {"before": cutoff_iso},
                        },
                    ]
                },
                sorts=[
                    {"timestamp": "last_edited_time", "direction": "ascending"}
                ],
                page_size=50,
            )
        )
        return response["results"]

    async def query_active_task_count(self) -> tuple[int, int]:
        """Returns (in_progress_count, todo_count) with pagination."""
        in_progress = 0
        todo = 0

        for status_name in ("In progress", "TODO"):
            has_more = True
            start_cursor = None
            while has_more:
                kwargs: dict = {
                    "data_source_id": DATA_SOURCE_ID,
                    "filter": {
                        "property": "Status",
                        "select": {"equals": status_name},
                    },
                    "page_size": 100,
                }
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                response = await _retry_on_rate_limit(
                    lambda: self.client.data_sources.query(**kwargs)
                )
                count = len(response["results"])
                if status_name == "In progress":
                    in_progress += count
                else:
                    todo += count
                has_more = response.get("has_more", False)
                start_cursor = response.get("next_cursor")

        return in_progress, todo

    async def query_cleanup_candidates(self, six_months_ago_iso: str) -> list[dict]:
        """6+ month old TODO/To Schedule tasks for cleanup."""
        results = []
        has_more = True
        start_cursor = None

        while has_more:
            kwargs: dict = {
                "data_source_id": DATA_SOURCE_ID,
                "filter": {
                    "and": [
                        {
                            "or": [
                                {"property": "Status", "select": {"equals": "TODO"}},
                                {"property": "Status", "select": {"equals": "To Schedule"}},
                            ]
                        },
                        {
                            "timestamp": "created_time",
                            "created_time": {"before": six_months_ago_iso},
                        },
                    ]
                },
                "sorts": [
                    {"timestamp": "created_time", "direction": "ascending"}
                ],
                "page_size": 100,
            }
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            response = await _retry_on_rate_limit(
                lambda: self.client.data_sources.query(**kwargs)
            )
            results.extend(response["results"])
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")

        return results

    async def update_task_status(self, page_id: str, new_status: str) -> dict:
        """Update a task's status (e.g., to 'Won't do')."""
        page = await _retry_on_rate_limit(
            lambda: self.client.pages.update(
                page_id=page_id,
                properties={"Status": {"select": {"name": new_status}}},
            )
        )
        logger.info("Updated task %s status to '%s'", page_id, new_status)
        return page

    async def search_tasks_by_title(
        self, query: str, active_only: bool = True
    ) -> list[dict]:
        """Search for tasks whose title contains the query string."""
        if active_only:
            filter_obj = {
                "and": [
                    {"property": "Name", "title": {"contains": query}},
                    {"property": "Status", "select": {"does_not_equal": "Done"}},
                    {"property": "Status", "select": {"does_not_equal": "Won't do"}},
                ]
            }
        else:
            filter_obj = {"property": "Name", "title": {"contains": query}}

        response = await _retry_on_rate_limit(
            lambda: self.client.data_sources.query(
                data_source_id=DATA_SOURCE_ID,
                filter=filter_obj,
                sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
                page_size=20,
            )
        )
        return response["results"]

    async def get_page(self, page_id: str) -> dict:
        """Fetch a single page by ID."""
        return await _retry_on_rate_limit(
            lambda: self.client.pages.retrieve(page_id=page_id)
        )

    async def get_page_content(self, page_id: str) -> str:
        """Fetch the body content (blocks) of a Notion page as plain text."""
        blocks = []
        has_more = True
        start_cursor = None

        while has_more:
            kwargs: dict = {"block_id": page_id, "page_size": 100}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            response = await _retry_on_rate_limit(
                lambda: self.client.blocks.children.list(**kwargs)
            )
            blocks.extend(response["results"])
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")

        if not blocks:
            return ""

        text_parts = []
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            rich_text = block_data.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                text_parts.append(text)

        return "\n".join(text_parts)

    async def fetch_pages_with_content(
        self, pages: list[dict], max_pages: int = 10
    ) -> list[dict]:
        """Fetch body content for multiple pages with rate limiting.

        Returns list of {"page_id", "title", "status", "action_date", "body_content"}.
        Limits concurrency to 3 to respect Notion's avg 3 req/s rate limit.
        """
        semaphore = asyncio.Semaphore(3)

        async def _fetch_one(page: dict) -> dict:
            page_id = page["id"]
            async with semaphore:
                content = await self.get_page_content(page_id)
            return {
                "page_id": page_id,
                "title": _get_title(page),
                "status": _get_status(page),
                "action_date": _get_action_date(page) or None,
                "body_content": content if content else "(본문 없음)",
            }

        tasks = [_fetch_one(p) for p in pages[:max_pages]]
        return await asyncio.gather(*tasks)
