"""
📈 자동매매 성적표 — 웹앱(app.py) 본문 맨 위에 크게 띄우는 읽기 전용 화면 (2026-11-10 감자 요청·확정,
같은 날 20:59 "가장자리 말고 중간화면에 크게" → 사이드바에서 본문으로 옮김).

원칙
- KIS·네트워크 호출 0. tools.py도 import하지 않는다(토큰·잔고 창구를 절대 건드리지 않음).
- 봇이 이미 저장한 파일만 읽는다. 봇은 이 파일들을 tmp + os.replace(원자적)로 저장하므로
  회차 도중에 읽어도 깨진 파일을 읽지 않는다.
- 어떤 파일도 쓰거나 고치지 않는다. 읽기에 실패하면 그 칸만 "확인 불가"로 표시하고 계속한다.

읽는 파일(모두 운영 폴더 기준 절대 경로)
- kill_switch_state.json  : 누적·오늘·규칙별 손익률(킬스위치가 계산해 저장한 값)
- trade_log.json          : 장부 — 규칙별 건수, 봇 보유(장부 기준), 최근 거래
- order_unknown_lock.json / order_unknown_lock_us.json : 잠금 여부
- auto_trader_dryrun_log.json : 미국 실주문 결정값(오늘 시도)
- logs/autotrader_YYYYMMDD.log · logs/autotrader_us_YYYYMMDD.log : 마지막 회차 시각, "보유 유지" 줄 수익률

투자 권유가 아닌 참고용 화면이다.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent

KS_BASE_KRW = 60_000_000          # 킬스위치 분모(A·B 각 3,000만) — tools._KS_TOTAL_BASE_KRW와 같은 값(표시용)
KS_DAILY_LIMIT_PCT = -5.0         # 표시용 — tools._KS_DAILY_LIMIT_PCT
KS_CUMULATIVE_LIMIT_PCT = -15.0   # 표시용 — tools._KS_CUMULATIVE_LIMIT_PCT
_US_LIVE_DECISIONS = ("US_LIVE_ORDER", "US_LIVE_UNFILLED", "US_LIVE_UNKNOWN")
_HOLD_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\].*📌 보유 유지 \d+종목: (.*)$")
_ITEM_RE = re.compile(r"^(\S+) (.+)\(([+-]?\d+(?:\.\d+)?)%\)$")
_TIME_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\]")


def _read_json(path: Path):
    """JSON 읽기. 없으면 (None, "없음"), 실패하면 (None, "읽기 실패: …")."""
    if not path.exists():
        return None, "없음"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:  # noqa: BLE001 — 화면용, 어떤 실패든 그 칸만 확인 불가
        return None, f"읽기 실패: {type(e).__name__}"


def _latest_log(base: Path, pattern: str) -> Path | None:
    files = sorted(glob.glob(str(base / "logs" / pattern)))
    return Path(files[-1]) if files else None


def _ledger_positions(trades: list) -> dict:
    """장부 기준 봇 보유: {ticker: {"A": qty, "B": qty, "currency": ..}}. MANUAL은 제외(C-1과 같은 기준)."""
    pos: dict = {}
    for t in trades:
        if not isinstance(t, dict):
            continue
        tag = str(t.get("rule_tag", "")).upper()
        side = str(t.get("side", "")).upper()
        tk = str(t.get("ticker", "")).strip()
        try:
            q = int(t.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if side == "BUY" and tag in ("A", "B"):
            rule = tag
        elif side == "SELL" and tag == "SELL" and str(t.get("source_rule") or "").upper() in ("A", "B"):
            rule = str(t.get("source_rule")).upper()
            q = -q
        else:
            continue
        p = pos.setdefault(tk, {"A": 0, "B": 0, "currency": t.get("currency") or "KRW"})
        p[rule] += q
    return {k: v for k, v in pos.items() if v["A"] or v["B"]}


def collect(base_dir: str | Path | None = None, now: datetime | None = None) -> dict:
    """화면에 쓸 값을 모은다(읽기 전용). 시험에서 base_dir·now를 주입한다."""
    base = Path(base_dir) if base_dir else _BASE_DIR
    out: dict = {"base": str(base)}

    # 1) 킬스위치 손익률
    ks, err = _read_json(base / "kill_switch_state.json")
    if err:
        out["ks_error"] = err
    else:
        lc = (ks or {}).get("last_check") or {}
        by = lc.get("by_rule") or {}
        out["ks"] = {
            "at": lc.get("at"),
            "cumulative": lc.get("cumulative_pnl_pct"),
            "daily": lc.get("daily_pnl_pct"),
            "A": by.get("A"),
            "B": by.get("B"),
            "daily_triggered": bool(((ks or {}).get("daily") or {}).get("triggered")),
            "cumulative_triggered": bool(((ks or {}).get("cumulative") or {}).get("triggered")),
        }

    # 2) 장부
    tl, err = _read_json(base / "trade_log.json")
    if err and err != "없음":
        out["ledger_error"] = err
    else:
        trades = tl if isinstance(tl, list) else []
        counts = {}
        for t in trades:
            if isinstance(t, dict):
                key = (str(t.get("rule_tag", "?")), str(t.get("side", "?")))
                counts[key] = counts.get(key, 0) + 1
        out["ledger"] = {
            "total": len(trades),
            "counts": {f"{r} {s}": n for (r, s), n in sorted(counts.items())},
            "positions": _ledger_positions(trades),
            "recent": [
                {k: t.get(k) for k in ("timestamp", "rule_tag", "side", "ticker", "qty", "price",
                                       "currency", "price_source", "order_no")}
                for t in trades[-10:][::-1] if isinstance(t, dict)
            ],
        }

    # 3) 잠금
    out["lock_kr"] = (base / "order_unknown_lock.json").exists()
    out["lock_us"] = (base / "order_unknown_lock_us.json").exists()

    # 4) 국내 마지막 로그 — 마지막 "보유 유지" 줄과 마지막 시각
    kr = _latest_log(base, "autotrader_2*.log")
    if kr:
        info = {"file": kr.name, "last_time": None, "hold_time": None, "hold": []}
        try:
            for line in open(kr, "r", encoding="utf-8", errors="replace"):
                line = line.rstrip("\n")
                m = _TIME_RE.match(line)
                if m:
                    info["last_time"] = m.group(1)
                h = _HOLD_RE.match(line)
                if h:
                    info["hold_time"] = h.group(1)
                    items = []
                    for part in h.group(2).split(", "):
                        im = _ITEM_RE.match(part.strip())
                        if im:
                            items.append({"ticker": im.group(1), "name": im.group(2), "pct": float(im.group(3))})
                    info["hold"] = items
        except Exception as e:  # noqa: BLE001
            info["error"] = f"읽기 실패: {type(e).__name__}"
        out["kr_log"] = info

    # 5) 미국 마지막 로그 — 마지막 시각, US_MODE 줄
    us = _latest_log(base, "autotrader_us_*.log")
    if us:
        info = {"file": us.name, "last_time": None, "mode_line": None}
        try:
            for line in open(us, "r", encoding="utf-8", errors="replace"):
                line = line.rstrip("\n")
                m = _TIME_RE.match(line)
                if m:
                    info["last_time"] = m.group(1)
                if "US_MODE=" in line:
                    info["mode_line"] = line.split("US_MODE=", 1)[1][:80]
        except Exception as e:  # noqa: BLE001
            info["error"] = f"읽기 실패: {type(e).__name__}"
        out["us_log"] = info

    # 6) 미국 실주문 결정값(최근 것부터 5건)
    dr, err = _read_json(base / "auto_trader_dryrun_log.json")
    if err and err != "없음":
        out["us_live_error"] = err
    else:
        recs = (dr or {}).get("records", []) if isinstance(dr, dict) else []
        live = [r for r in recs if isinstance(r, dict) and (
            r.get("decision") in _US_LIVE_DECISIONS
            or (r.get("decision") == "ERROR" and r.get("currency") == "USD"
                and bool((r.get("execution") or {}).get("executed"))))]
        # 국내 마지막 매도 평가 요약의 손절 보류 목록 (보유 유지 줄에 안 찍히는 종목)
        kr_sell = [r for r in recs if isinstance(r, dict) and r.get("rule_tag") == "SELL"
                   and r.get("decision") == "EVAL_SUMMARY" and str(r.get("timestamp", ""))[11:13] < "20"]
        if kr_sell:
            last = kr_sell[-1]
            ds = (last.get("eval_summary") or {}).get("stop_loss_deferred") or []
            out["deferred"] = {
                "timestamp": last.get("timestamp"),
                "items": [{"ticker": str(x.get("ticker")), "name": x.get("name"),
                           "pct": x.get("profit_loss_pct"), "note": x.get("trend_note")}
                          for x in ds if isinstance(x, dict)],
            }
        out["us_live"] = [
            {"timestamp": r.get("timestamp"), "decision": r.get("decision"), "ticker": r.get("ticker"),
             "limit": (r.get("execution") or {}).get("limit_price_usd"),
             "order_no": ((r.get("execution") or {}).get("summary") or {}).get("order_no")}
            for r in live[-5:][::-1]
        ]
    return out


def _pct(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "확인 불가"


def render_sidebar() -> None:
    """app.py 사이드바 맨 위에서 호출. 어떤 오류가 나도 사이트는 계속 뜬다."""
    import streamlit as st

    try:
        d = collect()
    except Exception as e:  # noqa: BLE001
        st.caption(f"📈 자동매매 성적 — 확인 불가({type(e).__name__})")
        return

    with st.expander("📈 자동매매 성적 (읽기 전용 · KIS 호출 없음)", expanded=True):
        ks = d.get("ks")
        if ks:
            st.metric("누적 수익률 (A+B)", _pct(ks["cumulative"]),
                      help=f"기준 {KS_BASE_KRW:,}원(A·B 각 3,000만), 실현+평가. 킬스위치 {KS_CUMULATIVE_LIMIT_PCT}%")
            c1, c2, c3 = st.columns(3)
            c1.metric("규칙 A", _pct(ks["A"]))
            c2.metric("규칙 B", _pct(ks["B"]))
            c3.metric("오늘", _pct(ks["daily"]))
            st.caption(f"계산 시각 {ks['at'] or '확인 불가'} (마지막 회차 킬스위치 계산값)")
            if ks["daily_triggered"] or ks["cumulative_triggered"]:
                st.error("⛔ 킬스위치 발동 상태")
        else:
            st.caption(f"손익률 확인 불가 — kill_switch_state.json {d.get('ks_error', '')}")

        if d.get("lock_us"):
            st.warning("⛔ 미국 실주문 잠금 중 — KIS 앱 해외 체결 확인 필요")
        if d.get("lock_kr"):
            st.warning("⛔ 국내 결과 미확인 잠금 중")

        kr = d.get("kr_log")
        if kr and kr.get("hold"):
            st.markdown(f"**보유 종목 수익률** ({kr['file'][11:19]} {kr['hold_time']} 로그 기준)")
            for it in kr["hold"]:
                st.caption(f"{it['name']} {it['pct']:+.2f}%")
            st.caption("손절 보류·매도 후보 종목은 이 줄에 없다(로그의 '보유 유지' 줄만).")

        lg = d.get("ledger")
        if lg:
            pos = lg["positions"]
            if pos:
                st.markdown("**봇 보유 (장부 기준)**")
                for tk, p in sorted(pos.items()):
                    st.caption(f"{tk} — A {p['A']}주 · B {p['B']}주" + (" · USD" if p["currency"] == "USD" else ""))
            with st.expander(f"장부 {lg['total']}건 · 최근 거래"):
                st.caption(" · ".join(f"{k} {v}" for k, v in lg["counts"].items()))
                for t in lg["recent"]:
                    price = t.get("price")
                    ptxt = (f"{price:,.2f} USD" if t.get("currency") == "USD" else f"{price:,.0f}원") \
                        if isinstance(price, (int, float)) else "—"
                    st.caption(f"{str(t.get('timestamp'))[:16]} {t.get('rule_tag')} {t.get('side')} "
                               f"{t.get('ticker')} {t.get('qty')}주 @{ptxt} ({t.get('price_source') or '—'})")
        elif d.get("ledger_error"):
            st.caption(f"장부 확인 불가 — {d['ledger_error']}")

        if d.get("us_live"):
            with st.expander("미국 실주문 최근 결과"):
                for r in d["us_live"]:
                    st.caption(f"{str(r['timestamp'])[:16]} {r['decision']} {r['ticker']} "
                               f"지정가 {r['limit']} · 주문번호 {r['order_no'] or '—'}")

        times = []
        if d.get("kr_log"):
            times.append(f"국내 {d['kr_log']['file'][11:19]} {d['kr_log']['last_time']}")
        if d.get("us_log"):
            times.append(f"미국 {d['us_log']['file'][14:22]} {d['us_log']['last_time']}")
        if times:
            st.caption("마지막 회차: " + " / ".join(times))
        st.caption("⚠️ 참고용 화면이며 투자 권유가 아닙니다.")


def _holdings_rows(d: dict) -> list[dict]:
    """장부 보유 + 로그 '보유 유지' 수익률 + 손절 보류 목록을 한 표로 합친다."""
    names, pcts, state = {}, {}, {}
    for it in (d.get("kr_log") or {}).get("hold", []):
        names[it["ticker"]] = it["name"]; pcts[it["ticker"]] = it["pct"]; state[it["ticker"]] = "보유 유지"
    for it in (d.get("deferred") or {}).get("items", []):
        names.setdefault(it["ticker"], it.get("name"))
        if isinstance(it.get("pct"), (int, float)):
            pcts[it["ticker"]] = it["pct"]
        state[it["ticker"]] = "손절 보류"
    rows = []
    for tk, p in sorted(((d.get("ledger") or {}).get("positions") or {}).items()):
        pct = pcts.get(tk)
        rows.append({
            "종목": names.get(tk) or tk,
            "코드": tk,
            "규칙 A": p["A"],
            "규칙 B": p["B"],
            "수익률": f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—",
            "상태": state.get(tk, "로그에 없음" + (" (미국)" if p["currency"] == "USD" else "")),
        })
    return rows


def render_main() -> None:
    """app.py 본문 맨 위(제목 아래)에서 호출. 어떤 오류가 나도 사이트는 계속 뜬다."""
    import streamlit as st

    try:
        d = collect()
    except Exception as e:  # noqa: BLE001
        st.caption(f"📈 자동매매 성적 — 확인 불가({type(e).__name__})")
        return

    with st.expander("📈 자동매매 성적 (읽기 전용 · KIS 호출 없음)", expanded=True):
        ks = d.get("ks")
        if ks:
            c0, c1, c2, c3 = st.columns(4)
            c0.metric("누적 수익률 (A+B)", _pct(ks["cumulative"]),
                      help=f"기준 {KS_BASE_KRW:,}원(A·B 각 3,000만), 실현+평가. 킬스위치 {KS_CUMULATIVE_LIMIT_PCT}%")
            c1.metric("규칙 A", _pct(ks["A"]))
            c2.metric("규칙 B", _pct(ks["B"]))
            c3.metric("오늘", _pct(ks["daily"]), help=f"킬스위치 {KS_DAILY_LIMIT_PCT}%")
            st.caption(f"계산 시각 {str(ks['at'] or '확인 불가')[:19].replace('T', ' ')} — 마지막 회차가 저장한 킬스위치 계산값. "
                       f"기준 {KS_BASE_KRW:,}원, 실현+평가.")
            if ks["daily_triggered"] or ks["cumulative_triggered"]:
                st.error("⛔ 킬스위치 발동 상태")
        else:
            st.caption(f"손익률 확인 불가 — kill_switch_state.json {d.get('ks_error', '')}")

        if d.get("lock_us"):
            st.warning("⛔ 미국 실주문 잠금 중 — KIS 앱 해외 체결 확인 필요")
        if d.get("lock_kr"):
            st.warning("⛔ 국내 결과 미확인 잠금 중")

        left, right = st.columns([1, 1])
        with left:
            st.markdown("**봇 보유 종목** (장부 기준)")
            rows = _holdings_rows(d)
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True)
                kr = d.get("kr_log") or {}
                st.caption(f"수익률: 국내 로그 {kr.get('file', '')[11:19]} {kr.get('hold_time') or ''} '보유 유지' 줄 · "
                           "손절 보류 종목은 그 회차 매도 평가 요약 값")
            elif d.get("ledger_error"):
                st.caption(f"장부 확인 불가 — {d['ledger_error']}")
            else:
                st.caption("봇 보유 없음")
        with right:
            lg = d.get("ledger")
            if lg:
                st.markdown(f"**최근 거래** (장부 {lg['total']}건)")
                recent = []
                for t in lg["recent"]:
                    price = t.get("price")
                    ptxt = (f"{price:,.2f} USD" if t.get("currency") == "USD" else f"{price:,.0f}원") \
                        if isinstance(price, (int, float)) else "—"
                    recent.append({"시각": str(t.get("timestamp"))[:16].replace("T", " "),
                                   "규칙": t.get("rule_tag"), "매매": t.get("side"), "종목": t.get("ticker"),
                                   "수량": t.get("qty"), "가격": ptxt})
                st.dataframe(recent, hide_index=True, use_container_width=True)

        if d.get("us_live"):
            st.markdown("**미국 실주문 최근 결과**")
            st.dataframe([{"시각": str(r["timestamp"])[:16].replace("T", " "), "결과": r["decision"],
                           "종목": r["ticker"], "지정가(USD)": r["limit"], "주문번호": r["order_no"] or "—"}
                          for r in d["us_live"]], hide_index=True, use_container_width=True)

        times = []
        if d.get("kr_log"):
            times.append(f"국내 {d['kr_log']['file'][11:19]} {d['kr_log']['last_time']}")
        if d.get("us_log"):
            times.append(f"미국 {d['us_log']['file'][14:22]} {d['us_log']['last_time']}")
        head = ("마지막 회차: " + " / ".join(times) + " · ") if times else ""
        st.caption(head + "숫자는 회차가 끝날 때 바뀝니다(새로고침). ⚠️ 참고용 화면이며 투자 권유가 아닙니다.")
