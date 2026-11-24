"""
웹앱 화면 꾸밈 전용 모듈 (2026-11-23 디자인 개편).

- 숫자·표에 들어가는 값은 전부 pnl_view의 기존 함수(collect · _holdings_rows · live_snapshot ·
  busy_window)에서 그대로 가져온다. 여기서는 값을 새로 계산하거나 파일을 쓰지 않는다.
- 화면 표시용 계산은 뺄셈 두 가지뿐: 킬스위치까지 남은 폭(누적 − 한도), 지금 시세와 회차 값의 차이.
- 누적 수익률 흐름 그래프는 국내 로그(logs/autotrader_YYYYMMDD.log)에서 날짜별 마지막
  "킬스위치 [SELL] … 누적 X%" 줄을 읽기만 한다(시즌 2 시작 2026-09-30부터). 없는 날은 건너뛴다.
- 이 모듈에서 어떤 오류가 나도 예전 화면(pnl_view.render_main)으로 돌아가 사이트는 계속 뜬다.
- 움직임(2026-11-23 3차): 그래프 선이 처음 한 번 그려지며 들어오고, 시세 재계산 숫자가 굴러간다.
  맥의 "동작 줄이기"를 켜면 둘 다 멈춘 최종 화면으로 바로 보인다. 둘 다 CSS만 쓴다(JS 없음).
"""
from __future__ import annotations

import glob
import html
import os
import re

import altair as alt
import pandas as pd
import streamlit as st

import pnl_view

_CSS = """
<style>
:root{
  --sa-up:#5FBF8F; --sa-down:#E2706A; --sa-hair:#24272D; --sa-muted:#8C8F96;
  --sa-panel:#121418; --sa-accent:#C8B27C;
}
::selection{background:rgba(200,178,124,.28)}
.block-container{max-width:1240px; padding-top:2.6rem; padding-bottom:3rem}
[data-testid="stSidebar"] .block-container{padding-top:2rem}
[data-testid="stSidebarContent"] h3, .sa-side-h{font-size:12.5px; font-weight:600; color:var(--sa-muted); letter-spacing:.01em; margin:0 0 6px}

.sa-top{display:flex; justify-content:space-between; align-items:flex-end; gap:32px;
  padding-bottom:22px; margin-bottom:26px; border-bottom:1px solid var(--sa-hair)}
.sa-title{font-size:28px; font-weight:650; letter-spacing:-.025em; line-height:1.2; margin:0}
.sa-sub{color:var(--sa-muted); font-size:13.5px; margin-top:8px}
.sa-status{font-size:12.5px; color:var(--sa-muted); text-align:right; line-height:1.75;
  font-variant-numeric:tabular-nums; white-space:nowrap}
.sa-status b{color:inherit; font-weight:500; color:#C9C6BF}
.sa-dot{display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:7px;
  vertical-align:1px; background:var(--sa-up)}
.sa-dot.off{background:var(--sa-down)}
@keyframes sa-pulse{0%{box-shadow:0 0 0 0 rgba(95,191,143,.55)}70%{box-shadow:0 0 0 7px rgba(95,191,143,0)}100%{box-shadow:0 0 0 0 rgba(95,191,143,0)}}
.sa-dot:not(.off){animation:sa-pulse 2.6s cubic-bezier(.16,1,.3,1) infinite}

.sa-sec{display:flex; justify-content:space-between; align-items:baseline; gap:16px; margin:6px 0 12px}
.sa-sec h3{font-size:15px; font-weight:600; letter-spacing:-.01em; margin:0; padding:0}
.sa-meta{font-size:12px; color:var(--sa-muted); font-variant-numeric:tabular-nums}

.sa-kpis{display:grid; grid-template-columns:1.35fr 1fr 1fr 1fr; background:var(--sa-panel);
  border:1px solid var(--sa-hair); border-radius:10px; overflow:hidden; margin-bottom:14px}
.sa-kpi{padding:20px 24px 18px; border-left:1px solid var(--sa-hair); min-width:0; transition:background-color .18s ease}
.sa-kpi:hover{background:rgba(255,255,255,.025)}
.sa-kpi:first-child{border-left:0}
.sa-kpi .l{font-size:12.5px; color:var(--sa-muted)}
.sa-kpi .v{font-size:36px; font-weight:450; letter-spacing:-.03em; line-height:1; margin-top:10px; height:52px;
  display:flex; align-items:flex-end; font-variant-numeric:tabular-nums; white-space:nowrap}
.sa-kpi:first-child .v{font-size:46px; font-weight:500}
.sa-kpi .s{font-size:12px; color:var(--sa-muted); margin-top:10px; font-variant-numeric:tabular-nums}
.sa-up{color:var(--sa-up)} .sa-down{color:var(--sa-down)}
@keyframes sa-rise{from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none}}
.sa-kpis.live{background:transparent; animation:sa-rise .45s cubic-bezier(.16,1,.3,1) both}
.sa-kpis.live .v{font-size:28px; height:38px} .sa-kpis.live .sa-kpi:first-child .v{font-size:32px}

.sa-note{font-size:12px; color:var(--sa-muted); line-height:1.6; margin:10px 0 0}
.sa-gap{height:22px}
.sa-alert{border:1px solid rgba(226,112,106,.45); background:rgba(226,112,106,.08); color:#F0B7B3;
  border-radius:8px; padding:10px 14px; font-size:13px; margin:0 0 12px}

[data-testid="stTabs"] [data-baseweb="tab-list"]{gap:22px; border-bottom:1px solid var(--sa-hair)}
[data-testid="stTabs"] button[role="tab"]{padding:8px 0; font-size:13.5px}
[data-testid="stTabs"] button[role="tab"] p{font-size:13.5px}

[data-testid="stChatInput"]{border-radius:10px}
[data-testid="stVegaLiteChart"]{margin-top:-4px}
@keyframes sa-draw{from{clip-path:inset(-20px 100% -20px 0)} to{clip-path:inset(-20px 0 -20px 0)}}
[data-testid="stVegaLiteChart"] .mark-line path, [data-testid="stVegaLiteChart"] .mark-area path{
  animation:sa-draw 1.3s cubic-bezier(.65,0,.35,1) .1s both}
.od{display:inline-block; height:1em; overflow:hidden; vertical-align:bottom; line-height:1}
.od-s{display:flex; flex-direction:column; transform:translateY(calc(var(--d) * -1em));
  animation:sa-roll .9s cubic-bezier(.16,1,.3,1) both}
.od-s>span{display:block; height:1em; line-height:1}
@keyframes sa-roll{from{transform:translateY(0)} to{transform:translateY(calc(var(--d) * -1em))}}
@media (prefers-reduced-motion:reduce){
  .sa-dot:not(.off){animation:none}
  .sa-kpis.live{animation:none}
  [data-testid="stVegaLiteChart"] .mark-line path, [data-testid="stVegaLiteChart"] .mark-area path{animation:none}
  .od-s{animation:none}
}
@media (max-width:980px){
  .sa-top{flex-direction:column; align-items:flex-start}
  .sa-status{text-align:left}
  .sa-kpis{grid-template-columns:1fr 1fr}
  .sa-kpi:nth-child(3){border-left:0}
  .sa-kpi:nth-child(n+3){border-top:1px solid var(--sa-hair)}
}
</style>
"""


def inject_css() -> None:
    st.html(_CSS)


def _e(v) -> str:
    return html.escape(str(v))


SEASON2_START = "20260930"   # 시즌 2 국내 첫 무인 회차(2026-09-30)
_SELL_KS_RE = re.compile(r"킬스위치 \[SELL\].*누적 ([+-]?\d+(?:\.\d+)?)%")
_US_DECISION_KO = {"US_LIVE_ORDER": "체결 · 장부 기록", "US_LIVE_UNFILLED": "확인 시점 미체결",
                   "US_LIVE_UNKNOWN": "결과 미확인", "ERROR": "오류"}
_UP, _DOWN, _ACCENT, _MUTED, _HAIR = "#5FBF8F", "#E2706A", "#C8B27C", "#8C8F96", "#24272D"


@st.cache_data(ttl=300, show_spinner=False)
def _parse_history(sig: tuple) -> list[tuple[str, float]]:
    """sig = ((경로, 수정시각), ...). 각 파일의 마지막 [SELL] 킬스위치 누적값만 뽑는다(읽기 전용)."""
    out = []
    for path, _mtime in sig:
        last = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _SELL_KS_RE.search(line)
                    if m:
                        last = float(m.group(1))
        except OSError:
            continue
        if last is not None:
            d = os.path.basename(path)[11:19]
            out.append((f"{d[:4]}-{d[4:6]}-{d[6:8]}", last))
    return out


def season_history(base=None) -> list[tuple[str, float]]:
    base = str(base or pnl_view._BASE_DIR)
    files = sorted(f for f in glob.glob(os.path.join(base, "logs", "autotrader_2*.log"))
                   if os.path.basename(f)[11:19] >= SEASON2_START)
    sig = tuple((f, os.path.getmtime(f)) for f in files)
    return _parse_history(sig)


def _color_pct(v) -> str:
    s = str(v)
    if s.startswith("+") and s != "+0.00%":
        return f"color:{_UP}"
    if s.startswith("-") and s != "-0.00%":
        return f"color:{_DOWN}"
    return ""


def _sign_cls(v) -> str:
    if isinstance(v, (int, float)):
        if v > 0:
            return "sa-up"
        if v < 0:
            return "sa-down"
    return ""


def _odometer(text: str) -> str:
    """숫자 자리마다 0~9 띠를 세워 계기판처럼 굴린다. 화면 읽기 프로그램에는 원문을 그대로 준다."""
    parts, k = [], 0
    for ch in text:
        if ch.isdigit():
            col = "".join(f"<span>{n}</span>" for n in range(10))
            parts.append(f'<span class="od"><span class="od-s" style="--d:{ch};animation-delay:{k * 35}ms">{col}</span></span>')
            k += 1
        else:
            parts.append(_e(ch))
    return f'<span role="img" aria-label="{_e(text)}"><span aria-hidden="true">{"".join(parts)}</span></span>'


def _kpi(label: str, value, sub: str = "", fmt=pnl_view._pct, roll: bool = False) -> str:
    shown = _odometer(fmt(value)) if roll else _e(fmt(value))
    return (f'<div class="sa-kpi"><div class="l">{_e(label)}</div>'
            f'<div class="v {_sign_cls(value)}">{shown}</div>'
            f'<div class="s">{_e(sub) if sub else "&nbsp;"}</div></div>')


def _last_runs(d: dict) -> list[str]:
    out = []
    kr = d.get("kr_log") or {}
    if kr.get("file"):
        f = kr["file"][11:19]
        out.append(f"국내 <b>{_e(f[4:6])}/{_e(f[6:8])} {_e(kr.get('last_time') or '—')}</b>")
    us = d.get("us_log") or {}
    if us.get("file"):
        f = us["file"][14:22]
        out.append(f"미국 <b>{_e(f[4:6])}/{_e(f[6:8])} {_e(us.get('last_time') or '—')}</b>")
    return out


def _render_top(d: dict) -> None:
    ks = d.get("ks") or {}
    tripped = bool(ks.get("daily_triggered") or ks.get("cumulative_triggered"))
    if not ks:
        ks_line = '<span class="sa-dot off"></span>킬스위치 확인 불가'
    elif tripped:
        ks_line = '<span class="sa-dot off"></span>킬스위치 발동'
    else:
        ks_line = '<span class="sa-dot"></span>킬스위치 정상'
    runs = _last_runs(d)
    runs_line = ("마지막 회차 · " + " · ".join(runs)) if runs else "마지막 회차 확인 불가"
    st.markdown(
        '<div class="sa-top"><div>'
        '<div class="sa-title">주식 투자 총괄 매니저</div>'
        '<div class="sa-sub">자동매매 성적을 한눈에 보고, 종목이나 시황은 아래에서 물어보세요.</div>'
        f'</div><div class="sa-status">{ks_line}<br>{runs_line}</div></div>',
        unsafe_allow_html=True,
    )


def _render_scoreboard(d: dict) -> None:
    ks = d.get("ks")
    meta = ""
    if ks:
        at = str(ks.get("at") or "확인 불가")[:16].replace("T", " ")
        meta = f"{at} 회차 계산 · 기준 {pnl_view.KS_BASE_KRW:,}원 · 실현+평가"
    st.markdown(f'<div class="sa-sec"><h3>자동매매 성적</h3><span class="sa-meta">{_e(meta)}</span></div>',
                unsafe_allow_html=True)

    if d.get("lock_us"):
        st.markdown('<div class="sa-alert">미국 실주문 잠금 중 — KIS 앱에서 해외 체결을 확인해 주세요.</div>',
                    unsafe_allow_html=True)
    if d.get("lock_kr"):
        st.markdown('<div class="sa-alert">국내 결과 미확인 잠금 중</div>', unsafe_allow_html=True)

    if not ks:
        st.caption(f"손익률 확인 불가 — kill_switch_state.json {d.get('ks_error', '')}")
    else:
        if ks.get("daily_triggered") or ks.get("cumulative_triggered"):
            st.markdown('<div class="sa-alert">킬스위치 발동 상태</div>', unsafe_allow_html=True)
        cum = ks.get("cumulative")
        room = (f"킬스위치 {pnl_view.KS_CUMULATIVE_LIMIT_PCT:.0f}%까지 {cum - pnl_view.KS_CUMULATIVE_LIMIT_PCT:.2f}%p"
                if isinstance(cum, (int, float)) else f"킬스위치 {pnl_view.KS_CUMULATIVE_LIMIT_PCT:.0f}%")
        st.markdown(
            '<div class="sa-kpis">'
            + _kpi("누적 수익률 (A+B)", cum, room)
            + _kpi("규칙 A", ks.get("A"), "점수 집중 · 기준 3,000만 원")
            + _kpi("규칙 B", ks.get("B"), "분산 채우기 · 기준 3,000만 원")
            + _kpi("오늘", ks.get("daily"), f"킬스위치 {pnl_view.KS_DAILY_LIMIT_PCT:.0f}%")
            + '</div>',
            unsafe_allow_html=True,
        )


def _render_history() -> None:
    hist = season_history()
    if len(hist) < 2:
        return
    df = pd.DataFrame(hist, columns=["날짜", "누적"])
    df["날짜"] = pd.to_datetime(df["날짜"])
    first, last = hist[0][0], hist[-1][0]
    st.markdown(f'<div class="sa-gap"></div><div class="sa-sec"><h3>누적 수익률 흐름</h3>'
                f'<span class="sa-meta">시즌 2 · {_e(first[5:].replace("-", "/"))}–{_e(last[5:].replace("-", "/"))} · '
                f'{len(hist)}거래일 · 날짜별 마지막 회차 킬스위치 기록</span></div>', unsafe_allow_html=True)
    line_color = _DOWN if hist[-1][1] < 0 else _UP
    x = alt.X("날짜:T", axis=alt.Axis(format="%m/%d", title=None, labelColor=_MUTED, tickColor=_HAIR,
                                     domainColor=_HAIR, labelFontSize=11, tickCount=8, grid=False))
    y = alt.Y("누적:Q", axis=alt.Axis(title=None, labelColor=_MUTED, gridColor="#1B1D22", domain=False,
                                     ticks=False, labelFontSize=11, format="+.1f", labelExpr="datum.label + '%'"),
              scale=alt.Scale(zero=True, nice=True))
    base = alt.Chart(df)
    area = base.mark_area(color=line_color, opacity=0.10).encode(x=x, y=y)
    line = base.mark_line(color=line_color, strokeWidth=1.8).encode(x=x, y=y)
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#3A3D44", strokeDash=[3, 3]).encode(y="y:Q")
    end = base.transform_window(rank="rank()", sort=[alt.SortField("날짜", order="descending")]) \
        .transform_filter("datum.rank == 1")
    end_dot = end.mark_circle(size=46, color=line_color, opacity=1).encode(x=x, y=y)
    hover = alt.selection_point(nearest=True, on="pointerover", fields=["날짜"], empty=False, clear="pointerout")
    pick = base.mark_point(size=60, opacity=0).encode(
        x=x, y=y, tooltip=[alt.Tooltip("날짜:T", format="%Y-%m-%d", title="날짜"),
                           alt.Tooltip("누적:Q", format="+.2f", title="누적 수익률(%)")]).add_params(hover)
    rule = base.mark_rule(color="#4A4E57").encode(x=x).transform_filter(hover)
    dot = base.mark_circle(size=52, color="#E9E6DF").encode(x=x, y=y).transform_filter(hover)
    chart = (area + zero + line + rule + dot + end_dot + pick).properties(height=190) \
        .configure(background="transparent", font="Pretendard Variable, sans-serif") \
        .configure_view(strokeWidth=0)
    st.altair_chart(chart, width="stretch", theme=None)


def _render_live(names: dict, ks: dict | None = None) -> None:
    busy = pnl_view.busy_window()
    c1, c2 = st.columns([0.9, 3.1], vertical_alignment="center")
    with c1:
        clicked = st.button("지금 시세로 다시 계산", icon=":material/refresh:", disabled=bool(busy),
                            help="누를 때만 네이버·야후 시세(FinanceDataReader)로 계산합니다. KIS 호출 없음, 저장 없음.")
    with c2:
        if busy:
            st.caption(f"회차 시간대({busy} KST)라 잠시 막았습니다. 봇 회차가 끝난 뒤 눌러 주세요.")
        else:
            st.caption("위 숫자는 마지막 회차 값입니다. 누르면 지금 시세로 다시 계산합니다(몇 초 · 시세는 몇 분 늦을 수 있음).")
    if clicked:
        with st.spinner("시세 가져오는 중…"):
            try:
                st.session_state["pv_live"] = pnl_view.live_snapshot()
            except Exception as e:  # noqa: BLE001
                st.session_state["pv_live"] = {"error": f"계산 실패: {type(e).__name__}"}
    live = st.session_state.get("pv_live")
    if not live:
        return
    if "error" in live:
        st.warning(f"지금 시세 계산 확인 불가 — {live['error']}")
        return
    at = str(live["at"])[:19].replace("T", " ")
    st.markdown(f'<div class="sa-gap"></div><div class="sa-sec"><h3>지금 시세 기준</h3>'
                f'<span class="sa-meta">{_e(at)} 계산 · 저장 안 함</span></div>', unsafe_allow_html=True)
    won = pnl_view._won
    ks = ks or {}

    def _vs(now, then) -> str:
        if isinstance(now, (int, float)) and isinstance(then, (int, float)):
            return f" · 회차 대비 {now - then:+.2f}%p"
        return ""
    st.markdown(
        '<div class="sa-kpis live">'
        + _kpi("누적 수익률 (A+B)", live["cumulative_pct"],
               won(live["cumulative_krw"]) + _vs(live["cumulative_pct"], ks.get("cumulative")), roll=True)
        + _kpi("규칙 A", live["A_pct"], won(live["A_krw"]) + _vs(live["A_pct"], ks.get("A")), roll=True)
        + _kpi("규칙 B", live["B_pct"], won(live["B_krw"]) + _vs(live["B_pct"], ks.get("B")), roll=True)
        + _kpi("평가 손익 (보유 중)", live["unrealized_krw"], "들고 있는 종목만", fmt=won, roll=True)
        + '</div>',
        unsafe_allow_html=True,
    )
    st.caption(f"실현 {won(live['realized_krw'])} + 평가 {won(live['unrealized_krw'])} = 누적 {won(live['cumulative_krw'])}"
               f" · 지금 평가액 {won(live['eval_krw']).lstrip('+')} · 수익률은 수수료 제외")
    table = []
    for r in live["rows"]:
        unit = "USD" if r["usd"] else "원"
        fmt = (lambda v: f"{v:,.2f}") if r["usd"] else (lambda v: f"{v:,.0f}")
        table.append({
            "종목": names.get(r["ticker"]) or r["ticker"], "코드": r["ticker"],
            "수량": r["qty"], "규칙 A": r["A"], "규칙 B": r["B"],
            "평균 매수가": f"{fmt(r['avg'])} {unit}" if r["avg"] else "—",
            "지금 가격": f"{fmt(r['price'])} {unit}" if r["price"] is not None else "—",
            "수익률": f"{r['pct']:+.2f}%" if isinstance(r["pct"], (int, float)) else "—",
        })
    if table:
        with st.expander(f"종목별 지금 시세 ({len(table)})"):
            st.dataframe(pd.DataFrame(table).style.map(_color_pct, subset=["수익률"]),
                         hide_index=True, width="stretch")
    if live.get("warnings"):
        st.caption("계산 경고: " + " / ".join(str(w)[:80] for w in live["warnings"]))


def _render_tables(d: dict) -> None:
    rows = pnl_view._holdings_rows(d)
    lg = d.get("ledger") or {}
    recent = []
    for t in lg.get("recent", []):
        price = t.get("price")
        ptxt = (f"{price:,.2f} USD" if t.get("currency") == "USD" else f"{price:,.0f}원") \
            if isinstance(price, (int, float)) else "—"
        recent.append({"시각": str(t.get("timestamp"))[:16].replace("T", " "),
                       "규칙": t.get("rule_tag"), "매매": t.get("side"), "종목": t.get("ticker"),
                       "수량": t.get("qty"), "가격": ptxt})
    us = d.get("us_live") or []

    labels = [f"보유 종목 {len(rows)}", f"최근 거래 {len(recent)}"]
    if us:
        labels.append(f"미국 실주문 {len(us)}")
    st.markdown('<div class="sa-gap"></div>', unsafe_allow_html=True)
    tabs = st.tabs(labels)
    with tabs[0]:
        if rows:
            st.dataframe(pd.DataFrame(rows).style.map(_color_pct, subset=["수익률"]),
                         hide_index=True, width="stretch")
            kr = d.get("kr_log") or {}
            st.caption(f"장부 기준 봇 보유 · 수익률은 국내 로그 {kr.get('file', '')[11:19]} {kr.get('hold_time') or ''} "
                       "'보유 유지' 줄, 손절 보류 종목은 그 회차 매도 평가 요약 값")
        elif d.get("ledger_error"):
            st.caption(f"장부 확인 불가 — {d['ledger_error']}")
        else:
            st.caption("봇 보유 없음")
    with tabs[1]:
        if recent:
            st.dataframe(recent, hide_index=True, width="stretch")
            st.caption(f"장부 전체 {lg.get('total', 0)}건 중 최근 {len(recent)}건")
        else:
            st.caption("거래 기록 없음")
    if us:
        with tabs[2]:
            st.dataframe([{"시각": str(r["timestamp"])[:16].replace("T", " "),
                           "결과": _US_DECISION_KO.get(r["decision"], r["decision"]),
                           "종목": r["ticker"], "지정가(USD)": r["limit"], "주문번호": r["order_no"] or "—"}
                          for r in us], hide_index=True, width="stretch")


def render_page_top() -> None:
    """app.py 본문 맨 위에서 호출: 머리말 + 성적 + 표. 오류가 나면 예전 화면으로 돌아간다."""
    try:
        d = pnl_view.collect()
        _render_top(d)
        _render_scoreboard(d)
        try:
            _render_history()
        except Exception as e:  # noqa: BLE001 — 그래프만 실패해도 나머지는 계속
            st.caption(f"누적 수익률 흐름 표시 실패({type(e).__name__})")
        names = {r["코드"]: r["종목"] for r in pnl_view._holdings_rows(d)}
        _render_live(names, d.get("ks"))
        _render_tables(d)
        st.markdown('<div class="sa-note">숫자는 회차가 끝날 때 바뀝니다(새로고침). 참고용 화면이며 투자 권유가 아닙니다.</div>',
                    unsafe_allow_html=True)
    except Exception as e:  # noqa: BLE001
        st.caption(f"새 화면 표시 실패({type(e).__name__}) — 예전 화면으로 보여 드립니다.")
        st.title("주식 투자 총괄 매니저")
        pnl_view.render_main()


def render_chat_header(has_messages: bool) -> None:
    st.markdown('<div class="sa-gap"></div><div class="sa-gap"></div>'
                '<div class="sa-sec"><h3>매니저에게 묻기</h3>'
                '<span class="sa-meta">질문에 맞는 직원만 불러 분석합니다</span></div>', unsafe_allow_html=True)
    if not has_messages:
        st.caption("예: 삼성전자 현재가 알려줘 · 애플이랑 테슬라 비교해줘 · 지금 손절 대상 있어? — 왼쪽 자주 쓰는 질문을 눌러도 됩니다.")
