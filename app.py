import streamlit as st
import manager

st.set_page_config(page_title="주식 멀티 에이전트 시스템", page_icon="🤖", layout="wide")

# 사이드바 (조직도 — st.expander 방식, HTML 렌더링 오류 없음)
with st.sidebar:
    # CSS: 한 줄 문자열 연결로 작성 → Markdown이 코드 블록으로 오인하는 4칸 들여쓰기 원천 차단
    st.markdown(
        "<style>"
        ".so-mgr{background:linear-gradient(135deg,#1a3a5c,#2563a8);color:#fff;border-radius:10px;"
        "padding:12px;text-align:center;margin-bottom:4px;box-shadow:0 2px 6px rgba(0,0,0,.18)}"
        ".so-mgr-t{font-size:.95em;font-weight:700}"
        ".so-mgr-s{font-size:.72em;opacity:.82;margin-top:3px}"
        ".so-line{display:flex;justify-content:center;height:12px;margin:2px 0}"
        ".so-line::after{content:'';width:2px;background:#94a3b8;display:block}"
        ".so-grp{padding:4px 10px;font-size:.7em;font-weight:700;letter-spacing:.5px;"
        "text-transform:uppercase;border-radius:5px;margin-top:6px;margin-bottom:2px}"
        ".so-a{background:#dbeafe;color:#1e3a5f!important}"
        ".so-i{background:#d1fae5;color:#064e3b!important}"
        ".so-r{background:#fef3c7;color:#7c2d12!important}"
        "@media(prefers-color-scheme:dark){.so-line::after{background:#475569}}"
        "</style>",
        unsafe_allow_html=True,
    )

    # 매니저 카드 (시각적 헤더) + expander (상세 설명)
    st.markdown(
        '<div class="so-mgr">'
        '<div class="so-mgr-t">👨‍💼 총괄 매니저</div>'
        '<div class="so-mgr-s">질문 분석 · 팀 배분 · 종합 답변</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    with st.expander("▸ 총괄 매니저 상세 설명"):
        st.caption(
            "사용자 질문을 분석해 꼭 필요한 직원만 선별 호출하고, 각 직원의 보고를 종합해 "
            "최종 답변을 작성합니다. 복합 분석 시에는 반드시 마지막에 리스크 검수를 거치며, "
            "불필요한 직원은 부르지 않아 비용과 시간을 절약합니다."
        )

    st.markdown('<div class="so-line"></div>', unsafe_allow_html=True)

    # ── 분석팀 ──
    st.markdown('<div class="so-grp so-a">📊 분석팀</div>', unsafe_allow_html=True)
    with st.expander("🔍 조회 (Research) — 현재가 · 등락률 · 거래량"):
        st.caption(
            "현재가, 등락률, 거래량 등 사실 정보를 `get_quote` 도구로 조회합니다. "
            "한국(6자리 코드)·미국(티커) 종목을 모두 처리하며, 투자 의견 없이 수치 사실만 전달합니다."
        )
    with st.expander("📈 시그널 (Signal) — MA · RSI · MACD"):
        st.caption(
            "`get_indicators` 도구로 MA, RSI, MACD 등 기술적 지표를 분석합니다. "
            "과매수·과매도, 골든/데드크로스 같은 단기 신호를 해석하되, 기술적 분석은 단기 참고용임을 항상 명시합니다."
        )
    with st.expander("🏢 펀더멘털 (Fundamental) — PER · PBR · 재무"):
        st.caption(
            "`get_fundamentals` 도구로 PER, PBR, EPS, 영업이익, 부채비율을 확인합니다. "
            "수치를 바탕으로 고평가·저평가 여부와 중장기 재무 건전성을 진단하며, 누락 지표는 명시합니다."
        )

    # ── 정보팀 ──
    st.markdown('<div class="so-grp so-i">📡 정보팀</div>', unsafe_allow_html=True)
    with st.expander("📰 뉴스 (News) — 공시 · 호재/악재"):
        st.caption(
            "`search_news` 도구로 종목 관련 최신 뉴스와 DART 공시를 조회합니다. "
            "각 뉴스를 호재·악재로 분류하고 출처를 명시하며, 사실과 해석을 명확히 구분해 전달합니다."
        )
    with st.expander("🌍 거시 (Macro) — 금리 · 환율 · 지수"):
        st.caption(
            "`get_macro` 도구로 금리, 환율, 주요 지수, 원자재 등 시장 전체 환경을 파악합니다. "
            "개별 종목이 아닌 '판 전체'를 보며, 섹터·매크로가 시장에 미치는 영향을 분석합니다."
        )

    # ── 발굴팀 ──
    st.markdown('<div class="so-grp" style="background:#ede9fe;color:#4c1d95!important;padding:4px 10px;font-size:.7em;font-weight:700;letter-spacing:.5px;text-transform:uppercase;border-radius:5px;margin-top:6px;margin-bottom:2px">🔭 발굴팀</div>', unsafe_allow_html=True)
    with st.expander("🔭 발굴 (Screener) — 종목 발굴·스크리닝"):
        st.caption(
            "`screen_stocks` 도구로 KOSPI200·S&P500 유니버스에서 실적(영업이익률·순이익률)과 "
            "성장성(매출·이익성장률)이 높은 종목을 발굴합니다. "
            "기저효과 보정·섹터 분산 필터 적용. '성장주 찾아줘', '실적 좋은 종목 알려줘' 등에 응답합니다."
        )

    # ── 검토팀 ──
    st.markdown('<div class="so-grp so-r">🔎 검토팀</div>', unsafe_allow_html=True)
    with st.expander("💼 포트폴리오 (Portfolio) — 섹터 분산"):
        st.caption(
            "`get_portfolio_analysis` 도구로 보유 종목들의 섹터 분포와 쏠림도(HHI)를 분석합니다. "
            "특정 섹터에 비중이 과도하게 쏠려 있으면 분산 투자의 필요성을 경고합니다."
        )
    with st.expander("⚖️ 비교 (Compare) — 두 종목 상대 평가"):
        st.caption(
            "`get_quote`·`get_fundamentals`·`get_indicators` 도구를 조합해 두 종목을 상대 평가합니다. "
            "'A vs B' 형태로 주가·재무·지표를 비교해 각 종목의 강점과 약점을 대조합니다."
        )
    with st.expander("🚨 리스크검수 (Risk Review) — 환각·과열 경고"):
        st.caption(
            "다른 직원들의 분석 보고서만 입력받아 검증합니다. "
            "숫자 오류·논리 충돌·근거 없는 주장(환각)을 적발하고, 지나치게 낙관적인 판단에 제동을 걸어 "
            "과열·변동성 리스크를 경고합니다. 외부 데이터는 직접 조회하지 않습니다."
        )

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
                "call_screener_employee":    "🔭 발굴",
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
                "call_screener_employee": "🔭 발굴 직원에게 종목 스크리닝 요청 중...",
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
