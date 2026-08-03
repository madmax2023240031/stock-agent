#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_pnl_report.py — 자동매매 장부 전용 손익 리포트 (읽기 전용)

목적
----
계좌 전체(수동 보유분 포함) 리포트와 섞이지 않도록,
rule_tag A/B/SELL 로 기록된 자동매매분만 떼어내어 성적을 보여준다.

⚠️ 이 스크립트는 100% 읽기 전용이다.
   - 주문 함수(place_kis_order)를 import 하지도, 호출하지도 않는다.
   - 어떤 파일도 생성·수정·삭제하지 않는다 (trade_log.json 도 읽기만).

사용법
------
  ./venv/bin/python auto_pnl_report.py
  ./venv/bin/python auto_pnl_report.py --with-account
  ./venv/bin/python auto_pnl_report.py --json

주의
----
이 리포트의 수치는 참고용 데이터 정리이며 투자 권유가 아니다.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

import tools

BAR = "=" * 78
SUB = "-" * 78


# ── 출력 헬퍼 ──────────────────────────────────────────────
def _num(v, digits: int = 0) -> str:
    if v is None:
        return "-"
    if digits:
        return f"{v:,.{digits}f}"
    return f"{round(v):,}"


def _signed(v, digits: int = 0) -> str:
    if v is None:
        return "-"
    sign = "+" if v >= 0 else ""
    if digits:
        return f"{sign}{v:,.{digits}f}"
    return f"{sign}{round(v):,}"


def _pct(v) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def _wlen(s) -> int:
    """터미널 표시 폭. 한글·전각 문자는 2칸으로 센다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def _cut(s, width: int) -> str:
    """표시 폭 기준으로 자르기 (한글 중간에서 깨지지 않도록)."""
    out = ""
    used = 0
    for ch in str(s):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out += ch
        used += w
    return out


def _pad(s, width: int) -> str:
    """표시 폭 기준 왼쪽 정렬."""
    s = _cut(s, width)
    return s + " " * max(0, width - _wlen(s))


def _lpad(s, width: int) -> str:
    """표시 폭 기준 오른쪽 정렬."""
    s = _cut(s, width)
    return " " * max(0, width - _wlen(s)) + s


# ── 데이터 수집 ────────────────────────────────────────────
def _name_map() -> dict:
    """trade_log.json 에서 ticker -> 종목명 매핑. 실패해도 빈 dict."""
    try:
        log = tools.get_trade_log()
        if "error" in log:
            return {}
        out = {}
        for t in log.get("trades", []):
            tk = str(t.get("ticker", "")).strip()
            nm = str(t.get("name", "") or "").strip()
            if tk and nm and tk not in out:
                out[tk] = nm
        return out
    except Exception:
        return {}


_BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_CACHE_FILE = str(_BASE_DIR / "universe_cache.json")


def _collect_names(node, out: dict, depth: int = 0) -> None:
    """JSON 트리를 훑으며 ticker/name 쌍을 모은다.

    캐시 최상위가 list 이든 {"KR": {...}} 형태의 dict 이든 상관없이 동작하도록
    경로를 고정하지 않는다 (캐시 구조가 바뀌어도 견디게 하기 위함).
    """
    if depth > 6:
        return
    if isinstance(node, dict):
        tk = str(node.get("ticker", "") or "").strip()
        nm = str(node.get("name", "") or "").strip()
        if tk and nm:
            out.setdefault(tk, nm)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _collect_names(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                _collect_names(v, out, depth + 1)


def _universe_name_map() -> dict:
    """universe_cache.json 에서 ticker -> 종목명 매핑. 네트워크 조회 없음.

    파일이 없거나 깨져 있어도 예외를 밖으로 내보내지 않고 빈 dict 를 돌려준다.
    (이름 때문에 리포트 전체가 죽는 일이 없도록 하는 것이 최우선)
    """
    try:
        with open(UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out: dict = {}
    try:
        _collect_names(data, out)
    except Exception:
        return {}
    return out


def _prices(tickers) -> dict:
    """현재가 조회. 실패한 종목은 None (집계는 calc_auto_trade_pnl 값을 신뢰)."""
    out = {}
    for tk in sorted(tickers):
        try:
            q = tools.get_quote(tk)
            if "error" in q or q.get("close") is None:
                out[tk] = None
            else:
                out[tk] = float(q["close"])
        except Exception:
            out[tk] = None
    return out


def build_report(with_account: bool = False) -> dict:
    pos = tools.get_auto_trade_positions()
    if "error" in pos:
        return {"error": f"포지션 계산 실패: {pos['error']}"}

    pnl = tools.calc_auto_trade_pnl()
    if "error" in pnl:
        return {"error": f"손익 계산 실패: {pnl['error']}"}

    combined = pos["combined"]
    by_rule = pos.get("by_rule", {})
    names = _name_map()
    for _tk, _nm in _universe_name_map().items():
        names.setdefault(_tk, _nm)   # 장부에 name 이 생기면 장부가 우선
    prices = _prices(combined["positions"].keys())

    rule_of = {}
    for rule in ("A", "B"):
        for tk in by_rule.get(rule, {}).get("positions", {}):
            rule_of.setdefault(tk, rule)

    rows = []
    for tk, p in combined["positions"].items():
        qty = p["qty"]
        avg = p["avg_price"]
        cur = prices.get(tk)
        eval_amt = None if cur is None else cur * qty
        pl = None if cur is None else (cur - avg) * qty
        pl_pct = None if (cur is None or not avg) else (cur / avg - 1) * 100
        rows.append({
            "ticker": tk,
            "name": names.get(tk, tk),
            "rule": rule_of.get(tk, "-"),
            "market": p.get("market", "KR"),
            "currency": "USD" if p.get("market") == "US" else "KRW",
            "qty": qty,
            "avg_price": avg,
            "current_price": cur,
            "eval_amount": eval_amt,
            "profit_loss": pl,
            "profit_loss_pct": pl_pct,
        })
    rows.sort(key=lambda r: -(r["eval_amount"] or 0))

    report = {
        "as_of": pnl.get("as_of"),
        "positions": rows,
        "summary": {
            "eval_krw": pnl.get("eval_krw"),
            "realized_pnl_krw": pnl.get("realized_pnl_krw"),
            "unrealized_pnl_krw": pnl.get("unrealized_pnl_krw"),
            "cumulative_pnl_krw": pnl.get("cumulative_pnl_krw"),
            "cumulative_pnl_pct": pnl.get("cumulative_pnl_pct"),
            "today_buy_krw": pnl.get("today_buy_krw"),
            "today_sell_krw": pnl.get("today_sell_krw"),
        },
        "by_rule": pnl.get("by_rule", {}),
        "warnings": list(pos.get("warnings", [])) + list(pnl.get("warnings", [])),
        "account": None,
    }

    if with_account:
        bal = tools.get_kis_balance()
        if "error" in bal:
            report["account"] = {"error": str(bal["error"])[:300]}
        else:
            auto_qty = {tk: p["qty"] for tk, p in combined["positions"].items()}
            held = set()
            items = []
            for h in bal.get("holdings", []):
                tk = h["ticker"]
                held.add(tk)
                a = auto_qty.get(tk, 0)
                items.append({
                    "ticker": tk,
                    "name": h.get("name", tk),
                    "market": h.get("market"),
                    "currency": h.get("currency"),
                    "account_qty": h.get("qty"),
                    "auto_qty": a,
                    "manual_qty": (h.get("qty") or 0) - a,
                    "eval_amount": h.get("eval_amount"),
                    "profit_loss_pct": h.get("profit_loss_pct"),
                })
            items.sort(key=lambda x: -(x["eval_amount"] or 0))
            report["account"] = {
                "as_of": bal.get("as_of"),
                "items": items,
                "missing_in_account": [tk for tk in auto_qty if tk not in held],
                "overseas_errors": bal.get("overseas", {}).get("errors", []),
            }

    return report


# ── 화면 출력 ──────────────────────────────────────────────
def print_report(r: dict) -> None:
    print(BAR)
    print("  자동매매 장부 전용 손익 리포트  (수동 보유분 제외)")
    print(f"  조회 시각: {r.get('as_of')}")
    print(BAR)

    rows = r["positions"]
    if not rows:
        print("\n보유 포지션 없음 (자동매매 장부 기준).")
    else:
        print("\n[보유 포지션]")
        print(f"{_pad('코드', 10)}{_pad('종목명', 18)}{_pad('규칙', 5)}{_lpad('수량', 6)}"
              f"{_lpad('평단', 13)}{_lpad('현재가', 13)}{_lpad('평가액', 15)}"
              f"{_lpad('평가손익', 15)}{_lpad('수익률', 10)}  통화")
        print(SUB)
        for x in rows:
            d = 2 if x["currency"] == "USD" else 0
            print(f"{_pad(x['ticker'], 10)}{_pad(x['name'], 18)}{_pad(x['rule'], 5)}"
                  f"{_lpad(x['qty'], 6)}"
                  f"{_lpad(_num(x['avg_price'], d), 13)}"
                  f"{_lpad(_num(x['current_price'], d), 13)}"
                  f"{_lpad(_num(x['eval_amount'], d), 15)}"
                  f"{_lpad(_signed(x['profit_loss'], d), 15)}"
                  f"{_lpad(_pct(x['profit_loss_pct']), 10)}  {x['currency']}")

    s = r["summary"]
    print("\n[누적 성적] — 분모: 합산 60,000,000원 / 규칙별 30,000,000원")
    print(SUB)
    print(f"  현재 평가액       : {_num(s['eval_krw'])} 원")
    print(f"  실현손익          : {_signed(s['realized_pnl_krw'])} 원")
    print(f"  평가손익          : {_signed(s['unrealized_pnl_krw'])} 원")
    print(f"  누적손익          : {_signed(s['cumulative_pnl_krw'])} 원  "
          f"({_pct(s['cumulative_pnl_pct'])})")
    print(f"  오늘 매수 / 매도  : {_num(s['today_buy_krw'])} 원 / {_num(s['today_sell_krw'])} 원")

    br = r.get("by_rule") or {}
    if br:
        print("\n[규칙별] — 기록·모니터링용 (킬 스위치 차단 판정에는 미사용)")
        print(SUB)
        for rule in ("A", "B"):
            b = br.get(rule) or {}
            print(f"  규칙 {rule} : {_signed(b.get('cumulative_pnl_krw'))} 원  "
                  f"({_pct(b.get('cumulative_pnl_pct'))})")

    acc = r.get("account")
    if acc:
        print("\n[계좌 대조] — 자동매매분 vs 수동 보유분")
        print(SUB)
        if "error" in acc:
            print(f"  계좌 조회 실패: {acc['error']}")
        else:
            if acc.get("overseas_errors"):
                print(f"  ⚠️ 해외 조회 오류: {acc['overseas_errors']}")
            print(f"{_pad('코드', 10)}{_pad('종목명', 18)}{_lpad('계좌수량', 9)}"
                  f"{_lpad('자동', 7)}{_lpad('수동', 7)}"
                  f"{_lpad('평가액', 15)}{_lpad('수익률', 10)}  통화")
            for it in acc["items"]:
                d = 2 if it["currency"] == "USD" else 0
                mark = " *" if it["auto_qty"] else "  "
                print(f"{_pad(it['ticker'], 10)}{_pad(it['name'], 18)}"
                      f"{_lpad(_num(it['account_qty']), 9)}"
                      f"{_lpad(_num(it['auto_qty']), 7)}"
                      f"{_lpad(_num(it['manual_qty']), 7)}"
                      f"{_lpad(_num(it['eval_amount'], d), 15)}"
                      f"{_lpad(_pct(it['profit_loss_pct']), 10)}  {it['currency']}{mark}")
            print("\n  * = 자동매매 장부에 포함된 종목")
            if acc.get("missing_in_account"):
                print(f"  ⚠️ 장부에는 있으나 계좌에 없는 종목: {acc['missing_in_account']}")

    if r.get("warnings"):
        print("\n[경고]")
        print(SUB)
        for w in r["warnings"]:
            print(f"  - {w}")

    print("\n" + SUB)
    print("  ⚠️ 이 수치는 자동매매 장부만의 성적이다. 계좌 전체 성적과 다르며,")
    print("     수동 보유분은 시스템 밖(C-1)이므로 봇 성능 판단에 포함하지 않는다.")
    print("  ⚠️ 참고용 데이터 정리이며 투자 권유가 아니다.")
    print(BAR)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="자동매매 장부 전용 손익 리포트 (읽기 전용)")
    ap.add_argument("--with-account", action="store_true",
                    help="KIS 계좌 잔고와 대조해 수동 보유분을 분리 표시")
    ap.add_argument("--json", action="store_true",
                    help="사람용 표 대신 JSON 출력")
    args = ap.parse_args()

    r = build_report(with_account=args.with_account)

    if "error" in r:
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(f"❌ 리포트 생성 실패: {r['error']}")
        return 1

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
