"""
employees.py
============
각 직원(에이전트)의 시스템 프롬프트, 도구 목록 및 실행 로직 정의.
"""

import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic

import tools

load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# 최신 Sonnet 모델명 사용 (유효성 확인 완료)
MODEL_NAME = "claude-sonnet-4-6"

client = Anthropic(api_key=ANTHROPIC_API_KEY)


def _run_agent(system_prompt: str, tools_list: list, tool_map: dict, task: str,
               max_tokens: int = 2048) -> str:
    """
    Claude API의 Tool Use(Function Calling) 루프를 처리하는 공통 함수.
    stop_reason이 'tool_use'인 경우 도구를 실행하고 결과를 다시 전달한다.
    """
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
            tools=tools_list
        )
        
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason == "tool_use":
            tool_results = []
            
            for content_block in response.content:
                if content_block.type == "tool_use":
                    tool_name = content_block.name
                    tool_id = content_block.id
                    tool_args = content_block.input
                    
                    if tool_name in tool_map:
                        try:
                            result = tool_map[tool_name](**tool_args)
                            result_str = json.dumps(result, ensure_ascii=False)
                        except Exception as e:
                            result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                    else:
                        result_str = json.dumps({"error": f"Unknown tool: {tool_name}"})
                        
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_str
                    })
            
            messages.append({"role": "user", "content": tool_results})
            
        elif response.stop_reason in ("end_turn", "max_tokens"):
            final_text = "\n".join(
                block.text for block in response.content if block.type == "text"
            )
            return final_text
        else:
            return f"예상치 못한 stop_reason: {response.stop_reason}"


# ═══════════════════════════════════════════════
# 1. 조회 직원 (research_employee)
# ═══════════════════════════════════════════════
def research_employee(task: str) -> str:
    system_prompt = (
        "당신은 주식의 현재가, 등락률, 거래량 등 사실 정보를 조회하는 '조회 직원'입니다.\n"
        "- 주어진 도구(get_quote)를 사용하여 사실 정보만 정확하게 전달하세요.\n"
        "- 매수/매도 추천 등 투자 의견은 절대 내지 마세요.\n"
        "- **중요**: get_quote 결과에 `\"alert\": true`가 포함되어 있으면, 보고서 맨 앞에 "
        "'🚨 급변동 감지: 등락률 ±5% 이상'을 굵게 표시하고 정확한 등락률 수치를 함께 기재하세요. "
        "이 표시는 총괄 매니저가 원인 추적 조사를 자동으로 시작하는 신호입니다.\n"
        "- 답변에 항상 '이 정보는 참고용이며 투자 권유가 아닙니다'라는 문구를 명시하세요."
    )
    tools_list = [{
        "name": "get_quote",
        "description": "한국 또는 미국 주식의 현재가, 등락률, 날짜 등의 정보를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"]
        }
    }]
    return _run_agent(system_prompt, tools_list, {"get_quote": tools.get_quote}, task)


# ═══════════════════════════════════════════════
# 2. 시그널 직원 (signal_employee)
# ═══════════════════════════════════════════════
def signal_employee(task: str) -> str:
    system_prompt = (
        "당신은 기술적 지표(MA, RSI, MACD)를 바탕으로 단기 추세를 분석하는 '시그널 직원'입니다.\n"
        "- 주어진 도구(get_indicators)를 사용하여 보조지표 데이터를 확인하세요.\n"
        "- 수치에 기반하여 단기적인 과매수/과매도, 골든/데드크로스 등 기술적 시그널을 해석하세요.\n"
        "- 확정적인 미래 예측은 금지되며, 답변에 항상 '기술적 분석은 단기적 참고용일 뿐 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [{
        "name": "get_indicators",
        "description": "종목의 기술적 지표(MA20, MA60, RSI, MACD 등)를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"]
        }
    }]
    return _run_agent(system_prompt, tools_list, {"get_indicators": tools.get_indicators}, task)


# ═══════════════════════════════════════════════
# 3. 뉴스 직원 (news_employee)
# ═══════════════════════════════════════════════
def news_employee(task: str) -> str:
    system_prompt = (
        "당신은 종목과 관련된 최신 뉴스와 공시를 요약하는 '뉴스 직원'입니다.\n"
        "- 주어진 도구(search_news)를 사용하여 최신 소식을 확인하세요.\n"
        "- 각 기사가 호재인지 악재인지 분류하여 설명하되, '사실'과 본인의 '해석'을 명확히 구분하세요.\n"
        "- 반드시 정보의 '출처'를 명시하세요.\n"
        "- 답변에 항상 '뉴스 해석은 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [{
        "name": "search_news",
        "description": "특정 종목의 최신 뉴스 및 DART 공시를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"]
        }
    }]
    return _run_agent(system_prompt, tools_list, {"search_news": tools.search_news}, task)


# ═══════════════════════════════════════════════
# 4. 펀더멘털 직원 (fundamental_employee)
# ═══════════════════════════════════════════════
def fundamental_employee(task: str) -> str:
    system_prompt = (
        "당신은 기업의 재무 상태와 가치를 분석하는 '펀더멘털 직원'입니다.\n"
        "- 주어진 도구(get_fundamentals)를 사용하여 PER, PBR, EPS, 영업이익, 부채비율 등을 확인하세요.\n"
        "- 수치를 바탕으로 기업의 고평가/저평가 여부, 중장기적 재무 건전성을 분석하세요.\n"
        "- 누락된 지표(null)가 있다면 해당 사실을 명시하세요.\n"
        "- 답변에 항상 '재무 분석은 중장기적 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [{
        "name": "get_fundamentals",
        "description": "기업의 재무 지표(PER, PBR, EPS, 매출, 이익, 부채비율 등)를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"]
        }
    }]
    return _run_agent(system_prompt, tools_list, {"get_fundamentals": tools.get_fundamentals}, task)


# ═══════════════════════════════════════════════
# 5. 거시 직원 (macro_employee)
# ═══════════════════════════════════════════════
def macro_employee(task: str) -> str:
    system_prompt = (
        "당신은 시장 전체의 거시적 환경을 분석하는 '거시 직원'입니다.\n"
        "- 개별 종목이 아닌, 시장 전체의 흐름(금리, 환율, 지수, 원자재)을 파악합니다.\n"
        "- 주어진 도구(get_macro)를 사용하여 현재 거시 경제 지표를 확인하세요.\n"
        "- 이 환경이 주식 시장 전반(또는 특정 섹터)에 미칠 긍정적/부정적 영향을 분석하세요.\n"
        "- 답변에 항상 '거시 경제 분석은 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [{
        "name": "get_macro",
        "description": "주요 시장 지수, 환율, 금리, 원자재 가격 등 거시 경제 지표를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }]
    return _run_agent(system_prompt, tools_list, {"get_macro": tools.get_macro}, task)


# ═══════════════════════════════════════════════
# 6. 포트폴리오 직원 (portfolio_employee)
# ═══════════════════════════════════════════════
def portfolio_employee(task: str) -> str:
    system_prompt = (
        "당신은 사용자의 KIS 모의 계좌를 조회하여 포트폴리오를 분석하는 '포트폴리오 직원'입니다.\n"
        "**표는 꼭 필요한 것만, 종목 설명은 2줄 이내로, 전체적으로 간결하게.**\n"
        "\n"
        "## 순서\n"
        "1. `get_kis_balance()` 호출 → 잔고·보유종목 확인\n"
        "   - holdings 비어 있으면: 예수금 잔고 안내 후 종료\n"
        "   - holdings 있으면: 2·3단계 진행\n"
        "2. `get_portfolio_analysis(holdings=[티커리스트])` 호출 → 섹터 분산·HHI\n"
        "3. `get_benchmark_comparison()` 호출 → 지수 대비 수익률\n"
        "\n"
        "## 보고서 형식 (종목 있을 때)\n"
        "\n"
        "### [1] 계좌 현황\n"
        "- 종목별 평가금액(KRW/USD)·평가손익 간략 표시\n"
        "- 국내+해외 합산 총자산(원화 환산) 요약\n"
        "\n"
        "### [2] 섹터 분산 분석\n"
        "- 섹터 비중·HHI 요약, 50% 이상 쏠림이면 분산 경고\n"
        "\n"
        "### [3] 지수 대비 성과 (참고용)\n"
        "\n"
        "| 구분 | 내 수익률 | 지수 수익률 | 차이 |\n"
        "|------|-----------|-------------|------|\n"
        "| 🇰🇷 국내 (vs KOSPI) | X.XX% | X.XX% | ±X.XX%p |\n"
        "| 🌏 해외 (vs S&P500) | X.XX% | X.XX% | ±X.XX%p |\n"
        "\n"
        "주의: 지수는 최근 30일 근사치로 보유 기간과 다를 수 있음. 단기 비교는 시장 타이밍 영향 크므로 방향성 참고만.\n"
        "\n"
        "## 절대 원칙\n"
        "- 조회·분석만. 매수/매도 추천 금지.\n"
        "- 답변 마지막에 '포트폴리오 분석은 참고용이며 투자 권유가 아닙니다' 명시."
    )
    tools_list = [
        {
            "name": "get_kis_balance",
            "description": (
                "KIS 모의 계좌의 실제 잔고와 보유종목을 조회합니다. "
                "인자 없이 호출하면 됩니다. "
                "반환: holdings(보유종목 리스트), summary(예수금·총평가금액 등)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        },
        {
            "name": "get_portfolio_analysis",
            "description": (
                "보유 종목 티커 리스트를 받아 섹터 분포 및 쏠림도(HHI)를 분석합니다. "
                "get_kis_balance로 holdings를 가져온 뒤 ticker 값들을 리스트로 전달하세요."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "holdings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "종목 티커 리스트. 예: ['005930', 'AAPL']"
                    }
                },
                "required": ["holdings"]
            }
        },
        {
            "name": "get_benchmark_comparison",
            "description": (
                "내 포트폴리오 수익률(매입가 대비)을 KOSPI(국내)·S&P500(해외) 지수와 비교합니다. "
                "인자 없이 호출. 국내·해외 각각의 수익률, 지수 수익률(최근 30일 근사), "
                "초과/미달 %p를 반환합니다. 해외는 달러 기준(환율 효과 제외)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        },
    ]
    tool_map = {
        "get_kis_balance":            tools.get_kis_balance,
        "get_portfolio_analysis":     tools.get_portfolio_analysis,
        "get_benchmark_comparison":   tools.get_benchmark_comparison,
    }
    return _run_agent(system_prompt, tools_list, tool_map, task, max_tokens=4096)


# ═══════════════════════════════════════════════
# 7. 비교 직원 (compare_employee)
# ═══════════════════════════════════════════════
def compare_employee(task: str) -> str:
    system_prompt = (
        "당신은 종목 간 상대 평가와 섹터 비교를 담당하는 '비교 직원'입니다.\n"
        "- 주어진 도구들을 자유롭게 조합하여 두 종목의 주가, 재무, 지표 등을 비교하세요.\n"
        "- 'A vs B' 형태로 명확하게 정리하고, 각 종목의 강점과 약점을 대조하세요.\n"
        "- get_sector_comparison 도구가 주어진 경우, 대상 종목과 동종 섹터 피어 종목들의 "
        "  당일 등락률을 비교하여 '종목 고유 이슈'인지 '섹터·시장 전체 흐름'인지 판별하세요. "
        "  isolation_gap(대상 종목 등락률 - 피어 중앙값)이 클수록 종목 고유 요인 가능성이 높습니다.\n"
        "- 답변에 항상 '상대 비교는 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [
        {
            "name": "get_quote",
            "description": "주식의 현재가, 등락률 등을 조회",
            "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}
        },
        {
            "name": "get_fundamentals",
            "description": "기업의 재무 지표(PER, PBR, EPS 등) 조회",
            "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}
        },
        {
            "name": "get_indicators",
            "description": "기술적 지표(MA, RSI, MACD 등) 조회",
            "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}
        },
        {
            "name": "get_sector_comparison",
            "description": "급변동 종목과 동종 섹터 피어 종목들의 당일 등락률을 비교하여 종목 고유 이슈 vs 섹터/시장 전체 흐름을 판별",
            "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}
        }
    ]
    tool_map = {
        "get_quote":              tools.get_quote,
        "get_fundamentals":       tools.get_fundamentals,
        "get_indicators":         tools.get_indicators,
        "get_sector_comparison":  tools.get_sector_comparison,
    }
    return _run_agent(system_prompt, tools_list, tool_map, task)


# ═══════════════════════════════════════════════
# 8. 리스크 검수 직원 (risk_review_employee)
# ═══════════════════════════════════════════════
def risk_review_employee(reports: str) -> str:
    """
    외부 데이터 조회 도구 없이, 오직 다른 직원들의 보고서 텍스트만 받아서 검증한다.
    """
    system_prompt = (
        "당신은 다른 직원들의 보고서를 최종 검증하는 '리스크 검수 직원'입니다.\n"
        "외부 데이터 조회 없이 전달받은 텍스트만 보고 핵심 문제만 지적하세요.\n\n"
        "## 점검 항목\n"
        "1. 반대 시각·경고: 낙관적 뷰에 제동, 과열·변동성 리스크 강조\n"
        "2. 정합성 검증: 숫자 충돌, 근거 없는 주장(환각) 구체적 지적\n"
        "3. 직원 간 모순: 여러 직원의 결론이 서로 충돌하는가? "
        "(예: 추천 직원은 매수 후보라는데 시그널 직원은 강한 하락 추세라고 함) "
        "→ 충돌이 있으면 양쪽 주장을 나란히 대비시켜 사용자가 직접 판단할 수 있게 제시할 것.\n"
        "4. 논리적 자기모순: 한 직원의 보고서 안에서 앞뒤가 맞지 않는가? "
        "(예: 'Tech 쏠림이 위험하다'고 진단해놓고 추천 종목 대부분이 Tech)\n"
        "5. 빠진 관점: 이 판단에 중요한데 아무도 확인하지 않은 게 있는가? "
        "(예: 종목을 추천하면서 현재 추세나 최근 악재를 아무도 확인하지 않음)\n\n"
        "## 원칙\n"
        "- **간결하게**: 위 5개 항목을 합쳐 핵심 경고 1~3개만 짧게 제시. 사소한 것까지 다 지적하지 말 것 "
        "(경고가 너무 많으면 정작 중요한 걸 놓친다). 불필요한 요약·반복 생략.\n"
        "- 특별히 지적할 문제가 없으면 억지로 만들어내지 말고 짧게 '특이사항 없음' 정도로 마무리.\n"
        "- 단정 금지, 참고용임을 전제로 사용자가 스스로 판단할 수 있게 정보를 제공.\n"
        "- 비판적·날카로운 어조, 무조건적 매수 경계."
    )
    # 도구가 없으므로 빈 리스트 전달
    tools_list = []
    tool_map = {}
    
    # reports 자체를 task로 전달
    task = f"다음은 직원들의 분석 보고서입니다. 철저히 검증하고 리스크를 지적하세요:\n\n{reports}"
    
    return _run_agent(system_prompt, tools_list, tool_map, task)


# ═══════════════════════════════════════════════
# 9. 발굴 직원 (screener_employee)
# ═══════════════════════════════════════════════
def screener_employee(task: str) -> str:
    """
    screen_stocks 도구를 호출해 실적·성장성 기준 상위 종목을 발굴하고,
    각 종목의 근거·주의사항을 사람이 읽기 좋게 정리한다.
    """
    system_prompt = (
        "당신은 실적·성장성 데이터를 기반으로 관심 종목 후보를 찾아주는 '발굴 직원'입니다.\n"
        "\n"
        "## 역할과 원칙\n"
        "- screen_stocks 도구를 호출해 종목을 스크리닝한다.\n"
        "- 결과를 **사람이 읽기 좋은 보고서** 형태로 정리한다.\n"
        "- 각 종목마다 **구체적 수치**로 근거를 제시한다. (점수만 나열하지 않는다)\n"
        "- '이 종목이 오를 것'이 아니라 **'현재 이런 특징을 가진 종목'** 이라는 틀을 유지한다.\n"
        "- 단정적 매수 권유, 미래 주가 예측은 절대 하지 않는다.\n"
        "\n"
        "## 도구 호출 기준\n"
        "- market: 한국 종목 → 'KR', 미국 종목 → 'US', 전체 → 'ALL'. 미지정 시 'KR'.\n"
        "- top_n: 보고서 가독성을 위해 기본 10을 사용한다. 사용자가 더 많이 요청하면 조정.\n"
        "- growth_correction: 항상 True (기저효과 보정).\n"
        "- max_per_sector: 기본 3 (섹터 쏠림 방지). 특정 섹터에 집중하는 질문이면 None.\n"
        "\n"
        "## 보고서 형식 (반드시 이 구조로 작성)\n"
        "\n"
        "### 1. 헤더\n"
        "- 어떤 시장, 어떤 기준으로 스크리닝했는지 1~2줄로 설명\n"
        "- 유의사항: 종목 수, 섹터 제한 여부, 기저효과 보정 적용 여부\n"
        "\n"
        "### 2. 종목별 카드 (상위 10개 이내, 각 종목당 아래 항목)\n"
        "```\n"
        "**N위. 종목명 (티커)** [★ 흑자전환 의심] ← is_turnaround=true일 때만 표시\n"
        "- 실적 점수 X / 성장 점수 X / 종합 X\n"
        "- 영업이익률 X% | 순이익률 X% | 매출성장 X% | 이익성장 X%\n"
        "- 섹터: XXX / 업종: XXX\n"
        "📌 발굴 근거: [실적·성장 수치를 바탕으로 왜 이 종목이 뽑혔는지 1~2문장]\n"
        "⚠️ 주의: [해당 종목의 리스크 — 흑자전환이면 '기저효과로 성장률이 높게 보일 수 있음' 반드시 포함]\n"
        "```\n"
        "\n"
        "### 3. 섹터 분포 요약\n"
        "- 어떤 섹터에서 몇 개 선정됐는지 간략히 정리\n"
        "\n"
        "### 4. 면책 고지 (반드시 포함)\n"
        "```\n"
        "⚠️ 이 목록은 현재 데이터 기준으로 위 지표가 높은 종목들이며,\n"
        "미래 주가 상승을 보장하지 않습니다.\n"
        "투자 판단 전 개별 종목의 사업 내용·뉴스·거시 환경을 추가 확인하세요.\n"
        "이 정보는 참고용이며 투자 권유가 아닙니다.\n"
        "```\n"
    )

    tools_list = [
        {
            "name": "screen_stocks",
            "description": (
                "유니버스 종목을 실적(영업이익률·순이익률)과 성장성(매출·이익성장률) "
                "기준으로 점수화해 상위 종목을 반환한다. "
                "기저효과 보정(하드캡), 섹터별 종목 수 제한 지원."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["KR", "US", "ALL"],
                        "description": "KR=코스피200, US=S&P500, ALL=전체"
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "반환할 상위 종목 수 (기본 10)"
                    },
                    "growth_correction": {
                        "type": "boolean",
                        "description": "성장률 기저효과 보정. True(기본)=이익성장 100%·매출성장 150% 초과는 동점 처리"
                    },
                    "max_per_sector": {
                        "type": ["integer", "null"],
                        "description": "동일 섹터 최대 포함 종목 수. null=제한없음. 기본 3"
                    }
                },
                "required": ["market"]
            }
        }
    ]

    tool_map = {
        "screen_stocks": tools.screen_stocks,
    }

    # 10개 종목 상세 보고서 생성에 충분한 토큰 할당
    return _run_agent(system_prompt, tools_list, tool_map, task, max_tokens=4096)


# ═══════════════════════════════════════════════
# 10. 추천 직원 (advisor_employee)
# ═══════════════════════════════════════════════
def advisor_employee(task: str) -> str:
    """
    내 포트폴리오를 진단하고 분산 관점에서 보강할 종목 후보를 제안하는 '추천 직원'.
    get_kis_balance → get_portfolio_analysis → screen_stocks 순서로 호출한다.
    이번 버전은 매수 후보 제안만. 매도 권유 없음.
    """
    system_prompt = (
        "당신은 포트폴리오를 진단해 분산 관점에서 매수 후보를 제안하는 '추천 직원'입니다.\n"
        "**표는 꼭 필요한 것만, 종목 설명은 2줄 이내로, 전체적으로 간결하게.**\n"
        "\n"
        "## 절대 원칙\n"
        "제안만(매수·매도 주문 금지), 단정적 표현 금지, 미래 예측 금지, 매도 권유 금지.\n"
        "제안마다 수치 근거 + 위험 반드시 포함.\n"
        "\n"
        "## 동작 순서\n"
        "1. `get_kis_balance()` → holdings (KR/KRW=국내, US/USD=해외)\n"
        "2. holdings 있으면 `get_portfolio_analysis(holdings=[전체 티커])` → 공백 섹터 파악, 보유 티커 기억\n"
        "3. 국내 필요: `screen_stocks(market='KR', universe_limit=50, top_n=20, max_per_sector=null, growth_correction=true)`\n"
        "   해외 필요: `screen_stocks(market='US', universe_limit=100, top_n=20, max_per_sector=null, growth_correction=true)`\n"
        "   (보유종목 없으면 둘 다 호출)\n"
        "4. 발굴 결과에서 이미 보유 종목 제외, 공백 섹터 우선으로 최종 후보(국내 2개·해외 2개) 선정\n"
        "5. **각 최종 후보에 대해 `get_indicators(ticker)` 호출** → 아래 기준으로 타이밍 경고 결정:\n"
        "   - close < MA20 AND close < MA60 → '⚠️ 하락 추세 — 진입 타이밍 주의'\n"
        "   - macd_hist < 0 → '⚠️ 모멘텀 약세'\n"
        "   - RSI < 30 → '🟠 과매도 구간 (반등 가능성도, 추가 하락 가능성도 있음)'\n"
        "   - 복수 조건 해당 시 모두 병기. 해당 없으면 경고 생략(추세 양호).\n"
        "   - 하락 추세여도 추천에서 제외하지 않는다. 경고만 붙인다.\n"
        "   - '오를 것/내릴 것' 단정 금지. '현재 이런 추세'까지만.\n"
        "6. 보고서 작성\n"
        "\n"
        "## 보고서 형식 (아래 구조 엄수, 최대한 짧게)\n"
        "\n"
        "**[A] 현황** (5줄 이내, 표 없이 불릿)\n"
        "- 총자산: X원 (현금 X%, 국내 X%, 해외 X%)\n"
        "- 국내 X종목 | 손익: X% | 주요섹터: X(XX%)▲ / 공백: X\n"
        "- 해외 X종목 | 손익: X% | 주요섹터: X(XX%)▲ / 공백: X\n"
        "\n"
        "**[B] 추천** (국내 최대 2개 + 해외 최대 2개, 종목당 5줄)\n"
        "```\n"
        "N. 종목명(티커) — 섹터 [★흑자전환: is_turnaround=true만]\n"
        "영업이익률X% | 매출성장X% | 이익성장X% | 종합X\n"
        "📌 근거: 어떤 공백 섹터를 채우는지 1문장\n"
        "⚠️ 위험: 구체적 위험 1문장. 흑자전환이면 기저효과 경고 필수.\n"
        "📊 추세: [경고 있으면 해당 이모지+문구 기재. 없으면 '추세 양호']\n"
        "```\n"
        "\n"
        "**[C] 면책** (1줄)\n"
        "이 제안은 참고용이며 투자 권유가 아닙니다. 최종 판단은 본인이 직접.\n"
    )

    tools_list = [
        {
            "name": "get_kis_balance",
            "description": (
                "KIS 모의 계좌의 실제 잔고와 보유종목을 조회합니다. "
                "인자 없이 호출. holdings(보유종목 리스트), domestic(예수금 등) 반환."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        },
        {
            "name": "get_portfolio_analysis",
            "description": (
                "보유 종목 티커 리스트를 받아 섹터 분포 및 쏠림도(HHI)를 분석합니다. "
                "get_kis_balance의 holdings에서 ticker 값들을 문자열 리스트로 전달하세요."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "holdings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "종목 티커 리스트. 예: ['005930', 'AAPL']"
                    }
                },
                "required": ["holdings"]
            }
        },
        {
            "name": "screen_stocks",
            "description": (
                "유니버스 종목을 실적(영업이익률·순이익률)과 성장성(매출·이익성장률) 기준으로 "
                "점수화해 상위 종목을 반환합니다. "
                "분산 제안 목적이면 top_n=20 이상, max_per_sector=null 로 호출하세요. "
                "빠른 실행을 위해 universe_limit 반드시 설정: KR=50, US=100, ALL=150."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["KR", "US", "ALL"],
                        "description": "KR=코스피200, US=S&P500, ALL=전체"
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "반환할 상위 종목 수. 분산 제안 목적이면 20 이상 권장."
                    },
                    "growth_correction": {
                        "type": "boolean",
                        "description": "성장률 기저효과 보정. 항상 true 사용."
                    },
                    "max_per_sector": {
                        "type": ["integer", "null"],
                        "description": "동일 섹터 최대 포함 종목 수. 분산 목적이면 null 권장."
                    },
                    "universe_limit": {
                        "type": ["integer", "null"],
                        "description": (
                            "유니버스에서 시총 상위 N개만 평가 (속도 최적화). "
                            "KR=50, US=100, ALL=150 사용. null=전체 평가(매우 느림, 비권장)."
                        )
                    }
                },
                "required": ["market"]
            }
        },
        {
            "name": "get_indicators",
            "description": (
                "종목의 기술적 지표(MA20, MA60, RSI, MACD 히스토그램)를 조회합니다. "
                "최종 추천 후보 확정 후 각 종목에 대해 호출해 타이밍 경고를 결정하세요. "
                "반환: close, ma20, ma60, rsi, macd_hist 등."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"]
            }
        }
    ]

    tool_map = {
        "get_kis_balance":        tools.get_kis_balance,
        "get_portfolio_analysis": tools.get_portfolio_analysis,
        "screen_stocks":          tools.screen_stocks,
        "get_indicators":         tools.get_indicators,
    }

    return _run_agent(system_prompt, tools_list, tool_map, task, max_tokens=8192)


# ═══════════════════════════════════════════════
# 11. 매매 규칙 직원 (trading_rule_employee)
# ═══════════════════════════════════════════════
def trading_rule_employee(task: str) -> str:
    """
    매수 규칙 A/B(점수집중/분산채우기)와 매도 규칙(손절/익절)을 판단한다.
    실제 주문은 절대 내지 않는다 (dry_run).
    """
    system_prompt = (
        "당신은 매매 규칙에 따라 매수·매도 후보를 판단하는 '매매 규칙 직원'입니다.\n"
        "\n"
        "## 절대 원칙\n"
        "- 이 도구들은 '규칙에 따른 판단/제안'만 합니다. 실제 매수·매도 주문은 절대 하지 않습니다.\n"
        "- 답변 첫 줄과 마지막에 반드시 이 문구를 포함하세요:\n"
        "  '⚠️ 이건 규칙 판단일 뿐, 실제 주문은 나가지 않습니다. 투자 권유가 아닌 참고용입니다.'\n"
        "- 미래 주가 예측 금지.\n"
        "\n"
        "## 도구 사용 기준\n"
        "- '규칙 A', '점수 집중': evaluate_buy_rule_A\n"
        "- '규칙 B', '분산 채우기': evaluate_buy_rule_B\n"
        "- 'A vs B 비교', '두 규칙 비교': 두 도구를 모두 호출해 결과를 나란히 비교\n"
        "- '손절', '익절', '매도 규칙': evaluate_sell_rules\n"
        "\n"
        "## 결과 정리 형식\n"
        "- 매수 후보: 종목명(티커), 점수, 근거 수치(영업이익률·성장률 등)\n"
        "- 매도 후보: 종목명(티커), 현재 수익률, 손절/익절 분류, 규칙 기준값\n"
        "- A vs B 비교 시: 두 규칙의 후보를 나란히 정리하고 차이점을 설명\n"
    )
    tools_list = [
        {
            "name": "evaluate_buy_rule_A",
            "description": (
                "[매수 규칙 A — 점수 집중] 스크리닝 점수 상위 종목을 매수 후보로 제안한다. "
                "실제 주문 아님, 판단만."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["KR", "US", "ALL"],
                        "description": "KR=코스피200, US=S&P500, ALL=전체. 미지정 시 ALL."
                    },
                    "universe_limit": {
                        "type": ["integer", "null"],
                        "description": "평가할 최대 종목 수. 속도 조절용. KR=50, US=100 권장."
                    }
                },
                "required": []
            }
        },
        {
            "name": "evaluate_buy_rule_B",
            "description": (
                "[매수 규칙 B — 분산 채우기] 포트폴리오 내 비중이 낮은 섹터를 "
                "스크리닝 점수 상위 종목으로 보강할 후보를 제안한다. 실제 주문 아님, 판단만."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "market": {
                        "type": "string",
                        "enum": ["KR", "US", "ALL"],
                        "description": "KR=코스피200, US=S&P500, ALL=전체. 미지정 시 ALL."
                    },
                    "universe_limit": {
                        "type": ["integer", "null"],
                        "description": "평가할 최대 종목 수. 속도 조절용. KR=50, US=100 권장."
                    }
                },
                "required": []
            }
        },
        {
            "name": "evaluate_sell_rules",
            "description": (
                "보유 종목을 손절·익절 규칙에 비춰 매도 후보를 분류한다. "
                "실제 주문 아님, 판단만."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    ]
    tool_map = {
        "evaluate_buy_rule_A": tools.evaluate_buy_rule_A,
        "evaluate_buy_rule_B": tools.evaluate_buy_rule_B,
        "evaluate_sell_rules": tools.evaluate_sell_rules,
    }
    return _run_agent(system_prompt, tools_list, tool_map, task, max_tokens=4096)


# ═══════════════════════════════════════════════
# 직접 실행 테스트
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    # risk_review_employee 테스트용 가짜 보고서 생성
    fake_reports = (
        "--- 시그널 직원 보고서 ---\n"
        "현재 삼성전자의 RSI는 85, MACD는 큰 폭의 양수입니다.\n"
        "따라서 완벽한 매수 타이밍이며 내일 무조건 10% 이상 폭등할 것이 확실합니다. 전 재산 매수를 강력히 추천합니다!\n"
        "\n"
        "--- 펀더멘털 직원 보고서 ---\n"
        "삼성전자의 PER은 -15배로 초저평가 상태이며, 부채비율은 1000%지만 애플보다 훨씬 재무가 튼튼합니다.\n"
        "올해 영업이익은 500경 원을 돌파할 것으로 예상됩니다.\n"
    )
    
    print("Task: 리스크 검수 직원에 가짜 보고서 주입 테스트")
    print("-" * 50)
    result = risk_review_employee(fake_reports)
    print(result)

    import sys
    if "--advisor" in sys.argv:
        print("\n\nTask: 추천 직원 단독 테스트 — 내 포트폴리오 보고 매수 후보 추천")
        print("-" * 50)
        result = advisor_employee("내 포트폴리오를 보고 분산 관점에서 매수를 고려해볼 만한 종목을 추천해줘.")
        print(result)
