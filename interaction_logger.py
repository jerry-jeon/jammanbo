"""Structured interaction logger for debugging agent responses.

Writes one JSON object per line to $STATE_DIR/logs/agent_log.jsonl.
Also emits a compact JSON line to stdout so Railway dashboard can display it.

Log rotation: keeps the file under MAX_LOG_SIZE_BYTES by truncating older entries.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
LOG_DIR = STATE_DIR / "logs"
LOG_FILE = LOG_DIR / "agent_log.jsonl"
MAX_LOG_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

logger = logging.getLogger(__name__)


@dataclass
class ToolStep:
    tool: str
    input: dict
    result_summary: str
    error: str | None = None


@dataclass
class InteractionLog:
    user_message: str
    mode: str = "chat"
    steps: list[ToolStep] = field(default_factory=list)
    response_text: str = ""
    response_sent: bool = False
    error: str | None = None
    _start_time: float = field(default_factory=time.monotonic, repr=False)

    def add_step(self, tool: str, input_data: dict, result: dict) -> None:
        error = result.get("error")
        # Build a compact summary instead of logging full Notion content
        summary_keys = ("count", "success", "page_id", "name", "status")
        summary = {k: v for k, v in result.items() if k in summary_keys}
        if not summary:
            summary = {"keys": list(result.keys())[:5]}

        self.steps.append(ToolStep(
            tool=tool,
            input=input_data,
            result_summary=json.dumps(summary, ensure_ascii=False),
            error=str(error) if error else None,
        ))

    def finalize(
        self, response_text: str, response_sent: bool, error: str | None = None
    ) -> None:
        self.response_text = response_text
        self.response_sent = response_sent
        self.error = error
        self._write()

    def _write(self) -> None:
        duration_ms = int((time.monotonic() - self._start_time) * 1000)
        record = {
            "ts": datetime.now(KST).isoformat(),
            "mode": self.mode,
            "user_message": self.user_message,
            "steps": [
                {
                    "tool": s.tool,
                    "input": s.input,
                    "result_summary": s.result_summary,
                    **({"error": s.error} if s.error else {}),
                }
                for s in self.steps
            ],
            "response_text": self.response_text[:500],  # truncate for log size
            "response_sent": self.response_sent,
            "duration_ms": duration_ms,
        }
        if self.error:
            record["error"] = self.error

        line = json.dumps(record, ensure_ascii=False)

        # Write to file
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.exception("Failed to write interaction log to file")

        # Also emit to stdout for Railway dashboard
        logger.info("interaction_log: %s", line)


def _rotate_if_needed() -> None:
    """If log file exceeds MAX_LOG_SIZE_BYTES, keep only the last half."""
    if not LOG_FILE.exists():
        return
    size = LOG_FILE.stat().st_size
    if size <= MAX_LOG_SIZE_BYTES:
        return

    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        half = len(lines) // 2
        LOG_FILE.write_text("\n".join(lines[half:]) + "\n", encoding="utf-8")
        logger.info("Rotated interaction log: kept %d of %d lines", len(lines) - half, len(lines))
    except Exception:
        logger.exception("Failed to rotate interaction log")
