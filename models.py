from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    TASK = "task"
    MEMO = "memo"
    IDEA = "idea"
    ACTION = "action"


class Status(str, Enum):
    TODO = "TODO"
    TO_SCHEDULE = "To Schedule"
    IN_PROGRESS = "In progress"


class Importance(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Urgency(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Category(str, Enum):
    MUST_DO = "Must Do"
    NICE_TO_HAVE = "Nice to have"


ALLOWED_TAGS = [
    "Tutorial", "Video", "Others", "Article", "Documentation",
    "Team management", "Community Engagement", "Content Creation",
    "Product Feedback", "Analysis", "Jane", "Katherine", "Teddie",
    "AI Chatbot", "Developer Experience", "Platform API",
    "Business messaging", "Chat",
]

ALLOWED_PRODUCTS = ["UIKit", "SBM", "AI"]


ALL_STATUSES = ["TODO", "To Schedule", "In progress", "Done", "Won't do", "Pending"]


class TaskAction(BaseModel):
    """Output model when user wants to modify existing tasks."""

    type: TaskType = TaskType.ACTION
    search_query: str = Field(..., description="Keywords to search for in task titles")
    new_status: Optional[str] = Field(None, description="Target status to set")


class ClassifiedTask(BaseModel):
    """Output model for Claude's classification of a user message."""

    type: TaskType
    name: str = Field(..., description="Concise task title extracted from input")
    status: Status = Status.TODO
    importance: Optional[Importance] = None
    urgency: Optional[Urgency] = None
    category: Optional[Category] = None
    tags: list[str] = Field(default_factory=list)
    product: list[str] = Field(default_factory=list)
    action_date: Optional[date] = None
    link: Optional[str] = None
