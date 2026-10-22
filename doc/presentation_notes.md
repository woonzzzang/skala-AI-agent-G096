# 5분 발표 노트 — 기존 장애 대응 에이전트 개선

발표 첫 화면에 본인의 번호와 이름을 적습니다. 아래는 약 5분 분량의 구두 설명 초안입니다. 화면은 [README](../README.md)의 캡처와 코드 위치를 사용하면 됩니다.

## 0:00–2:00 추가·수정 기능과 결과

### 0:00–0:30 문제와 선택 과제

“저는 과제 1, 기존 실습 코드의 기능 개선을 선택했습니다. 기존 에이전트는 대상 시스템이 불명확하면 기본 서비스로 처리하거나 미등록 시스템을 조회할 수 있었고, 장애 키워드에 맞는 Runbook 하나만 골랐습니다. 보고서는 길이와 형식 중심으로 검사했고, 반복 실패하면 강제 통과하는 경로가 있었습니다. 이번에는 대상 확인, 복수 Runbook 검색, 근거 검증에 집중했습니다.”

### 0:30–1:15 기능 변화

“대상 판별에서는 LLM이 `monitoring.json`에 등록된 시스템 중 후보를 제안하고, `SystemRegistry`가 실제 키인지 검사합니다. 대상이 빠졌거나 모호하면 `interrupt()`로 질문을 되돌려주고, 답을 받으면 같은 Graph 실행을 재개합니다. Runbook은 질문과 Monitoring 알림을 함께 보고 최대 세 개를 고른 뒤 MCP로 원문을 읽습니다. 보고서는 사실·추정·권장 조치·한계로 나누고, 코드 검사와 별도 Judge 호출이 숫자와 출처를 확인합니다. 세 번 실패해도 초안을 강제 채택하지 않습니다.”

### 1:15–2:00 같은 입력의 결과와 한계

“고정 데이터와 Fake 백엔드로 같은 복합 장애 질문을 비교했습니다. 기존에는 Monitoring을 세 번 조회해 불필요한 `web-01`까지 포함하고 Runbook 하나만 사용했습니다. 개선 후에는 필요한 두 시스템을 조회하고 `cpu-spike`, `network-timeout` 두 문서를 사용했습니다. 이 사례의 실행 시간은 719.8ms에서 15.4ms로 줄었지만, 주된 이유는 기존의 인위적 0.35초 지연 제거입니다. 가상 비용은 검색·검증 호출이 늘어 오히려 증가했습니다. 실제 OpenAI API 두 건에서도 대상 확인과 재질문·재개가 동작했고, 누락된 Runbook 출처를 반려한 뒤 재작성했습니다. 다만 관련성이 낮은 `db-down` 문서가 함께 선택된 사례가 있어 검색 품질은 추가 평가가 필요합니다.”

화면: [전후 비교](screenshots/before_after_results.jpg), [최종 채팅 화면](screenshots/final_chat.jpg). 수치 원본: [평가 결과](evaluation_results.json).

## 2:00–5:00 Agent Graph, MCP, RAG 코드

### 2:00–3:10 Graph와 재질문

“[Graph 코드](../agent/graph.py)의 `build_graph()`는 노드를 등록하고 조건부 Edge를 연결합니다. `route`가 일반 대화와 장애 요청을 나누고, 장애는 `resolve_target`으로 갑니다. [대상 노드](../agent/target_nodes.py)는 LLM 후보를 Registry로 재검증합니다. 확정되지 않으면 `clarify_target`의 `interrupt()`에서 상태를 저장합니다. [서버](../server.py)는 SQLite checkpointer와 `graph_thread_id`를 사용해 다음 답변을 `Command(resume=...)`로 전달합니다. 원래 장애 질문은 유지되고 새 답으로 대상만 다시 판별합니다. 확정되면 `plan → gate → research`로 진행합니다. `gate`는 허용되지 않은 분석 태스크를 코드로 제거합니다.”

화면: [Graph 배선 캡처](screenshots/graph_wiring_code.jpg), [대상 판별 캡처](screenshots/target_resolution_code.jpg).

### 3:10–4:15 MCP와 RAG

“`plan`은 MCP Prompt에서 표준 진단 절차를 가져옵니다. `research`는 MCP Tool `get_monitoring_status`로 현재 수치를 조회하고, 뒤이어 MCP Resource로 선택한 Runbook 원문을 읽습니다. [MCP 클라이언트](../agent/mcp_client.py)는 사용 가능한 Prompt와 Resource를 먼저 찾아서 연결합니다. [Runbook 검색기](../agent/runbook_retrieval.py)는 여섯 문서의 제목·발췌를 읽고 질문과 Monitoring 알림을 합쳐 모델에 전달합니다. 모델이 문서 ID·관련도·이유를 반환하면 코드는 실제 파일 ID와 0.55 이상 점수, 최대 세 개 조건을 검증합니다. 선택된 문서만 원문을 읽어 보고서 근거로 사용합니다. 현재는 작은 문서 카탈로그를 모델로 재랭킹하는 RAG이고, 임베딩이나 벡터 DB는 사용하지 않았습니다.”

화면: [검색 코드 캡처](screenshots/runbook_retrieval_code.jpg), [MCP 서버](../mcp_server.py).

### 4:15–5:00 검증·모듈화·마무리

“마지막으로 [보고서 노드](../agent/report_nodes.py)가 초안을 작성하고, [검증 모듈](../agent/answer_validation.py)이 시스템명·수치·Runbook 출처를 코드로 확인합니다. 별도 Judge 호출은 근거 대비 사실성과 추정 표현을 평가합니다. 반려되면 피드백을 넣어 최대 세 번 다시 쓰고, 계속 실패하면 확인된 사실만 반환합니다. 대상 노드, 검색기, 검증기를 별도 모듈로 분리해 확장 지점을 명확히 했습니다. 단위 테스트 일곱 개와 고정 데이터 전후 평가를 추가했습니다. 다음 개선은 Runbook 오선택 감소와 더 많은 실제 모델 입력에 대한 정확도 평가입니다.”

화면: [검증 코드 캡처](screenshots/report_validation_code.jpg).

## 질의응답 때 구분할 점

- **“Multi-Agent인가?”** Supervisor가 조사·분석·작성·검증 역할을 조정하지만, 독립 프로세스의 여러 Agent를 운영하는 구조는 아닙니다.
- **“RAG에 벡터 DB를 썼나?”** 아닙니다. 6개 문서 카탈로그의 제목·발췌를 모델이 랭킹하고, 선택한 원문을 MCP로 조회합니다.
- **“속도가 47배 빨라졌나?”** 해당 Fake·고정 데이터 한 사례의 관찰값입니다. 기존 인위적 지연 제거 영향이 크며 실제 API 속도로 일반화하지 않습니다.
- **“OpenAI 비용은?”** 실제 API 호출은 확인했지만 README의 `cost_usd`는 통일된 가상 단가로 계산한 수치여서 실제 청구액이 아닙니다.
- **“남은 문제는?”** `db-down`처럼 관련성이 낮은 문서를 함께 가져올 수 있고, 출처를 ‘참고하지 않았다’고만 적어도 현재 검사를 통과할 수 있습니다. 검증 전 초안이 SSE로 전송되는 점도 운영 환경에서는 재설계가 필요합니다.
