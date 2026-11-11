"""
미국 실주문 1차(2026-11-06 결정 D1–D8) 오프라인 시험.

- 실제 KIS·네트워크 호출 0: requests.post/get을 가짜로 바꿔 끼우고, 실제로 나가려 하면 예외.
- 실제 장부(trade_log.json)·dry-run 기록·잠금 파일은 건드리지 않는다:
  모든 경로를 임시 폴더로 돌린 뒤에만 시험을 시작한다 (경로 확인 assert).
- .env 없이 돈다: 필요한 환경 변수는 이 파일이 임시로 넣는다 (가짜 계좌번호).

실행: ./venv/bin/python test_us_live.py   (끝에 "전부 통과" + exit 0)
"""
import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

# ── 0. import 전에 환경 변수 (4단계 세트 + 핀) — .env 파일은 만들지 않는다 ──
os.environ["STOCK_AGENT_PHASE"] = "4"
os.environ["PHASE4_CONFIRM"] = "승인 면제 4단계를 승인합니다"
os.environ["KIS_ACCOUNT_NO"] = "12345678-01"            # 가짜 계좌 (시험 전용)
os.environ["KIS_ALLOWED_MOCK_ACCOUNT"] = "12345678-01"
os.environ["KIS_APP_KEY"] = "TEST"
os.environ["KIS_APP_SECRET"] = "TEST"
os.environ.pop("US_LIVE_CONFIRM", None)

BASE = Path(__file__).resolve().parent
assert not (BASE / ".env").exists() or os.environ.get("ALLOW_ENV_FOR_TEST") == "1", \
    ".env가 있는 폴더(운영 폴더)에서는 이 시험을 돌리지 않는다 — dev 클론에서만"

import requests  # noqa: E402

_NET = {"post": [], "get": []}


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {}, ensure_ascii=False)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


POST_BEHAVIOR = {"mode": "ok"}


def _fake_post(url, json=None, headers=None, timeout=None, **kw):
    _NET["post"].append({"url": url, "body": json, "tr_id": (headers or {}).get("tr_id"), "timeout": timeout})
    assert "openapivts" in url or "vts" in url or url.startswith("https://openapivts"), f"모의 도메인 아님: {url}"
    m = POST_BEHAVIOR["mode"]
    if m == "ok":
        return _Resp(200, {"rt_cd": "0", "msg1": "모의 주문 완료", "output": {"ODNO": "0000099001", "ORD_TMD": "094012"}})
    if m == "read_timeout":
        raise requests.exceptions.ReadTimeout("가짜 응답 지연")
    if m == "connect_timeout":
        raise requests.exceptions.ConnectTimeout("가짜 연결 실패")
    if m == "http500":
        return _Resp(500, {}, "server error")
    if m == "api_error":
        return _Resp(200, {"rt_cd": "1", "msg1": "모의 주문 거부(가짜)"})
    raise AssertionError(f"알 수 없는 mode {m}")


def _fake_get(*a, **k):
    _NET["get"].append(a)
    raise AssertionError("시험 중 GET 네트워크 호출 금지")


requests.post = _fake_post
requests.get = _fake_get

import tools  # noqa: E402
import auto_trader as at  # noqa: E402

# ── 1. 모든 파일 경로를 임시 폴더로 ──
TMP = Path(tempfile.mkdtemp(prefix="us_live_test_"))
REAL_TRADE_LOG = tools.TRADE_LOG_PATH
tools.TRADE_LOG_PATH = str(TMP / "trade_log_TEST.json")
at.DRYRUN_LOG_PATH = str(TMP / "dryrun_TEST.json")
at.ORDER_UNKNOWN_LOCK_PATH = str(TMP / "lock_kr_TEST.json")
at.ORDER_UNKNOWN_LOCK_US_PATH = str(TMP / "lock_us_TEST.json")
tools.ORDER_UNKNOWN_LOCK_US_PATH_FOR_BOOK = at.ORDER_UNKNOWN_LOCK_US_PATH   # 2026-11-10 보완(4a) us_book도 임시 폴더
assert os.path.abspath(tools.TRADE_LOG_PATH) != os.path.abspath(REAL_TRADE_LOG)
assert str(TMP) in at.DRYRUN_LOG_PATH and str(TMP) in at.ORDER_UNKNOWN_LOCK_US_PATH
assert str(TMP) in tools.ORDER_UNKNOWN_LOCK_US_PATH_FOR_BOOK

# ── 2. 바깥 의존 가짜로 ──
tools.get_kis_token = lambda *a, **k: {"access_token": "FAKE"}
at.update_kill_switch_state = lambda now=None: {
    "daily_pnl_pct": 0.0, "cumulative_pnl_pct": -0.9, "daily_triggered": False,
    "cumulative_triggered": False, "as_of": "TEST"}
at.get_combined_auto_trade_stats_today = lambda: {
    "per_rule": {"A": {}, "B": {}}, "daily_trades": 0}
QUOTE = {"close": 150.0}
at.get_quote = lambda t: {"close": QUOTE["close"], "name": t}
at.time.sleep = lambda s: None

FILL = {"mode": "filled"}


FILL_CALLS = []


def _fake_fill(order_no, ticker, expected_qty, order_date_kst, **k):
    FILL_CALLS.append({"order_no": order_no, "date": order_date_kst, **k})
    m = FILL["mode"]
    if m == "filled":
        return {"order_no": order_no, "row": {"odno": order_no, "ft_ccld_qty": "1",
                                              "ft_ccld_unpr3": "150.40", "nccs_qty": "0"}}
    if m == "unfilled":
        return {"order_no": order_no, "row": {"odno": order_no, "ft_ccld_qty": "0",
                                              "ft_ccld_unpr3": "0", "nccs_qty": "1"}}
    if m == "nofield":
        return {"order_no": order_no, "row": {"odno": order_no, "something": "x"}}
    return {"error": "가짜 체결 조회 실패"}


_REAL_FILL_US = tools.get_kis_fill_price_us   # 2026-11-11 U30: 실제 함수의 주문번호 비교 시험용
tools.get_kis_fill_price_us = _fake_fill

SCORED = {"n": 500}
CANDS = {"list": [{"ticker": "EOG", "name": "EOG Resources", "score": 80, "sector": "Energy",
                   "market": "US", "exchange": "NYSE", "reason": "점수 상위"}]}


def _fake_rule(market="US", universe_limit=None):
    return {"candidates": [dict(c) for c in CANDS["list"]], "excluded": [],
            "screen_scored": SCORED["n"], "screen_total": 503}


at.evaluate_buy_rule_A = _fake_rule
at.evaluate_buy_rule_B = _fake_rule

# 월 11/9 23:40 KST = 뉴욕 09:40 (장중)
NOW_OK = "2026-11-09T23:40"


def reset(us_mode="live", pin=True, fill="filled", post="ok", scored=500, price=150.0):
    for p in (tools.TRADE_LOG_PATH, at.DRYRUN_LOG_PATH, at.ORDER_UNKNOWN_LOCK_US_PATH, at.ORDER_UNKNOWN_LOCK_PATH):
        if os.path.exists(p):
            os.remove(p)
    at._US_UNKNOWN_IN_PROCESS = None
    at._UNKNOWN_ORDER_IN_PROCESS = None
    at.US_MODE = us_mode
    if pin:
        os.environ["US_LIVE_CONFIRM"] = "미국 실주문 1차를 승인합니다"
    else:
        os.environ.pop("US_LIVE_CONFIRM", None)
    FILL["mode"] = fill
    POST_BEHAVIOR["mode"] = post
    SCORED["n"] = scored
    QUOTE["close"] = price
    CANDS["list"] = [{"ticker": "EOG", "name": "EOG Resources", "score": 80, "sector": "Energy",
                      "market": "US", "exchange": "NYSE", "reason": "점수 상위"}]
    _NET["post"].clear()
    FILL_CALLS.clear()


def recs():
    if not os.path.exists(at.DRYRUN_LOG_PATH):
        return []
    return json.load(open(at.DRYRUN_LOG_PATH, encoding="utf-8"))["records"]


def decisions():
    return [r["decision"] for r in recs()]


def ledger():
    if not os.path.exists(tools.TRADE_LOG_PATH):
        return []
    return json.load(open(tools.TRADE_LOG_PATH, encoding="utf-8"))


PASS = []


def check(name, cond, detail=""):
    if not cond:
        print(f"  ❌ {name} {detail}")
        raise SystemExit(1)
    PASS.append(name)
    print(f"  ✅ {name}")


def run(rule="A", now=NOW_OK):
    return at.run_buy_rule(rule, "US", test_now=now)


try:
    print("[U1] US_MODE=dry_run → 기존 dry-run, 전송 0")
    reset(us_mode="dry_run")
    run()
    check("U1 결정 US_DRYRUN", "US_DRYRUN" in decisions(), decisions())
    check("U1 POST 0", len(_NET["post"]) == 0)
    check("U1 장부 0", ledger() == [])

    print("[U2] live인데 US_LIVE_CONFIRM 핀 없음 → dry-run으로 내려감")
    reset(pin=False)
    check("U2 상태 live=False", at._us_live_status()["live"] is False)
    run()
    check("U2 POST 0", len(_NET["post"]) == 0)
    check("U2 결정 US_DRYRUN", "US_DRYRUN" in decisions(), decisions())

    print("[U3] live 정상 체결 → 장부 1건(USD, 체결가), 지정가 150.75")
    reset()
    out = run()
    check("U3 POST 1", len(_NET["post"]) == 1, _NET["post"])
    body = _NET["post"][0]["body"]
    check("U3 지정가 150.75", body["OVRS_ORD_UNPR"] == "150.75", body)
    check("U3 수량 1", body["ORD_QTY"] == "1")
    check("U3 모의 매수 TR", _NET["post"][0]["tr_id"] == "VTTT1002U")
    check("U3 거래소 NYSE", body["OVRS_EXCG_CD"] == "NYSE")
    check("U3 결정 US_LIVE_ORDER", "US_LIVE_ORDER" in decisions(), decisions())
    lg = ledger()
    check("U3 장부 1건", len(lg) == 1, lg)
    check("U3 장부 USD·150.4·fill_avg",
          lg[0]["currency"] == "USD" and lg[0]["price"] == 150.4 and lg[0]["price_source"] == "fill_avg"
          and lg[0]["exchange"] == "NYSE" and lg[0]["order_no"] == "0000099001", lg[0])
    check("U3 잠금 없음", not os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))
    check("U3 us_live_sent 1", out.get("us_live_sent") == 1, out)

    print("[U4] 같은 뉴욕 날짜에 장부 1건 있음 → 실주문 안 함")
    reset()
    json.dump([{"rule_tag": "A", "ticker": "ADI", "side": "BUY", "qty": 1, "price": 400.0,
                "currency": "USD", "timestamp": "2026-11-09T23:45:00+09:00"}],
              open(tools.TRADE_LOG_PATH, "w", encoding="utf-8"))
    run(now="2026-11-10T01:00")   # 뉴욕 11/9 11:00 — 같은 날
    check("U4 POST 0", len(_NET["post"]) == 0)
    check("U4 note 하루 상한", any("하루 1건" in (r.get("note") or "") for r in recs()))
    print("[U4b] 다음 뉴욕 날짜(11/10)면 다시 가능")
    reset()
    json.dump([{"rule_tag": "A", "ticker": "ADI", "side": "BUY", "qty": 1, "price": 400.0,
                "currency": "USD", "timestamp": "2026-11-09T23:45:00+09:00"}],
              open(tools.TRADE_LOG_PATH, "w", encoding="utf-8"))
    run(now="2026-11-10T23:40")
    check("U4b POST 1", len(_NET["post"]) == 1)

    print("[U5] 전송 성공·미체결 → 장부 0, 미국 잠금")
    reset(fill="unfilled")
    run()
    check("U5 POST 1", len(_NET["post"]) == 1)
    check("U5 결정 US_LIVE_UNFILLED", "US_LIVE_UNFILLED" in decisions(), decisions())
    check("U5 장부 0", ledger() == [])
    check("U5 잠금 파일", os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))
    print("[U5b] 체결 행 필드 확인 불가 → 미체결로 처리(장부 0, 잠금)")
    reset(fill="nofield")
    run()
    check("U5b 장부 0 + 잠금", ledger() == [] and os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))
    print("[U5c] 체결 조회 자체 실패 → 장부 0, 잠금")
    reset(fill="error")
    run()
    check("U5c 장부 0 + 잠금", ledger() == [] and os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))

    print("[U6] 미국 잠금 파일 있음 → 실주문 안 함, dry-run 기록은 계속")
    reset()
    json.dump({"note": "test", "events": [{"kind": "test"}]},
              open(at.ORDER_UNKNOWN_LOCK_US_PATH, "w", encoding="utf-8"))
    run()
    check("U6 POST 0", len(_NET["post"]) == 0)
    check("U6 US_DRYRUN + 잠금 note", any(r["decision"] == "US_DRYRUN" and "미국 잠금" in r["note"] for r in recs()))
    print("[U6b] 잠금 파일이 깨져 있음 → 잠금으로 간주")
    reset()
    open(at.ORDER_UNKNOWN_LOCK_US_PATH, "w").write("{깨짐")
    run()
    check("U6b POST 0", len(_NET["post"]) == 0)

    print("[U7] 보낸 뒤 응답 지연(ReadTimeout) → 결과 미확인, 장부 0, 잠금")
    reset(post="read_timeout")
    run()
    check("U7 POST 1", len(_NET["post"]) == 1)
    check("U7 결정 US_LIVE_UNKNOWN", "US_LIVE_UNKNOWN" in decisions(), decisions())
    check("U7 장부 0 + 잠금", ledger() == [] and os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))
    print("[U7b] 서버 5xx → 결과 미확인")
    reset(post="http500")
    run()
    check("U7b US_LIVE_UNKNOWN + 잠금", "US_LIVE_UNKNOWN" in decisions() and os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))

    print("[U8] 연결 전 실패(ConnectTimeout) → 미전송 ERROR, 잠금 없음")
    reset(post="connect_timeout")
    run()
    check("U8 ERROR", "ERROR" in decisions(), decisions())
    check("U8 잠금 없음", not os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH))
    print("[U9] API 거부(rt_cd≠0) → ERROR, 잠금 없음, 장부 0")
    reset(post="api_error")
    run()
    check("U9 ERROR·잠금 없음·장부 0", "ERROR" in decisions()
          and not os.path.exists(at.ORDER_UNKNOWN_LOCK_US_PATH) and ledger() == [])

    print("[U10] 스크리너 점수화 449 < 450 → 실주문 안 함")
    reset(scored=449)
    run()
    check("U10 POST 0", len(_NET["post"]) == 0)
    check("U10 note 하한", any("하한" in (r.get("note") or "") for r in recs()))
    print("[U10b] 점수화 수 없음(None) → 실주문 안 함(fail-safe)")
    reset()
    SCORED["n"] = None
    run()
    check("U10b POST 0", len(_NET["post"]) == 0)
    print("[U10c] 450이면 진행")
    reset(scored=450)
    run()
    check("U10c POST 1", len(_NET["post"]) == 1)

    print("[U11] 한 회차 후보 2개 → 첫 번째만 실주문, 두 번째는 dry-run")
    reset()
    CANDS["list"].append({"ticker": "ADI", "name": "Analog Devices", "score": 79, "sector": "Tech",
                          "market": "US", "exchange": "NASD", "reason": "점수 상위"})
    run()
    check("U11 POST 1", len(_NET["post"]) == 1)
    check("U11 결정 = ORDER + DRYRUN", decisions().count("US_LIVE_ORDER") == 1
          and decisions().count("US_DRYRUN") == 1, decisions())
    print("[U11b] A 뒤 B(별도 실행) → B는 실주문 안 함(장부 1건으로 하루 상한)")
    # log_trade는 실제 시계로 timestamp를 찍으므로, 시험 시각(11/9 뉴욕)에 맞춰 바꿔 둔다
    _lg = ledger()
    check("U11b 장부 1건", len(_lg) == 1, _lg)
    _lg[0]["timestamp"] = "2026-11-09T23:41:00+09:00"
    json.dump(_lg, open(tools.TRADE_LOG_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    at._US_UNKNOWN_IN_PROCESS = None
    run(rule="B")
    check("U11b POST 여전히 1", len(_NET["post"]) == 1)

    print("[U12] 거래시간 밖(뉴욕 13:00) → 가드레일 차단, 전송 0")
    reset()
    run(now="2026-11-10T03:00")   # 뉴욕 11/9 13:00
    check("U12 US_DRYRUN_BLOCKED", "US_DRYRUN_BLOCKED" in decisions(), decisions())
    check("U12 POST 0", len(_NET["post"]) == 0)
    print("[U13] 미국 휴장일 11/26 뉴욕 09:40 → 차단")
    reset()
    run(now="2026-11-26T23:40")
    check("U13 US_DRYRUN_BLOCKED", "US_DRYRUN_BLOCKED" in decisions(), decisions())
    check("U13 POST 0", len(_NET["post"]) == 0)
    check("U13 사유 휴장일", any("휴장일" in (r.get("note") or "") for r in recs()))
    print("[U14] 주말(뉴욕 토요일) → 차단")
    reset()
    run(now="2026-11-14T23:40")
    check("U14 POST 0", len(_NET["post"]) == 0)
    check("U14 US_DRYRUN_BLOCKED", "US_DRYRUN_BLOCKED" in decisions(), decisions())

    print("[U15] 현재가 1,600 USD → 1주 상한 초과 SKIPPED")
    reset(price=1600.0)
    run()
    check("U15 POST 0 + SKIPPED", len(_NET["post"]) == 0 and "SKIPPED" in decisions())
    print("[U16] 현재가 1,495 → 지정가 1,502.47 > 1,500 → 주문 함수가 거절, ERROR, 전송 0")
    reset(price=1495.0)
    run()
    check("U16 지정가 계산", at._us_limit_price(1495.0) == 1502.47, at._us_limit_price(1495.0))
    check("U16 POST 0 + ERROR", len(_NET["post"]) == 0 and "ERROR" in decisions(), decisions())

    print("[U17] 지정가 계산은 +0.5% 이내(센트 아래 버림)")
    for p in (150.0, 155.17, 419.79, 312.49, 1.01, 99.99):
        lp = at._us_limit_price(p)
        check(f"U17 {p} → {lp}", p <= lp <= p * 1.005 + 1e-9 and round(lp, 2) == lp)

    print("[U18] 조건이 안 맞는데 _execute_order_us 호출 → 예외(배선 버그 방지)")
    reset(pin=False)
    try:
        at._execute_order_us("EOG", 1, 150.75, "NYSE")
        check("U18 예외", False)
    except RuntimeError:
        check("U18 예외", True)
    check("U18 POST 0", len(_NET["post"]) == 0)

    print("[U19] parse_us_fill_row")
    check("U19 정상", tools.parse_us_fill_row({"ft_ccld_qty": "1", "ft_ccld_unpr3": "150.4", "nccs_qty": "0"}, 1)["filled"])
    check("U19 부분", not tools.parse_us_fill_row({"ft_ccld_qty": "0", "ft_ccld_unpr3": "0", "nccs_qty": "1"}, 1)["filled"])
    check("U19 필드 없음", not tools.parse_us_fill_row({"x": 1}, 1)["filled"])
    check("U19 None", not tools.parse_us_fill_row(None, 1)["filled"])
    check("U19 대문자 키", tools.parse_us_fill_row({"FT_CCLD_QTY": "1", "FT_CCLD_UNPR3": "10", "NCCS_QTY": "0"}, 1)["filled"])

    print("[U20] 코드 규칙: auto_trader.py에 dry_run=False 리터럴 0건, US_MODE 값 live(2026-11-10 775d8da 운영 반영)")
    src = open(BASE / "auto_trader.py", encoding="utf-8").read()
    check("U20 dry_run=False 0건", src.count("dry_run=False") == 0)
    check("U20 US_MODE 값 live", '\nUS_MODE = "live"\n' in src)

    # ── 2026-11-10 보완 패치 시험 (U22–U29) ──
    print("[U22] 보완(4b): 체결 확인 6회 × 10초 상수, 미체결이면 6번 조회")
    _src = open(BASE / "auto_trader.py", encoding="utf-8").read()
    check("U22 ROUNDS 6", "\nUS_FILL_CHECK_ROUNDS = 6 " in _src)
    check("U22 INTERVAL 10", "\nUS_FILL_CHECK_INTERVAL_SEC = 10 " in _src)
    check("U22 모듈 값", at.US_FILL_CHECK_ROUNDS == 6 and at.US_FILL_CHECK_INTERVAL_SEC == 10)
    reset(fill="unfilled")
    run()
    check("U22 조회 6번", len(FILL_CALLS) == 6, len(FILL_CALLS))

    print("[U23] 보완(3): A 주문 거절(ERROR) 뒤 같은 뉴욕 날 B → 전송 안 함")
    reset(post="api_error")
    run()
    check("U23 A 거절 POST 1", len(_NET["post"]) == 1 and "ERROR" in decisions(), decisions())
    _d = json.load(open(at.DRYRUN_LOG_PATH, encoding="utf-8"))
    for _r in _d["records"]:
        _r["timestamp"] = "2026-11-09T23:41:00+09:00"   # 기록은 실제 시계라 시험 시각(뉴욕 11/9)에 맞춤
    json.dump(_d, open(at.DRYRUN_LOG_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    check("U23 시도 집계 1", at._us_live_attempts_today(at._parse_test_now(NOW_OK))["count"] == 1)
    POST_BEHAVIOR["mode"] = "ok"
    run(rule="B")
    check("U23 B POST 여전히 1", len(_NET["post"]) == 1, _NET["post"])
    check("U23 note 거절 포함", any("거절 포함" in (r.get("note") or "") for r in recs()))
    print("[U23b] 다음 뉴욕 날짜면 다시 가능")
    run(now="2026-11-10T23:40")
    check("U23b POST 2", len(_NET["post"]) == 2)
    print("[U23c] 실주문 경로가 아닌 미국 ERROR(execution 없음)는 세지 않음")
    reset()
    json.dump({"note": "t", "count": 1, "records": [{"timestamp": "2026-11-09T23:41:00+09:00", "decision": "ERROR",
               "currency": "USD", "execution": None}]}, open(at.DRYRUN_LOG_PATH, "w", encoding="utf-8"))
    check("U23c 집계 0", at._us_live_attempts_today(at._parse_test_now(NOW_OK))["count"] == 0)
    print("[U23d] dry-run 기록이 깨져 있으면 집계 실패 → 진행 안 함")
    open(at.DRYRUN_LOG_PATH, "w").write("{깨짐")
    check("U23d 오류", "error" in at._us_live_attempts_today(at._parse_test_now(NOW_OK)))
    check("U23d precheck 차단", "집계 실패" in (at._us_live_precheck({"screen_scored": 500, "screen_total": 503}, 0,
                                                                    at._parse_test_now(NOW_OK)) or ""))

    print("[U24] 보완(2): 요약 가격 표시는 통화로 — 영문 섞인 국내 코드도 원화")
    check("U24 국내 기본 KRW", at._order_brief("00104K", "가짜", "BUY", 1, 7220, None)["currency"] == "KRW")
    check("U24 미국 USD", at._order_brief("EOG", "EOG", "BUY", 1, 150.4, None, currency="USD")["currency"] == "USD")
    check("U24 요약 코드", 'o.get("currency") == "USD"' in _src and ".isdigit() else f\"@{_p:,.2f} USD\"" not in _src)

    print("[U25] 보완(1): 매도 US 분기 문구 조건에 live 포함")
    check("U25 조건", 'if market == "US" and US_MODE in ("dry_run", "live"):' in _src)

    print("[U26] 보완(4a): 미체결 잠금 사건에 장부 재작성 정보")
    reset(fill="unfilled")
    run()
    _lk = json.load(open(at.ORDER_UNKNOWN_LOCK_US_PATH, encoding="utf-8"))
    _ev = _lk["events"][-1]
    check("U26 사건 필드", _ev["kind"] == "unfilled" and _ev["rule_tag"] == "A" and _ev["exchange"] == "NYSE"
          and _ev["order_no"] == "0000099001" and len(_ev["order_date"]) == 8 and _ev.get("recorded_at"), _ev)

    print("[U27] us_book 드라이런 → 기록 예정 1, 장부·잠금 무변경")
    _lock_before = open(at.ORDER_UNKNOWN_LOCK_US_PATH, "rb").read()
    FILL["mode"] = "filled"
    _res = tools.us_book_from_lock(_ev["order_date"])
    check("U27 드라이런 1건", len(_res["booked"]) == 1 and _res["dry_run"] is True, _res)
    check("U27 장부 0", ledger() == [])
    check("U27 조회 max_attempts=1", FILL_CALLS[-1].get("max_attempts") == 1, FILL_CALLS[-1])
    print("[U27b] us_book --write → 장부 1건(체결가, fill_avg_retro, 주문 밤 timestamp), 잠금 그대로")
    _res = tools.us_book_from_lock(_ev["order_date"], dry_run=False)
    _lg = ledger()
    check("U27b 장부 1건", len(_lg) == 1, _lg)
    check("U27b 레코드", _lg[0]["price"] == 150.4 and _lg[0]["price_source"] == "fill_avg_retro"
          and _lg[0]["currency"] == "USD" and _lg[0]["exchange"] == "NYSE" and _lg[0]["rule_tag"] == "A"
          and _lg[0]["order_no"] == "0000099001" and _lg[0]["timestamp"] == _ev["recorded_at"]
          and _lg[0]["side"] == "BUY" and _lg[0]["qty"] == 1 and _lg[0].get("booked_at"), _lg[0])
    check("U27b 잠금 파일 그대로", open(at.ORDER_UNKNOWN_LOCK_US_PATH, "rb").read() == _lock_before)
    check("U27b get_trade_log 읽힘", tools.get_trade_log().get("count") == 1)
    check("U27b 하루 1건 집계에 잡힘", tools.count_us_live_buys_today()["count"] == 1)
    print("[U27c] 같은 날 다시 --write → 이미 장부에 있음, 장부 1건 그대로")
    _res = tools.us_book_from_lock(_ev["order_date"], dry_run=False)
    check("U27c 건너뜀", len(_res["booked"]) == 0 and any("이미 장부" in x["reason"] for x in _res["skipped"]), _res)
    check("U27c 장부 1건", len(ledger()) == 1)

    print("[U28] us_book: 여전히 미체결이면 기록 안 함")
    reset(fill="unfilled")
    run()
    _ev2 = json.load(open(at.ORDER_UNKNOWN_LOCK_US_PATH, encoding="utf-8"))["events"][-1]
    _res = tools.us_book_from_lock(_ev2["order_date"], dry_run=False)
    check("U28 기록 0 + 사유", len(_res["booked"]) == 0 and any("전량 체결 확인 안 됨" in x["reason"] for x in _res["skipped"]), _res)
    check("U28 장부 0", ledger() == [])
    print("[U28b] us_book: 필드 확인 불가·조회 실패도 기록 안 함")
    FILL["mode"] = "nofield"
    check("U28b 필드 불가", len(tools.us_book_from_lock(_ev2["order_date"], dry_run=False)["booked"]) == 0)
    FILL["mode"] = "error"
    check("U28b 조회 실패", len(tools.us_book_from_lock(_ev2["order_date"], dry_run=False)["booked"]) == 0)
    check("U28b 장부 0", ledger() == [])
    print("[U28c] us_book: 날짜가 다르면 대상 0")
    check("U28c 대상 0", tools.us_book_from_lock("20200101")["targets"] == [])
    print("[U28d] us_book: 결과 미확인(unknown) 사건은 대상 아님")
    reset(post="read_timeout")
    run()
    _ev3 = json.load(open(at.ORDER_UNKNOWN_LOCK_US_PATH, encoding="utf-8"))["events"][-1]
    check("U28d unknown 사건", _ev3["kind"] == "unknown" and _ev3["rule_tag"] == "A", _ev3)
    check("U28d 대상 0", tools.us_book_from_lock(_ev3["order_date"])["targets"] == [])

    print("[U29] us_book: 잠금 파일 없음 → 대상 0 / 깨짐 → 오류 / 날짜 형식 오류")
    reset()
    check("U29 없음", tools.us_book_from_lock("20261109")["targets"] == [])
    open(at.ORDER_UNKNOWN_LOCK_US_PATH, "w").write("{깨짐")
    check("U29 깨짐 오류", "error" in tools.us_book_from_lock("20261109"))
    check("U29 날짜 오류", "error" in tools.us_book_from_lock("2026-11-09"))
    check("U29 장부 0", ledger() == [])

    # ── 2026-11-11 시험 (U30): 미국 체결 조회의 주문번호 비교 — 앞 0 차이 ──
    print("[U30] get_kis_fill_price_us: 응답 odno '38202'(앞 0 없음) ↔ 주문번호 '0000038202' 같은 주문으로 찾음")
    _U30 = {"rows": [], "calls": 0}

    class _U30Resp(_Resp):
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(f"{self.status_code}")

    def _u30_get(url, params=None, headers=None, timeout=None, **kw):
        _U30["calls"] += 1
        assert "openapivts" in url and "inquire-ccnl" in url, url
        return _U30Resp(200, {"rt_cd": "0", "msg1": "모의투자 조회가 완료되었습니다.", "output": list(_U30["rows"]),
                           "ctx_area_nk200": "", "ctx_area_fk200": ""})

    _row = {"odno": "38202", "orgn_odno": "0", "pdno": "APP", "ord_dt": "20261110", "ord_tmd": "234106",
            "sll_buy_dvsn_cd": "02", "ft_ord_qty": "1", "ft_ccld_qty": "1", "ft_ccld_unpr3": "402.28000000", "nccs_qty": "0"}
    requests.get = _u30_get
    try:
        _U30["rows"] = [_row]
        _r = _REAL_FILL_US("0000038202", "APP", 1, "20261110", max_attempts=1)
        check("U30 찾음", "row" in _r and _r["row"]["odno"] == "38202", _r)
        check("U30 반환 주문번호는 원래 값", _r.get("order_no") == "0000038202", _r)
        _p = tools.parse_us_fill_row(_r.get("row"), 1)
        check("U30 판정 전량 402.28", _p.get("filled") is True and _p.get("fill_price") == 402.28, _p)
        _r = _REAL_FILL_US("38202", "APP", 1, "20261110", max_attempts=1)
        check("U30 짧은 번호로도 찾음", "row" in _r, _r)
        _U30["rows"] = [dict(_row, odno="0000038202")]
        check("U30 응답이 10자리여도 찾음", "row" in _REAL_FILL_US("0000038202", "APP", 1, "20261110", max_attempts=1))
        print("[U30b] 다른 번호·빈 번호는 찾지 않음")
        _U30["rows"] = [dict(_row, odno="382020"), dict(_row, odno="138202"), dict(_row, odno=""), dict(_row, odno="0")]
        _r = _REAL_FILL_US("0000038202", "APP", 1, "20261110", max_attempts=2, retry_interval=0)
        check("U30b 미발견", "error" in _r and "미발견" in _r["error"], _r)
        _r = _REAL_FILL_US("", "APP", 1, "20261110", max_attempts=1)
        check("U30b 빈 주문번호는 빈 odno와 맞추지 않음", "error" in _r, _r)
        _r = _REAL_FILL_US("0000000000", "APP", 1, "20261110", max_attempts=1)
        check("U30b 0뿐인 주문번호도 맞추지 않음", "error" in _r, _r)
        check("U30 조회 수", _U30["calls"] == 7, _U30["calls"])
    finally:
        requests.get = _fake_get

    print("[U21] 실제 장부 파일 무접촉")
    check("U21 실제 장부 경로 다름", os.path.abspath(tools.TRADE_LOG_PATH) != os.path.abspath(REAL_TRADE_LOG))
    check("U21 GET 네트워크 0", len(_NET["get"]) == 0)
    check("U21 us_book 잠금 경로 임시", str(TMP) in tools.ORDER_UNKNOWN_LOCK_US_PATH_FOR_BOOK)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 55)
print(f"  ✅ 미국 실주문 1차 오프라인 시험 전부 통과 ({len(PASS)}개 확인)")
print("=" * 55)
