# Jammanbo - Claude Code 가이드

## 프로젝트 개요
Telegram + Claude AI 기반 개인 Notion 태스크 관리 봇.
사용자가 텔레그램에 자연어로 메시지를 보내면, Claude가 도구(create_task, search_tasks, update_task_status, get_task_detail, request_user_confirmation)를 사용해 Notion DB를 조작한다.

## 디버깅: Agent Interaction Log

에이전트 응답 관련 버그가 리포팅되면 `$STATE_DIR/logs/agent_log.jsonl` 파일을 확인한다.

### 로그 위치
- **Railway 환경**: `railway shell` 접속 후 `$STATE_DIR/logs/agent_log.jsonl`
- **로컬 환경**: `./logs/agent_log.jsonl` (STATE_DIR 기본값 = `.`)

### 로그 포맷 (JSON Lines, 한 줄 = 한 interaction)
```json
{
  "ts": "2026-02-10T14:32:01+09:00",
  "mode": "chat",
  "user_message": "야호 페이지에 내용이 있나요",
  "steps": [
    {"tool": "search_tasks", "input": {"query": "야호"}, "result_summary": "{\"count\": 2}"}
  ],
  "response_text": "야호 페이지에는...",
  "response_sent": true,
  "duration_ms": 3420
}
```

### 주요 디버깅 시나리오

**"응답이 안 왔어요"** → `response_sent: false`인 항목 찾기
```bash
grep '"response_sent": false' logs/agent_log.jsonl
```

**에러 발생 건** → `error` 필드 존재하는 항목
```bash
grep '"error":' logs/agent_log.jsonl
```

**특정 메시지 추적** → user_message로 검색
```bash
grep '야호' logs/agent_log.jsonl
```

### 알려진 이슈 패턴

| 증상 | 원인 | 해결 |
|------|------|------|
| 응답 미수신, error 없음 | Telegram Markdown 파싱 실패 (Notion 본문의 `*`, `_` 등) | `_safe_reply()`가 plain text fallback 처리 (수정 완료) |
| `response_sent: false` + error | reply_text 호출 실패 | error 메시지로 원인 파악 |
| steps가 5회 반복 | Claude가 도구 루프에 빠짐 | max_iterations=5 도달, fallback 메시지 반환됨 |
| agent.run failed | Claude API 타임아웃(30s) 또는 API 에러 | raw task로 fallback 생성됨 |

## 아키텍처 요약

```
bot.py          → Telegram 핸들러, 스케줄러
agent.py        → Claude agentic loop (도구 호출 최대 5회)
notion_service.py → Notion API 래퍼 (429 재시도 포함)
scanner.py      → 시간별 proactive 체크인
cleanup.py      → 6개월+ 오래된 태스크 정리
interaction_logger.py → 구조화 로그 (JSONL)
models.py       → Pydantic 데이터 모델
```

## 개발 컨벤션
- Python 3.12, `uv` 패키지 매니저
- Lint: `ruff check`
- 모델: `claude-sonnet-4-5-20250929`
- 환경변수: `.env` 파일 (`.env.example` 참고)
