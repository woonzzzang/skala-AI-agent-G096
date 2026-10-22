# 발표 대본 — 과제 1: 기존 장애 대응 에이전트 개선

SKALA 광주 3반 G096 정다운 · [발표자료](presentation/AI_서비스_Mini_project_G096_정다운_최종발표_v12.pptx)와 같은 페이지 순서입니다. 따옴표 안은 과정을 설명할 수 있게 풀어 쓴 대본입니다. 모두 읽으면 5분을 넘길 수 있으므로 실제 발표에서는 4~12쪽의 핵심 흐름을 중심으로 속도를 조절하세요. 코드 링크와 아래 보충 메모는 질문을 받았을 때만 참고합니다.

## 1쪽 — 제목

“안녕하세요. 과제 1로 기존 장애 대응 에이전트의 코드를 개선한 정다운입니다. 이 에이전트는 사용자의 장애 질문을 받아 시스템 상태와 Runbook을 조사하고 진단 보고서를 작성합니다. 저는 질문 속 조사 대상을 확인하는 단계부터, 문서를 찾고 보고서 근거를 검증하는 단계까지 개선했습니다.”

## 2쪽 — 선택 과제와 개선 Task

“저는 새 프로젝트를 만드는 대신 기존 실습 코드의 문제를 해결하는 과제 1을 선택했습니다. 개선 Task는 다섯 가지입니다. 첫째, 사용자가 시스템명을 말하지 않아도 대상을 확인합니다. 둘째, 여러 장애 증상에 맞는 Runbook을 여러 개 찾습니다. 셋째, 보고서가 수집한 근거와 일치하는지 검사합니다. 넷째, 일부 조회가 실패해도 성공한 정보는 유지합니다. 마지막으로 코드를 역할별로 분리하고 같은 질문으로 전후를 비교했습니다.”

## 3쪽 — 기존과 개선 구조

“왼쪽이 기존 Graph, 오른쪽이 개선한 Graph입니다. 기존에도 route가 질문을 분류하고, plan이 작업을 만든 뒤 research가 MCP Tool과 Resource를 조회했습니다. 따라서 MCP를 새로 붙인 프로젝트는 아닙니다. 제가 추가한 핵심은 plan 전에 조사 대상을 확정하는 과정, Monitoring 결과에서 관련 문서를 복수로 고르는 과정, 그리고 최종 답변을 근거와 비교해 검증하는 과정입니다. 그림에서는 흐름을 보기 쉽게 공통 gate와 analysis 노드를 생략했습니다.” [Graph](../agent/graph.py)

## 4쪽 — 대상 판별의 네 가지 결과

“첫 번째 Task는 조사 대상 확인입니다. 기존 route는 일반 대화와 장애 질문을 나눴지만, 시스템을 못 찾으면 auth-service로 대체할 수 있었습니다. 저는 장애 질문이 plan으로 가기 전에 [resolve_target](../agent/target_nodes.py#L12)을 새로 넣었습니다. 이 노드는 `monitoring.json`의 시스템명과 설명을 LLM에 보여줘 후보를 받고, 새로 만든 [SystemRegistry](../agent/system_registry.py)가 실제 등록된 key인지 확인합니다. Registry는 별도 DB가 아니며 상태 수치 조회는 뒤의 MCP Tool이 맡습니다. 결과는 `resolved`(대상 확정), `missing`(단서 부족), `unknown`(미등록 이름), `ambiguous`(복수 후보 미확정)입니다. [Graph](../agent/graph.py#L219)는 `resolved`만 plan으로 보내고 나머지는 질문합니다. 답변이 오면 같은 Graph가 Registry로 다시 확인하고, 확정되면 원래 장애 질문을 유지해 plan으로 갑니다.”

## 5쪽 — 중단·저장·재개

“`missing`, `unknown`, `ambiguous`가 나오면 Graph의 [clarify_target](../agent/target_nodes.py#L47)이 사용자에게 확인 질문을 보냅니다. 슬라이드 왼쪽 아래의 `answer = interrupt({...})`가 실제 중단 호출이며, Graph는 여기서 답변을 기다립니다. 이때 서버가 연결한 [SQLite checkpointer](../server.py#L103)에 원래 장애 질문과 그래프의 진행 위치가 보존됩니다. 사용자가 ‘auth-service예요’라고 답하면 화면은 `graph_thread_id`를 함께 보내고, [서버](../server.py#L157)는 같은 체크포인트에 `Command(resume=답변)`을 전달합니다. 재개된 Graph의 resolve_target이 그 답변을 Registry로 다시 검증합니다. 부족하면 또 묻고, `resolved`가 되면 원래 질문은 그대로 둔 채 확정된 `target_systems`와 함께 plan으로 갑니다. 앱의 `thread_id`는 채팅 세션용이고 이 `graph_thread_id`는 LangGraph 체크포인트를 찾는 키라는 점도 구분했습니다. 즉 새 Graph를 시작하거나 원래 질문 문자열에 답을 붙이는 방식이 아닙니다.”

## 6쪽 — 기존 MCP Tool 설명

“이제 MCP 부분을 먼저 기존 구조부터 보겠습니다. 기존 [get_monitoring_status Tool](../mcp_server.py#L33)은 `system_name`을 입력받고, 호출할 때 `monitoring.json`에서 해당 시스템 정보를 읽습니다. 반환값은 상태, CPU·메모리 수치, `alerts[]` 경고 문구입니다. 여기서 `alerts[]`는 ‘CPU 91%’나 ‘upstream timeout’ 같은 문자열 목록이며 숫자 필드와 별개입니다. 기존 MCP 서버에는 표준 조사 절차를 주는 [Prompt](../mcp_server.py#L65)와 Runbook 마크다운 원문을 주는 [Resource](../mcp_server.py#L56)도 있었습니다. Graph의 plan은 Prompt를 참고해 작업을 만들고, research는 Tool이나 Resource를 실제로 호출합니다. 이 Tool·Prompt·Resource의 정의는 제가 새로 만든 것이 아닙니다. 다음 페이지에서 기존 Graph가 이 자료를 어떻게 사용했는지 보겠습니다.”

## 7쪽 — 기존 Tool 사용과 Runbook 1개 선택

“기존 코드에서도 research는 MCP 클라이언트의 `call_tool`로 `get_monitoring_status`를 호출했습니다. 이후 replan_check는 Tool이 돌려준 `status`와 `alerts[]`를 한 문자열로 합치고, `cpu`, `timeout` 같은 키워드를 정해진 순서대로 검사했습니다. 첫 번째로 일치한 유형 **하나만** 반환해서 Runbook task 하나를 만들었고, research가 기존 MCP Resource에서 그 원문만 읽었습니다. 예를 들어 CPU 급증과 timeout이 함께 있으면 CPU 규칙이 먼저 맞아 `cpu-spike`만 고르고 `network-timeout`은 빠질 수 있습니다. 여기서는 어떤 문서의 본문이 관련 있는지를 비교하기 전에 하나만 고른다는 것이 핵심 한계입니다. 키워드가 없을 때 `db-down`으로 대체하는 점도 위험했습니다. 따라서 개선 대상은 MCP Tool 자체가 아니라 Tool 결과를 해석해 문서를 고르는 Graph 로직입니다.” 원본: `git show f6d7ea2:agent/graph.py` 159~230행.

## 8쪽 — 개선 과정 전체

“개선한 흐름에서도 MCP 서버의 Tool과 Resource 정의는 그대로 사용합니다. 먼저 resolve_target이 등록된 조사 대상을 확정합니다. [plan](../agent/graph.py#L51)은 MCP Prompt의 절차를 읽고, 확정된 시스템마다 Monitoring task를 만듭니다. payments와 gateway가 대상이라면 조회 작업은 두 개입니다. gate는 허용되지 않은 분석 task를 정리하지만 Monitoring task는 조사 대상으로 남깁니다. 첫 [research](../agent/graph.py#L114)가 같은 `get_monitoring_status` Tool을 대상별로 호출하고 성공 결과를 `collected`에 남깁니다. [replan_check](../agent/graph.py#L164)는 원래 질문과 성공한 결과의 `alerts[]`를 [검색기](../agent/runbook_retrieval.py#L38)에 전달합니다. 검색기가 CPU·네트워크 Runbook을 모두 찾으면 replan_check가 문서별 task를 만들고, 같은 research가 다시 실행돼 기존 MCP Resource에서 원문을 각각 읽습니다. 이때 research는 새 노드가 아니라 pending task가 달라져 재실행되는 같은 노드입니다. 문서 선택은 MCP 호출이 아니라 Graph 내부 검색 판단입니다.”

## 9쪽 — 개선 과정 1: 확정 대상별 Tool 조회

“이 페이지는 개선 흐름의 첫 번째 조사 단계, Monitoring Tool 조회입니다. [plan](../agent/graph.py#L51)이 확정된 시스템별 task를 만들면 [research](../agent/graph.py#L114)가 각 task의 `target`을 `system_name`으로 넘겨 기존 Tool을 병렬 호출합니다. 바뀐 점은 Tool 정의가 아니라, 사용자가 확인한 대상에 대해서만 호출하고 각 조회의 예외를 따로 기록한다는 점입니다. 오른쪽 화면은 일부러 payments 조회만 실패시킨 **별도 시연**입니다. gateway의 성공 결과는 `collected`에 남고 payments의 실패는 task의 `error`에 남습니다. 다음 페이지의 두 시스템이 모두 성공한 사례와 같은 실행 화면은 아닙니다.”

## 10쪽 — RAG: 검색에서 원문 활용까지

“여기부터는 payments와 gateway 조회가 모두 성공한 **다른 사례**입니다. 사용자는 두 서비스의 CPU 급증과 timeout을 질문했습니다. plan이 두 Monitoring task를 만들고 research가 Tool을 호출하면, [Monitoring 데이터](../data/monitoring.json)의 `alerts[]`에서 CPU 91%와 upstream timeout이라는 경고 문구를 얻습니다. CPU 수치 필드도 따로 반환되지만 현재 임베딩 질의에는 원래 질문과 `alerts[]`의 각 문자열을 사용합니다. [RunbookRetriever](../agent/runbook_retrieval.py#L38)는 이 문구들을 별도 검색 질의로 사용해 `cpu-spike`와 `network-timeout` 문서 ID를 찾습니다. replan_check가 두 문서의 task를 추가하고 research가 MCP Resource에서 [Runbook 원문](../data/runbooks/network-timeout.md)을 읽어 `collected`에 넣습니다. 마지막으로 write는 그 원문의 점검 절차를 보고서의 **권장 조치**에 반영합니다. 따라서 검색은 문서 ID를 고르는 단계이고, 원문을 답변의 맥락에 넣어 쓰는 단계까지 있어야 RAG라고 설명할 수 있습니다. 오른쪽에 보이는 타임아웃 경로 확인은 Runbook의 후속 점검 지시이지, 이미 수행했거나 확인된 사실이 아닙니다.”

## 11쪽 — replan_check의 분기

“여기서는 replan_check가 언제 research로 돌아가는지 보겠습니다. 첫 research가 Monitoring 조회를 마치면 성공 자료는 `collected`에, 실패 정보는 task의 `error`에 남습니다. 첫 [replan_check](../agent/graph.py#L165)는 성공 자료의 경고 문구와 원래 질문으로 Runbook을 검색합니다. 관련 문서가 있으면 각 문서의 pending task를 추가하고 `is_sufficient=False`로 같은 research에 되돌아가 원문을 읽게 합니다. 문서가 없거나 검색 오류가 나면 추가 task가 없어서 analysis로 갑니다. 문서를 읽은 뒤 두 번째 replan_check는 `replan_rounds`가 이미 1이므로 다시 검색하지 않고 analysis로 갑니다. Resource 조회가 실패해도 여기서 자동으로 무한 재시도하지 않고 그 실패를 상태에 기록합니다. 여기서 `is_sufficient`는 **더 수행할 Runbook 조사 task가 있는가**를 뜻하며, 보고서 내용의 합격 판정은 뒤의 validate가 합니다.”

## 12쪽 — 임베딩 첫 시도와 수정 결과

“처음 임베딩을 적용할 때는 Runbook 본문 전체를 문서별 벡터 하나로 만들고, 사용자 질문과 여러 Monitoring 경고 문구를 합쳐 질의 벡터 하나로 만들었습니다. 고정 질문 다섯 건에서 검색 정밀도와 재현율이 각각 66.7%로, 기대보다 나빴습니다. 저는 여러 Runbook이 공유하는 점검·조치 문장이 긴 본문에서 핵심 장애 유형을 흐리고, CPU와 timeout 증상을 한 질의로 합치면 각각의 문서를 찾기 어렵다고 **추정**했습니다. 그래서 [검색기](../agent/runbook_retrieval.py#L89)를 바꿔 문서 제목을 임베딩하고, 원래 질문과 `alerts[]`의 각 경고 문구를 별도 질의로 비교했습니다. 각 질의가 찾은 관련 문서 ID를 합치므로 CPU와 timeout 문서를 동시에 선택할 수 있습니다. 선택한 문서의 전체 내용은 이후 MCP Resource에서 읽습니다. 수정 후 같은 다섯 질문에서 정밀도가 100%였지만, 이 질문들은 방식 조정에도 썼으므로 독립 평가 결과는 아닙니다. 따라서 이 수치만으로 일반적인 장애 질문이나 최종 보고서 품질까지 좋아졌다고 주장하지는 않겠습니다.” [평가](../scripts/evaluate_retrieval.py)

## 13쪽 — 벡터 DB가 없는 이유

“현재 Runbook은 여섯 개뿐이라 [검색기](../agent/runbook_retrieval.py#L89)는 제목 벡터를 메모리에 보관하고 질의 벡터와 코사인 유사도를 직접 계산합니다. 문서가 바뀌면 본문 해시를 확인해 해당 벡터를 다시 만듭니다. 벡터 DB를 쓰지 않은 이유는 현재 규모에서 별도 저장소의 설치·운영 부담을 추가하지 않기 위해서입니다. 이 방식이 벡터 DB보다 빠르다고 측정한 것은 아닙니다. 서버를 재시작하면 메모리 캐시는 사라지고 새 질문·경고 문구의 임베딩 API 비용도 남습니다. 문서가 늘어나면 영속 인덱스나 벡터 DB를 다시 검토하겠습니다.”

## 14쪽 — 근거 검증과 재작성

“세 번째 Task는 보고서의 근거 검증입니다. 기존 validate는 답변이 30자 이상인지, `1)`이 들어 있는지만 검사했고, 세 번째 실패에는 품질과 관계없이 강제로 통과시켰습니다. 지금은 [검증 코드](../agent/answer_validation.py#L22)가 보고서의 시스템명·수치·Runbook 출처·조회 실패 고지를 수집 자료와 대조합니다. 이어 OpenAI나 Ollama 모드의 [LLM Judge](../agent/openai_llm.py#L232)가 초안과 Monitoring·Runbook 원문을 다시 비교합니다. 예를 들어 문서에 적힌 ‘확인할 점검 절차’를 보고서가 ‘이미 확인된 사실’로 바꿔 쓰면 Judge가 문제를 지적할 수 있습니다. 코드 검사와 Judge가 모두 통과해야 합격이고, 불합격 사유는 write에 전달돼 최대 세 번 재작성합니다. 끝내 통과하지 못하면 강제 합격 대신 검증 미통과와 확인된 사실을 안내합니다. 단, 기본 Fake 시연에서는 [Judge 메서드](../agent/fake_llm.py#L275)가 실제 모델이 아니라 규칙 기반 테스트 대역이므로, 그 화면만으로 LLM 판정을 입증할 수는 없습니다. 그리고 현재 SSE는 검증 전 초안을 먼저 보여줄 수 있어 운영용으로는 추가 보완이 필요합니다.”

## 15쪽 — 부분 실패 대응

“네 번째 Task는 일부 조회가 실패했을 때의 처리입니다. 기존 research는 여러 시스템을 병렬 조회했지만, 한 도구 호출이 예외를 내면 전체 gather가 중단돼 다른 성공 결과도 `collected`에 저장하지 못했습니다. 현재 [research의 run_one](../agent/graph.py#L111)은 각 작업의 예외를 따로 잡습니다. 성공한 조회 결과는 `collected`에 유지하고, 실패한 task에는 대상과 오류를 남깁니다. 이 [시연](../scripts/demo_partial_failure.py)에서는 payments Monitoring만 실패했지만 gateway의 상태와 경고는 보고서에 사용됩니다. 보고서는 payments를 조사하지 못한 점을 진단 한계로 밝혀, 성공한 사실을 버리거나 실패한 대상을 추측하지 않습니다.”

## 16쪽 — Graph 연결과 모듈화

“이 페이지는 개선 후 전체 Graph 연결을 다시 보여줍니다. [build_graph](../agent/graph.py#L236)는 각 노드를 등록하고 조건부 edge로 다음 단계를 정합니다. 일반 대화는 chat_answer로 끝나고, 장애 질문은 resolve_target에서 대상을 확정한 뒤 plan·gate·research로 갑니다. 그림에 research가 두 번 보이는 이유는 같은 노드가 먼저 Monitoring Tool을 조회하고, replan_check가 새 문서 task를 만들면 다시 실행돼 Runbook Resource를 읽기 때문입니다. replan_check는 **더 조사할 작업이 있는지**, validate는 **보고서가 근거와 일치하는지** 각각 다른 기준으로 판단합니다. 조사 후 analysis는 남은 분석 작업이 있을 때만 내용을 비교하고, write는 collected 근거로 보고서 초안을 씁니다. validate가 불합격을 내면 write가 재작성하고, 통과하거나 재작성 한도에 도달하면 finalize가 최종 응답을 만듭니다. 저는 이 흐름을 유지하면서 대상 판별, Runbook 검색, 보고서 작성을 [역할별 모듈](../agent/)로 분리했습니다.”

## 17쪽 — 동일 질문의 전후 비교

“마지막 Task로 같은 입력을 이용해 개선 전후를 비교했습니다. [평가 스크립트](../scripts/evaluate.py#L63)는 기존 Graph와 개선 Graph를 **각각 별도로 실행**하고, 그 반환값을 한 평가 항목의 `before`와 `after`에 나란히 저장했습니다. 앞선 Graph의 결과를 뒤 Graph가 기억한 것은 아닙니다. 각각의 실행 안에서는 Graph state의 `tasks`, `collected`, `events`가 노드를 지나며 갱신되지만, 평가 JSON의 `case`는 단지 두 독립 실행을 묶어 보여주는 이름입니다. 이 복합 장애 질문에서 기존 Graph는 payments·gateway 외에 불필요한 web-01도 조회하고 CPU Runbook 하나만 골랐습니다. 개선 Graph는 관련 두 시스템을 조회하고 CPU와 네트워크 Runbook을 함께 골랐습니다. 화면의 지연 감소는 주로 기존 코드에 있던 교육용 고정 지연을 제거한 효과입니다. 두 실행 모두 최종 보고서 검증을 통과하지 못했으므로, 여기서는 **대상·문서 선택 개선**만 결과로 말하겠습니다.” [결과](evaluation_results.json)

## 18쪽 — 최종 실행 화면

“이 화면은 사용자가 시스템을 바로 특정하지 않았을 때의 최종 동작입니다. Graph가 대상 확인 질문을 보내고, 사용자가 auth-service라고 답하면 서버가 같은 `graph_thread_id`의 실행을 재개합니다. 재개된 Graph가 원래 장애 질문을 유지하면서 Monitoring 상태와 관련 Runbook 원문을 조사하고, 그 근거로 보고서를 작성합니다. 화면에 보이는 점검 절차는 운영 시스템에서 실제로 복구 명령을 실행했다는 뜻이 아니라, 이후 사람이 확인할 권장 조치입니다.”

## 19쪽 — 결론

“이번 과제를 하면서 작은 Agent를 만들 때도 정확도와 속도, 구현 복잡성과 확장성을 동시에 고려해야 한다는 점이 가장 어려웠습니다. 키워드 방식은 단순하지만 복합 장애에서 문서를 놓칠 수 있었고, 임베딩은 검색 결과를 개선했지만 API 호출과 캐시 관리가 추가됐습니다. 앞으로는 검색 방식을 조정할 때 사용하지 않은 독립 질문으로 다시 평가하고, SSE가 검증 전 초안을 보여주는 문제를 고치고, 오래된 체크포인트의 보관 정책도 마련하겠습니다. 감사합니다.”

## 질문을 받으면 구분할 점

- MCP Tool·Prompt·Resource는 기존에도 있었습니다. 이번 개선은 **대상 검증, Tool 결과를 이용한 복수 문서 선택, 근거 검증, 부분 실패 처리**에 있습니다.
- `alerts[]`는 경고 문구 목록입니다. `cpu_percent`·`memory_percent` 수치 필드와 별개이며, 현재 검색기는 질문과 각 경고 문구를 임베딩합니다.
- `graph_thread_id`는 LangGraph 체크포인트 키이고 앱의 `thread_id`는 채팅 세션 ID입니다. 답변을 원래 질문 문자열에 붙이지 않고 Graph 상태에 보관합니다.
- `is_sufficient`는 추가 Runbook task 유무입니다. 답변 합격 여부는 validate가 판정합니다.
- 17쪽의 `before`·`after`는 독립 실행의 평가 결과입니다. 두 실행이 기억을 공유하거나 한 Graph가 평가 항목을 계속 갱신한 것이 아닙니다.
- OpenAI·Ollama의 Judge는 실제 LLM 호출이며, 기본 Fake 실행의 Judge는 규칙 기반 테스트 대역입니다.
