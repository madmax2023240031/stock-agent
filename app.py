import streamlit as st
import manager

st.set_page_config(page_title="주식 멀티 에이전트 시스템", page_icon="🤖", layout="wide")

# 사이드바 (직원 목록)
with st.sidebar:
    st.title("👨‍💼 에이전트 팀 구성도")
    st.markdown("---")
    st.markdown("**🔍 조회 직원 (Research)**: 단순 시세, 주가 조회")
    st.markdown("**📊 시그널 직원 (Signal)**: 기술적 분석, 단기 추세")
    st.markdown("**📰 뉴스 직원 (News)**: 최신 공시, 뉴스 요약")
    st.markdown("**🏢 펀더멘털 직원 (Fundamental)**: 재무/가치 분석")
    st.markdown("**🌍 거시 직원 (Macro)**: 금리, 환율, 지수 분석")
    st.markdown("**💼 포트폴리오 직원 (Portfolio)**: 섹터 분산, 쏠림 점검")
    st.markdown("**⚖️ 비교 직원 (Compare)**: 두 종목의 상대 비교")
    st.markdown("**🚨 리스크 검수 직원 (Risk Review)**: 환각/숫자 검증 및 위험 지적")
    st.markdown("---")
    st.caption("※ 모든 의견은 투자 권유가 아니며, 미래 주가 예측을 제공하지 않습니다.")

st.title("🤖 주식 투자 총괄 매니저")
st.markdown("궁금한 주식 종목이나 시황을 물어보세요! (예: 삼성전자 현재가 알려줘, 애플이랑 테슬라 비교해줘)")

if "messages" not in st.session_state:
    st.session_state.messages = []

# 기존 대화 표시
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("메시지를 입력하세요...")
if user_input:
    # 1. 사용자 메시지 화면에 출력
    with st.chat_message("user"):
        st.markdown(user_input)
    
    # 2. 대화 기록에 저장
    st.session_state.messages.append({"role": "user", "content": user_input})
    
    # 3. 매니저 응답 생성
    with st.chat_message("assistant"):
        # manager()에 넘길 때는 이번 입력(user_input)을 제외한 과거 내역만 넘김
        history_for_manager = st.session_state.messages[:-1]
        
        with st.status("매니저가 팀원들과 상의 중입니다...", expanded=True) as status:
            EMPLOYEE_LABELS = {
                "call_research_employee": "🔍 조회",
                "call_signal_employee": "📊 시그널",
                "call_news_employee": "📰 뉴스",
                "call_fundamental_employee": "🏢 펀더멘털",
                "call_macro_employee": "🌍 거시",
                "call_portfolio_employee": "💼 포트폴리오",
                "call_compare_employee": "⚖️ 비교",
                "call_risk_review_employee": "🚨 리스크검수",
            }
            EMPLOYEE_DETAIL = {
                "call_research_employee": "🔍 조회 직원에게 정보 요청 중...",
                "call_signal_employee": "📊 시그널 직원에게 분석 요청 중...",
                "call_news_employee": "📰 뉴스 직원에게 소식 확인 중...",
                "call_fundamental_employee": "🏢 펀더멘털 직원에게 재무 분석 요청 중...",
                "call_macro_employee": "🌍 거시 직원에게 시장 환경 분석 중...",
                "call_portfolio_employee": "💼 포트폴리오 직원에게 점검 요청 중...",
                "call_compare_employee": "⚖️ 비교 직원에게 종목 비교 요청 중...",
                "call_risk_review_employee": "🚨 리스크 검수 직원에게 최종 검증 받는 중...",
            }

            def status_cb(tool_name):
                if tool_name.startswith("__parallel_batch__:"):
                    names = tool_name.split(":", 1)[1].split(",")
                    labels = [EMPLOYEE_LABELS.get(n, n) for n in names]
                    status.write(f"🔄 {len(names)}명 직원 **병렬** 동시 분석 중: {' · '.join(labels)}")
                else:
                    status.write(EMPLOYEE_DETAIL.get(tool_name, f"작업 요청: {tool_name}"))
                
            try:
                response = manager.manager(user_input, history=history_for_manager, status_callback=status_cb)
                status.update(label="보고서 작성이 완료되었습니다!", state="complete", expanded=False)
            except Exception as e:
                response = f"오류가 발생했습니다: {str(e)}"
                status.update(label="에러 발생", state="error")
                
        st.markdown(response)
        
    st.session_state.messages.append({"role": "assistant", "content": response})

st.markdown("---")
st.caption("⚠️ **면책 조항**: 본 서비스에서 제공하는 모든 정보와 분석 내용은 투자 참고용일 뿐이며, 어떠한 경우에도 투자 결과에 대한 법적 책임 소재의 증빙자료로 사용될 수 없습니다. 최종 투자 결정은 전적으로 투자자 본인의 판단과 책임하에 하시기 바랍니다.")
