# Jammanbo (잠만보) — Project Handoff Document

## TL;DR

Jammanbo는 **Telegram Bot + AI Agent** 기반의 개인 Task Management 시스템이다.
핵심 철학: **"내가 proactive할 필요 없이, Agent가 알아서 정리하고 먼저 말을 건다."**

기존에 JerryBoard, notion-iu-graph, notion-migrator, NotionAgent 등을 만들었지만, 전부 "사용자가 먼저 열고/실행해야 동작하는" 시스템이었고, 실제 생활 패턴(급한 출근 → 미팅 연속 → 정리할 여유 없음)에서 작동하지 않았다. Jammanbo는 이 문제를 해결한다.

---

## 1. Background & Problem

### Owner Profile
- Engineer, Sendbird 근무
- 미팅이 많고, work task가 급하게 쏟아지는 환경
- 우울증 + 수면 패턴 문제로 아침 여유가 없음
- Notion을 주력 도구로 사용 중

### Core Problem
```
늦게 잠 → 늦잠 → 급하게 출근 → 미팅 → 나만의 시간 없음
→ 정리 못하고 바로 업무 → 지침 → 쉬고 싶을 뿐 → 반복
```

이 사이클에서 **"정리할 시간이 없다"**가 핵심 병목. 결과적으로:
- Urgent task를 놓침
- Notion DB에 태스크가 chaos 상태로 쌓임
- 해야 할 것이 뭔지 명확하지 않아서 불안함

### Why Previous Systems Failed
| Project | What it did | Why it failed |
|---------|------------|---------------|
| JerryBoard | Phase 기반 daily dashboard (Next.js) | 아침에 열어서 planning해야 동작 → 여유 없어서 안 씀 |
| notion-iu-graph | Importance-Urgency 매트릭스 시각화 | 시각화를 보는 행위 자체를 안 하게 됨 |
| notion-migrator | AI로 Notion workspace 자동 정리 | "정리 시스템을 정리하는 시스템" — 메타 루프 |
| NotionAgent | Claude 기반 workspace 분석 | 만들고 돌려보지도 않음 |

**공통 실패 원인: 전부 User → System 방향. 사용자가 먼저 행동해야 동작.**

### What Jammanbo Does Differently
**방향을 뒤집는다: System → User.**
- Input: 사용자는 텔레그램에 아무 때나 한 줄만 던지면 됨 (activation energy ≈ 0)
- Processing: Agent가 알아서 분류, 정리, Notion DB에 반영
- Output: Agent가 먼저 말을 걸어서 suggestion/질문을 함

---

## 2. Architecture — 3 Modules

### Module 1: Input → Organize (MVP)
**"텔레그램에 던지면 Notion에 자동 분류"**

```
[User] --텔레그램 메시지--> [Telegram Bot]
                              |
                              v
                    [AI Classification Agent]
                         - task vs memo vs idea 분류
                         - deadline 추출
                         - importance/urgency 추정
                         - product/tags 자동 매핑
                              |
                              v
                    [Notion API] --> Sendbird Tasks DB에 자동 생성
                              |
                              v
                    [Telegram Reply] --> "✅ Task 생성: 'PR 리뷰' / Due: 금요일 / Urgency: High"
```

**Input 예시:**
- `"금요일까지 FCT 방향 정리 문서 써야됨"` → Task, deadline=금요일, Product=AI?, Urgency=High
- `"오늘 민수랑 한 얘기 정리해야됨"` → Task, deadline=오늘, Category=Must Do
- `"SBM 튜토리얼 영상 아이디어 있는데 나중에"` → Task, Status=To Schedule, Tags=Tutorial,Video, Product=SBM
- `"아 오늘 힘들다"` → 메모/감정 기록 (Task DB에 안 넣음, 별도 처리 or 무시)

**AI Classification이 매핑해야 하는 필드들:**
- Name (title) — 인풋에서 핵심 추출
- Status — 기본 TODO, 맥락에 따라 To Schedule / In progress
- Importance — High / Medium / Low
- Urgency — High / Medium / Low
- Category — Must Do / Nice to have
- Tags — 기존 옵션에서 매칭 (아래 참조)
- Product — UIKit / SBM / AI (해당 시)
- Action Date — deadline이 있으면 설정
- Link — URL이 포함되어 있으면 추출

### Module 2: Cron Suggestion
**"Agent가 먼저 말을 건다"**

Scheduled job (cron)이 주기적으로 Notion DB를 스캔하고, 텔레그램으로 suggestion을 보냄.

**Suggestion 유형 (우선순위순):**

| Priority | Type | Trigger | Message 예시 |
|----------|------|---------|-------------|
| P0 | Deadline 경과 | Action Date < today & Status != Done | "⚠️ 'PR 리뷰' deadline이 지난 월요일이었어요. 마무리했나요?" |
| P0 | Overload 감지 | In progress + TODO 개수 > threshold | "📊 현재 진행 중인 태스크가 12개입니다. Push back이 필요한 것 있나요?" |
| P1 | Deadline 임박 | Action Date = 내일 or 모레 | "⏰ 'FCT 문서' 모레까지예요. 진행 상황 어때요?" |
| P1 | Stale 감지 | Edited time > 2주 전 & Status = TODO/In progress | "🧹 'SBM 리팩토링' 2주째 업데이트 없는데, 아직 유효한가요?" |
| P2 | Insight 감지 | 메모/태스크 내용 분석 | "💡 이 내용 블로그 포스팅이나 팀 공유하면 좋겠어요" |

**타이밍:**
- 매일 아침 (예: 9:00 or 출근 시간) — Daily summary 메시지 1개
- 이 메시지는 **편집(update)** 방식으로 유지 (새 메시지 폭탄 방지)
  - Telegram Bot API의 `editMessageText`를 활용
- 중요 알림(P0)만 별도 메시지로 push

**Daily Summary 메시지 형식 (예시):**
```
📋 잠만보 Daily Report (2026-02-10 월)
━━━━━━━━━━━━━━━━━━━

🔴 Overdue (2)
• PR 리뷰 — due: 2/7 (3일 지남)
• 디자인 피드백 — due: 2/9 (1일 지남)

🟡 Today (3)
• FCT 방향 문서 작성
• 팀 미팅 준비
• Katherine 1:1 follow-up

🔵 This week (4)
• SBM 튜토리얼 영상 기획
• ...

📊 현재 In progress: 8개 | TODO: 15개
⚠️ 손에 들고 있는 게 많습니다. 정리가 필요해요.
```

### Module 3: Cleanup Queue
**"기존 chaos를 하루 3-5개씩 정리"**

기존 Sendbird Tasks DB에 쌓인 2600+개의 태스크를 스캔하여, 정리가 필요한 것들을 큐에 넣고 하루에 소량씩 텔레그램으로 보내줌.

**Cleanup 대상 기준:**
- 제목 없는 항목
- 6개월 이상 된 TODO/To Schedule 상태 항목
- Status 중복 (TODO vs To Do) — 자동 통합
- Action Date 없이 방치된 항목

**UX Flow (Telegram Inline Buttons):**
```
[Agent] --> "🧹 Cleanup #47: 'wordpress plugin' (2024-04-18 생성, Status: To Schedule)"
            "아직 유효한가요?"
            [유효 ✓] [삭제 ✗] [나중에 ⏭]

Button actions:
  [유효 ✓] → Status 유지, cleanup 큐에서 제거 (= "아직 필요함, 건드리지 마")
  [삭제 ✗] → Status를 "Won't do"로 변경
  [나중에 ⏭] → 큐 맨 뒤로 이동 (다음에 다시 물어봄)
```

```
[User]  --> [삭제 ✗]
[Agent] --> "✅ 'wordpress plugin' → Won't do. (남은 큐: 234개)"
```

**하루 3-5개 × 30일 = 90-150개/월 정리**

---

## 3. Notion DB Schema (현행)

### Database: 🪁 Sendbird Tasks
- **Database ID**: `$NOTION_DATABASE_ID`
- **Data Source ID**: `$NOTION_DATA_SOURCE_ID`

### Properties

| Property | Type | Values/Options |
|----------|------|---------------|
| Name | title | — |
| Status | select | In progress, TODO, Pending, To Schedule, Done, Won't do, To Do |
| Importance | select | High, Medium, Low |
| Urgency | select | High, Medium, Low |
| Category | select | Must Do, Nice to have |
| Tags | multi_select | Tutorial, Video, Others, Article, Documentation, Team management, Community Engagement, Content Creation, Product Feedback, Analysis, Jane, Katherine, Teddie, AI Chatbot, Developer Experience, Platform API, Business messaging, Chat |
| Product | multi_select | UIKit, SBM, AI |
| Action Date | date | — |
| Link | url | — |
| Spend Time | number | — |
| ID | auto_increment_id | — |
| Created time | created_time | — |
| Edited time | last_edited_time | — |
| Action Date edited | date | (tracking용) |
| Action Date history | text | (tracking용) |
| History edited | date | (tracking용) |

### 필요한 Schema 변경 (최소한)
1. **Status 통합**: "TODO"와 "To Do" 중복 → "TODO"로 통일. 기존 "To Do" 항목은 migration 스크립트로 일괄 변경.
2. **Source 필드 추가** (select): `manual`, `jammanbo-input`, `jammanbo-cleanup` — 어디서 생성되었는지 추적. 기존 항목은 Source = null로 남겨둬도 무방.
3. 나머지는 현행 유지

### Status 정의 (통합 후)
| Status | 의미 | Agent 동작 |
|--------|------|-----------|
| TODO | 해야 할 일 | Overdue/stale 스캔 대상 |
| In progress | 진행 중 | Overload 감지 대상 |
| Pending | 외부 대기 | Stale 스캔 대상 (별도 threshold) |
| To Schedule | 나중에 할 것 | Cleanup 후보 |
| Done | 완료 | 스캔 제외 |
| Won't do | 안 함 | 스캔 제외 |

### Existing Views
- Today (gallery) — Action Date = today, grouped by Status
- Today (table) — Action Date >= today OR status in To Schedule/TODO
- Follow-up required (gallery) — Status in Info required/To Schedule
- In progress (table) — Status in TODO/In progress
- Yesterday, Tomorrow (table)
- Retro (table) — Done/Won't do, grouped by week
- List (table) — To Schedule
- Chart — Status별 count

---

## 4. Tech Stack

### Recommended
- **Language**: Python 3.11+
- **Telegram Bot**: `python-telegram-bot` (async)
- **AI/LLM**: Claude API (Anthropic) — 분류/추출용
- **Notion API**: `notion-client` (official Python SDK)
- **Scheduler**: APScheduler 또는 시스템 cron
- **Deployment**: 개인 서버, 또는 Railway/Fly.io 등 (24/7 상시 실행 필요)

### 이유
- Owner가 Python 프로젝트(NotionAgent, notion-migrator 등)에 이미 익숙
- Telegram Bot은 python-telegram-bot이 가장 성숙한 라이브러리
- Claude API는 자연어 분류에 최적 (Owner가 이미 사용 중)

---

## 5. Implementation Plan

### Phase 1: MVP — Input → Organize (1-2일)
**Goal: 텔레그램에 메시지 보내면 Notion Task 자동 생성**

1. Telegram Bot 생성 (BotFather)
2. 기본 bot server 구현 (메시지 수신 → echo)
3. Claude API 연동 — 메시지 분류/필드 추출
   - System prompt에 Notion DB schema 정보 포함
   - 기존 Tags, Product 목록을 context로 제공하여 정확한 매핑
4. Notion API 연동 — Task 자동 생성
5. 확인 메시지 회신 ("✅ Task 생성됨: ...")

**MVP 성공 기준**: 텔레그램에 `"금요일까지 PR 리뷰"` 치면 Notion DB에 적절한 필드로 Task가 생성됨.

### Phase 2: Cron Suggestion (3-5일)
1. DB 스캔 로직 구현 (overdue, stale, overload 감지)
2. Daily summary 메시지 생성 + 발송
3. Message editing (같은 메시지 업데이트)
4. P0 알림 별도 push

### Phase 3: Cleanup Queue (3-5일)
1. 기존 DB 전체 스캔 → cleanup 대상 큐 생성
2. 하루 N개 텔레그램으로 전송
3. 인라인 버튼으로 응답 처리 (유효/삭제/나중에)
4. Notion DB 자동 업데이트

### Phase 4: Polish & Iterate
- 분류 정확도 개선 (피드백 루프)
- 대화형 interaction 추가
- Personal task 확장
- 기타 개선사항

---

## 6. Key Design Decisions

### 확정된 것
- **프로젝트 이름**: Jammanbo (잠만보)
- **Input 채널**: Telegram Bot (MVP)
- **Target scope**: Work tasks (Sendbird Tasks DB) 우선
- **Suggestion 방식**: Cron scheduled + message editing (폭탄 방지)
- **Cleanup 방식**: Queue 기반, 하루 소량씩
- **대화형 interaction**: Phase 1에서는 불필요, 나중에 추가

### 논의 필요 / Owner 판단 필요
- **Telegram 보안**: 회사 업무를 Telegram bot으로 보내는 것에 대한 보안 정책 확인 필요. 민감한 내부 정보는 보내지 않는 가이드라인 필요할 수 있음.
- **Cron 시간**: 매일 아침 몇 시? (출근 시간 기준 제안: 09:00)
- **Overload threshold**: In progress + TODO 몇 개 이상이면 경고? (제안: 10개)
- **Cleanup 하루 개수**: 3개? 5개?
- **Deployment 환경**: 로컬 상시 실행 vs 클라우드

---

## 7. Reference: Existing Codebase

### ~/develop/routine/JerryBoard
- Next.js 기반 daily dashboard
- Phase 기반 구조 (Morning Planning → Work Focus → Wrap-up → Personal → Reflection)
- Notion DB 연동 코드 참고 가능
- Importance-Urgency matrix UI 있음

### ~/develop/routine/notion-iu-graph
- Notion Tasks의 Importance-Urgency 시각화
- Notion API 연동 코드 참고 가능

### ~/develop/routine/notion-migrator
- Claude AI 기반 Notion workspace 자동 정리
- AI + Notion API 통합 패턴 참고 가능

### ~/PycharmProjects/NotionAgent
- Python 기반 Notion workspace 분석 에이전트
- 구조/코드 참고 가능 (아직 실행되지 않은 상태)

---

## 8. Notion API Quick Reference

### Create a Task
```python
{
    "parent": {"database_id": "$NOTION_DATABASE_ID"},
    "properties": {
        "Name": {"title": [{"text": {"content": "PR 리뷰"}}]},
        "Status": {"select": {"name": "TODO"}},
        "Importance": {"select": {"name": "High"}},
        "Urgency": {"select": {"name": "High"}},
        "Category": {"select": {"name": "Must Do"}},
        "Tags": {"multi_select": [{"name": "Documentation"}]},
        "Product": {"multi_select": [{"name": "AI"}]},
        "Action Date": {"date": {"start": "2026-02-13"}},
        "Link": {"url": "https://github.com/..."},
        "Source": {"select": {"name": "jammanbo-input"}}
    }
}
```

### Update a Task (e.g., Cleanup — mark as Won't do)
```python
# PATCH /v1/pages/{page_id}
{
    "properties": {
        "Status": {"select": {"name": "Won't do"}}
    }
}
```

### Query: Overdue Tasks
```python
{
    "filter": {
        "and": [
            {"property": "Action Date", "date": {"before": "2026-02-08"}},
            {"property": "Status", "select": {"does_not_equal": "Done"}},
            {"property": "Status", "select": {"does_not_equal": "Won't do"}}
        ]
    },
    "sorts": [{"property": "Action Date", "direction": "ascending"}],
    "page_size": 50
}
```

### Query: Stale Tasks (2주 이상 미수정)
```python
{
    "filter": {
        "and": [
            {"property": "Edited time", "last_edited_time": {"before": "2026-01-25"}},
            {"or": [
                {"property": "Status", "select": {"equals": "TODO"}},
                {"property": "Status", "select": {"equals": "In progress"}}
            ]}
        ]
    },
    "page_size": 50
}
```

### Query: Active Task Count (Overload 감지용)
```python
# In progress + TODO 전체 count — page_size: 100으로 가져와서 len() 비교
{
    "filter": {
        "or": [
            {"property": "Status", "select": {"equals": "TODO"}},
            {"property": "Status", "select": {"equals": "In progress"}}
        ]
    },
    "page_size": 100
}
```

---

## 9. Operational Notes

### Secrets Management
- `TELEGRAM_BOT_TOKEN` — BotFather에서 발급
- `NOTION_API_KEY` — Notion Integration에서 발급 (DB에 connection 필요)
- `ANTHROPIC_API_KEY` — Claude API key
- `.env` 파일로 관리, `.gitignore`에 반드시 포함

### Error Handling
- **Claude API 실패**: 원본 메시지를 그대로 Notion에 Name으로 넣고, Status=TODO, 나머지 필드 비워둠. 사용자에게 "⚠️ 자동 분류 실패, 수동 정리 필요" 알림.
- **Notion API 실패**: 사용자에게 에러 알림 + 메시지를 로컬 큐에 저장, 다음 시도에 재전송.
- **Telegram API 실패**: 로깅만. Cron 메시지는 다음 cycle에 재시도.

### Timezone
- 모든 날짜 처리는 `Asia/Seoul` (KST, UTC+9) 기준.
- "오늘", "내일", "이번 주" 등의 상대 날짜는 KST 기준으로 계산.

### Misclassification 대응 (Phase 1)
- AI가 잘못 분류한 경우, 사용자가 Notion에서 직접 수정.
- Phase 4에서 텔레그램 내 인라인 수정 기능 추가 가능.

### Deployment (권장)
- **MVP**: 로컬 실행 (개발/테스트)
- **Production**: Railway, Fly.io, 또는 개인 서버 (24/7 uptime 필요)
- Docker container 권장 (재현성)

---

## Appendix: AI Classification Prompt (Draft)

Module 1에서 사용할 classification system prompt 초안:

```
You are Jammanbo, a task classification agent.

Given a natural language input from the user, extract and classify it into a structured task.

## Output Format (JSON)
{
  "type": "task" | "memo" | "idea",
  "name": "concise task title",
  "status": "TODO" | "To Schedule" | "In progress",
  "importance": "High" | "Medium" | "Low" | null,
  "urgency": "High" | "Medium" | "Low" | null,
  "category": "Must Do" | "Nice to have" | null,
  "tags": [...],  // from allowed list
  "product": [...],  // from allowed list
  "action_date": "YYYY-MM-DD" | null,
  "link": "URL" | null
}

## Allowed Tags
Tutorial, Video, Others, Article, Documentation, Team management,
Community Engagement, Content Creation, Product Feedback, Analysis,
Jane, Katherine, Teddie, AI Chatbot, Developer Experience, Platform API,
Business messaging, Chat

## Allowed Products
UIKit, SBM, AI

## Rules
- If no explicit deadline, set action_date to null
- "오늘", "내일", "이번 주 금요일" 등은 날짜로 변환 (today = {today})
- If input is emotional/personal (e.g., "오늘 힘들다"), classify as "memo"
- If input mentions a person name matching Tags (Jane, Katherine, Teddie), include in tags
- Urgency: "급함", "ASAP", "바로" → High
- Default status is "TODO" unless context suggests otherwise
```
