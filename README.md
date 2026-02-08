# Jammanbo (잠만보)

Telegram Bot + AI Agent 기반 개인 Task Management 시스템.

핵심 철학: **"내가 proactive할 필요 없이, Agent가 알아서 정리하고 먼저 말을 건다."**

## Features

### Phase 1: Input → Organize
텔레그램에 메시지를 보내면 Claude가 자동 분류하여 Notion Task를 생성합니다.

```
"금요일까지 FCT 방향 정리 문서 써야됨"
→ ✅ Task 생성: 'FCT 방향 정리 문서 작성'
  📅 Due: 2026-02-14 | 🔥 Urgency: High
```

- task / memo / idea 자동 분류
- Importance, Urgency, Category 추정
- Tags, Product 자동 매핑
- Action Date 추출 (오늘, 내일, 금요일 등 자연어 지원)

### Phase 2: Daily Summary + Alerts
매일 09:00 KST에 Notion DB를 스캔하여 데일리 요약을 전송합니다.

- 🔴 **Overdue** — 마감 지난 작업
- 📌 **Today** — 오늘 할 일
- 📅 **This Week** — 이번 주 예정
- 🧊 **Stale** — 2주 이상 방치된 작업
- 📊 **Stats** — 활성 작업 수 (In progress / TODO)

**P0 Alerts** (별도 push):
- Overload: 활성 작업 > 10개
- Severe Overdue: 밀린 작업 ≥ 3개

### Phase 3: Cleanup Queue
6개월 이상 된 TODO/To Schedule 작업을 하루 3개씩 정리합니다.

```
🧹 정리 대상
SBM 리팩토링
Status: TODO | Created: 2025-06-15

[유효 ✓] [삭제 ✗] [나중에 ⏭]
```

- **유효 ✓** — 큐에서 제거 (아직 필요한 작업)
- **삭제 ✗** — Notion에서 "Won't do"로 변경
- **나중에 ⏭** — 큐 맨 뒤로 이동

## Setup

### Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)

### Environment Variables

`.env.example`을 복사하여 `.env`를 만들고 값을 채워주세요:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급 |
| `TELEGRAM_CHAT_ID` | 본인 Telegram chat ID |
| `ANTHROPIC_API_KEY` | Claude API key |
| `NOTION_API_KEY` | Notion Integration API key |

### Install & Run

```bash
uv sync
uv run python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | 봇 소개 메시지 |
| `/scan` | 수동으로 데일리 스캔 + 정리 큐 실행 |

일반 텍스트 메시지를 보내면 자동으로 Task가 생성됩니다.

## Tech Stack

- **Python 3.12** + uv
- **python-telegram-bot** — Telegram Bot API (async)
- **Anthropic Claude API** — 메시지 분류/추출
- **notion-client** — Notion API (async)
- **APScheduler** — 09:00 KST 일일 스케줄러

## Project Structure

```
bot.py              # Telegram bot entry point, handler wiring, APScheduler
classifier.py       # Claude API 기반 메시지 분류
models.py           # Pydantic models (ClassifiedTask, enums)
notion_service.py   # Notion API CRUD + query methods
scanner.py          # DailyScanner — 일일 요약 + P0 알림
cleanup.py          # CleanupManager — 정리 큐 + 인라인 버튼
JAMMANBO.md         # 프로젝트 설계 문서
```

## Notion DB

Target database: **🪁 Sendbird Tasks** (`8c494555019043ebb83fe1afb5280467`)

주요 property: Name, Status, Importance, Urgency, Category, Tags, Product, Action Date, Link, Source
