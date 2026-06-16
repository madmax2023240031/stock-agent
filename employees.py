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


def _run_agent(system_prompt: str, tools_list: list, tool_map: dict, task: str) -> str:
    """
    Claude API의 Tool Use(Function Calling) 루프를 처리하는 공통 함수.
    stop_reason이 'tool_use'인 경우 도구를 실행하고 결과를 다시 전달한다.
    """
    messages = [{"role": "user", "content": task}]
    
    while True:
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=2048,
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
        "당신은 사용자의 보유 종목들의 섹터 쏠림과 리스크를 점검하는 '포트폴리오 직원'입니다.\n"
        "- 주어진 도구(get_portfolio_analysis)를 사용하여 보유 종목 목록의 섹터 분포와 쏠림도(HHI)를 분석하세요.\n"
        "- 특정 섹터에 비중이 과도하게 몰려 있다면 분산 투자의 필요성을 경고하세요.\n"
        "- 답변에 항상 '포트폴리오 분석은 참고용이며 투자 권유가 아닙니다'라고 명시하세요."
    )
    tools_list = [{
        "name": "get_portfolio_analysis",
        "description": "보유 종목 리스트를 받아 섹터 분포 및 쏠림도(HHI)를 분석합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "holdings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "종목 티커 리스트. 예: ['005930', 'AAPL', 'MSFT']"
                }
            },
            "required": ["holdings"]
        }
    }]
    return _run_agent(system_prompt, tools_list, {"get_portfolio_analysis": tools.get_portfolio_analysis}, task)


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
