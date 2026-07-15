"""
auto_trader.py — 0단계(관찰) 전용 자동매매 "조립" 프로그램  [v3]

역할
----
tools.py에 이미 만들어진 부품들을 **순서대로 연결만** 한다. 새 매매 로직은 없다.

    매수 파이프라인 (규칙 A / B):
        evaluate_buy_rule_A/B  →  get_quote(가격)  →  size_buy_order(수량 환산)
        →  get_auto_trade_stats_today + check_guardrails(가드레일)
        →  place_kis_order(dry_run=True, 주문서만)  →  dry-run 로그 기록

    매도 파이프라인 (규칙 SELL — 손절 -10% / 익절 +20%):
        evaluate_sell_rules  →  get_kis_balance(보유 수량)  →  size_sell_order(전량)
        →  check_guardrails(거래일·시간·횟수 확인)
        →  place_kis_order(dry_run=True, 주문서만)  →  dry-run 로그 기록

0단계 안전장치 (코드로 강제 — 관행 아님)
----------------------------------------
1. MASTER_ENABLE = False, DRY_RUN = True 가 하드코딩돼 있고,
   모든 진입점이 _assert_phase0()로 이 값을 검사한다. 값이 바뀌면 즉시 예외로 중단.
2. place_kis_order 호출부는 dry_run=True 리터럴 고정. 이 파일 어디에도
   dry_run=False 경로가 없다 (grep으로 검증 가능).
3. 기록은 auto_trader_dryrun_log.json 에만 쓴다.
   실제 A/B 실험 장부(trade_log.json / log_trade)는 절대 건드리지 않는다.
   _assert_phase0()가 두 경로가 같지 않은지도 검사한다.
4. 매도 기록의 rule_tag는 처음부터 정식 "SELL" 태그를 쓴다.

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
    get_auto_trade_stats_today,
    get_kis_balance,
    get_quote,
    place_kis_order,
)

# ═══════════════════════════════════════════════
# 0단계 상수 — 이 블록의 값을 바꾸는 코드를 작성하지 말 것
# ═══════════════════════════════════════════════

PHASE = 0
MASTER_ENABLE = False          # 실주문 마스터 스위치 — 0단계에서는 잠금 (True 금지)
DRY_RUN = True                 # 항상 True — 이 파일에 dry_run=False 경로는 없다

_BASE_DIR = Path(__file__).resolve().parent
DRYRUN_LOG_PATH = str(_BASE_DIR / "auto_trader_dryrun_log.json")

DEFAULT_BUDGET_PER_ORDER_KRW = 500_000   # 매수 1건당 예산 (place_kis_order 상한 100만 원의 절반)
DEFAULT_MAX_ORDERS_PER_RULE = 3          # 규칙 1회 실행당 주문서 작성 최대 건수


# ═══════════════════════════════════════════════
# 안전장치
# ═══════════════════════════════════════════════

def _assert_phase0() -> None:
    """0단계 안전장치 검사. 하나라도 어긋나면 즉시 예외로 전체 중단."""
    if MASTER_ENABLE is not False:
        raise RuntimeError(
            "[안전장치] MASTER_ENABLE이 False가 아닙니다. "
            "0단계에서는 실주문 마스터 스위치가 잠겨 있어야 합니다. 실행을 중단합니다."
        )
    if DRY_RUN is not True:
        raise RuntimeError(
            "[안전장치] DRY_RUN이 True가 아닙니다. "
            "0단계에서는 dry-run만 허용됩니다. 실행을 중단합니다."
        )
    if os.path.abspath(DRYRUN_LOG_PATH) == os.path.abspath(tools.TRADE_LOG_PATH):
        raise RuntimeError(
            "[안전장치] dry-run 로그 경로가 실제 실험 장부(trade_log.json)와 같습니다. "
            "장부 오염 위험 — 실행을 중단합니다."
        )


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
    _assert_phase0()

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
) -> dict:
    """dry-run 로그 1건 양식. decision: ORDER_DRAFTED | BLOCKED | SKIPPED | ERROR"""
    return {
        "timestamp": _now_kst().isoformat(timespec="seconds"),
        "run_id": run_id,
        "phase": PHASE,
        "dry_run": True,
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


def _draft_order(ticker: str, side: str, qty: int) -> dict:
    """
    주문서 작성 — 반드시 dry_run=True 리터럴 고정.
    이 함수 외의 곳에서 place_kis_order를 직접 호출하지 말 것.
    """
    _assert_phase0()
    return place_kis_order(ticker, side, qty, "MARKET", dry_run=True)


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
    _assert_phase0()
    run_id = f"buy{rule_tag}-{uuid.uuid4().hex[:8]}"
    now_inject = _parse_test_now(test_now)

    rule_tag = rule_tag.upper().strip()
    if rule_tag not in ("A", "B"):
        return {"error": f"run_buy_rule은 'A' 또는 'B'만 허용합니다. 입력: '{rule_tag}'"}

    rule_fn = evaluate_buy_rule_A if rule_tag == "A" else evaluate_buy_rule_B

    # ── 1. 규칙 평가 (판단/제안만) ──────────────────────────────
    result = rule_fn(market=market, universe_limit=universe_limit)
    if "error" in result:
        entry = _make_entry(run_id, rule_tag, "ERROR",
                            note=f"규칙 평가 실패: {result['error']}", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": result["error"]}

    candidates = result.get("candidates", [])
    if not candidates:
        entry = _make_entry(run_id, rule_tag, "SKIPPED",
                            note="후보 없음 (규칙 기준 미달)", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "drafted": 0, "records": 1, "note": "후보 없음"}

    # ── 2. 오늘 누적치 집계 (가드레일 입력 — 손 계산 금지) ──────
    stats = get_auto_trade_stats_today(rule_tag)
    if "error" in stats:
        # fail-safe: 가드레일 입력을 못 구하면 진행하지 않는다.
        entry = _make_entry(run_id, rule_tag, "ERROR",
                            note=f"오늘 누적치 집계 실패 → 진행 중단: {stats['error']}",
                            test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": stats["error"]}

    # 이번 실행(세션) 안에서 통과한 주문서 금액도 누적에 더한다.
    # (dry-run이라 trade_log에 없으므로 — 같은 실행 내 중복 초과를 막기 위한 산수)
    session_accum = 0
    session_by_ticker: dict[str, int] = {}
    session_by_sector: dict[str, int] = {}
    session_trades = 0

    drafted = 0
    records = 0

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
        gr = check_guardrails(
            ticker, order_amount,
            sector=sector,
            accumulated_krw=stats["accumulated_krw"] + session_accum,
            ticker_accumulated_krw=stats["by_ticker"].get(ticker, 0)
                                   + session_by_ticker.get(ticker, 0),
            sector_accumulated_krw=stats["by_sector"].get(sector, 0)
                                   + session_by_sector.get(sector, 0),
            daily_trades=stats["daily_trades"] + session_trades,
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

        _append_dryrun_log(_make_entry(
            run_id, rule_tag, "ORDER_DRAFTED", ticker=ticker, name=name, side="BUY",
            qty=qty, price=price, order_amount_krw=order_amount, sector=sector,
            guardrail=gr, order_draft=draft,
            note=f"[DRY RUN] 규칙 {rule_tag} 매수 주문서 작성 — 실제 전송 안 됨. "
                 f"근거: {cand.get('reason', '')}",
            test_now=test_now))
        records += 1
        drafted += 1

        # 세션 누적 갱신 (통과분만)
        session_accum += order_amount
        session_by_ticker[ticker] = session_by_ticker.get(ticker, 0) + order_amount
        session_by_sector[sector] = session_by_sector.get(sector, 0) + order_amount
        session_trades += 1

    return {"run_id": run_id, "rule_tag": rule_tag,
            "candidates": len(candidates), "records": records, "drafted": drafted}


# ═══════════════════════════════════════════════
# 매도 파이프라인 (규칙 SELL — 손절/익절)
# ═══════════════════════════════════════════════

def run_sell_rule(test_now: str | None = None) -> dict:
    """
    매도 규칙 1회 dry-run 실행. 실제 주문은 절대 나가지 않는다.
    rule_tag는 처음부터 정식 "SELL" 태그를 쓴다 (MANUAL 임시 방편 폐기).
    """
    _assert_phase0()
    run_id = f"sell-{uuid.uuid4().hex[:8]}"
    now_inject = _parse_test_now(test_now)

    # ── 1. 매도 규칙 평가 (판단/제안만) ─────────────────────────
    result = evaluate_sell_rules()
    if "error" in result:
        entry = _make_entry(run_id, "SELL", "ERROR",
                            note=f"매도 규칙 평가 실패: {result['error']}", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "error": result["error"]}

    sell_candidates = list(result.get("stop_loss", [])) + list(result.get("take_profit", []))
    if not sell_candidates:
        entry = _make_entry(run_id, "SELL", "SKIPPED",
                            note="손절/익절 후보 없음", test_now=test_now)
        _append_dryrun_log(entry)
        return {"run_id": run_id, "drafted": 0, "records": 1, "note": "후보 없음"}

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
    records = 0
    session_trades = 0

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

        holding = holdings_by_ticker.get(ticker)
        qty = size_sell_order(holding.get("qty") if holding else None)
        price = float(holding.get("current_price") or 0) if holding else 0.0
        if qty < 1 or price <= 0:
            _append_dryrun_log(_make_entry(
                run_id, "SELL", "ERROR", ticker=ticker, name=name, side="SELL",
                note="잔고에서 보유 수량/현재가를 찾지 못함", test_now=test_now))
            records += 1
            continue

        order_amount = int(qty * price)

        # ── 4. 가드레일 검사 ────────────────────────────────────
        # 매도에서 실질적으로 의미 있는 검사는 거래일·거래시간·하루 거래 횟수다.
        # 누적 "매수" 한도 입력은 0으로 전달한다 (매도는 산 금액 누적과 무관).
        # 단, order_amount가 커서 금액 검사에 걸리면 그대로 기록한다 — 0단계 관찰 데이터.
        gr = check_guardrails(
            ticker, order_amount,
            sector=None,                 # 매도는 섹터 비중 검사 대상 아님 → 건너뜀
            accumulated_krw=0,
            ticker_accumulated_krw=0,
            sector_accumulated_krw=0,
            daily_trades=session_trades,
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

        _append_dryrun_log(_make_entry(
            run_id, "SELL", "ORDER_DRAFTED", ticker=ticker, name=name, side="SELL",
            qty=qty, price=price, order_amount_krw=order_amount,
            guardrail=gr, order_draft=draft,
            note=f"[DRY RUN] 매도 규칙 주문서 작성 (전량) — 실제 전송 안 됨. 근거: {reason}",
            test_now=test_now))
        records += 1
        drafted += 1
        session_trades += 1

    return {"run_id": run_id, "rule_tag": "SELL",
            "candidates": len(sell_candidates), "records": records, "drafted": drafted}


# ═══════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════

def main() -> None:
    _assert_phase0()

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

    print("=" * 60)
    print(f"auto_trader v3 — 0단계 DRY RUN 전용 (실제 주문 없음)")
    print(f"MASTER_ENABLE={MASTER_ENABLE} / DRY_RUN={DRY_RUN}")
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
                  f"주문서 {s.get('drafted', 0)}건 (전부 dry-run)")
    print(f"\n상세 기록: {DRYRUN_LOG_PATH}")
    print("⚠️  이 프로그램은 0단계 관찰용입니다. 실제 주문은 단 1건도 전송되지 않았습니다.")


if __name__ == "__main__":
    main()
