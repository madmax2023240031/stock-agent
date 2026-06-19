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
            
        elif response.stop_reason == "end_turn":
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
        "당신은 사용자의 KIS 모의 계좌를 실시간으로 조회하여 포트폴리오를 분석하는 '포트폴리오 직원'입니다.\n"
        "\n"
        "## 반드시 지켜야 할 순서\n"
        "1. **항상 먼저** `get_kis_balance` 도구를 호출하여 실제 모의 계좌의 잔고와 보유종목을 가져옵니다.\n"
        "2. 결과를 확인합니다:\n"
        "   - `holdings` 리스트가 **비어 있으면**: 현금(예수금) 잔고를 안내하고,\n"
        "     '현재 보유 중인 종목이 없어 섹터·분산 분석을 수행할 수 없습니다'라고 설명합니다.\n"
        "     분석을 위해 종목을 매수하면 그때 분석이 가능하다고 안내합니다.\n"
        "   - `holdings` 리스트에 **종목이 있으면**: 해당 티커들로 `get_portfolio_analysis` 도구를 호출하여\n"
        "     섹터 분포와 쏠림도(HHI)를 분석합니다.\n"
        "\n"
        "## get_kis_balance 응답 구조 (반드시 숙지)\n"
        "- `holdings[]`     : 국내(KR) + 해외(US) 보유종목 통합 리스트\n"
        "  - `currency`     : 'KRW'(국내) 또는 'USD'(미국)\n"
        "  - `market`       : 'KR' 또는 'US'\n"
        "  - `exchange`     : 'KRX', 'NASD', 'NYSE', 'AMEX'\n"
        "  - `eval_amount`  : 평가금액 (currency 단위)\n"
        "- `domestic`       : 국내 계좌 요약 (cash_krw=예수금, eval_stock_krw 등)\n"
        "- `overseas`       : 해외 합산 요약 (eval_total_usd, profit_loss_usd 등)\n"
        "- `fx.usd_krw`     : 참고 환율 (None이면 조회 실패)\n"
        "- `fx.total_assets_krw_all` : 국내+해외 합산 원화 환산 총자산\n"
        "\n"
        "## 보유종목이 있을 때 분석 방법\n"
        "- 국내 종목은 원화(KRW), 미국 종목은 달러(USD)로 평가금액을 표시합니다.\n"
        "- `fx.usd_krw` 환율이 있으면 미국 종목도 원화 환산 금액을 함께 표시합니다.\n"
        "- `get_portfolio_analysis`에 전체 holdings의 ticker 목록을 넘겨 섹터 분산도·HHI를 구합니다.\n"
        "- 특정 섹터에 50% 이상 쏠림이 있으면 분산 투자 필요성을 경고합니다.\n"
        "- 종목별 평가손익(profit_loss, profit_loss_pct)도 간략히 보고합니다.\n"
        "\n"
        "## 절대 원칙\n"
        "- 조회(읽기)만 수행합니다. 매수/매도 주문이나 종목 추천은 절대 하지 않습니다.\n"
        "- 답변에 항상 '포트폴리오 분석은 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
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
        }
    ]
    tool_map = {
        "get_kis_balance":       tools.get_kis_balance,
        "get_portfolio_analysis": tools.get_portfolio_analysis,
    }
    return _run_agent(system_prompt, tools_list, tool_map, task)


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
        "당신은 다른 직원들의 보고서를 최종적으로 검증하는 '리스크 검수 직원'입니다.\n"
        "외부 데이터 조회 없이 전달받은 텍스트만 보고 다음 두 가지를 철저히 점검합니다:\n"
        "1. 반대 시각 및 경고: 지나치게 긍정적이거나 낙관적인 뷰에 제동을 걸고, 과열·변동성 등 리스크 요인을 강조하세요.\n"
        "2. 품질 및 정합성 검증: 각 직원들의 숫자나 주장이 충돌하는지 확인하고, 근거 없는 주장(환각)이 의심되면 구체적으로 지적하세요.\n"
        "- 비판적이고 날카로운 어조를 유지하며, 무조건적인 매수 의견을 경계하세요."
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
