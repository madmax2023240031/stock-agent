"""
auto_trader.py — 0단계(관찰) 전용 자동매매 "조립" 프로그램  [v5 — 진입 커밋]

역할
----
tools.py에 이미 만들어진 부품들을 **순서대로 연결만** 한다. 새 매매 로직은 없다.

    매수 파이프라인 (규칙 A / B):
        update_kill_switch_state(킬 스위치)  →  evaluate_buy_rule_A/B
        →  get_quote(가격)  →  size_buy_order(수량 환산)
        →  get_combined_auto_trade_stats_today + check_guardrails(가드레일)
        →  place_kis_order(dry_run=True, 주문서만)  →  dry-run 로그 기록

    매도 파이프라인 (규칙 SELL — 손절 -10% / 익절 +20%):
        update_kill_switch_state(킬 스위치)  →  evaluate_sell_rules
        →  get_kis_balance(보유 수량)  →  size_sell_order(전량)
        →  check_guardrails(거래일·시간·횟수 확인)
        →  place_kis_order(dry_run=True, 주문서만)  →  dry-run 로그 기록

안전장치 (코드로 강제 — 관행 아님)
----------------------------------------
1. 상수 세트는 모든 진입점의 _assert_phase_config()가 검사한다. 허용 조합은 정확히 두 가지 —
   · 0단계 세트: PHASE=0, MASTER_ENABLE=False, DRY_RUN=True,  APPROVAL_REQUIRED=True
   · 3단계 세트: PHASE=3, MASTER_ENABLE=True,  DRY_RUN=False, APPROVAL_REQUIRED=True
   그 외 모든 조합은 즉시 예외로 중단. **현재 상수는 0단계 세트다** —
   실주문 게이트(_execute_order)는 존재하지만 0단계 세트에서는 도달 불가능하다.
   (진입 커밋 — JJG의 "진입 커밋을 승인합니다" 명시 승인. 절대 불변 규칙 1 공식 개정.)
2. place_kis_order 호출은 정확히 두 곳 — 주문서 작성 _draft_order(dry_run=True 리터럴)와
   실주문 게이트 _execute_order(dry_run=DRY_RUN 전달). 이 파일 어디에도
   dry_run 인자에 False를 직접 쓴 리터럴은 없다 (grep으로 검증 가능).
3. 3단계 세트로 실행하려면 (a) .env의 PHASE3_CONFIRM이 확인 문구와 정확히 일치하고
   (b) 실행 시작 시 사람이 같은 확인 문구를 직접 타이핑해야 한다.
   불일치·비대화식(EOF)·빈 입력은 전부 즉시 종료(주문 0건).
   0단계 세트에서는 두 검사 모두 호출조차 하지 않는다.
4. 기록은 auto_trader_dryrun_log.json 에만 쓴다.
   실제 A/B 실험 장부(trade_log.json / log_trade)는 절대 건드리지 않는다.
   _assert_phase_config()가 두 경로가 같지 않은지도 검사한다.
5. 매도 기록의 rule_tag는 처음부터 정식 "SELL" 태그를 쓴다.
6. 승인 모드(작업 3-c): APPROVAL_REQUIRED = True — 주문서마다 사람이 y/n 승인.
   'y' 이외의 모든 입력(Enter·EOF 포함)은 거절 ("기본값은 거절").
   Phase 0에서는 승인해도 실주문이 나가지 않는다 (dry_run=True 고정).

사용 예 (반드시 ./venv/bin/python 사용)
----------------------------------------
    # 매수 규칙 A를 국내장에서 dry-run (빠른 테스트: 유니버스 5종목만)
    ./venv/bin/python auto_trader.py --rule A --market KR --universe-limit 5

    # 매도 규칙 dry-run
    ./venv/bin/python auto_trader.py --rule SELL

    # 전부 실행 (A → B → SELL)
    ./venv/bin/python auto_trader.py --rule ALL --market KR

    # 장시간이 아닐 때 가드레일 통과 경로까지 관찰하고 싶으면 (테스트 전용 시각 주입):
    ./venv/bin/python auto_trader.py --rule A --market KR --universe-limit 5 \
        --test-now "2026-07-15T10:00"
    (--test-now 는 check_guardrails의 시각 판정에만 쓰이며 로그에 그대로 기록된다)
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import tools
from tools import (
    check_guardrails,
    evaluate_buy_rule_A,
    evaluate_buy_rule_B,
    evaluate_sell_rules,
    get_auto_trade_positions,
    get_combined_auto_trade_stats_today,
    get_kis_balance,
    get_quote,
    log_trade,
    place_kis_order,
    update_kill_switch_state,
)

# ═══════════════════════════════════════════════
# 상수 세트 — 이 블록의 값을 바꾸는 코드를 작성하지 말 것
# 허용 조합은 정확히 두 가지 (_assert_phase_config가 강제):
#   0단계 세트: PHASE=0, MASTER_ENABLE=False, DRY_RUN=True,  APPROVAL_REQUIRED=True
#   3단계 세트: PHASE=3, MASTER_ENABLE=True,  DRY_RUN=False, APPROVAL_REQUIRED=True
# 세트 전환은 사람이 별도 세션에서 네 값을 한꺼번에 바꿀 때만 가능하다.
# ═══════════════════════════════════════════════

PHASE = 3                      # 현재 단계 — 3단계 세트 (2026-07-20 JJG 승인 전환)
MASTER_ENABLE = True           # 실주문 마스터 스위치 — 3단계에서 열림 (승인 필수)
DRY_RUN = False                # 3단계 세트에서는 False — 실주문 게이트는 dry_run=DRY_RUN으로만 전달
APPROVAL_REQUIRED = True       # 작업 3-c: 승인 모드 — 주문서마다 사람이 y/n 승인.
                               # 두 세트 모두 True — 승인 없는 실주문 조합은 존재하지 않는다.

_PHASE3_CONFIRM_PHRASE = "실주문 3단계를 승인합니다"   # .env PHASE3_CONFIRM · 시작 확인 입력 공용 문구

_BASE_DIR = Path(__file__).resolve().parent
DRYRUN_LOG_PATH = str(_BASE_DIR / "auto_trader_dryrun_log.json")

DEFAULT_BUDGET_PER_ORDER_KRW = 500_000   # 매수 1건당 예산 (place_kis_order 상한 100만 원의 절반)
DEFAULT_MAX_ORDERS_PER_RULE = 3          # 규칙 1회 실행당 주문서 작성 최대 건수


# ═══════════════════════════════════════════════
# 안전장치
# ═══════════════════════════════════════════════

def _assert_phase_config() -> None:
    """
    상수 세트 안전장치 검사. 허용 조합은 정확히 두 가지 —
    0단계 세트(PHASE=0, MASTER_ENABLE=False, DRY_RUN=True, APPROVAL_REQUIRED=True)와
    3단계 세트(PHASE=3, MASTER_ENABLE=True, DRY_RUN=False, APPROVAL_REQUIRED=True).
    그 외 모든 조합(어중간한 세트)은 즉시 예외로 전체 중단.
    """
    phase0_set = (PHASE == 0 and MASTER_ENABLE is False
                  and DRY_RUN is True and APPROVAL_REQUIRED is True)
    phase3_set = (PHASE == 3 and MASTER_ENABLE is True
                  and DRY_RUN is False and APPROVAL_REQUIRED is True)
    if not (phase0_set or phase3_set):
        raise RuntimeError(
            "[안전장치] 허용되지 않는 상수 조합입니다 — 실행을 중단합니다. "
            f"현재 값: PHASE={PHASE!r}, MASTER_ENABLE={MASTER_ENABLE!r}, "
            f"DRY_RUN={DRY_RUN!r}, APPROVAL_REQUIRED={APPROVAL_REQUIRED!r}. "
            "허용 조합은 정확히 두 가지입니다 — "
            "0단계 세트(PHASE=0, MASTER_ENABLE=False, DRY_RUN=True, APPROVAL_REQUIRED=True) "
            "또는 3단계 세트(PHASE=3, MASTER_ENABLE=True, DRY_RUN=False, APPROVAL_REQUIRED=True). "
            "네 값이 세트 단위로 함께 바뀌지 않으면 어떤 파이프라인도 실행되지 않습니다."
        )
    if os.path.abspath(DRYRUN_LOG_PATH) == os.path.abspath(tools.TRADE_LOG_PATH):
        raise RuntimeError(
            "[안전장치] dry-run 로그 경로가 실제 실험 장부(trade_log.json)와 같습니다. "
            "장부 오염 위험 — 실행을 중단합니다."
        )


def _is_live_mode() -> bool:
    """3단계 세트(실주문 게이트 활성) 여부. 0단계 세트에서는 항상 False."""
    return MASTER_ENABLE and not DRY_RUN


def _check_phase3_confirm() -> None:
    """
    3단계 세트 전용 검사 — .env의 PHASE3_CONFIRM이 확인 문구와 정확히 일치해야 한다.
    0단계 세트에서는 호출조차 하지 않는다 (.env에 키가 없어도 0단계는 정상 동작).
    """
    from dotenv import load_dotenv
    load_dotenv()
    value = os.environ.get("PHASE3_CONFIRM", "")
    if value != _PHASE3_CONFIRM_PHRASE:
        raise RuntimeError(
            "[안전장치] .env의 PHASE3_CONFIRM이 확인 문구와 일치하지 않습니다. "
            f'3단계 실행에는 PHASE3_CONFIRM="{_PHASE3_CONFIRM_PHRASE}" 가 필요합니다. '
            "실행을 중단합니다 (주문 0건)."
        )


def _confirm_live_start() -> None:
    """
    3단계 세트 전용 — 실행 시작 시 사람이 확인 문구를 직접 타이핑해야 진행한다.
    불일치·빈 입력·비대화식 실행(EOF)·입력 중단은 전부 즉시 종료 (주문 0건).
    """
    print("\n" + "🔴" * 30)
    print("=" * 60)
    print("  ⚠️⚠️⚠️  실 주 문   모 드  ⚠️⚠️⚠️")
    print("  3단계 세트 — 승인된 주문서는 KIS 모의계좌로 실제 전송됩니다.")
    print(f"  PHASE={PHASE} / MASTER_ENABLE={MASTER_ENABLE} / DRY_RUN={DRY_RUN}")
    print("=" * 60)
    print("🔴" * 30)
    try:
        answer = input(
            f'  계속하려면 확인 문구를 정확히 입력하세요 ("{_PHASE3_CONFIRM_PHRASE}"): '
        ).strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(
            "[안전장치] 확인 문구를 입력받을 수 없습니다 (비대화식 실행 또는 입력 중단). "
            "즉시 종료합니다 (주문 0건)."
        )
    if answer != _PHASE3_CONFIRM_PHRASE:
        raise SystemExit(
            "[안전장치] 확인 문구가 일치하지 않습니다. 즉시 종료합니다 (주문 0건)."
        )
    print("  확인 문구 일치 — 3단계 실주문 모드로 진행합니다.\n")


def _now_kst() -> datetime:
    """KST 현재 시각 (tz 포함)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Seoul"))


# ═══════════════════════════════════════════════
# dry-run 전용 로그 (trade_log.json과 완전히 분리)
# ═══════════════════════════════════════════════

def _append_dryrun_log(entry: dict) -> dict:
    """
    dry-run 기록 1건을 DRYRUN_LOG_PATH에 추가한다.
    파일 구조: {"note": str, "count": int, "records": [ ... ]}
    """
    _assert_phase_config()

    data = {
        "note": (
            "auto_trader.py dry-run 전용 로그. 실제 주문·실제 거래 아님. "
            "A/B 실험 장부(trade_log.json)와 절대 섞지 말 것."
        ),
        "count": 0,
        "records": [],
    }
    try:
        if os.path.exists(DRYRUN_LOG_PATH):
            with open(DRYRUN_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"dry-run 로그 읽기 실패: {e}"}

    data.setdefault("records", []).append(entry)
    data["count"] = len(data["records"])

    try:
        with open(DRYRUN_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return {"error": f"dry-run 로그 쓰기 실패: {e}"}

    return {"logged": True}


def _make_entry(
    run_id: str,
    rule_tag: str,
    decision: str,
    *,
    ticker: str = "",
    name: str = "",
    side: str = "",
    qty: int | None = None,
    price: float | None = None,
    order_amount_krw: int | None = None,
    sector: str | None = None,
    guardrail: dict | None = None,
    order_draft: dict | None = None,
    note: str = "",
    test_now: str | None = None,
    eval_summary: dict | None = None,
    rule_split: dict | None = None,
    approval: dict | None = None,
    execution: dict | None = None,
    book_log: dict | None = None,
) -> dict:
    """dry-run 로그 1건 양식. decision: ORDER_DRAFTED | BLOCKED | SKIPPED | ERROR | EVAL_SUMMARY"""
    return {
        "timestamp": _now_kst().isoformat(timespec="seconds"),
        "run_id": run_id,
        "phase": PHASE,
        "dry_run": DRY_RUN,
        "rule_tag": rule_tag,          # "A" | "B" | "SELL"
        "decision": decision,
        "ticker": ticker,
        "name": name,
        "side": side,                  # "BUY" | "SELL"
        "qty": qty,
        "price": price,
        "order_amount_krw": order_amount_krw,
        "sector": sector,
        "guardrail": guardrail,
        "order_draft": order_draft,
        "note": note,
        "test_now": test_now,          # --test-now 사용 시 그대로 기록 (실험 투명성)
        "eval_summary": eval_summary,  # EVAL_SUMMARY 기록 전용 — excluded/stop_loss_deferred 관찰 데이터
        "rule_split": rule_split,     # C-2: 매도 주문 1건의 A/B 규칙별 수량 분할 (기록 준비용)
        "approval": approval,          # 작업 3-c: 승인 모드 결정 기록 (required/approved/answer/decided_at)
        "execution": execution,        # 진입 커밋: 실주문 게이트 실행 결과 (0단계 세트에서는 항상 None)
        "book_log": book_log,   # 장부 기록 결과 — 실행 성공 시에만 채워짐 (0단계에서는 항상 None)
    }


# ═══════════════════════════════════════════════
# 수량 환산 헬퍼 (조립 계층 — 전략 로직 아님, 산수만)
# ═══════════════════════════════════════════════

def size_buy_order(budget_krw: int, price: float | None) -> int:
    """
    매수 수량 환산: 예산 ÷ 현재가 (내림). 규칙 후보에는 수량이 없어서 필요하다.
    가격이 없거나 0 이하이면 0을 반환한다 (호출부에서 SKIPPED 처리).
    """
    if price is None or price <= 0:
        return 0
    return int(budget_krw // price)


def size_sell_order(holding_qty: int | None) -> int:
    """
    매도 수량 환산: 보유 전량. (손절/익절 규칙은 포지션 정리이므로 전량 기준.)
    보유 수량이 없으면 0을 반환한다.
    """
    if holding_qty is None or holding_qty < 1:
        return 0
    return int(holding_qty)


def _split_sell_qty(sell_qty: int, book_a_qty: int, book_b_qty: int) -> dict:
    """
    C-2: 매도 수량을 규칙 A/B분으로 분할한다 (기록 준비용 산수 — 주문은 항상 1건).
    - 정상: sell_qty == 장부 A+B 합 → 장부 수량 그대로.
    - 불일치(sell_qty < 합): A:B 장부 비율로 비례 배분 (A는 내림, 나머지 B).
      A/B 실험 공정성을 위해 한쪽 우선 차감 대신 비례 배분을 쓴다.
    - 반환 A+B는 항상 sell_qty와 일치한다 (log_trade 2건 분할의 전제).
    """
    book_total = book_a_qty + book_b_qty
    if sell_qty <= 0 or book_total <= 0:
        return {"A": 0, "B": 0, "book_total": book_total,
                "mismatch": sell_qty != book_total}
    if sell_qty >= book_total:
        return {"A": book_a_qty, "B": book_b_qty, "book_total": book_total,
                "mismatch": sell_qty != book_total}
    a = (sell_qty * book_a_qty) // book_total
    b = sell_qty - a
    if b > book_b_qty:          # 내림 보정 안전장치 (이론상 도달 불가)
        a += b - book_b_qty
        b = book_b_qty
    return {"A": a, "B": b, "book_total": book_total, "mismatch": True}


def _draft_order(ticker: str, side: str, qty: int) -> dict:
    """
    주문서 작성 — 반드시 dry_run=True 리터럴 고정 (3단계 세트에서도 주문서는 초안이다).
    place_kis_order 호출은 이 함수(주문서)와 _execute_order(실주문 게이트) 두 곳뿐이다.
    """
    _assert_phase_config()
    return place_kis_order(ticker, side, qty, "MARKET", dry_run=True)


def _execute_order(ticker: str, side: str, qty: int) -> dict:
    """
    실주문 게이트 (진입 커밋) — 승인된 주문서를 실제 전송한다.

    - 3단계 세트에서만 도달 가능. 0단계 세트에서 호출되면 배선 버그이므로 즉시 예외.
    - dry_run 인자에 False 리터럴을 직접 쓰지 않고 반드시 dry_run=DRY_RUN 을 전달한다
      (_assert_phase_config가 상수 세트 일관성을 보장하므로, 이 함수까지 왔다면
      DRY_RUN is False 인 3단계 세트임이 이미 검증돼 있다).
    - 반환: 기록용 execution dict — 성공/실패, API 응답 요약, 시각.
      성공/실패 판정은 place_kis_order 반환 규약("error" 키 유무)을 따른다.
    """
    _assert_phase_config()
    if not _is_live_mode():
        raise RuntimeError(
            "[안전장치] _execute_order는 3단계 세트에서만 호출할 수 있습니다. "
            "0단계 세트에서 도달했다면 배선 버그입니다 — 실행을 중단합니다."
        )
    _check_phase3_confirm()   # 방어 심층화 — 주문 직전에도 .env 확인 문구 재검사

    result = place_kis_order(ticker, side, qty, "MARKET", dry_run=DRY_RUN)
    success = isinstance(result, dict) and "error" not in result
    if success:
        r = result.get("result", {})
        summary = {
            "order_no":   r.get("order_no", ""),
            "order_time": r.get("order_time", ""),
            "rt_cd":      r.get("rt_cd"),
            "msg":        r.get("msg", ""),
        }
    else:
        summary = {"error": result.get("error") if isinstance(result, dict) else str(result)}
    return {
        "executed": True,
        "success": success,
        "summary": summary,
        "executed_at": _now_kst().isoformat(timespec="seconds"),
    }


def _ask_approval(
    rule_tag: str,
    ticker: str,
    name: str,
    side: str,
    qty: int,
    price: float,
    order_amount: int,
    reason: str,
    guardrail: dict,
    draft: dict,
) -> dict:
    """
    승인 모드 (작업 3-c, 설계 확정 A): 주문서 요약을 터미널에 표시하고 y/n을 받는다.

    - 'y' 이외의 모든 입력(Enter 포함)은 거절 — "기본값은 거절".
    - 터미널 없이 실행(launchd 등)되어 input()이 EOFError를 내면 거절 처리
      (기본값은 거절 원칙의 연장 — 비대화식 실행에서 자동 승인되는 경로는 없다).
    - 주문 실행 코드는 없다. 사람의 결정을 dict로 돌려주기만 한다.
    - Phase 0에서는 승인해도 실주문이 나가지 않는다 (dry_run=True 고정) —
      여기서는 y/n 흐름과 기록만 검증한다.
    """
    side_kor = "매수" if side == "BUY" else "매도"
    order = draft.get("order", {}) if isinstance(draft, dict) else {}
    est = order.get("estimated_amount_krw")
    est_display = f"{est:,}원" if isinstance(est, int) else "—"
    mode_tag = "DRY RUN" if DRY_RUN else "🔴 LIVE"

    print("\n" + "-" * 55)
    print(f"  📋 [승인 요청] 규칙 {rule_tag} — {side_kor} 주문서 ({mode_tag})")
    print("-" * 55)
    print(f"  종목      : {name} ({ticker})")
    print(f"  수량      : {qty:,}주")
    print(f"  현재가    : {price:,.0f}원")
    print(f"  주문금액  : {order_amount:,}원 (주문서 추정 {est_display})")
    print(f"  사유      : {reason or '—'}")
    print(f"  가드레일  : {'✅ 통과' if guardrail.get('passed') else '❌ 미통과'}")
    print("-" * 55)
    if DRY_RUN:
        print("  ⚠️  Phase 0 — 승인해도 실제 주문은 전송되지 않습니다 (dry-run 기록만).")
    else:
        print("  🔴 Phase 3 — 승인하면 실제 주문이 KIS 모의계좌로 전송됩니다!")

    note = ""
    try:
        answer = input("  이 주문서를 승인할까요? (y 이외의 모든 입력은 거절): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
        note = "입력 불가(비대화식 실행 또는 입력 중단) — 기본값 거절"
        print("\n  입력을 받을 수 없어 거절 처리합니다 (기본값은 거절).")

    approved = (answer == "y")
    print(f"  → 결정: {'승인' if approved else '거절'} (입력값: '{answer}')")
    return {
        "required": True,
        "approved": approved,
        "answer": answer,
        "decided_at": _now_kst().isoformat(timespec="seconds"),
        "note": note,
    }


def _parse_test_now(s: str | None) -> datetime | None:
    """--test-now "YYYY-MM-DDTHH:MM" → naive datetime (check_guardrails가 KST로 간주)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise SystemExit(
            f'--test-now 형식 오류: "{s}" — "YYYY-MM-DDTHH:MM" 형식으로 입력하세요 '
            f'(예: --test-now "2026-07-15T10:00")'
        )


# ═══════════════════════════════════════════════
# 매수 파이프라인 (규칙 A / B)
# ═══════════════════════════════════════════════

def run_buy_rule(
    rule_tag: str,
    market: str = "KR",
    budget_per_order_krw: int = DEFAULT_BUDGET_PER_ORDER_KRW,
    universe_limit: int | None = None,
    max_orders: int = DEFAULT_MAX_ORDERS_PER_RULE,
    test_now: str | None = None,
) -> dict:
    """
    매수 규칙 1회 dry-run 실행. 실제 주문은 절대 나가지 않는다.

    순서: 규칙 평가 → 오늘 누적치 집계 → 후보별 (가격→수량→가드레일→주문서) → 로그.
    """
    _assert_phase_config()
    run_id = f"buy{rule_tag}-{uuid.uuid4().hex[:8]}"
    now_inject = _parse_test_now(test_now)

    rule_tag = rule_tag.upper().strip()
    if rule_tag not in ("A", "B"):
        return {"error": f"run_buy_rule은 'A' 또는 'B'만 허용합니다. 입력: '{rule_tag}'"}

    rule_fn = evaluate_buy_rule_A if rule_tag == "A" else evaluate_buy_rule_B

    # ── 0. 킬 스위치 상태 갱신 (작업 1-b 배선 — 판단은 check_guardrails가 한다) ──
    ks = update_kill_switch_state(now=now_inject)
    if "error" in ks:
        # fail-safe: 킬 스위치 입력을 못 구하면 진행하지 않는다 (누적치 집계 실패와 동일 철학)
        entry = _make_entry(run_id, rule_tag, "ERROR",
                            note=f"킬 스위치 상태 갱신 실패 → 진행 중단: {ks['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": ks["error"]}

    # ── 1. 규칙 평가 (판단/제안만) ──────────────────────────────
    result = rule_fn(market=market, universe_limit=universe_limit)
    if "error" in result:
        entry = _make_entry(run_id, rule_tag, "ERROR",
                            note=f"규칙 평가 실패: {result['error']}", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": result["error"]}

    candidates = result.get("candidates", [])
    excluded = result.get("excluded", [])
    records = 0

    # ── 1-1. 평가 요약 기록 (Phase 0 관찰 데이터) ───────────────
    # 후보가 0개여도 excluded는 있을 수 있으므로 후보 없음 판정보다 먼저 기록한다.
    _append_dryrun_log(_make_entry(
        run_id, rule_tag, "EVAL_SUMMARY", side="BUY",
        eval_summary={
            "candidates_count": len(candidates),
            "excluded_count": result.get("excluded_count", len(excluded)),
            "excluded": [
                {"ticker": x.get("ticker"), "name": x.get("name"),
                 "reason": x.get("reason")}
                for x in excluded
            ],
        },
        note="규칙 평가 요약 (excluded 포함 — Phase 0 관찰 데이터)",
        test_now=test_now))
    records += 1

    if not candidates:
        entry = _make_entry(run_id, rule_tag, "SKIPPED",
                            note="후보 없음 (규칙 기준 미달)", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "drafted": 0, "records": records + 1, "note": "후보 없음"}

    # ── 2. 오늘 누적치 집계 (가드레일 입력 — 손 계산 금지) ──────
    # 설계 결정 1·3: 하루 거래 횟수(매수만)는 A+B 합산, 금액 한도는 규칙별.
    # 합산 집계 함수 하나로 둘 다 얻는다 (합산 횟수 + per_rule 금액).
    stats = get_combined_auto_trade_stats_today()
    if "error" in stats:
        # fail-safe: 가드레일 입력을 못 구하면 진행하지 않는다.
        entry = _make_entry(run_id, rule_tag, "ERROR",
                            note=f"오늘 누적치 집계 실패 → 진행 중단: {stats['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": stats["error"]}
    rule_stats = stats["per_rule"][rule_tag]

    # 이번 실행(세션) 안에서 통과한 주문서 금액도 누적에 더한다.
    # (dry-run이라 trade_log에 없으므로 — 같은 실행 내 중복 초과를 막기 위한 산수)
    session_accum = 0
    session_by_ticker: dict[str, int] = {}
    session_by_sector: dict[str, int] = {}
    session_trades = 0

    drafted = 0
    approved_count = 0
    rejected_count = 0

    for cand in candidates:
        if drafted >= max_orders:
            break

        ticker = cand.get("ticker", "")
        name = cand.get("name", "")
        sector = cand.get("sector") or "기타/미분류"

        # ── 3. 국내 전용 확인 (place_kis_order 제약) ────────────
        if cand.get("market") != "KR":
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "SKIPPED", ticker=ticker, name=name, side="BUY",
                sector=sector,
                note="미국 종목 — place_kis_order는 국내 6자리 전용이라 주문서 생략",
                test_now=test_now))
            records += 1
            continue

        # ── 4. 가격 조회 → 수량 환산 ────────────────────────────
        quote = get_quote(ticker)
        if "error" in quote or quote.get("close") is None:
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "ERROR", ticker=ticker, name=name, side="BUY",
                sector=sector,
                note=f"가격 조회 실패: {quote.get('error', 'close 없음')}",
                test_now=test_now))
            records += 1
            continue

        price = float(quote["close"])
        qty = size_buy_order(budget_per_order_krw, price)
        if qty < 1:
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "SKIPPED", ticker=ticker, name=name, side="BUY",
                price=price, sector=sector,
                note=f"예산 {budget_per_order_krw:,}원으로 1주도 살 수 없음 (현재가 {price:,.0f}원)",
                test_now=test_now))
            records += 1
            continue

        order_amount = int(qty * price)

        # ── 5. 가드레일 검사 (오늘 누적 + 이번 세션 누적) ───────
        # 결정 3 — 횟수는 A+B 합산, 금액 한도는 규칙별(자금 배분 계획)
        gr = check_guardrails(
            ticker, order_amount,
            side="BUY",
            sector=sector,
            accumulated_krw=rule_stats["accumulated_krw"] + session_accum,
            ticker_accumulated_krw=rule_stats["by_ticker"].get(ticker, 0)
                                   + session_by_ticker.get(ticker, 0),
            sector_accumulated_krw=rule_stats["by_sector"].get(sector, 0)
                                   + session_by_sector.get(sector, 0),
            daily_trades=stats["daily_buy_trades"] + session_trades,
            daily_pnl_pct=ks["daily_pnl_pct"],
            cumulative_pnl_pct=ks["cumulative_pnl_pct"],
            now=now_inject,
        )
        if "error" in gr:
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "ERROR", ticker=ticker, name=name, side="BUY",
                qty=qty, price=price, order_amount_krw=order_amount, sector=sector,
                note=f"가드레일 검사 자체 실패: {gr['error']}", test_now=test_now))
            records += 1
            continue

        if not gr.get("passed"):
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "BLOCKED", ticker=ticker, name=name, side="BUY",
                qty=qty, price=price, order_amount_krw=order_amount, sector=sector,
                guardrail=gr,
                note=f"가드레일 차단: {gr.get('blocked_by')} — {gr.get('reason')}",
                test_now=test_now))
            records += 1
            continue

        # ── 6. 주문서 작성 (dry_run=True 고정 — 실제 주문 아님) ──
        draft = _draft_order(ticker, "BUY", qty)
        if "error" in draft:
            _append_dryrun_log(_make_entry(
                run_id, rule_tag, "ERROR", ticker=ticker, name=name, side="BUY",
                qty=qty, price=price, order_amount_krw=order_amount, sector=sector,
                guardrail=gr, note=f"주문서 작성 실패: {draft['error']}",
                test_now=test_now))
            records += 1
            continue

        # ── 6-1. 승인 모드 (작업 3-c, 설계 확정 A) — 기본값은 거절 ──
        approval = None
        if APPROVAL_REQUIRED:
            approval = _ask_approval(rule_tag, ticker, name, "BUY", qty, price,
                                     order_amount, cand.get("reason", ""), gr, draft)
        approved = (approval is None) or approval["approved"]

        # ── 6-2. 실주문 게이트 (진입 커밋) — 0단계 세트에서는 절대 진입하지 않는다 ──
        execution = None
        if approved and MASTER_ENABLE and not DRY_RUN:
            execution = _execute_order(ticker, "BUY", qty)

        # 장부 기록 — 실행이 "성공"했을 때만 (주문이 안 나갔으면 장부에 적지 않는다)
        book_log = None
        if execution is not None and execution.get("success"):
            r = log_trade(
                rule_tag=rule_tag, ticker=ticker, side="BUY",
                qty=qty, price=price,
                reason=f"[auto_trader {run_id}] 규칙 {rule_tag} 자동매수 "
                       f"(시장가 — 기록가는 주문시점 현재가)",
                sector=sector, source_rule=None,
            )
            book_log = {"entries": [{"source_rule": None, "qty": qty, "result": r}],
                        "all_logged": "error" not in r}

        _append_dryrun_log(_make_entry(
            run_id, rule_tag, "ORDER_DRAFTED", ticker=ticker, name=name, side="BUY",
            qty=qty, price=price, order_amount_krw=order_amount, sector=sector,
            guardrail=gr, order_draft=draft, approval=approval, execution=execution,
            book_log=book_log,
            note=(f"[DRY RUN] 규칙 {rule_tag} 매수 주문서 작성 — 실제 전송 안 됨. "
                  if DRY_RUN else
                  f"[LIVE] 규칙 {rule_tag} 매수 주문서 작성 — 실행 결과는 execution 필드 참조. ")
                 + f"근거: {cand.get('reason', '')}"
                 + (f" / 승인 결정: {'승인' if approved else '거절'}" if approval else "")
                 + (" / ⚠️ 장부 기록 실패 — 수동 확인 필요" if book_log and not book_log["all_logged"] else ""),
            test_now=test_now))
        records += 1
        drafted += 1

        if not approved:
            rejected_count += 1
            # 거절된 주문서는 (3단계라면) 집행되지 않으므로 세션 누적에 더하지 않는다.
            # → 다음 후보의 가드레일 판정이 3단계 실제 동작과 같아진다 (거절도 관찰 데이터).
            continue
        approved_count += 1

        # 세션 누적 갱신 (승인 통과분만)
        session_accum += order_amount
        session_by_ticker[ticker] = session_by_ticker.get(ticker, 0) + order_amount
        session_by_sector[sector] = session_by_sector.get(sector, 0) + order_amount
        session_trades += 1

    return {"run_id": run_id, "rule_tag": rule_tag,
            "candidates": len(candidates), "records": records, "drafted": drafted,
            "approved": approved_count, "rejected": rejected_count}


# ═══════════════════════════════════════════════
# 매도 파이프라인 (규칙 SELL — 손절/익절)
# ═══════════════════════════════════════════════

def run_sell_rule(test_now: str | None = None) -> dict:
    """
    매도 규칙 1회 dry-run 실행. 실제 주문은 절대 나가지 않는다.
    rule_tag는 처음부터 정식 "SELL" 태그를 쓴다 (MANUAL 임시 방편 폐기).
    """
    _assert_phase_config()
    run_id = f"sell-{uuid.uuid4().hex[:8]}"
    now_inject = _parse_test_now(test_now)

    # ── 0. 킬 스위치 상태 갱신 (작업 1-b 배선 — 판단은 check_guardrails가 한다) ──
    ks = update_kill_switch_state(now=now_inject)
    if "error" in ks:
        # fail-safe: 킬 스위치 입력을 못 구하면 진행하지 않는다 (누적치 집계 실패와 동일 철학)
        entry = _make_entry(run_id, "SELL", "ERROR",
                            note=f"킬 스위치 상태 갱신 실패 → 진행 중단: {ks['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": ks["error"]}

    # ── 1. 매도 규칙 평가 (판단/제안만) ─────────────────────────
    result = evaluate_sell_rules()
    if "error" in result:
        entry = _make_entry(run_id, "SELL", "ERROR",
                            note=f"매도 규칙 평가 실패: {result['error']}", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": result["error"]}

    sell_candidates = list(result.get("stop_loss", [])) + list(result.get("take_profit", []))
    summary = result.get("summary", {})
    deferred = result.get("stop_loss_deferred", [])
    records = 0

    # ── 1-1. 평가 요약 기록 (Phase 0 관찰 데이터) ───────────────
    # 후보가 0개여도 stop_loss_deferred는 있을 수 있으므로 후보 없음 판정보다 먼저 기록한다.
    _append_dryrun_log(_make_entry(
        run_id, "SELL", "EVAL_SUMMARY", side="SELL",
        eval_summary={
            "stop_loss_count":          summary.get("stop_loss_count"),
            "take_profit_count":        summary.get("take_profit_count"),
            "stop_loss_deferred_count": summary.get("stop_loss_deferred_count"),
            "stop_loss_deferred": [
                {"ticker": d.get("ticker"), "name": d.get("name"),
                 "profit_loss_pct": d.get("profit_loss_pct"),
                 "trend_note": d.get("trend_note")}
                for d in deferred
            ],
        },
        note="매도 규칙 평가 요약 (stop_loss_deferred 포함 — Phase 0 관찰 데이터)",
        test_now=test_now))
    records += 1

    # ── 1-2. 자동매매 장부 조회 (작업 3-b, C-1: 자동매매는 자동매매가 산 것만 판다) ──
    # fail-safe: 장부 재생 실패(로그 불일치)는 킬 스위치 실패와 동일 — 이 사이클 매도 전부 중단.
    book = get_auto_trade_positions()
    if "error" in book:
        entry = _make_entry(run_id, "SELL", "ERROR",
                            note=f"자동매매 장부 조회 실패 → 매도 전체 중단: {book['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": book["error"], "records": records}

    combined_pos = book.get("combined", {}).get("positions", {})
    rule_a_pos = book.get("by_rule", {}).get("A", {}).get("positions", {})
    rule_b_pos = book.get("by_rule", {}).get("B", {}).get("positions", {})

    if not sell_candidates:
        entry = _make_entry(run_id, "SELL", "SKIPPED",
                            note="손절/익절 후보 없음", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "drafted": 0, "records": records + 1, "note": "후보 없음"}

    # ── 2. 보유 수량·현재가 확보 (후보에는 수량이 없다) ─────────
    balance = get_kis_balance()
    if "error" in balance:
        entry = _make_entry(run_id, "SELL", "ERROR",
                            note=f"잔고 조회 실패 → 진행 중단: {balance['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": balance["error"]}

    holdings_by_ticker = {h.get("ticker"): h for h in balance.get("holdings", [])}

    drafted = 0
    session_trades = 0
    approved_count = 0
    rejected_count = 0

    for cand in sell_candidates:
        ticker = cand.get("ticker", "")
        name = cand.get("name", "")
        reason = cand.get("reason", "")

        # ── 3. 국내 전용 확인 ───────────────────────────────────
        if cand.get("market") != "KR":
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "SKIPPED", ticker=ticker, name=name, side="SELL",
                note=f"미국 종목 — place_kis_order는 국내 6자리 전용이라 주문서 생략 ({reason})",
                test_now=test_now))
            records += 1
            continue

        # ── 3-1. C-1 필터: 자동매매 장부에 없는 종목(수동 보유분)은 팔지 않는다 ──
        # 조용히 버리지 않고 로그에 남긴다 (Phase 0 관찰 데이터).
        auto_pos = combined_pos.get(ticker)
        auto_qty = int(auto_pos["qty"]) if auto_pos else 0
        if auto_qty < 1:
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "SKIPPED", ticker=ticker, name=name, side="SELL",
                note=f"자동매매 장부에 없음 — 수동 보유분은 사람이 직접 관리 ({reason})",
                test_now=test_now))
            records += 1
            continue

        holding = holdings_by_ticker.get(ticker)
        account_qty = int(holding.get("qty") or 0) if holding else 0
        # C-1: 매도 수량 상한 = 자동매매 장부 수량 (계좌 전량 아님)
        qty = size_sell_order(min(account_qty, auto_qty))
        price = float(holding.get("current_price") or 0) if holding else 0.0
        if qty < 1 or price <= 0:
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "ERROR", ticker=ticker, name=name, side="SELL",
                note="잔고에서 보유 수량/현재가를 찾지 못함", test_now=test_now))
            records += 1
            continue

        order_amount = int(qty * price)

        # ── 4. 가드레일 검사 ────────────────────────────────────
        # 재검토 ② — side="SELL": 금액 검사 3종 면제, 거래일·거래시간·kill switch만 적용.
        gr = check_guardrails(
            ticker, order_amount,
            side="SELL",
            sector=None,
            accumulated_krw=0,
            ticker_accumulated_krw=0,
            sector_accumulated_krw=0,
            # 설계 결정 1: 매도는 하루 5회 상한 제외 (리스크 축소 행위)
            daily_trades=0,
            # 킬 스위치는 매도에도 적용 — 재검토 ② 결정. 발동 시 보유분 정리는 사람 몫
            daily_pnl_pct=ks["daily_pnl_pct"],
            cumulative_pnl_pct=ks["cumulative_pnl_pct"],
            now=now_inject,
        )
        if "error" in gr:
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "ERROR", ticker=ticker, name=name, side="SELL",
                qty=qty, price=price, order_amount_krw=order_amount,
                note=f"가드레일 검사 자체 실패: {gr['error']}", test_now=test_now))
            records += 1
            continue

        if not gr.get("passed"):
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "BLOCKED", ticker=ticker, name=name, side="SELL",
                qty=qty, price=price, order_amount_krw=order_amount, guardrail=gr,
                note=f"가드레일 차단: {gr.get('blocked_by')} — {gr.get('reason')} ({reason})",
                test_now=test_now))
            records += 1
            continue

        # ── 5. 주문서 작성 (dry_run=True 고정 — 실제 주문 아님) ──
        draft = _draft_order(ticker, "SELL", qty)
        if "error" in draft:
            # 예: 전량 매도 금액이 place_kis_order 상한(100만 원)을 넘는 경우도 여기 기록된다.
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "ERROR", ticker=ticker, name=name, side="SELL",
                qty=qty, price=price, order_amount_krw=order_amount, guardrail=gr,
                note=f"주문서 작성 실패: {draft['error']} ({reason})", test_now=test_now))
            records += 1
            continue

        # C-2: 주문은 1건, 기록은 A/B 분할 준비 (log_trade 배선은 진입 커밋 이후)
        split = _split_sell_qty(
            qty,
            int(rule_a_pos.get(ticker, {}).get("qty", 0)),
            int(rule_b_pos.get(ticker, {}).get("qty", 0)),
        )

        # ── 5-1. 승인 모드 (작업 3-c, 설계 확정 A) — 기본값은 거절 ──
        approval = None
        if APPROVAL_REQUIRED:
            approval = _ask_approval("SELL", ticker, name, "SELL", qty, price,
                                     order_amount, reason, gr, draft)
        approved = (approval is None) or approval["approved"]

        # ── 5-2. 실주문 게이트 (진입 커밋) — 0단계 세트에서는 절대 진입하지 않는다 ──
        execution = None
        if approved and MASTER_ENABLE and not DRY_RUN:
            execution = _execute_order(ticker, "SELL", qty)

        # 장부 기록 — C-2: 주문은 1건이지만 장부는 rule_split대로 A/B 각각 기록
        book_log = None
        if execution is not None and execution.get("success"):
            entries = []
            for src in ("A", "B"):
                part_qty = int(split.get(src, 0))
                if part_qty < 1:
                    continue  # 0주 분할은 기록 생략 (log_trade가 qty<1을 거절)
                r = log_trade(
                    rule_tag="SELL", ticker=ticker, side="SELL",
                    qty=part_qty, price=price,
                    reason=f"[auto_trader {run_id}] 매도 규칙 자동매도 — {reason} "
                           f"(시장가 — 기록가는 주문시점 현재가)",
                    sector=None, source_rule=src,
                )
                entries.append({"source_rule": src, "qty": part_qty, "result": r})
            book_log = {"entries": entries,
                        "all_logged": all("error" not in e["result"] for e in entries)}

        _append_dryrun_log(_make_entry(
            run_id, "SELL", "ORDER_DRAFTED", ticker=ticker, name=name, side="SELL",
            qty=qty, price=price, order_amount_krw=order_amount,
            guardrail=gr, order_draft=draft, rule_split=split, approval=approval,
            execution=execution, book_log=book_log,
            note=(f"[DRY RUN] 매도 규칙 주문서 작성 (전량) — 실제 전송 안 됨. 근거: {reason}"
                  if DRY_RUN else
                  f"[LIVE] 매도 규칙 주문서 작성 (전량) — 실행 결과는 execution 필드 참조. 근거: {reason}")
                 + f" / C-2 분할: A={split['A']} B={split['B']}"
                 + (f" / 승인 결정: {'승인' if approved else '거절'}" if approval else "")
                 + (" / ⚠️ 장부 기록 실패 — 수동 확인 필요" if book_log and not book_log["all_logged"] else ""),
            test_now=test_now))
        records += 1
        drafted += 1
        if approved:
            approved_count += 1
            session_trades += 1
        else:
            rejected_count += 1

    return {"run_id": run_id, "rule_tag": "SELL",
            "candidates": len(sell_candidates), "records": records, "drafted": drafted,
            "approved": approved_count, "rejected": rejected_count}


# ═══════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════

def main() -> None:
    _assert_phase_config()

    parser = argparse.ArgumentParser(
        description="stock-agent 0단계 자동매매 조립 프로그램 (dry-run 전용 — 실제 주문 없음)")
    parser.add_argument("--rule", default="ALL", choices=["A", "B", "SELL", "ALL"],
                        help="실행할 규칙 (기본 ALL = A → B → SELL)")
    parser.add_argument("--market", default="KR", choices=["KR", "US", "ALL"],
                        help="매수 규칙 대상 시장 (기본 KR — 주문서는 어차피 국내 전용)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_PER_ORDER_KRW,
                        help=f"매수 1건당 예산(원, 기본 {DEFAULT_BUDGET_PER_ORDER_KRW:,})")
    parser.add_argument("--universe-limit", type=int, default=None,
                        help="스크리닝 유니버스 종목 수 제한 (빠른 테스트용, 예: 5)")
    parser.add_argument("--max-orders", type=int, default=DEFAULT_MAX_ORDERS_PER_RULE,
                        help=f"규칙당 주문서 최대 건수 (기본 {DEFAULT_MAX_ORDERS_PER_RULE})")
    parser.add_argument("--test-now", default=None,
                        help='가드레일 시각 주입 (테스트 전용, 예: "2026-07-15T10:00" — KST로 간주, 로그에 기록됨)')
    args = parser.parse_args()

    if args.budget > tools._KIS_ORDER_LIMIT_KRW:
        print(f"⚠️  예산 {args.budget:,}원이 place_kis_order 상한"
              f"({tools._KIS_ORDER_LIMIT_KRW:,}원)을 넘습니다. 주문서 작성이 거부될 수 있습니다.")

    # ── 3단계 세트 전용 시작 검사 (0단계 세트에서는 호출조차 하지 않는다) ──
    if _is_live_mode():
        _check_phase3_confirm()   # .env PHASE3_CONFIRM 확인 문구
        _confirm_live_start()     # 경고 배너 + 사람이 확인 문구 직접 타이핑

    print("=" * 60)
    if _is_live_mode():
        print("auto_trader v5 — 🔴 3단계 실주문 모드 (승인된 주문서는 실제 전송됩니다)")
    else:
        print("auto_trader v5 — 0단계 DRY RUN 전용 (실제 주문 없음)")
    print(f"PHASE={PHASE} / MASTER_ENABLE={MASTER_ENABLE} / DRY_RUN={DRY_RUN}")
    print(f"기록 파일: {DRYRUN_LOG_PATH}")
    print("=" * 60)

    summaries = []
    if args.rule in ("A", "ALL"):
        print("\n▶ 매수 규칙 A (점수 집중) dry-run 시작...")
        summaries.append(run_buy_rule("A", args.market, args.budget,
                                      args.universe_limit, args.max_orders, args.test_now))
    if args.rule in ("B", "ALL"):
        print("\n▶ 매수 규칙 B (분산 채우기) dry-run 시작...")
        summaries.append(run_buy_rule("B", args.market, args.budget,
                                      args.universe_limit, args.max_orders, args.test_now))
    if args.rule in ("SELL", "ALL"):
        print("\n▶ 매도 규칙 (손절/익절) dry-run 시작...")
        summaries.append(run_sell_rule(args.test_now))

    print("\n" + "=" * 60)
    print("실행 요약")
    print("=" * 60)
    for s in summaries:
        if "error" in s:
            print(f"  [{s.get('run_id', '?')}] ❌ 에러: {s['error']}")
        else:
            print(f"  [{s['run_id']}] 규칙 {s.get('rule_tag', '?')}: "
                  f"후보 {s.get('candidates', 0)}건 / 기록 {s.get('records', 0)}건 / "
                  f"주문서 {s.get('drafted', 0)}건 ({'전부 dry-run' if DRY_RUN else '실주문 모드'})"
                  + (f" — 승인 {s.get('approved', 0)}건 · 거절 {s.get('rejected', 0)}건"
                     if s.get('drafted', 0) else ""))
    print(f"\n상세 기록: {DRYRUN_LOG_PATH}")
    if _is_live_mode():
        print("🔴 3단계 실주문 모드로 실행되었습니다. 실제 전송 결과는 각 기록의 execution 필드를 확인하세요.")
    else:
        print("⚠️  이 프로그램은 0단계 관찰용입니다. 실제 주문은 단 1건도 전송되지 않았습니다.")


if __name__ == "__main__":
    main()
