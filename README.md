# 주식 멀티 에이전트 시스템 (Stock Multi-Agent System)

한국·미국 주식을 다루는 **멀티 에이전트 기반 주식 분석 도구**입니다.
사용자가 자연어로 질문하면, 총괄 매니저가 적절한 전문 직원(에이전트)에게 일을 배분하고
각 직원의 분석을 종합해 균형 잡힌 답변을 제공합니다.

> ⚠️ **면책**: 본 도구의 모든 정보와 분석은 투자 참고용일 뿐이며 투자 권유가 아닙니다.
> 미래 주가 예측은 제공하지 않으며, 최종 투자 결정은 사용자 본인의 판단과 책임 하에 이루어져야 합니다.

---

## 주요 기능

- **종목 분석**: 현재가·기술적 지표(MA/RSI/MACD)·재무(PER/PBR 등)·뉴스·거시환경을 종합 분석
- **급변동 원인 추적**: 당일 ±5% 이상 변동 시 자동으로 원인 단서를 추적하고,
  종목 고유 요인 / 섹터 요인 / 시장 전체 요인으로 구분해 제시
- **종목 발굴(스크리너)**: KOSPI200·S&P500에서 실적+성장 기준으로 우량 종목을 발굴
  (기저효과 보정, 섹터 분산 필터 적용)
- **리스크 검수**: 다른 직원들의 분석을 교차 검증해 숫자 오류·과장·환각을 걸러냄
- **대화 맥락 기억**: 이전 질문을 기억해 "그럼 그건?" 같은 이어지는 질문 가능

---

## 에이전트 구성 (총괄 매니저 + 8 직원)

| 직원 | 역할 |
|------|------|
| 총괄 매니저 | 질문 분석, 직원 배분, 결과 종합 |
| 조회 (Research) | 현재가·등락률·거래량 등 사실 조회 |
| 시그널 (Signal) | MA·RSI·MACD 기술적 분석 |
| 펀더멘털 (Fundamental) | PER·PBR·재무 가치 분석 |
| 뉴스 (News) | 최신 공시·뉴스 요약, 호재/악재 분류 |
| 거시 (Macro) | 금리·환율·지수 등 거시환경 분석 |
| 포트폴리오 (Portfolio) | 섹터 분산·쏠림 점검 |
| 비교 (Compare) | 두 종목 상대 평가, 섹터 비교 |
| 발굴 (Screener) | 실적+성장 기준 종목 스크리닝 |
| 리스크 검수 (Risk Review) | 환각·숫자 오류 검증, 과열 경고 |

매니저는 질문에 꼭 필요한 직원만 선별 호출하며, 분석 직원들을 병렬로 실행한 뒤
마지막에 리스크 검수를 거쳐 답변을 종합합니다.

---

## 기술 스택

- **언어**: Python 3.10+
- **가격·지표·재무**: finance-datareader, ta, pandas, yfinance
- **뉴스/공시**: yfinance(미국), DART 공시 API + 구글 뉴스 RSS(한국)
- **LLM**: Anthropic Claude API
- **웹 UI**: Streamlit

---

## 설치 및 실행

### 1. 저장소 클론 및 가상환경

```bash
git clone https://github.com/madmax2023240031/stock-agent.git
cd stock-agent
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
```

### 2. 라이브러리 설치

```bash
pip install -r requirements.txt
```

> requirements.txt가 없다면:
> `pip install anthropic finance-datareader ta pandas yfinance python-dotenv feedparser streamlit`

### 3. API 키 설정

프로젝트 루트에 `.env` 파일을 만들고 키를 입력합니다.

```
ANTHROPIC_API_KEY=sk-ant-...
DART_API_KEY=...
```

- **ANTHROPIC_API_KEY**: [Anthropic Console](https://console.anthropic.com)에서 발급 (직원들의 LLM 호출용)
- **DART_API_KEY**: [OpenDART](https://opendart.fss.or.kr)에서 무료 발급 (한국 공시 조회용)

> ⚠️ `.env` 파일은 절대 GitHub에 올리지 마세요. (`.gitignore`에 등록되어 있습니다)

### 4. 실행

```bash
streamlit run app.py
```

브라우저에서 채팅 화면이 열립니다. 종목이나 시황을 자연어로 물어보세요.

---

## 사용 예시

```
삼성전자 지금 어때?
애플이랑 엔비디아 비교해줘
삼성전자 지금 사도 될까?
지금 시장 거시 환경 어때?
미국 성장주 찾아줘
요즘 실적 좋은 한국 종목 찾아줘
```

급변동(±5% 이상)이 있는 종목을 물으면 자동으로 원인 추적 분석이 작동합니다.

---

## 파일 구조

```
stock-agent/
├── tools.py        # 데이터·지표·뉴스·재무·거시·스크리닝 함수
├── employees.py    # 직원(에이전트) 정의
├── manager.py      # 총괄 매니저 + 실행 로직
├── app.py          # Streamlit 웹 UI
├── CLAUDE.md       # 프로젝트 지침 (개발용)
├── .env            # API 키 (git 제외)
└── .gitignore
```

---

## 설계 원칙

- **역할 분리**: 각 직원은 자기 전용 도구만 사용하며 역할을 섞지 않음
- **정직성 우선**: 미래 주가 예측을 하지 않으며, 모든 분석은 "현재 데이터 기반 참고용"
- **견고성**: 무료 데이터 소스의 간헐적 실패에 대비해 캐싱·재시도·부분 성공 처리
- **안전성**: 실매매 기능은 포함하지 않음

---

*이 프로젝트는 학습 및 개인 투자 참고 목적으로 제작되었습니다.*
