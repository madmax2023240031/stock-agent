"""
manager.py
==========
총괄 매니저 로직 및 실행 진입점(대화형 루프).
"""

import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic

import employees

load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL_NAME = "claude-sonnet-4-6"

client = Anthropic(api_key=ANTHROPIC_API_KEY)


def manager(user_input: str, history: list = None, status_callback=None) -> str:
    """
    사용자의 입력을 받아 적절한 직원들을 호출하고 종합하여 답변한다.
    history: 이전 대화 내역 (role: user/assistant 형태의 딕셔너리 리스트)
    status_callback: 직원 호출 시 진행 상황을 알리기 위한 콜백 함수
    """
    system_prompt = (
        "당신은 주식 멀티 에이전트 시스템의 '총괄 매니저'입니다.\n"
        "사용자의 질문을 분석하고 필요한 전담 직원(도구)들을 호출하여 답변을 작성하세요.\n\n"
        "### 지침 (매우 중요) ###\n"
        "1. **비용 절감**: 질문에 꼭 필요한 직원만 선별해서 호출하세요. (예: 현재가만 묻는다면 조회 직원만 호출)\n"
        "2. **역할 분담**: 각 도구(직원)의 특성을 고려해 질문에 맞는 직원을 호출하세요.\n"
        "   - 단순 사실/시세: call_research_employee\n"
        "   - 기술적 분석/시그널: call_signal_employee\n"
        "   - 재무/기업가치: call_fundamental_employee\n"
        "   - 뉴스/공시: call_news_employee\n"
        "   - 금리/환율/거시: call_macro_employee\n"
        "   - 보유 종목 분산/쏠림: call_portfolio_employee\n"
        "   - 두 종목 비교: call_compare_employee\n"
        "3. **리스크 검수**: 분석, 비교, 매수 판단 등이 포함된 복합 질문의 경우 여러 분석 직원을 부르게 됩니다. "
        "분석 직원의 결과를 받은 후에는 반드시 그 결과들을 모아 `call_risk_review_employee`를 마지막에 호출하여 "
        "환각이나 숫자 충돌이 없는지, 위험한 주장이 없는지 검증받으세요. 단순 시세 조회라면 생략해도 무방합니다.\n"
        "4. **절대 원칙**: 미래 주가 예측(예: '3개월 뒤 10만원 갑니다')은 절대 하지 마세요.\n"
        "5. **최종 답변 형식**: 각 직원의 보고를 깔끔하게 종합하여 사용자에게 전달하되, "
        "답변 마지막에는 반드시 '이 정보는 참고용이며 투자 권유가 아닙니다'라는 문구를 포함하세요."
    )
    
    # 매니저가 사용할 도구(직원들) 목록
    tools_list = [
        {
            "name": "call_research_employee",
            "description": "단순 시세(현재가, 등락률, 거래량 등) 사실 정보 조회 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string", "description": "직원에게 지시할 내용"}}, "required": ["task"]}
        },
        {
            "name": "call_signal_employee",
            "description": "기술적 분석(MA, RSI, MACD) 기반 단기 추세/시그널 분석 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_news_employee",
            "description": "종목 관련 뉴스, 공시, 호재/악재 분류 분석 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_fundamental_employee",
            "description": "기업 재무 지표(PER, PBR, EPS, 영업이익) 및 가치 분석 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_macro_employee",
            "description": "거시 경제(금리, 환율, 지수, 원자재) 분석 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_portfolio_employee",
            "description": "보유 종목 리스트의 섹터 분포 및 쏠림 리스크 분석 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_compare_employee",
            "description": "두 종목의 장단점(주가, 재무, 지표) 상대 평가 직원 호출",
            "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}
        },
        {
            "name": "call_risk_review_employee",
            "description": "다른 직원들이 작성한 보고서 텍스트들을 모아 입력받고 환각, 논리 충돌, 과열 위험을 검수받음",
            "input_schema": {"type": "object", "properties": {"reports": {"type": "string", "description": "분석 직원들의 결과 텍스트 모음"}}, "required": ["reports"]}
        }
    ]
    
    tool_map = {
        "call_research_employee": employees.research_employee,
        "call_signal_employee": employees.signal_employee,
        "call_news_employee": employees.news_employee,
        "call_fundamental_employee": employees.fundamental_employee,
        "call_macro_employee": employees.macro_employee,
        "call_portfolio_employee": employees.portfolio_employee,
        "call_compare_employee": employees.compare_employee,
        "call_risk_review_employee": employees.risk_review_employee,
    }
    
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    
    while True:
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
            tools=tools_list
        )
        
        messages.append({"role": "assistant", "content": response.content})
        
        if response.stop_reason == "tool_use":
            tool_results = []
            
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_id = block.id
                    tool_args = block.input
                    
                    print(f"\n[Manager] 👨‍💼 ➡ 🧑‍💻 직원 호출: {tool_name}")
                    if status_callback:
                        status_callback(tool_name)
                    
                    if tool_name in tool_map:
                        try:
                            # 실제 직원 함수 실행 (API 통신 발생)
                            result_str = tool_map[tool_name](**tool_args)
                        except Exception as e:
                            result_str = f"직원 실행 오류: {str(e)}"
                    else:
                        result_str = f"Unknown tool: {tool_name}"
                        
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


if __name__ == "__main__":
    print("==================================================")
    print(" 🤖 주식 멀티 에이전트 시스템 매니저가 시작되었습니다.")
    print("   종료하려면 'quit' 또는 'exit'를 입력하세요.")
    print("==================================================")
    
    while True:
        try:
            user_msg = input("\n👤 사용자: ")
            if user_msg.strip().lower() in ["quit", "exit"]:
                print("시스템을 종료합니다.")
                break
            
            if not user_msg.strip():
                continue
                
            print("\n매니저가 팀원들과 상의 중입니다...\n")
            answer = manager(user_msg)
            print("\n" + "="*50)
            print("👔 총괄 매니저 답변:")
            print("="*50)
            print(answer)
            print("="*50)
            
        except KeyboardInterrupt:
            print("\n시스템을 종료합니다.")
            break
        except Exception as e:
            print(f"\n시스템 에러 발생: {e}")
