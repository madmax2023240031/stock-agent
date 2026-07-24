"""
tools.py
========
데이터·지표·뉴스·재무·거시 함수 모음.

규칙 (CLAUDE.md):
- 모든 함수는 dict 반환. 실패 시 {"error": "..."} 반환.
- 한국 종목: 6자리 코드(예: "005930"), 미국: 티커(예: "AAPL").
- 함수 시그니처(이름·인자)는 안정적으로 유지한다.
"""

import math
import re
import time
import threading
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import FinanceDataReader as fdr
import pandas as pd

warnings.filterwarnings("ignore")  # yfinance deprecation 등 노이즈 억제

# ── 피어 종목 견적 캐시 (스레드 안전, TTL 10분) ─────────────────────────
_quote_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 600        # 초
_PEER_RETRIES = 2       # 피어 조회 실패 시 최대 재시도 횟수
_PEER_MIN_COUNT = 2     # 이 수 미만이면 "판별 제한"으로 표시

# ── 파일 경로 기준 폴더 ───────────────────────────────────────
# 실행 위치(cwd)와 무관하게 tools.py가 있는 폴더 기준으로 고정
_BASE_DIR = Path(__file__).resolve().parent

# ── 거래 로그 ─────────────────────────────────────────────────
TRADE_LOG_PATH = str(_BASE_DIR / "trade_log.json")
# A=매수규칙A(점수집중) / B=매수규칙B(분산채우기) / MANUAL=사람 직접 / SELL=매도규칙 자동매도(손절/익절)
_VALID_RULE_TAGS = {"A", "B", "MANUAL", "SELL"}

# ── 킬 스위치 상태 파일 ──────────────────────────────────────
# 테스트에서 TRADE_LOG_PATH와 같은 패턴으로 TEST 경로로 바꿔치기할 수 있게 모듈 상수로 둔다.
KILL_SWITCH_STATE_PATH = str(_BASE_DIR / "kill_switch_state.json")


# ═══════════════════════════════════════════════
# 내부 헬퍼
# ═══════════════════════════════════════════════

def _is_korean(ticker: str) -> bool:
    """6자리 숫자면 한국 종목으로 판단."""
    return bool(re.fullmatch(r"\d{6}", ticker.strip()))


def _safe_float(value) -> float | None:
    """NaN / None → None."""
    try:
        v = float(value)
        return None if pd.isna(v) else round(v, 4)
    except (TypeError, ValueError):
        return None


def _yf_ticker(ticker: str) -> str:
    """한국 코드 → yfinance 형식 (005930 → 005930.KS)."""
    return ticker + ".KS" if _is_korean(ticker) else ticker


def _next_us_earnings_date(ticker_obj, info: dict) -> str:
    """미국 종목의 다음 실적 발표 예정일 문자열을 반환한다.

    yf.Ticker.calendar["Earnings Date"](미래 날짜 목록)를 우선 사용하고,
    없으면 Ticker.earnings_dates(과거+미래 혼재 테이블)에서 오늘 이후 가장
    가까운 날짜를 찾는다. 둘 다 실패하면 지어내지 않고 "확인 불가"를 반환한다.
    """
    today = datetime.now().date()
    next_date = None

    try:
        cal = ticker_obj.calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw:
            dates = raw if isinstance(raw, (list, tuple)) else [raw]
            future = [pd.Timestamp(d).date() for d in dates]
            future = [d for d in future if d >= today]
            if future:
                next_date = min(future)
    except Exception:
        pass

    if next_date is None:
        try:
            ed = ticker_obj.earnings_dates
            if ed is not None and not ed.empty:
                future_idx = [pd.Timestamp(d).date() for d in ed.index
                              if pd.Timestamp(d).date() >= today]
                if future_idx:
                    next_date = min(future_idx)
        except Exception:
            pass

    if next_date is None:
        return "확인 불가"

    date_str = next_date.strftime("%Y-%m-%d")
    if info.get("isEarningsDateEstimate"):
        date_str += " (추정치)"
    return date_str


def _filter_halted_rows(df: pd.DataFrame) -> pd.DataFrame:
    """매매정지 구간의 '거래 없음' 행을 제외한다.

    한국 종목은 액면분할 등으로 2~3주 매매정지가 걸리면 FinanceDataReader가
    open/high/low=0, volume=0(또는 결측)인 행을 채워 넣는다. close는 정지 중에도
    직전 종가로 채워지므로 close만으로는 정지 여부를 판별할 수 없다.
    """
    required = {"open", "high", "low", "volume"}
    if not required.issubset(df.columns):
        return df
    no_volume = df["volume"].isna() | (df["volume"] == 0)
    zero_ohl  = (df["open"] == 0) | (df["high"] == 0) | (df["low"] == 0)
    return df[~(no_volume | zero_ohl)]


def _fetch_ohlcv(ticker: str, days: int = 200) -> pd.DataFrame | None:
    """OHLCV DataFrame 반환. 매매정지 구간의 0값 행은 제외한다. 실패 시 None."""
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=days + 30)  # 주말·공휴일 여유
    try:
        df = fdr.DataReader(ticker,
                            start=start_dt.strftime("%Y-%m-%d"),
                            end=end_dt.strftime("%Y-%m-%d"))
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        df = _filter_halted_rows(df)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════
# 0. KIS 모의투자 인증 (토큰 발급 + 캐싱)
# ═══════════════════════════════════════════════

_KIS_DOMAIN          = "https://openapivts.koreainvestment.com:29443"
_KIS_TOKEN_CACHE_FILE = str(_BASE_DIR / ".kis_token_cache.json")
_KIS_TOKEN_BUFFER_SEC = 600   # 만료 10분 전에 갱신 트리거

# ── place_kis_order 안전장치 상수 ──────────────────────────────
_KIS_MOCK_ACCOUNT   = "50193730-01"   # 허용된 모의투자 계좌 (하드코딩)
_KIS_ORDER_LIMIT_KRW = 1_000_000      # 1회 매수 주문 금액 상한 (100만 원) — 매도는 제외 (결정 2)

# 모의투자 tr_id (국내주식 주문)
# ⚠️ 실전: TTTC0802U(매수) / TTTC0801U(매도) — 이 코드에서 절대 사용 금지
# ⚠️ 모의: VTTC0802U(매수) / VTTC0801U(매도)
_KIS_MOCK_TR_BUY  = "VTTC0802U"
_KIS_MOCK_TR_SELL = "VTTC0801U"

# 모의투자에서 조회할 미국 거래소 목록 (거래소코드, 통화코드)
_KIS_US_EXCHANGES = [
    ("NASD", "USD"),   # 나스닥
    ("NYSE", "USD"),   # 뉴욕증권거래소
    ("AMEX", "USD"),   # 아멕스
]


def _kis_maintenance_hint(now=None) -> str:
    """
    현재 한국 시각 기준으로 KIS 점검 시간 여부를 판단해 안내 문구를 반환한다.
    now를 주입하면 실제 시각과 무관하게 테스트할 수 있다.
    """
    try:
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
    except ImportError:
        from datetime import timezone, timedelta as _td
        kst = timezone(_td(hours=9))

    if now is None:
        now = datetime.now(kst)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=kst)

    hour    = now.hour
    weekday = now.weekday()   # 0=월, 6=일
    is_weekend    = weekday >= 5
    is_maint_hour = hour < 7  # 자정~오전 7시
    is_market     = (
        not is_weekend
        and (9 <= hour < 15 or (hour == 15 and now.minute <= 30))
    )

    if is_maint_hour or is_weekend:
        return (
            "⏰ 지금은 KIS 서버 점검 시간(자정~오전 7시)일 수 있습니다. "
            "특히 주말 밤에 점검이 잦습니다. "
            "코드 문제가 아닐 가능성이 높으니, "
            "평일 장 시간(09:00~15:30)에 다시 시도해보세요."
        )
    if is_market:
        return (
            "평일 장 시간인데도 실패했다면 키 만료·설정 오류 등 다른 원인일 수 있습니다. "
            "KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 환경변수를 확인해보세요."
        )
    # 평일 장외 시간 (07:00~09:00 또는 15:30 이후)
    return (
        "지금은 KIS 장외 시간입니다. "
        "자정~오전 7시에는 서버 점검이 있을 수 있습니다. "
        "환경변수를 확인하거나 잠시 후 다시 시도해보세요."
    )


def _fetch_usd_krw_rate() -> float | None:
    """USD/KRW 환율을 FinanceDataReader에서 가져온다. 실패 시 None."""
    try:
        end   = datetime.today()
        start = end - timedelta(days=7)
        df    = fdr.DataReader("USD/KRW",
                               start=start.strftime("%Y-%m-%d"),
                               end=end.strftime("%Y-%m-%d"))
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        col = "close" if "close" in df.columns else df.columns[0]
        return _safe_float(df[col].dropna().iloc[-1])
    except Exception:
        return None


def get_kis_token() -> dict:
    """
    KIS 모의투자 접근토큰을 반환한다.

    유효한 캐시가 있으면 재사용하고, 없거나 만료 10분 전이면 새로 발급한다.
    KIS는 토큰 발급 횟수 제한이 있으므로 반드시 캐시를 경유한다.

    환경변수: KIS_APP_KEY, KIS_APP_SECRET  (.env)

    Returns
    -------
    dict  {access_token, token_type, expires_at, source}
          source : "cache" | "new"
          실패 시: {"error": "..."}
    """
    import json
    import os
    import requests
    from dotenv import load_dotenv

    load_dotenv()

    app_key    = os.getenv("KIS_APP_KEY",    "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()

    if not app_key or not app_secret:
        return {"error": "KIS_APP_KEY 또는 KIS_APP_SECRET 환경변수가 설정되지 않았습니다."}

    now = datetime.now()

    # ── 캐시 확인 ──────────────────────────────────────
    if os.path.exists(_KIS_TOKEN_CACHE_FILE):
        try:
            with open(_KIS_TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            expires_at = datetime.fromisoformat(cached["expires_at"])
            if now < expires_at - timedelta(seconds=_KIS_TOKEN_BUFFER_SEC):
                return {
                    "access_token": cached["access_token"],
                    "token_type":   cached.get("token_type", "Bearer"),
                    "expires_at":   cached["expires_at"],
                    "source":       "cache",
                }
        except Exception:
            pass  # 캐시 손상 → 재발급

    # ── 토큰 신규 발급 ─────────────────────────────────
    url  = f"{_KIS_DOMAIN}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     app_key,
        "appsecret":  app_secret,
    }

    try:
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        body_text = exc.response.text if exc.response is not None else ""
        hint = _kis_maintenance_hint()
        return {"error": f"{hint}\n\nKIS 토큰 발급 HTTP 오류: {exc} — {body_text}"}
    except Exception as exc:
        hint = _kis_maintenance_hint()
        return {"error": f"{hint}\n\nKIS 토큰 발급 실패: {exc}"}

    access_token = data.get("access_token")
    if not access_token:
        hint = _kis_maintenance_hint()
        return {"error": f"{hint}\n\nKIS 응답에 access_token 없음: {data}"}

    token_type = data.get("token_type", "Bearer")

    # 만료 시각: 응답의 access_token_token_expired 우선, 없으면 expires_in 사용
    raw_exp = data.get("access_token_token_expired")
    if raw_exp:
        try:
            expires_at = datetime.strptime(raw_exp, "%Y-%m-%d %H:%M:%S")
        except Exception:
            expires_at = now + timedelta(seconds=int(data.get("expires_in", 86400)))
    else:
        expires_at = now + timedelta(seconds=int(data.get("expires_in", 86400)))

    # ── 캐시 저장 ──────────────────────────────────────
    try:
        with open(_KIS_TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "access_token": access_token,
                    "token_type":   token_type,
                    "expires_at":   expires_at.isoformat(),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass  # 캐시 저장 실패는 치명적이지 않음

    return {
        "access_token": access_token,
        "token_type":   token_type,
        "expires_at":   expires_at.isoformat(),
        "source":       "new",
    }


def get_kis_balance() -> dict:
    """
    KIS 모의투자 계좌의 국내 + 해외(미국) 주식 잔고를 통합 조회한다.

    - 국내주식: tr_id VTTC8434R  (모의투자 전용)
    - 해외주식: tr_id VTTS3012R  (모의투자 전용, NASD·NYSE·AMEX 각각 조회 후 합산)
    ⚠️ 조회(읽기) 전용. 주문 기능 없음.

    환경변수: KIS_ACCOUNT_NO (.env)  — "XXXXXXXX-XX" 또는 "XXXXXXXXXX" 형식 모두 허용.
    토큰은 get_kis_token()을 통해 자동으로 가져온다.

    Returns
    -------
    dict
        {
            "account_no": str,
            "as_of":      str,           # 조회 시각 (ISO)
            "holdings": [
                {
                    "ticker":          str,    # 종목코드 (국내 6자리 / 미국 티커)
                    "name":            str,
                    "qty":             int,
                    "avg_price":       float,
                    "current_price":   float,
                    "eval_amount":     float,  # 평가금액 (currency 단위)
                    "purchase_amount": float,  # 매입금액 (currency 단위)
                    "profit_loss":     float,  # 평가손익 (currency 단위)
                    "profit_loss_pct": float,  # 수익률(%)
                    "currency":        str,    # "KRW" | "USD"
                    "market":          str,    # "KR"  | "US"
                    "exchange":        str,    # "KRX" | "NASD" | "NYSE" | "AMEX"
                }
            ],
            "domestic": {
                "cash_krw":           int,   # 예수금(원화)
                "eval_stock_krw":     int,   # 국내주식 평가금액
                "total_assets_krw":   int,   # 국내 총평가금액(현금 포함)
                "net_assets_krw":     int,   # 순자산금액
                "purchase_total_krw": int,
                "profit_loss_krw":    int,
            },
            "overseas": {
                "eval_total_usd":    float,  # 해외주식 평가금액 합계(USD)
                "purchase_total_usd": float,
                "profit_loss_usd":   float,
                "errors":            list,   # 거래소별 조회 실패 메시지 (정상이면 빈 리스트)
            },
            "fx": {
                "usd_krw":             float | None,   # 참고 환율
                "eval_total_usd_krw":  int   | None,   # 해외 평가금액 원화 환산
                "total_assets_krw_all": int  | None,   # 국내+해외 합산 원화 환산
            },
        }
        실패 시: {"error": "..."}
    """
    import os
    import requests
    from dotenv import load_dotenv

    load_dotenv()

    app_key    = os.getenv("KIS_APP_KEY",    "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    account_no = os.getenv("KIS_ACCOUNT_NO", "").strip().replace("-", "")

    if not account_no:
        return {"error": "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다."}
    if len(account_no) != 10:
        return {"error": f"KIS_ACCOUNT_NO 형식 오류 (10자리 필요): '{account_no}'"}

    cano         = account_no[:8]
    acnt_prdt_cd = account_no[8:]

    tok = get_kis_token()
    if "error" in tok:
        return tok  # error message already has maintenance hint

    base_headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {tok['access_token']}",
        "appkey":        app_key,
        "appsecret":     app_secret,
        "custtype":      "P",
    }

    def _i(v):
        try:
            return int(float(v)) if v not in (None, "", "0") else 0
        except Exception:
            return 0

    def _f(v):
        try:
            return round(float(v), 4) if v not in (None, "") else 0.0
        except Exception:
            return 0.0

    # ── 1. 국내주식 잔고 (VTTC8434R) ───────────────────────
    dom_holdings: list[dict] = []
    domestic: dict = {}

    try:
        resp = requests.get(
            f"{_KIS_DOMAIN}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={**base_headers, "tr_id": "VTTC8434R"},
            params={
                "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "",
                "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        d = resp.json()
        if d.get("rt_cd") == "0":
            for item in d.get("output1", []):
                qty = _i(item.get("hldg_qty", "0"))
                if qty == 0:
                    continue
                dom_holdings.append({
                    "ticker":          item.get("pdno", ""),
                    "name":            item.get("prdt_name", ""),
                    "qty":             qty,
                    "avg_price":       _f(item.get("pchs_avg_pric")),
                    "current_price":   _f(item.get("prpr")),
                    "eval_amount":     _i(item.get("evlu_amt")),
                    "purchase_amount": _i(item.get("pchs_amt")),
                    "profit_loss":     _i(item.get("evlu_pfls_amt")),
                    "profit_loss_pct": _f(item.get("evlu_pfls_rt")),
                    "currency":        "KRW",
                    "market":          "KR",
                    "exchange":        "KRX",
                })
            o2 = d.get("output2") or {}
            if isinstance(o2, list):
                o2 = o2[0] if o2 else {}
            domestic = {
                "cash_krw":           _i(o2.get("dnca_tot_amt")),
                "eval_stock_krw":     _i(o2.get("evlu_amt_smtl_amt")),
                "total_assets_krw":   _i(o2.get("tot_evlu_amt")),
                "net_assets_krw":     _i(o2.get("nass_amt")),
                "purchase_total_krw": _i(o2.get("pchs_amt_smtl_amt")),
                "profit_loss_krw":    _i(o2.get("evlu_pfls_smtl_amt")),
            }
        else:
            msg = d.get("msg1") or d.get("msg") or str(d)
            hint = _kis_maintenance_hint()
            domestic = {"error": f"{hint}\n\n국내 잔고 API 오류: {msg}"}
    except Exception as exc:
        hint = _kis_maintenance_hint()
        domestic = {"error": f"{hint}\n\n국내 잔고 조회 실패: {exc}"}

    # ── 2. 해외주식 잔고 (VTTS3012R, 거래소별 순회) ────────
    ovrs_holdings: list[dict] = []
    ovrs_errors:   list[str]  = []

    def _fetch_ovrs_exchange(excg_cd: str, crcy_cd: str) -> tuple[list, str | None]:
        """단일 거래소 해외잔고 조회. rate-limit(EGW00201) 시 1.5초 후 1회 재시도."""
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD":   excg_cd,
            "TR_CRCY_CD":     crcy_cd,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        for attempt in range(2):
            try:
                r = requests.get(
                    f"{_KIS_DOMAIN}/uapi/overseas-stock/v1/trading/inquire-balance",
                    headers={**base_headers, "tr_id": "VTTS3012R"},
                    params=params,
                    timeout=10,
                )
                r.raise_for_status()
                d = r.json()
                if d.get("rt_cd") == "0":
                    items = []
                    for item in d.get("output1", []):
                        qty = _i(item.get("ovrs_cblc_qty", "0"))
                        if qty == 0:
                            continue
                        items.append({
                            "ticker":          item.get("ovrs_pdno", ""),
                            "name":            item.get("ovrs_item_name", ""),
                            "qty":             qty,
                            "avg_price":       _f(item.get("pchs_avg_pric")),
                            "current_price":   _f(item.get("now_pric2")),
                            "eval_amount":     _f(item.get("ovrs_stck_evlu_amt")),
                            "purchase_amount": _f(item.get("frcr_pchs_amt1")),
                            "profit_loss":     _f(item.get("frcr_evlu_pfls_amt")),
                            "profit_loss_pct": _f(item.get("evlu_pfls_rt")),
                            "currency":        crcy_cd,
                            "market":          "US",
                            "exchange":        excg_cd,
                        })
                    return items, None
                # rate-limit → 재시도
                if d.get("msg_cd") == "EGW00201" and attempt == 0:
                    time.sleep(1.5)
                    continue
                msg = d.get("msg1") or d.get("msg") or str(d)
                return [], f"{excg_cd}: {msg}"
            except requests.HTTPError as exc:
                body = exc.response.text if exc.response is not None else ""
                # rate-limit HTTP 응답 → 재시도
                if "EGW00201" in body and attempt == 0:
                    time.sleep(1.5)
                    continue
                return [], f"{excg_cd} HTTP오류: {body[:120]}"
            except Exception as exc:
                return [], f"{excg_cd}: {exc}"
        return [], f"{excg_cd}: 재시도 후에도 실패"

    for idx, (excg_cd, crcy_cd) in enumerate(_KIS_US_EXCHANGES):
        if idx > 0:
            time.sleep(1.1)   # KIS 초당 거래건수 초과 방지
        items, err = _fetch_ovrs_exchange(excg_cd, crcy_cd)
        ovrs_holdings.extend(items)
        if err:
            ovrs_errors.append(err)

    # KIS 해외잔고 API가 NASD 조회에 미국 전체를 반환해 NYSE 상장 종목이
    # 이중 집계됨 (2026-07-22 실측) — 티커 기준 첫 행 유지
    seen: dict = {}
    for h in ovrs_holdings:
        first = seen.get(h["ticker"])
        if first is None:
            seen[h["ticker"]] = h
        elif h["qty"] != first["qty"] or h["avg_price"] != first["avg_price"]:
            print(f"⚠️ get_kis_balance: {h['ticker']} 중복 행의 수량/평단 불일치 — 첫 행 유지")
    ovrs_holdings = list(seen.values())

    # ── 3. 해외 합산 및 환율 환산 ──────────────────────────
    ovrs_eval_usd     = round(sum(h["eval_amount"]     for h in ovrs_holdings), 2)
    ovrs_purchase_usd = round(sum(h["purchase_amount"] for h in ovrs_holdings), 2)
    ovrs_pnl_usd      = round(sum(h["profit_loss"]     for h in ovrs_holdings), 2)

    usd_krw              = _fetch_usd_krw_rate()
    eval_usd_krw         = round(ovrs_eval_usd * usd_krw)   if usd_krw else None
    total_assets_krw_all = (
        domestic.get("total_assets_krw", 0) + eval_usd_krw
        if usd_krw and "error" not in domestic else None
    )

    return {
        "account_no": f"{cano}-{acnt_prdt_cd}",
        "as_of":      datetime.now().isoformat(timespec="seconds"),
        "holdings":   dom_holdings + ovrs_holdings,
        "domestic":   domestic,
        "overseas": {
            "eval_total_usd":     ovrs_eval_usd,
            "purchase_total_usd": ovrs_purchase_usd,
            "profit_loss_usd":    ovrs_pnl_usd,
            "errors":             ovrs_errors,
        },
        "fx": {
            "usd_krw":              usd_krw,
            "eval_total_usd_krw":   eval_usd_krw,
            "total_assets_krw_all": total_assets_krw_all,
        },
    }


# ═══════════════════════════════════════════════
# 0-B. place_kis_order
# ═══════════════════════════════════════════════

def place_kis_order(
    ticker: str,
    side: str,
    qty: int,
    order_type: str,
    price: int = 0,
    dry_run: bool = True,
) -> dict:
    """
    KIS 모의투자 국내주식 주문을 낸다.

    Parameters
    ----------
    ticker     : str   한국 6자리 종목코드 (예: "005930"). 국내 주문만 지원.
    side       : str   "BUY" | "SELL"
    qty        : int   주문 수량 (1 이상 정수)
    order_type : str   "MARKET"(시장가) | "LIMIT"(지정가)
    price      : int   지정가 주문 시 주문단가 (원). 시장가면 0 또는 생략.
    dry_run    : bool  True(기본) → 실제 주문 없이 주문서만 반환.
                       False      → KIS API 실제 호출 (평일 장중에만 체결 가능).

    안전장치 (하드코딩, 변경 불가)
    --------------------------------
    1. 허용 계좌: _KIS_MOCK_ACCOUNT(50193730-01)만 허용.
       환경변수 계좌가 다르면 즉시 거부.
    2. 주문 금액 상한: 매수(BUY) 1회 ≤ 100만 원. 매도(SELL)는 상한 제외
       (결정 2 — 매도는 리스크 축소 행위). 금액 계산·기록은 매도도 수행.
       - 지정가: price × qty
       - 시장가: get_quote() 현재가 × qty (근사)
       매수 상한 초과 시 즉시 거부.
       재검토 ①: 매도+시장가에서 현재가 조회 실패 시 거부하지 않고 금액 미상
       (estimated_amount_krw=None, safety.quote_failed=True)으로 진행한다.

    Returns
    -------
    dict
        dry_run=True  : {"dry_run": True,  "order": {...}, "api_body": {...}, "safety": {...}, "message": str}
        dry_run=False : {"dry_run": False, "order": {...}, "result": {...}}
        오류 시       : {"error": "..."}
    """
    import os
    import requests
    from dotenv import load_dotenv

    load_dotenv()

    # ── 입력 검증 ──────────────────────────────────────────────
    ticker = ticker.strip()
    if not _is_korean(ticker):
        return {"error": f"place_kis_order는 국내 종목(6자리 코드)만 지원합니다. 입력: '{ticker}'"}

    side = side.upper().strip()
    if side not in ("BUY", "SELL"):
        return {"error": f"side는 'BUY' 또는 'SELL' 이어야 합니다. 입력: '{side}'"}

    order_type = order_type.upper().strip()
    if order_type not in ("MARKET", "LIMIT"):
        return {"error": f"order_type은 'MARKET' 또는 'LIMIT' 이어야 합니다. 입력: '{order_type}'"}

    if not isinstance(qty, int) or qty < 1:
        return {"error": f"qty는 1 이상 정수여야 합니다. 입력: {qty}"}

    if order_type == "LIMIT" and (not isinstance(price, int) or price <= 0):
        return {"error": f"LIMIT 주문에는 price(양수 정수)가 필요합니다. 입력: {price}"}

    # ── 환경변수 로드 ───────────────────────────────────────────
    app_key    = os.getenv("KIS_APP_KEY",    "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    account_no = os.getenv("KIS_ACCOUNT_NO", "").strip().replace("-", "")

    if not account_no:
        return {"error": "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다."}
    if len(account_no) != 10:
        return {"error": f"KIS_ACCOUNT_NO 형식 오류 (10자리 필요): '{account_no}'"}

    cano         = account_no[:8]
    acnt_prdt_cd = account_no[8:]
    account_fmt  = f"{cano}-{acnt_prdt_cd}"

    # ── 안전장치 1: 계좌 확인 ──────────────────────────────────
    allowed_no = _KIS_MOCK_ACCOUNT.replace("-", "")
    if account_no != allowed_no:
        return {
            "error": (
                f"계좌 불일치 — 허용 모의계좌: {_KIS_MOCK_ACCOUNT}, "
                f"현재 설정: {account_fmt}. "
                "실전 계좌 또는 다른 모의 계좌에는 주문할 수 없습니다."
            )
        }

    # ── 안전장치 2: 주문 금액 상한 ─────────────────────────────
    quote_failed = False
    if order_type == "LIMIT":
        estimated_amount = price * qty
        price_source     = f"지정가({price:,}원)"
    else:
        quote = get_quote(ticker)
        current_price = None if "error" in quote else quote.get("close")
        if current_price is None:
            # 재검토 ①: 매도는 상한 검사가 없어 금액 미상으로 진행 가능.
            # 매수는 100만 원 상한 검사에 금액이 필수이므로 기존대로 거부.
            if side == "SELL":
                quote_failed     = True
                estimated_amount = None
                price_source     = "현재가 조회 실패 — 금액 미상으로 진행 (재검토 ①)"
            elif "error" in quote:
                return {"error": f"시장가 금액 확인을 위한 현재가 조회 실패: {quote['error']}"}
            else:
                return {"error": "현재가 조회 결과에 종가(close)가 없습니다."}
        else:
            estimated_amount = int(current_price) * qty
            price_source     = f"현재가 근사({int(current_price):,}원)"

    if side == "BUY" and estimated_amount > _KIS_ORDER_LIMIT_KRW:
        return {
            "error": (
                f"주문 금액 상한 초과 — 상한: {_KIS_ORDER_LIMIT_KRW:,}원 (매수 전용 상한 — 결정 2), "
                f"예상 주문금액: {estimated_amount:,}원 "
                f"({price_source} × {qty}주). "
                "수량을 줄이거나 낮은 가격 종목을 선택해 주세요."
            )
        }

    # ── 주문 파라미터 구성 ─────────────────────────────────────
    # KIS API 주문구분 코드: 00=지정가, 01=시장가
    ord_dvsn = "01" if order_type == "MARKET" else "00"
    tr_id    = _KIS_MOCK_TR_BUY if side == "BUY" else _KIS_MOCK_TR_SELL

    api_body = {
        "CANO":         cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "PDNO":         ticker,
        "ORD_DVSN":     ord_dvsn,
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     str(price) if order_type == "LIMIT" else "0",
    }

    order_summary = {
        "account":               account_fmt,
        "ticker":                ticker,
        "side":                  side,
        "order_type":            order_type,
        "qty":                   qty,
        "price":                 price if order_type == "LIMIT" else None,
        "tr_id":                 tr_id,
        "estimated_amount_krw":  estimated_amount,
        "price_source":          price_source,
    }

    # amount_ok는 "금액 상한 검사를 통과했는가"의 의미.
    # 매도(SELL)는 상한 면제(결정 2)이므로 금액과 무관하게 항상 True 처리됨.
    safety_info = {
        "account_ok":    True,
        "amount_ok":     True,
        "limit_krw":     _KIS_ORDER_LIMIT_KRW,
        "estimated_krw": estimated_amount,
        "quote_failed":  quote_failed,   # 재검토 ① — True면 매도 금액 미상 진행
        "mock_account":  _KIS_MOCK_ACCOUNT,
    }

    # ── dry_run 모드: 주문서만 반환 ───────────────────────────
    if dry_run:
        return {
            "dry_run":  True,
            "order":    order_summary,
            "api_body": api_body,
            "safety":   safety_info,
            "message":  (
                "[DRY RUN] 실제 주문이 전송되지 않았습니다. "
                "order/api_body 내용을 확인한 뒤 dry_run=False로 재호출하면 실제 주문이 전송됩니다."
            ),
        }

    # ── 실제 주문 API 호출 (dry_run=False) ────────────────────
    tok = get_kis_token()
    if "error" in tok:
        return tok  # error message already has maintenance hint

    headers = {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {tok['access_token']}",
        "appkey":        app_key,
        "appsecret":     app_secret,
        "tr_id":         tr_id,
        "custtype":      "P",
    }

    url = f"{_KIS_DOMAIN}/uapi/domestic-stock/v1/trading/order-cash"

    try:
        resp = requests.post(url, json=api_body, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        body_text = exc.response.text if exc.response is not None else ""
        hint = _kis_maintenance_hint()
        return {"error": f"{hint}\n\nKIS 주문 HTTP 오류: {exc} — {body_text}"}
    except Exception as exc:
        hint = _kis_maintenance_hint()
        return {"error": f"{hint}\n\nKIS 주문 요청 실패: {exc}"}

    if data.get("rt_cd") != "0":
        msg = data.get("msg1") or data.get("msg") or str(data)
        return {"error": f"KIS 주문 API 오류: {msg}", "raw": data}

    output = data.get("output", {})
    return {
        "dry_run": False,
        "order":   order_summary,
        "result": {
            "order_no":   output.get("ODNO", ""),
            "order_time": output.get("ORD_TMD", ""),
            "rt_cd":      data.get("rt_cd"),
            "msg":        data.get("msg1", ""),
        },
    }


# ═══════════════════════════════════════════════
# 0-B-1-b. get_kis_fill_price (안건 2 — 체결가 조회, 읽기 전용)
# ═══════════════════════════════════════════════

def get_kis_fill_price(
    order_no: str,
    ticker: str,
    expected_qty: int,
    order_date: str | None = None,
) -> dict:
    """
    주문번호(ODNO)로 체결평균가를 조회한다 (읽기 전용 — 주문·장부 무접촉).

    KIS 국내주식 주식일별주문체결조회.
    tr_id는 모의투자 전용 VTTC0081R만 사용한다 (실전 분기 없음 — 구조적 안전장치).
    안건 2: trade_log 기록가를 주문시점 현재가 → 실제 체결평균가로 바꾸는 부품.

    동작
    ----
    - 호출 즉시 2.5초 대기(체결·조회 DB 반영 시간) 후 조회.
      실패 시 3.0초 추가 대기 후 1회만 재시도 (모의계좌 호출 제한 고려).
    - 전량 체결(총체결수량 == expected_qty)일 때만 success=True.
      부분체결·미체결·취소·미발견·종목 불일치는 전부 실패로 돌려서
      호출부의 fail-open(기존 주문시점가 기록)을 유도한다.
    - order_date(YYYYMMDD)를 주면 해당 일자를 조회한다(소급 보정용).
      기본값 None이면 오늘.

    Returns
    -------
    dict
      성공: {"success": True, "fill_price": float, "fill_qty": int, "fill_amount": int}
      실패: {"success": False, "reason": "..."}
    """
    import os
    import time
    import requests
    from dotenv import load_dotenv

    load_dotenv()

    order_no = str(order_no).strip()
    if not order_no:
        return {"success": False, "reason": "주문번호 없음"}

    app_key    = os.getenv("KIS_APP_KEY",    "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    account_no = os.getenv("KIS_ACCOUNT_NO", "").strip().replace("-", "")
    if len(account_no) != 10:
        return {"success": False, "reason": f"KIS_ACCOUNT_NO 형식 오류: '{account_no}'"}

    tok = get_kis_token()
    if "error" in tok:
        return {"success": False, "reason": f"토큰 실패: {tok['error']}"}

    inqr_dt = (order_date or datetime.now().strftime("%Y%m%d")).strip()
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {tok['access_token']}",
        "appkey":        app_key,
        "appsecret":     app_secret,
        "tr_id":         "VTTC0081R",   # 모의투자 전용(3개월 이내) — 실전 tr_id 사용 금지
        "custtype":      "P",
    }
    params = {
        "CANO": account_no[:8], "ACNT_PRDT_CD": account_no[8:],
        "INQR_STRT_DT": inqr_dt, "INQR_END_DT": inqr_dt,
        "SLL_BUY_DVSN_CD": "00", "PDNO": ticker.strip(),
        "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00",
        "ORD_GNO_BRNO": "", "ODNO": order_no, "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }

    def _query() -> dict:
        try:
            resp = requests.get(
                f"{_KIS_DOMAIN}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                headers=headers, params=params, timeout=10,
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as exc:
            return {"success": False, "reason": f"조회 API 에러: {exc}"}
        if d.get("rt_cd") != "0":
            return {"success": False, "reason": f"조회 API 오류: {d.get('msg1') or d}"}

        # 주문번호 앞자리 0 개수가 주문 응답과 조회 응답에서 다를 수 있어 정규화 후 비교
        target = order_no.lstrip("0")
        for row in d.get("output1", []):
            if str(row.get("odno", "")).lstrip("0") != target:
                continue
            if row.get("pdno", "").strip() != ticker.strip():
                return {"success": False,
                        "reason": f"종목 불일치: 조회={row.get('pdno')}, 기대={ticker}"}
            if str(row.get("cncl_yn", "")).upper() == "Y":
                return {"success": False, "reason": "취소된 주문"}
            try:
                fill_qty   = int(float(row.get("tot_ccld_qty") or 0))
                fill_price = round(float(row.get("avg_prvs") or 0), 2)
                fill_amt   = int(float(row.get("tot_ccld_amt") or 0))
            except Exception as exc:
                return {"success": False, "reason": f"응답 파싱 실패: {exc}"}
            if fill_qty == 0:
                return {"success": False, "reason": "미체결"}
            if fill_qty != int(expected_qty):
                return {"success": False,
                        "reason": f"부분체결(체결 {fill_qty}/주문 {expected_qty})"}
            if fill_price <= 0:
                return {"success": False, "reason": f"체결평균가 이상값: {fill_price}"}
            return {"success": True, "fill_price": fill_price,
                    "fill_qty": fill_qty, "fill_amount": fill_amt}
        return {"success": False, "reason": "주문번호 미발견"}

    time.sleep(2.5)
    result = _query()
    if not result["success"]:
        time.sleep(3.0)   # 재시도는 딱 1회 (모의계좌 REST 호출 제한)
        result = _query()
    return result


# ═══════════════════════════════════════════════
# 0-B-2. propose_and_confirm_order
# ═══════════════════════════════════════════════

def propose_and_confirm_order(
    ticker: str,
    side: str,
    qty: int,
    order_type: str,
    price: int = 0,
) -> dict:
    """
    AI가 제안한 주문을 사람이 승인한 뒤에만 실행하는 안전 래퍼.

    흐름
    ----
    1. place_kis_order(dry_run=True) → 주문서 생성 + 안전장치 통과 여부 확인
    2. 주문 내역을 터미널에 보기 좋게 출력
    3. input()으로 'y' 입력 시에만 place_kis_order(dry_run=False) 실행
       'y' 이외의 모든 입력 → 주문 취소

    승인 없이 자동 실행되는 경로는 없다.
    """
    # ── 1단계: dry_run으로 주문서 생성 + 안전장치 확인 ────────
    draft = place_kis_order(ticker, side, qty, order_type, price, dry_run=True)

    if "error" in draft:
        print(f"\n❌ 주문 사전 검증 실패: {draft['error']}")
        return draft

    order = draft["order"]
    safety = draft["safety"]

    # 종목명 조회 (실패해도 코드로 대체)
    try:
        q = get_quote(ticker)
        stock_name = q.get("name", ticker) if "error" not in q else ticker
    except Exception:
        stock_name = ticker

    side_kor      = "매수" if order["side"] == "BUY" else "매도"
    order_type_kor = "시장가" if order["order_type"] == "MARKET" else "지정가"
    price_display = (
        f"{order['price']:,}원" if order["price"] is not None else "시장가(현재가 근사)"
    )

    # ── 2단계: 사람이 읽기 쉬운 주문서 출력 ───────────────────
    print("\n" + "=" * 55)
    print("  📋 주문 제안서 — 사람 승인 필요")
    print("=" * 55)
    print(f"  종목명  : {stock_name} ({order['ticker']})")
    print(f"  구분    : {side_kor}")
    print(f"  주문유형: {order_type_kor}")
    print(f"  수량    : {order['qty']:,}주")
    print(f"  주문단가: {price_display}")
    print(f"  예상금액: {order['estimated_amount_krw']:,}원  ({order['price_source']})")
    print(f"  계좌    : {order['account']}  [모의투자]")
    print(f"  금액상한: {safety['limit_krw']:,}원  ✅ 통과")
    print("=" * 55)
    print("  ⚠️  이 주문은 모의투자 계좌로 전송됩니다.")
    print("=" * 55)

    # ── 3단계: 사람의 승인 ─────────────────────────────────────
    try:
        answer = input("\n이 주문을 실행할까요? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n주문 취소됨 (입력 중단)")
        return {"cancelled": True, "reason": "입력 중단", "order": order}

    if answer != "y":
        print(f"\n주문 취소됨 (입력값: '{answer}')")
        return {"cancelled": True, "reason": f"사용자가 '{answer}' 입력", "order": order}

    # ── 4단계: 실제 주문 실행 ──────────────────────────────────
    print("\n주문 전송 중...")
    result = place_kis_order(ticker, side, qty, order_type, price, dry_run=False)

    if "error" in result:
        print(f"\n❌ 주문 실패: {result['error']}")
        return result

    r = result.get("result", {})
    print("\n" + "=" * 55)
    print("  ✅ 주문 완료")
    print("=" * 55)
    print(f"  주문번호: {r.get('order_no', '—')}")
    print(f"  주문시각: {r.get('order_time', '—')}")
    print(f"  메시지  : {r.get('msg', '—')}")
    print("=" * 55)

    return result


# ═══════════════════════════════════════════════
# 0-C. get_benchmark_comparison
# ═══════════════════════════════════════════════

def get_benchmark_comparison(period_days: int = 30) -> dict:
    """
    내 포트폴리오 수익률을 지수(KOSPI, S&P500)와 비교한다.

    - 국내 포트폴리오: 매입가 대비 현재 평가금액 수익률(KRW 기준) vs KOSPI
    - 해외 포트폴리오: 매입가 대비 현재 평가금액 수익률(USD 기준) vs S&P500
      → 환율 영향은 제외하고 달러 기준 순수 주식 성적만 비교
    - 지수 비교 구간: 최근 period_days일(기본 30일) — 실제 보유 기간과 다를 수 있으며
      이는 근사치임을 결과에 명시

    ⚠️ 정밀 벤치마크가 아님. 종목별 보유 기간이 다르기 때문에 지수 구간을 최근
    고정 기간으로 근사한 "방향성 참고" 비교다.

    Parameters
    ----------
    period_days : int
        지수 수익률 비교 구간 (달력 기준 일수, 기본 30일).

    Returns
    -------
    dict
        {
            "as_of"       : str,          # 조회 시각 (ISO)
            "period_days" : int,
            "period_note" : str,          # 근사 방식 설명 문구
            "domestic"    : {             # 국내 포트폴리오 (보유 종목 없으면 None)
                "portfolio_return_pct" : float | None,
                "benchmark"            : "KOSPI",
                "benchmark_return_pct" : float | None,
                "excess_return_pct"    : float | None,
                "assessment"           : str,
                "holdings_count"       : int,
                "purchase_total_krw"   : int,
                "eval_total_krw"       : int,
                "currency"             : "KRW",
            } | None,
            "overseas"    : {             # 해외 포트폴리오 (보유 종목 없으면 None)
                "portfolio_return_pct" : float | None,
                "benchmark"            : "S&P500",
                "benchmark_return_pct" : float | None,
                "excess_return_pct"    : float | None,
                "assessment"           : str,
                "holdings_count"       : int,
                "purchase_total_usd"   : float,
                "eval_total_usd"       : float,
                "currency"             : "USD",
                "note_fx"              : str,
            } | None,
            "disclaimer"  : str,
        }
        실패 시: {"error": "..."}
    """
    # ── 1. KIS 잔고 조회 ──────────────────────────────────
    balance = get_kis_balance()
    if "error" in balance:
        return {"error": f"잔고 조회 실패: {balance['error']}"}

    holdings = balance.get("holdings", [])
    kr_holdings = [h for h in holdings if h.get("market") == "KR"]
    us_holdings = [h for h in holdings if h.get("market") == "US"]

    # ── 2. 지수 수익률 (최근 period_days일) ──────────────
    def _index_return_pct(symbol: str, days: int) -> float | None:
        """지수의 최근 days일(달력 기준) 수익률(%)을 반환. 실패 시 None."""
        end   = datetime.today()
        start = end - timedelta(days=days + 10)   # 주말/공휴일 여유
        try:
            df = fdr.DataReader(symbol,
                                start=start.strftime("%Y-%m-%d"),
                                end=end.strftime("%Y-%m-%d"))
            if df is None or df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            col    = "close" if "close" in df.columns else df.columns[0]
            closes = df[col].dropna()
            if len(closes) < 2:
                return None
            p_start = float(closes.iloc[0])
            p_end   = float(closes.iloc[-1])
            if p_start == 0:
                return None
            return round((p_end - p_start) / p_start * 100, 2)
        except Exception:
            return None

    kospi_ret = _index_return_pct("KS11",  period_days)
    sp500_ret = _index_return_pct("US500", period_days)

    # ── 3. 국내 포트폴리오 수익률 ─────────────────────────
    domestic_result = None
    if kr_holdings:
        purchase_kr = sum(h.get("purchase_amount", 0) for h in kr_holdings)
        eval_kr     = sum(h.get("eval_amount",     0) for h in kr_holdings)
        port_ret_kr = (
            round((eval_kr - purchase_kr) / purchase_kr * 100, 2)
            if purchase_kr > 0 else None
        )
        excess_kr = (
            round(port_ret_kr - kospi_ret, 2)
            if (port_ret_kr is not None and kospi_ret is not None) else None
        )
        if excess_kr is not None:
            direction  = "초과" if excess_kr >= 0 else "미달"
            assessment = f"KOSPI 대비 {direction} {abs(excess_kr):.2f}%p"
        else:
            assessment = "지수 데이터 없음 — 비교 불가"

        domestic_result = {
            "portfolio_return_pct": port_ret_kr,
            "benchmark":            "KOSPI",
            "benchmark_return_pct": kospi_ret,
            "excess_return_pct":    excess_kr,
            "assessment":           assessment,
            "holdings_count":       len(kr_holdings),
            "purchase_total_krw":   round(purchase_kr),
            "eval_total_krw":       round(eval_kr),
            "currency":             "KRW",
        }

    # ── 4. 해외 포트폴리오 수익률 (USD 기준) ───────────────
    overseas_result = None
    if us_holdings:
        purchase_us = sum(h.get("purchase_amount", 0) for h in us_holdings)
        eval_us     = sum(h.get("eval_amount",     0) for h in us_holdings)
        port_ret_us = (
            round((eval_us - purchase_us) / purchase_us * 100, 2)
            if purchase_us > 0 else None
        )
        excess_us = (
            round(port_ret_us - sp500_ret, 2)
            if (port_ret_us is not None and sp500_ret is not None) else None
        )
        if excess_us is not None:
            direction  = "초과" if excess_us >= 0 else "미달"
            assessment = f"S&P500 대비 {direction} {abs(excess_us):.2f}%p"
        else:
            assessment = "지수 데이터 없음 — 비교 불가"

        overseas_result = {
            "portfolio_return_pct": port_ret_us,
            "benchmark":            "S&P500",
            "benchmark_return_pct": sp500_ret,
            "excess_return_pct":    excess_us,
            "assessment":           assessment,
            "holdings_count":       len(us_holdings),
            "purchase_total_usd":   round(purchase_us, 2),
            "eval_total_usd":       round(eval_us,     2),
            "currency":             "USD",
            "note_fx":              "달러 기준 순수 주식 성적 — 환율(USD/KRW) 효과 제외",
        }

    if domestic_result is None and overseas_result is None:
        return {"error": "보유 종목이 없어 벤치마크 비교를 수행할 수 없습니다."}

    return {
        "as_of":       datetime.now().isoformat(timespec="seconds"),
        "period_days": period_days,
        "period_note": (
            f"⚠️ 지수 비교 구간은 최근 {period_days}일(달력 기준)로 근사했습니다. "
            "보유 기간이 종목마다 다르므로, 이 비교는 '대략 지수보다 나았나'의 "
            "방향성을 참고하는 용도이며 정밀 벤치마크가 아닙니다."
        ),
        "domestic":    domestic_result,
        "overseas":    overseas_result,
        "disclaimer":  "이 정보는 참고용이며 투자 권유가 아닙니다. 미래 수익률을 보장하지 않습니다.",
    }


# ═══════════════════════════════════════════════
# 1. get_quote
# ═══════════════════════════════════════════════

def get_quote(ticker: str) -> dict:
    """
    현재가(최근 종가)·전일대비 등락률·날짜를 반환한다.

    Parameters
    ----------
    ticker : str
        한국 6자리 코드(예: "005930") 또는 미국 티커(예: "AAPL").

    Returns
    -------
    dict  {ticker, name, date, close, open, high, low, volume,
           prev_close, change, change_pct, market, alert}
          alert=True : 등락률 절댓값 ≥ 5% (급변동 감지)
          실패 시: {"error": "..."}
    """
    ticker = ticker.strip()
    market = "KR" if _is_korean(ticker) else "US"

    df = _fetch_ohlcv(ticker, days=10)
    if df is None:
        return {"error": f"데이터 없음: {ticker}"}
    if "close" not in df.columns:
        return {"error": f"'close' 컬럼 없음. 실제 컬럼: {list(df.columns)}"}

    # close=NaN 행 제거 (US: 주말/공휴일 부분 데이터 포함될 수 있음)
    df = df[df["close"].notna()]
    if df.empty:
        return {"error": f"유효한 종가 없음: {ticker}"}

    latest    = df.iloc[-1]
    date_str  = df.index[-1].strftime("%Y-%m-%d")
    close     = _safe_float(latest.get("close"))
    open_     = _safe_float(latest.get("open"))
    high      = _safe_float(latest.get("high"))
    low       = _safe_float(latest.get("low"))
    volume    = int(latest.get("volume", 0)) if latest.get("volume") is not None else None
    prev_close = _safe_float(df.iloc[-2]["close"]) if len(df) >= 2 else None

    if close is not None and prev_close is not None and prev_close != 0:
        change     = round(close - prev_close, 4)
        change_pct = round((close - prev_close) / prev_close * 100, 2)
    else:
        change, change_pct = None, None

    # 종목명 (한국만)
    name = None
    if market == "KR":
        try:
            listing  = fdr.StockListing("KRX")
            listing.columns = [c.lower() for c in listing.columns]
            code_col = next((c for c in listing.columns if "code" in c or "symbol" in c), None)
            # "name" 정확 일치 → "name"으로 끝나는 컬럼 → fallback
            # ("name" in col) 방식은 "unnamed: 0" 같은 컬럼을 먼저 잡는 오류 발생
            if "name" in listing.columns:
                name_col = "name"
            else:
                name_col = next((c for c in listing.columns if c.endswith("name")), None)
            if code_col and name_col:
                row = listing[listing[code_col] == ticker]
                if not row.empty:
                    name = row.iloc[0][name_col]
        except Exception:
            pass

    alert = (abs(change_pct) >= 5.0) if change_pct is not None else False

    return {
        "ticker":     ticker,
        "name":       name,
        "date":       date_str,
        "close":      close,
        "open":       open_,
        "high":       high,
        "low":        low,
        "volume":     volume,
        "prev_close": prev_close,
        "change":     change,
        "change_pct": change_pct,
        "market":     market,
        "alert":      alert,
    }


# ═══════════════════════════════════════════════
# 2. get_indicators
# ═══════════════════════════════════════════════

def get_indicators(ticker: str) -> dict:
    """
    기술적 지표(MA20, MA60, RSI, MACD, 거래량)를 반환한다.

    Parameters
    ----------
    ticker : str
        한국 6자리 코드 또는 미국 티커.

    Returns
    -------
    dict  {ticker, date, close, ma20, ma60, rsi,
           macd, macd_signal, macd_hist,
           volume, volume_ma20, volume_ratio}
          volume_ratio = 최근 거래량 / 20일 평균 거래량 (예: 2.5 → 평소의 2.5배)
          실패 시: {"error": "..."}
    """
    import ta

    ticker = ticker.strip()
    df = _fetch_ohlcv(ticker, days=200)
    if df is None or "close" not in df.columns:
        return {"error": f"지표 계산용 데이터 없음: {ticker}"}
    if len(df) < 60:
        return {"error": f"데이터 부족 ({len(df)}행). MA60 계산에 60행 이상 필요."}

    close_s = df["close"]

    # ── 이동평균
    ma20 = _safe_float(close_s.rolling(20).mean().iloc[-1])
    ma60 = _safe_float(close_s.rolling(60).mean().iloc[-1])

    # ── RSI (14일)
    try:
        rsi = _safe_float(ta.momentum.RSIIndicator(close=close_s, window=14).rsi().iloc[-1])
    except Exception:
        rsi = None

    # ── MACD (12, 26, 9)
    macd_val = macd_signal = macd_hist = None
    try:
        macd_obj   = ta.trend.MACD(close=close_s)
        macd_val   = _safe_float(macd_obj.macd().iloc[-1])
        macd_signal = _safe_float(macd_obj.macd_signal().iloc[-1])
        macd_hist  = _safe_float(macd_obj.macd_diff().iloc[-1])
    except Exception:
        pass

    # ── 거래량 (매매정지 0값 행은 _filter_halted_rows에서 이미 제외됨)
    volume = volume_ma20 = volume_ratio = None
    if "volume" in df.columns:
        volume_s = df["volume"]
        volume = _safe_float(volume_s.iloc[-1])
        volume_ma20 = _safe_float(volume_s.rolling(20).mean().iloc[-1])
        if volume is not None and volume_ma20 not in (None, 0):
            volume_ratio = round(volume / volume_ma20, 2)

    return {
        "ticker":       ticker,
        "date":         df.index[-1].strftime("%Y-%m-%d"),
        "close":        _safe_float(close_s.iloc[-1]),
        "ma20":         ma20,
        "ma60":         ma60,
        "rsi":          rsi,
        "macd":         macd_val,
        "macd_signal":  macd_signal,
        "macd_hist":    macd_hist,
        "volume":       volume,
        "volume_ma20":  volume_ma20,
        "volume_ratio": volume_ratio,
    }


# ═══════════════════════════════════════════════
# 3. get_fundamentals
# ═══════════════════════════════════════════════

def get_fundamentals(ticker: str) -> dict:
    """
    재무 지표(PER, PBR, 영업이익, 부채비율)를 반환한다.

    yfinance를 통해 한국(.KS)·미국 모두 처리.

    Parameters
    ----------
    ticker : str
        한국 6자리 코드 또는 미국 티커.

    Returns
    -------
    dict  {ticker, market, per, pbr, eps, revenue,
           operating_income, net_income, debt_ratio,
           market_cap, currency, next_earnings_date}
          next_earnings_date : 미국은 "YYYY-MM-DD"(추정치면 "(추정치)" 접미),
                                한국은 "확인 불가 (DART는 사전 예정일 미제공)" 고정.
          실패 시: {"error": "..."}
    """
    import yfinance as yf

    ticker = ticker.strip()
    market = "KR" if _is_korean(ticker) else "US"
    yf_sym = _yf_ticker(ticker)

    try:
        yf_ticker_obj = yf.Ticker(yf_sym)
        info = yf_ticker_obj.info
    except Exception as exc:
        return {"error": f"yfinance 조회 실패 ({yf_sym}): {exc}"}

    if not info or info.get("regularMarketPrice") is None:
        # .KS 실패 시 .KQ(코스닥) 재시도
        if market == "KR":
            try:
                yf_sym = ticker + ".KQ"
                yf_ticker_obj = yf.Ticker(yf_sym)
                info   = yf_ticker_obj.info
            except Exception:
                pass
        if not info:
            return {"error": f"재무 데이터 없음: {ticker}"}

    def _fmt_num(val, scale=1):
        """큰 숫자를 억 단위로 변환 (한국), USD는 그대로."""
        if val is None:
            return None
        try:
            v = float(val)
            return None if pd.isna(v) else round(v / scale, 2)
        except Exception:
            return None

    per            = _safe_float(info.get("trailingPE"))
    pbr            = _safe_float(info.get("priceToBook"))
    eps            = _safe_float(info.get("trailingEps"))
    revenue        = info.get("totalRevenue")           # 매출액
    
    op_income      = info.get("operatingIncome")        # 영업이익
    if op_income is None and revenue and info.get("operatingMargins"):
        op_income = revenue * info.get("operatingMargins")

    net_income     = info.get("netIncomeToCommon")      # 당기순이익
    total_debt     = info.get("totalDebt")              # 총부채
    equity         = info.get("totalStockholdersEquity") # 자기자본
    market_cap     = info.get("marketCap")
    currency       = info.get("currency", "USD")

    # 부채비율 = 총부채 / 자기자본 * 100
    debt_ratio = info.get("debtToEquity")
    if debt_ratio is None and total_debt is not None and equity and equity != 0:
        try:
            debt_ratio = round(float(total_debt) / float(equity) * 100, 2)
        except Exception:
            pass

    # None 값을 "확인 불가"로 변환하여 에이전트의 환각 방지
    def _val(x):
        return x if x is not None else "확인 불가"

    # 다음 실적 발표 예정일 — 한국은 DART가 사전 예정일을 제공하지 않으므로 고정 문구,
    # 미국은 yfinance에서 확인. 못 가져오면 지어내지 않고 "확인 불가".
    if market == "KR":
        next_earnings_date = "확인 불가 (DART는 사전 예정일 미제공)"
    else:
        try:
            next_earnings_date = _next_us_earnings_date(yf_ticker_obj, info)
        except Exception:
            next_earnings_date = "확인 불가"

    return {
        "ticker":             ticker,
        "market":             market,
        "currency":           currency,
        "per":                _val(per),
        "pbr":                _val(pbr),
        "eps":                _val(eps),
        "revenue":            _val(revenue),
        "operating_income":   _val(op_income),
        "net_income":         _val(net_income),
        "debt_ratio":         _val(debt_ratio),      # %
        "market_cap":         _val(market_cap),
        "next_earnings_date": next_earnings_date,
    }


# ═══════════════════════════════════════════════
# 4. get_macro
# ═══════════════════════════════════════════════

def get_macro() -> dict:
    """
    주요 거시 지표를 반환한다.
    - 지수: KOSPI, KOSDAQ, S&P500, NASDAQ
    - 환율: USD/KRW
    - 금리: 미국 10년물 국채(yfinance ^TNX)
    - 원자재: WTI 원유(CL=F), 금(GC=F)

    Returns
    -------
    dict  {as_of, indices, fx, rates, commodities}
          실패한 항목은 None으로 채움.
    """
    import yfinance as yf

    def _last_close(sym: str, source: str = "fdr", days: int = 5):
        end = datetime.today()
        start = end - timedelta(days=days + 5)
        try:
            if source == "fdr":
                df = fdr.DataReader(sym, start=start.strftime("%Y-%m-%d"),
                                    end=end.strftime("%Y-%m-%d"))
                if df is None or df.empty:
                    return "확인 불가"
                df.columns = [c.lower() for c in df.columns]
                col = "close" if "close" in df.columns else df.columns[0]
                val = _safe_float(df[col].dropna().iloc[-1])
                return val if val is not None else "확인 불가"
            else:  # yfinance
                tk = yf.Ticker(sym)
                hist = tk.history(period="5d")
                if hist.empty:
                    return "확인 불가"
                val = _safe_float(hist["Close"].dropna().iloc[-1])
                return val if val is not None else "확인 불가"
        except Exception:
            return "확인 불가"

    as_of = datetime.today().strftime("%Y-%m-%d")

    indices = {
        "KOSPI":   _last_close("KS11",  "fdr"),
        "KOSDAQ":  _last_close("KQ11",  "fdr"),
        "SP500":   _last_close("US500", "fdr"),
        "NASDAQ":  _last_close("IXIC",  "fdr"),
    }

    fx = {
        "USD_KRW": _last_close("USD/KRW", "fdr"),
    }

    # 미국 10년물: yfinance ^TNX (값 자체가 % 단위)
    us10y = _last_close("^TNX", "yf")
    rates = {
        "US_10Y_pct": us10y,
    }

    commodities = {
        "WTI_USD":  _last_close("CL=F", "yf"),
        "Gold_USD": _last_close("GC=F", "yf"),
    }

    return {
        "as_of":       as_of,
        "indices":     indices,
        "fx":          fx,
        "rates":       rates,
        "commodities": commodities,
    }


# ═══════════════════════════════════════════════
# 5. search_news  (DART 공시 + 구글 뉴스 RSS / yfinance)
# ═══════════════════════════════════════════════

# ── DART 고유번호 캐시 헬퍼 ──────────────────────────────

_DART_CORP_CACHE_FILE = ".dart_corp_codes.json"
_DART_CORP_CACHE_DAYS = 7  # 캐시 유효 기간(일)


def _load_dart_corp_codes(api_key: str) -> dict:
    """
    종목 6자리 코드 → DART corp_code 매핑을 반환한다.
    로컬 캐시(.dart_corp_codes.json)가 7일 이내면 재사용하고,
    만료되거나 없으면 OpenDART corpCode.xml을 새로 받아 갱신한다.
    """
    import io, json, os, zipfile
    import xml.etree.ElementTree as ET
    import requests

    # ── 캐시 유효 확인
    if os.path.exists(_DART_CORP_CACHE_FILE):
        mtime = datetime.fromtimestamp(os.path.getmtime(_DART_CORP_CACHE_FILE))
        if (datetime.now() - mtime).days < _DART_CORP_CACHE_DAYS:
            try:
                with open(_DART_CORP_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass  # 캐시 손상 → 재다운로드

    # ── DART에서 corpCode.xml(zip) 다운로드
    url  = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={api_key}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xml_data = z.read("CORPCODE.xml")

    root    = ET.fromstring(xml_data)
    mapping = {}
    for corp in root.findall("list"):
        stock_code = corp.findtext("stock_code", "").strip()
        corp_code  = corp.findtext("corp_code",  "").strip()
        if stock_code and len(stock_code) == 6 and corp_code:
            mapping[stock_code] = corp_code

    # ── 캐시 저장 (실패해도 치명적이지 않음)
    try:
        with open(_DART_CORP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
    except Exception:
        pass

    return mapping


def search_news(ticker: str, max_items: int = 5) -> dict:
    """
    종목 관련 뉴스를 반환한다.

    - 미국: yfinance .news  (기존 그대로)
    - 한국: DART 공시(list.json) + 구글 뉴스 RSS → 최신순 합산

    인터페이스는 고정. 소스는 나중에 교체 가능.

    Parameters
    ----------
    ticker    : str  한국 6자리 코드 또는 미국 티커.
    max_items : int  최대 기사 수 (기본 5).

    Returns
    -------
    dict  {ticker, market, count, news: [{title, date, source, url}]}
          실패 시: {"error": "..."}
    """
    import yfinance as yf

    ticker = ticker.strip()
    market = "KR" if _is_korean(ticker) else "US"
    news_list = []

    # ══════════════════════════════════════════════════════════
    # 미국: yfinance  (기존 그대로)
    # ══════════════════════════════════════════════════════════
    if market == "US":
        try:
            raw = yf.Ticker(ticker).news or []
            for item in raw[:max_items]:
                ct     = item.get("content", {})
                title  = ct.get("title") or item.get("title", "")
                url    = (ct.get("canonicalUrl", {}) or {}).get("url") or item.get("link", "")
                source = (ct.get("provider",    {}) or {}).get("displayName") or item.get("publisher", "")
                pub_ts = ct.get("pubDate") or item.get("providerPublishTime")
                if pub_ts:
                    try:
                        date_str = (
                            datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d")
                            if isinstance(pub_ts, (int, float))
                            else str(pub_ts)[:10]
                        )
                    except Exception:
                        date_str = str(pub_ts)
                else:
                    date_str = None

                if title:
                    news_list.append({
                        "title":  title,
                        "date":   date_str,
                        "source": source or "Yahoo Finance",
                        "url":    url,
                    })
        except Exception as exc:
            return {"error": f"yfinance 뉴스 조회 실패 ({ticker}): {exc}"}

    # ══════════════════════════════════════════════════════════
    # 한국: DART 공시 + 구글 뉴스 RSS
    # ══════════════════════════════════════════════════════════
    else:
        import os
        import requests
        import feedparser
        from urllib.parse import quote_plus
        from dotenv import load_dotenv

        load_dotenv()
        dart_key = os.getenv("DART_API_KEY", "").strip()

        combined: list[dict] = []   # _dt(정렬용 YYYYMMDD 문자열) 포함

        # ── 1. DART 공시 ──────────────────────────────────────
        if dart_key:
            try:
                corp_map  = _load_dart_corp_codes(dart_key)
                corp_code = corp_map.get(ticker)
                if corp_code:
                    today  = datetime.today()
                    bgn_de = (today - timedelta(days=90)).strftime("%Y%m%d")
                    end_de = today.strftime("%Y%m%d")
                    api_url = (
                        f"https://opendart.fss.or.kr/api/list.json"
                        f"?crtfc_key={dart_key}&corp_code={corp_code}"
                        f"&bgn_de={bgn_de}&end_de={end_de}"
                        f"&sort=date&sort_mth=desc&page_count={max_items}"
                    )
                    resp = requests.get(api_url, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("status") == "000":   # 정상
                        for item in data.get("list", [])[:max_items]:
                            rcept_no = item.get("rcept_no", "")
                            rcept_dt = item.get("rcept_dt", "")     # "20260606"
                            date_str = (
                                f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}"
                                if len(rcept_dt) == 8 else None
                            )
                            combined.append({
                                "title":  item.get("report_nm", "").strip(),
                                "date":   date_str,
                                "source": "DART",
                                "url":    f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                                "_dt":    rcept_dt or "00000000",
                            })
            except Exception:
                pass    # DART 실패 → 구글 뉴스만 사용

        # ── 2. 구글 뉴스 RSS ──────────────────────────────────
        try:
            # 종목명 조회 (검색 정확도 향상)
            stock_name = ticker
            try:
                listing  = fdr.StockListing("KRX")
                listing.columns = [c.lower() for c in listing.columns]
                code_col = next((c for c in listing.columns if "code" in c or "symbol" in c), None)
                name_col = next((c for c in listing.columns if "name" in c), None)
                if code_col and name_col:
                    row = listing[listing[code_col] == ticker]
                    if not row.empty:
                        stock_name = row.iloc[0][name_col]
            except Exception:
                pass

            rss_url = (
                f"https://news.google.com/rss/search"
                f"?q={quote_plus(stock_name + ' 주식')}&hl=ko&gl=KR&ceid=KR:ko"
            )
            feed = feedparser.parse(rss_url)

            for entry in feed.entries[:max_items]:
                title = entry.get("title", "").strip()
                link  = entry.get("link",  "")
                pub   = entry.get("published_parsed")
                if pub:
                    from time import strftime as _strftime
                    date_str = _strftime("%Y-%m-%d", pub)
                    _dt_sort = _strftime("%Y%m%d",   pub)
                else:
                    date_str = None
                    _dt_sort = "00000000"

                if title:
                    combined.append({
                        "title":  title,
                        "date":   date_str,
                        "source": "Google News",
                        "url":    link,
                        "_dt":    _dt_sort,
                    })
        except Exception:
            pass    # RSS 실패 → DART 결과만 사용

        # ── 3. 최신순 정렬 후 _dt 제거 ──────────────────────
        combined.sort(key=lambda x: x.get("_dt", "00000000"), reverse=True)
        for item in combined:
            item.pop("_dt", None)
        news_list = combined[:max_items]

    return {
        "ticker": ticker,
        "market": market,
        "count":  len(news_list),
        "news":   news_list,
    }


# ═══════════════════════════════════════════════
# 6. get_portfolio_analysis
# ═══════════════════════════════════════════════

def get_portfolio_analysis(holdings: list) -> dict:
    """
    보유 종목의 섹터 분포·쏠림도를 분석한다.

    Parameters
    ----------
    holdings : list
        아래 두 형식 모두 지원.
        - 티커 리스트: ["005930", "AAPL", "MSFT"]
        - 비중 딕트 리스트: [{"ticker": "005930", "weight": 0.4}, ...]
          weight 합이 1 미만이면 균등 배분으로 보정.

    Returns
    -------
    dict
        {
            "total_tickers"   : int,
            "holdings"        : [{ticker, market, sector, weight}],
            "sector_breakdown": {섹터명: {"weight_pct": float, "tickers": []}},
            "concentration"   : {
                "top1_pct"  : float,   # 최대 섹터 비중 (%)
                "hhi"       : float,   # Herfindahl-Hirschman Index (쏠림도)
                "warning"   : str | None  # 50% 초과 시 경고 메시지
            }
        }
        실패 시: {"error": "..."}
    """
    import yfinance as yf

    if not holdings:
        return {"error": "holdings가 비어 있습니다."}

    # ── 입력 정규화 ──────────────────────────────────────
    items: list[dict] = []
    for h in holdings:
        if isinstance(h, str):
            items.append({"ticker": h.strip(), "weight": None})
        elif isinstance(h, dict) and "ticker" in h:
            items.append({"ticker": h["ticker"].strip(), "weight": h.get("weight")})
        else:
            return {"error": f"잘못된 holdings 항목: {h}"}

    # 비중 미제공 → 균등 배분
    has_weight = all(it["weight"] is not None for it in items)
    if not has_weight:
        eq = round(1.0 / len(items), 6)
        for it in items:
            it["weight"] = eq
    else:
        total_w = sum(it["weight"] for it in items)
        if total_w > 0:
            for it in items:
                it["weight"] = it["weight"] / total_w  # 합이 1이 되도록 정규화

    # ── 섹터 조회 ─────────────────────────────────────────
    # 한국 KRX 섹터 목록 (한 번만 로드)
    krx_df = None
    try:
        krx_df = fdr.StockListing("KRX")
        krx_df.columns = [c.lower() for c in krx_df.columns]
    except Exception:
        pass

    result_holdings = []
    sector_map: dict[str, float] = {}  # 섹터명 → 누적 비중

    for it in items:
        ticker = it["ticker"]
        weight = it["weight"]
        market = "KR" if _is_korean(ticker) else "US"
        sector = None

        # ── 섹터 조회: 한국
        if market == "KR" and krx_df is not None:
            try:
                code_col = next((c for c in krx_df.columns if "code" in c or "symbol" in c), None)
                sec_col  = next((c for c in krx_df.columns
                                 if "sector" in c or "업종" in c or "industry" in c), None)
                if code_col and sec_col:
                    row = krx_df[krx_df[code_col] == ticker]
                    if not row.empty:
                        sector = str(row.iloc[0][sec_col]).strip() or None
            except Exception:
                pass

        # ── 섹터 조회: yfinance (미국 + 한국 fallback)
        if sector is None:
            try:
                yf_sym = _yf_ticker(ticker)
                info   = yf.Ticker(yf_sym).info
                sector = info.get("sector") or info.get("industry")
            except Exception:
                pass

        sector = sector or "기타/미분류"

        result_holdings.append({
            "ticker": ticker,
            "market": market,
            "sector": sector,
            "weight": round(weight * 100, 2),  # % 로 표현
        })

        sector_map[sector] = sector_map.get(sector, 0) + weight

    # ── 섹터 breakdown ────────────────────────────────────
    sector_breakdown: dict[str, dict] = {}
    for sec, w in sorted(sector_map.items(), key=lambda x: -x[1]):
        pct = round(w * 100, 2)
        tickers_in_sec = [h["ticker"] for h in result_holdings if h["sector"] == sec]
        sector_breakdown[sec] = {
            "weight_pct": pct,
            "tickers":    tickers_in_sec,
        }

    # ── 쏠림 지수 (HHI) ──────────────────────────────────
    weights_pct = [v["weight_pct"] for v in sector_breakdown.values()]
    hhi = round(sum(w ** 2 for w in weights_pct), 2)  # 0~10000 범위
    top1_pct = max(weights_pct) if weights_pct else 0
    warning = (
        f"⚠️ '{max(sector_breakdown, key=lambda s: sector_breakdown[s]['weight_pct'])}' "
        f"섹터 비중이 {top1_pct:.1f}%로 과도하게 집중되어 있습니다."
    ) if top1_pct > 50 else None

    return {
        "total_tickers":    len(result_holdings),
        "holdings":         result_holdings,
        "sector_breakdown": sector_breakdown,
        "concentration": {
            "top1_pct": top1_pct,
            "hhi":      hhi,
            "warning":  warning,
        },
    }


# ═══════════════════════════════════════════════
# 7. get_sector_comparison
# ═══════════════════════════════════════════════

# 미국 산업(industry) → 동종 피어 종목 테이블
_US_INDUSTRY_PEERS: dict[str, list[str]] = {
    "Semiconductors":                     ["NVDA", "AMD", "INTC", "TSM", "QCOM", "AVGO", "MU"],
    "Semiconductor Equipment & Materials":["ASML", "AMAT", "KLAC", "LRCX", "TER"],
    "Software—Infrastructure":            ["MSFT", "ORCL", "IBM", "CSCO"],
    "Software—Application":               ["CRM", "ADBE", "NOW", "INTU", "WDAY"],
    "Consumer Electronics":               ["AAPL", "SONO", "HPQ", "DELL"],
    "Internet Content & Information":     ["GOOGL", "META", "SNAP", "PINS"],
    "Auto Manufacturers":                 ["TSLA", "GM", "F", "TM", "STLA"],
    "Drug Manufacturers—General":         ["JNJ", "PFE", "MRK", "ABBV", "LLY"],
    "Drug Manufacturers—Specialty & Generic": ["BMY", "AMGN", "GILD", "BIIB"],
    "Biotechnology":                      ["AMGN", "GILD", "BIIB", "MRNA", "REGN"],
    "Financial Services":                 ["V", "MA", "AXP", "PYPL"],
    "Banks—Diversified":                  ["JPM", "BAC", "WFC", "C", "GS"],
    "Investment Banking & Brokerage":     ["GS", "MS", "JPM", "BAC"],
    "Oil & Gas Integrated":               ["XOM", "CVX", "BP", "SHEL"],
    "Oil & Gas E&P":                      ["COP", "OXY", "PXD", "DVN"],
    "Online Retail":                      ["AMZN", "EBAY", "ETSY", "W"],
    "Specialty Retail":                   ["NKE", "TGT", "COST", "HD", "LOW"],
    "Restaurants":                        ["MCD", "SBUX", "YUM", "CMG"],
    "Airlines":                           ["DAL", "UAL", "AAL", "LUV"],
    "Aerospace & Defense":                ["BA", "LMT", "RTX", "NOC", "GD"],
    "Telecom Services":                   ["T", "VZ", "TMUS"],
    "Media—Diversified":                  ["DIS", "NFLX", "WBD", "PARA"],
    "Real Estate":                        ["AMT", "PLD", "CCI", "EQIX"],
    "Utilities—Regulated Electric":       ["NEE", "DUK", "SO", "D"],
}

# 한국 산업(industry) → 피어 종목 테이블 (yfinance industry 기준)
_KR_INDUSTRY_PEERS: dict[str, list[str]] = {
    "Consumer Electronics":              ["005930", "000660", "066570", "034220", "009150"],
    "Semiconductors":                    ["005930", "000660", "000990", "336370", "042700"],
    "Software—Application":              ["035420", "035720", "259960", "293490"],
    "Internet Content & Information":    ["035420", "035720", "034730", "036570"],
    "Auto Manufacturers":                ["005380", "000270", "012330", "064350"],
    "Auto Parts":                        ["012330", "018880", "161390", "000880"],
    "Specialty Chemicals":               ["051910", "011170", "010950", "006400"],
    "Steel":                             ["005490", "004020", "086790", "002680"],
    "Banks—Diversified":                 ["105560", "055550", "086790", "316140", "138930"],
    "Insurance":                         ["000810", "032830", "088350", "001450"],
    "Telecom Services":                  ["030200", "017670", "032640"],
    "Pharmaceutical Retailers":          ["068270", "207940", "326030", "000100"],
    "Drug Manufacturers—Specialty & Generic": ["068270", "207940", "128940", "326030"],
    "Real Estate":                       ["145720", "336370", "042660"],
    "Oil & Gas Integrated":              ["010130", "078930", "011790"],
    "Packaged Foods":                    ["097950", "007070", "003070", "004370"],
    "Aerospace & Defense":               ["047810", "000880", "064350", "012450"],
    "Department Stores":                 ["023530", "069960", "004170"],
}

# 미국 섹터 → 피어 fallback 테이블
_US_SECTOR_PEERS: dict[str, list[str]] = {
    "Technology":              ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD"],
    "Healthcare":              ["JNJ", "PFE", "UNH", "ABBV", "MRK", "LLY"],
    "Financial Services":      ["JPM", "BAC", "GS", "MS", "WFC", "V"],
    "Consumer Cyclical":       ["AMZN", "TSLA", "NKE", "HD", "MCD"],
    "Communication Services":  ["GOOGL", "META", "NFLX", "DIS", "T"],
    "Energy":                  ["XOM", "CVX", "COP", "SLB", "OXY"],
    "Industrials":             ["CAT", "BA", "GE", "MMM", "HON"],
    "Consumer Defensive":      ["WMT", "PG", "KO", "PEP", "COST"],
    "Real Estate":             ["AMT", "PLD", "CCI", "EQIX", "PSA"],
    "Utilities":               ["NEE", "DUK", "SO", "D", "AEP"],
    "Basic Materials":         ["LIN", "APD", "SHW", "FCX", "NEM"],
}


def get_sector_comparison(ticker: str) -> dict:
    """
    같은 섹터 내 주요 종목들의 당일 등락률을 비교하여
    '종목 고유 이슈'인지 '섹터·시장 전체 흐름'인지 판별 단서를 제공한다.

    Returns
    -------
    dict  {ticker, sector, target_change_pct, peers: [{ticker, name, change_pct}],
           assessment, peer_median_pct, isolation_gap}
          실패 시: {"error": "..."}
    """
    import yfinance as yf

    ticker = ticker.strip()
    market = "KR" if _is_korean(ticker) else "US"

    sector   = None
    industry = None
    peer_tickers: list[str] = []

    # ── 섹터/피어 탐색 ─────────────────────────────────────
    if market == "US":
        try:
            info     = yf.Ticker(ticker).info
            sector   = info.get("sector") or "Unknown"
            industry = info.get("industry") or "Unknown"
        except Exception:
            sector = industry = "Unknown"

        # industry 우선 → sector fallback
        candidates = (
            _US_INDUSTRY_PEERS.get(industry, [])
            or _US_SECTOR_PEERS.get(sector, [])
        )
        peer_tickers = [p for p in candidates if p != ticker][:4]

    else:  # KR
        # yfinance로 industry/sector 조회 후 한국 피어 테이블 매칭
        try:
            yf_info  = yf.Ticker(_yf_ticker(ticker)).info
            sector   = yf_info.get("sector") or "Unknown"
            industry = yf_info.get("industry") or "Unknown"
        except Exception:
            sector = industry = "Unknown"

        candidates = _KR_INDUSTRY_PEERS.get(industry, [])
        peer_tickers = [p for p in candidates if p != ticker][:4]

    if not peer_tickers:
        return {
            "ticker":           ticker,
            "sector":           sector or "Unknown",
            "industry":         industry,
            "target_change_pct": None,
            "peers":            [],
            "assessment":       "동종 피어 종목을 찾지 못했습니다.",
            "peer_median_pct":  None,
            "isolation_gap":    None,
        }

    # ── 캐싱+재시도 래퍼 (내부 전용) ────────────────────────
    def _fetch_with_cache(t: str) -> dict:
        now = time.time()
        with _cache_lock:
            if t in _quote_cache:
                ts, cached = _quote_cache[t]
                if now - ts < _CACHE_TTL:
                    return cached

        result = None
        last = None
        for attempt in range(_PEER_RETRIES + 1):
            try:
                r = get_quote(t)
                last = r
                if "error" not in r and r.get("change_pct") is not None:
                    result = r
                    break
            except Exception:
                pass
            if attempt < _PEER_RETRIES:
                time.sleep(0.4 * (attempt + 1))  # 0.4s → 0.8s 백오프

        final = result if result is not None else (last or {"error": "조회 실패"})
        with _cache_lock:
            _quote_cache[t] = (time.time(), final)
        return final

    # ── 대상 종목 등락률 ──────────────────────────────────
    target_q   = _fetch_with_cache(ticker)
    target_pct = target_q.get("change_pct") if "error" not in target_q else None

    # ── 피어 등락률 (부분 성공 허용) ──────────────────────
    peer_results = []
    for p in peer_tickers:
        q = _fetch_with_cache(p)
        if "error" not in q and q.get("change_pct") is not None:
            peer_results.append({
                "ticker":     p,
                "name":       q.get("name", p),
                "change_pct": q["change_pct"],
            })

    # ── 판별 로직 ─────────────────────────────────────────
    peer_median_pct = None
    isolation_gap   = None

    n = len(peer_results)
    if n == 0 or target_pct is None:
        assessment = "피어 데이터 없음 — 판별 불가"
    elif n < _PEER_MIN_COUNT:
        # 피어가 1개뿐이면 단일 비교로 수치는 산출하되 신뢰도 경고 표시
        pct0 = peer_results[0]["change_pct"]
        peer_median_pct = pct0
        isolation_gap   = round(target_pct - pct0, 2)
        assessment = f"판별 제한 — 피어 {n}개만 수신 (최소 {_PEER_MIN_COUNT}개 권장). 참고치만 제공"
    else:
        pcts = sorted(p["change_pct"] for p in peer_results)
        mid  = len(pcts) // 2
        peer_median_pct = (
            pcts[mid] if len(pcts) % 2 == 1
            else round((pcts[mid - 1] + pcts[mid]) / 2, 4)
        )
        isolation_gap = round(target_pct - peer_median_pct, 2)

        abs_gap = abs(isolation_gap)
        suffix  = f"(피어 {n}개 기준)" if n < len(peer_tickers) else ""
        if abs_gap <= 1.5:
            assessment = f"섹터·시장 전체 흐름 가능성 높음 (동종 피어와 유사한 변동) {suffix}".strip()
        elif abs_gap <= 3.5:
            assessment = f"부분적 종목 고유 요인 가능성 (동종 피어보다 다소 큰 변동) {suffix}".strip()
        else:
            assessment = f"종목 고유 이슈 가능성 높음 (동종 피어와 확연히 다른 변동) {suffix}".strip()

    return {
        "ticker":            ticker,
        "sector":            sector or "Unknown",
        "industry":          industry,
        "target_change_pct": target_pct,
        "peers":             peer_results,
        "assessment":        assessment,
        "peer_median_pct":   peer_median_pct,
        "isolation_gap":     isolation_gap,
    }


# ═══════════════════════════════════════════════
# 8. get_universe
# ═══════════════════════════════════════════════

# 유니버스 캐시 (TTL 1시간 — 구성종목은 자주 안 바뀜)
_universe_cache: dict[str, tuple[float, list]] = {}
_universe_lock = threading.Lock()
_UNIVERSE_TTL = 3600

# 유니버스 디스크 캐시 (작업 큐 ② — fdr 원격 404 시 폴백용)
# gitignore 대상 로컬 상태 파일. 신선도 한도를 넘긴 캐시는 사용하지 않는다 —
# "확실하지 않으면 안 산다" 원칙 유지 (캐시는 '과거에 확실했던 목록'일 때만 대체 허용).
_UNIVERSE_DISK_CACHE_PATH = _BASE_DIR / "universe_cache.json"
_UNIVERSE_DISK_MAX_AGE_DAYS = 7


def _save_universe_disk_cache(key: str, tickers: list) -> None:
    """유니버스 조회 성공분을 디스크 캐시에 저장한다. 저장 실패는 경고만 (본 기능을 죽이지 않음)."""
    import json
    try:
        data: dict = {}
        if _UNIVERSE_DISK_CACHE_PATH.exists():
            with open(_UNIVERSE_DISK_CACHE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        data[key] = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "tickers": tickers,
        }
        with open(_UNIVERSE_DISK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 유니버스 디스크 캐시 저장 실패 (계속 진행): {e}")


def _load_universe_disk_cache(key: str) -> tuple[list, str] | dict:
    """
    디스크 캐시에서 유니버스를 읽는다.
    성공: (tickers, saved_at) 튜플 / 실패: {"error": 사유} dict.
    신선도 한도(_UNIVERSE_DISK_MAX_AGE_DAYS)를 넘긴 캐시는 만료로 거부한다.
    """
    import json
    try:
        if not _UNIVERSE_DISK_CACHE_PATH.exists():
            return {"error": "디스크 캐시 파일 없음"}
        with open(_UNIVERSE_DISK_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(key) if isinstance(data, dict) else None
        if not entry or not entry.get("tickers"):
            return {"error": f"디스크 캐시에 {key} 항목 없음"}
        saved_at = str(entry.get("saved_at", ""))
        saved_dt = datetime.fromisoformat(saved_at)
        age_days = (datetime.now() - saved_dt).total_seconds() / 86400
        if age_days > _UNIVERSE_DISK_MAX_AGE_DAYS:
            return {"error": (f"디스크 캐시 만료 — {age_days:.1f}일 경과 "
                              f"(한도 {_UNIVERSE_DISK_MAX_AGE_DAYS}일)")}
        return (entry["tickers"], saved_at)
    except Exception as e:
        return {"error": f"디스크 캐시 읽기 실패: {e}"}

def get_universe(market: str = "ALL") -> dict:
    """
    발굴 대상 종목 유니버스를 반환한다.

    Parameters
    ----------
    market : str
        'KR'  → 코스피 시총 상위 200 (우선주 제외) — KOSPI200 근사
        'US'  → S&P500 구성종목 (503개)
        'ALL' → KR + US 합산

    Returns
    -------
    dict
        {
            "market" : str,
            "count"  : int,
            "tickers": [{"ticker": str, "name": str, "sector": str | None}]
        }
        실패 시: {"error": "..."}
    """
    market = market.upper().strip()
    if market not in ("KR", "US", "ALL"):
        return {"error": f"지원하지 않는 market: '{market}'. 'KR'·'US'·'ALL' 중 선택하세요."}

    # 작업 큐 ②: 이번 호출에서 디스크 캐시 폴백이 발동했는지 기록 (반환값 명시용)
    fallback_notes: list[str] = []

    def _fetch_kr() -> list | dict:
        now = time.time()
        with _universe_lock:
            if "KR" in _universe_cache:
                ts, cached = _universe_cache["KR"]
                if now - ts < _UNIVERSE_TTL:
                    return cached
        try:
            listing = fdr.StockListing("KRX")
            listing.columns = [c.lower() for c in listing.columns]

            # KOSPI만 추출
            kospi = listing[listing["market"] == "KOSPI"].copy()

            # 우선주 제외: 종목명이 '우', '우B', '우C', '1우', '2우' 등으로 끝나는 경우
            kospi = kospi[~kospi["name"].str.contains(r"\d?우[A-Z]?$", regex=True, na=False)]

            # 시가총액 기준 상위 200
            kospi["marcap"] = pd.to_numeric(kospi["marcap"], errors="coerce").fillna(0)
            top200 = kospi.nlargest(200, "marcap")

            result = [
                {"ticker": str(row["code"]), "name": str(row["name"]), "sector": None}
                for _, row in top200.iterrows()
            ]
            with _universe_lock:
                _universe_cache["KR"] = (time.time(), result)
            _save_universe_disk_cache("KR", result)
            return result
        except Exception as e:
            fb = _load_universe_disk_cache("KR")
            if isinstance(fb, tuple):
                cached_tickers, saved_at = fb
                print(f"⚠️ KR 유니버스 원격 조회 실패 → 디스크 캐시 폴백 사용 "
                      f"(저장 {saved_at}) / 원인: {e}")
                fallback_notes.append(f"KR 유니버스 디스크 캐시 폴백 사용 (저장 {saved_at})")
                with _universe_lock:
                    _universe_cache["KR"] = (time.time(), cached_tickers)
                return cached_tickers
            return {"error": f"KR 유니버스 조회 실패: {e} / 캐시 폴백 불가: {fb['error']}"}

    def _fetch_us() -> list | dict:
        now = time.time()
        with _universe_lock:
            if "US" in _universe_cache:
                ts, cached = _universe_cache["US"]
                if now - ts < _UNIVERSE_TTL:
                    return cached
        try:
            sp500 = fdr.StockListing("S&P500")
            result = [
                {
                    "ticker": str(row["Symbol"]),
                    "name":   str(row["Name"]),
                    "sector": str(row["Sector"]) if pd.notna(row.get("Sector")) else None,
                }
                for _, row in sp500.iterrows()
            ]
            with _universe_lock:
                _universe_cache["US"] = (time.time(), result)
            _save_universe_disk_cache("US", result)
            return result
        except Exception as e:
            fb = _load_universe_disk_cache("US")
            if isinstance(fb, tuple):
                cached_tickers, saved_at = fb
                print(f"⚠️ US 유니버스 원격 조회 실패 → 디스크 캐시 폴백 사용 "
                      f"(저장 {saved_at}) / 원인: {e}")
                fallback_notes.append(f"US 유니버스 디스크 캐시 폴백 사용 (저장 {saved_at})")
                with _universe_lock:
                    _universe_cache["US"] = (time.time(), cached_tickers)
                return cached_tickers
            return {"error": f"US 유니버스 조회 실패: {e} / 캐시 폴백 불가: {fb['error']}"}

    if market == "KR":
        kr = _fetch_kr()
        if isinstance(kr, dict):
            return kr
        result = {"market": "KR", "count": len(kr), "tickers": kr}
        if fallback_notes:
            result["universe_cache_note"] = "; ".join(fallback_notes)
        return result

    if market == "US":
        us = _fetch_us()
        if isinstance(us, dict):
            return us
        result = {"market": "US", "count": len(us), "tickers": us}
        if fallback_notes:
            result["universe_cache_note"] = "; ".join(fallback_notes)
        return result

    # ALL
    kr = _fetch_kr()
    us = _fetch_us()
    warnings_list: list[str] = []
    tickers: list[dict] = []

    if isinstance(kr, dict):
        warnings_list.append(kr["error"])
    else:
        tickers.extend(kr)

    if isinstance(us, dict):
        warnings_list.append(us["error"])
    else:
        tickers.extend(us)

    if not tickers:
        return {"error": "; ".join(warnings_list)}

    result: dict = {"market": "ALL", "count": len(tickers), "tickers": tickers}
    if warnings_list:
        result["warnings"] = warnings_list
    if fallback_notes:
        result["universe_cache_note"] = "; ".join(fallback_notes)
    return result


# ═══════════════════════════════════════════════
# 9. screen_stocks
# ═══════════════════════════════════════════════

# 스크리닝 데이터 캐시 (TTL 6시간 — 재무지표는 장중에 변하지 않음)
_screen_cache: dict[str, tuple[float, dict]] = {}
_screen_lock  = threading.Lock()
_SCREEN_TTL   = 21600

# 스크리닝 결과 캐시 (TTL 6시간, key=(market, ulimit, correction))
# 점수화까지 완료된 scored 리스트를 보관 — top_n/sector 필터는 조회 시 적용
_scored_cache: dict[tuple, tuple[float, tuple]] = {}
_scored_lock  = threading.Lock()


def _fetch_screen_data(ticker: str) -> dict:
    """
    스크리닝에 필요한 재무 지표를 yfinance에서 한 번에 수집한다.
    캐시(6h) + 재시도(최대 2회, 0.5/1.0s 백오프) 포함.
    """
    import yfinance as yf

    now = time.time()
    with _screen_lock:
        if ticker in _screen_cache:
            ts, cached = _screen_cache[ticker]
            # sector 필드가 없는 구버전 캐시는 재수집
            if now - ts < _SCREEN_TTL and "sector" in cached:
                return cached

    result = None
    for attempt in range(3):
        try:
            info = yf.Ticker(_yf_ticker(ticker)).info
            result = {
                "ticker":      ticker,
                "name":        info.get("longName") or info.get("shortName") or ticker,
                "sector":      info.get("sector") or None,
                "industry":    info.get("industry") or None,
                "op_margin":   _safe_float(info.get("operatingMargins")),
                "net_margin":  _safe_float(info.get("profitMargins")),
                "rev_growth":  _safe_float(info.get("revenueGrowth")),
                "earn_growth": _safe_float(info.get("earningsGrowth")),
            }
            break
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    if result is None:
        result = {"ticker": ticker, "error": "데이터 수집 실패"}

    with _screen_lock:
        _screen_cache[ticker] = (time.time(), result)
    return result


def screen_stocks(
    market: str = "KR",
    top_n: int = 20,
    universe_limit: int | None = None,
    growth_correction: bool = True,
    max_per_sector: int | None = 3,
) -> dict:
    """
    유니버스 종목을 실적·성장성 기준으로 평가해 상위 top_n개를 반환한다.

    Parameters
    ----------
    market            : 'KR' / 'US' / 'ALL'
    top_n             : 반환할 상위 종목 수
    universe_limit    : 유니버스에서 시총 상위 N개만 평가 (테스트·빠른 실행용)
                        추천 목적: KR=50, US=100, ALL=150 권장
    growth_correction : True(기본) → 성장률 기저효과 보정 적용
                        False → 보정 없이 raw 성장률 그대로 사용 (비교용)
    max_per_sector    : 같은 섹터(업종)에서 결과에 포함할 최대 종목 수.
                        None → 제한 없음. 기본 3.

    Returns
    -------
    dict
        {market, total_evaluated, scored, top_n, growth_correction,
         max_per_sector, results:[...]}
        results 항목: {rank, ticker, name, sector, industry,
                       profitability_score, growth_score,
                       total_score, is_turnaround, key_metrics}

    점수화 방식
    -----------
    각 지표를 배치 내 percentile rank(0~100)로 변환 후 가중 합산.

    [실적 점수, 50%] — "지금 잘 버는가"  (보정 없음)
        op_margin  (영업이익률) 60% — 본업 경쟁력, 세금·이자 영향 적음
        net_margin (순이익률)   40% — 최종 주주 귀속 이익력

    [성장 점수, 50%] — "점점 더 버는가"  (growth_correction=True 시 보정 적용)
        rev_growth  (매출성장률)  50%
        earn_growth (이익성장률)  50%

    성장률 기저효과 보정 (growth_correction=True):
        적자→흑자 전환 시 earn_growth +400~500% 등 극단값이 발생, 꾸준한 성장주를
        하위권으로 밀어내는 착시 문제를 하드 캡으로 보정한다.

        하드 캡: earn_growth > 100% → 100%로 평탄화, rev_growth > 150% → 150%.
            동점 처리된 percentile rank로 극단값 그룹이 같은 순위를 받음 →
            cap 이하 진짜 성장주들이 상대적으로 올라감.

    흑자전환 플래그 (is_turnaround):
        raw earn_growth > 200% 인 경우 True.
        점수에서 제외하지 않고 식별 목적으로만 표시.

    섹터 분산 (max_per_sector):
        점수 정렬 후 결과 선별 단계에서 동일 섹터 종목은 상위 N개까지만 포함.
        점수 계산에는 영향 없음.

    가중치 근거:
        · 영업이익률 > 순이익률 : 회계 처리에 덜 민감해 본업 수익력을 더 잘 반영
        · 성장 : 실적 = 50:50   : 고마진 성숙기업과 고성장 기업 균형 포착
        · 부분 데이터 허용      : 가용 지표 비중만으로 점수화, 전무하면 제외

    캐시 전략:
        (market, universe_limit, growth_correction) 조합의 scored 리스트를 6시간 보관.
        max_per_sector / top_n 필터는 캐시 조회 후 적용하므로 캐시 키에 포함하지 않는다.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    market = market.upper().strip()

    # ── 결과 레벨 캐시 확인 ──────────────────────────────────
    # key: (market, universe_limit or 0, growth_correction)
    # 점수화 완료된 scored 리스트를 재사용 → 데이터 수집 전체를 건너뜀
    rkey   = (market, universe_limit or 0, growth_correction)
    scored: list[dict] | None = None
    total  = 0
    universe_cache_note: str | None = None   # 작업 큐 ②: 유니버스 디스크 캐시 폴백 사용 기록

    with _scored_lock:
        if rkey in _scored_cache:
            ts, (cached_total, cached_scored) = _scored_cache[rkey]
            if time.time() - ts < _SCREEN_TTL:
                total  = cached_total
                scored = cached_scored
                print(f"[Screener] 결과 캐시 히트 — {total}개 평가 결과 재사용 "
                      f"(market={market}, ulimit={universe_limit or '전체'})")

    if scored is None:
        # ── 1. 유니버스 로드 ─────────────────────────────────────
        univ = get_universe(market)
        if "error" in univ:
            return univ
        universe_cache_note = univ.get("universe_cache_note")

        tickers_info = univ["tickers"]
        if universe_limit:
            tickers_info = tickers_info[:universe_limit]

        ticker_list = [t["ticker"] for t in tickers_info]
        name_map    = {t["ticker"]: t["name"] for t in tickers_info}
        total       = len(ticker_list)

        # ── 2. 재무 데이터 병렬 수집 ────────────────────────────
        print(f"[Screener] {total}개 종목 데이터 수집 시작 "
              f"(market={market}, correction={growth_correction})")
        done_count  = [0]
        report_lock = threading.Lock()

        def _fetch_and_report(t: str) -> dict:
            r = _fetch_screen_data(t)
            with report_lock:
                done_count[0] += 1
                n = done_count[0]
                if n % 10 == 0 or n == total:
                    print(f"  [Screener] {n}/{total} 처리 중...")
            return r

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_fetch_and_report, t) for t in ticker_list]
            raw = [f.result() for f in as_completed(futures)]

        valid = [r for r in raw if "error" not in r]
        print(f"[Screener] 유효 {len(valid)}/{total}개 → 점수 계산 시작")

        # ── 3. 성장률 기저효과 보정 헬퍼 ───────────────────────
        #
        # [왜 '하드 캡'인가]
        #   윈저라이징·로그 변환은 값 크기를 줄이지만 순서를 바꾸지 않는 단조 변환이다.
        #   Percentile rank는 순서만 보기 때문에 단조 변환 전후 결과가 동일하다.
        #   → 실제로 순위를 바꾸려면 순서 자체를 바꿔야 하고, 그 방법이 하드 캡이다.
        #
        #   earn_growth > CAP 인 종목들을 모두 CAP 값으로 평탄화(clamp) →
        #   동점 처리 percentile rank에서 같은 순위를 받음 →
        #   cap 이하 진짜 성장주들이 상대적으로 올라감.
        #
        # [흑자전환 플래그]
        #   raw earn_growth > TURNAROUND_THRESHOLD → is_turnaround = True
        #   점수에서 제외하지 않고 식별 목적으로만 표시.

        # 성장률 상한 (기저효과가 주로 이 범위를 넘어서 발생)
        EARN_GROWTH_CAP      = 1.0   # 이익성장 100% 초과 → 100%로 처리
        REV_GROWTH_CAP       = 1.5   # 매출성장 150% 초과 → 150%로 처리 (기저효과 덜함)
        TURNAROUND_THRESHOLD = 2.0   # 이익성장 200% 초과 → 흑자전환 의심

        def _clamp(val_dict: dict, cap: float) -> dict:
            """CAP 이상 값을 CAP으로 평탄화. None은 유지."""
            return {t: (min(v, cap) if v is not None else None) for t, v in val_dict.items()}

        def _pct_rank(val_dict: dict) -> dict:
            """
            유니크 값 기반 percentile rank(0~100).
            동일한 값은 동일한 순위를 부여 — 캡으로 평탄화된 동점 그룹이 올바르게 묶임.
            """
            pairs = [(t, v) for t, v in val_dict.items() if v is not None]
            if not pairs:
                return {t: None for t in val_dict}
            unique_sorted = sorted(set(v for _, v in pairs))
            n = len(unique_sorted)
            val_to_rank = {v: round(i / max(n - 1, 1) * 100, 1) for i, v in enumerate(unique_sorted)}
            lookup = {t: val_to_rank[v] for t, v in pairs}
            return {t: lookup.get(t) for t in val_dict}

        # ── 4. 지표별 percentile rank 계산 ─────────────────────
        # 실적 지표: 보정 없이 raw → rank
        op_rank  = _pct_rank({r["ticker"]: r.get("op_margin")  for r in valid})
        net_rank = _pct_rank({r["ticker"]: r.get("net_margin") for r in valid})

        # 성장 지표: 보정 여부에 따라 하드 캡 적용 후 → rank
        rev_raw  = {r["ticker"]: r.get("rev_growth")  for r in valid}
        earn_raw = {r["ticker"]: r.get("earn_growth") for r in valid}

        if growth_correction:
            rev_rank  = _pct_rank(_clamp(rev_raw,  REV_GROWTH_CAP))
            earn_rank = _pct_rank(_clamp(earn_raw, EARN_GROWTH_CAP))
        else:
            rev_rank  = _pct_rank(rev_raw)
            earn_rank = _pct_rank(earn_raw)

        # ── 5. 종목별 점수 계산 ─────────────────────────────────
        scored = []
        for r in valid:
            t = r["ticker"]

            # 실적 점수: op 60% + net 40%
            op_s, net_s = op_rank.get(t), net_rank.get(t)
            if   op_s is not None and net_s is not None:
                profit_score = round(op_s * 0.6 + net_s * 0.4, 1)
            elif op_s is not None:
                profit_score = round(op_s, 1)
            elif net_s is not None:
                profit_score = round(net_s, 1)
            else:
                profit_score = None

            # 성장 점수: rev 50% + earn 50%
            rev_s, earn_s = rev_rank.get(t), earn_rank.get(t)
            if   rev_s is not None and earn_s is not None:
                growth_score = round(rev_s * 0.5 + earn_s * 0.5, 1)
            elif rev_s is not None:
                growth_score = round(rev_s, 1)
            elif earn_s is not None:
                growth_score = round(earn_s, 1)
            else:
                growth_score = None

            # 종합 점수: 실적 50% + 성장 50%
            if   profit_score is not None and growth_score is not None:
                total_score = round(profit_score * 0.5 + growth_score * 0.5, 1)
            elif profit_score is not None:
                total_score = profit_score
            elif growth_score is not None:
                total_score = growth_score
            else:
                continue  # 점수화 불가 → 제외

            # 흑자전환 플래그 (raw earn_growth 기준)
            raw_eg = r.get("earn_growth")
            is_turnaround = (raw_eg is not None and raw_eg > TURNAROUND_THRESHOLD)

            def _pct(v):
                return round(v * 100, 2) if v is not None else None

            scored.append({
                "ticker":              t,
                "name":                r.get("name") or name_map.get(t, t),
                "sector":              r.get("sector") or "미분류",
                "industry":            r.get("industry") or "미분류",
                "profitability_score": profit_score,
                "growth_score":        growth_score,
                "total_score":         total_score,
                "is_turnaround":       is_turnaround,
                "key_metrics": {
                    "op_margin_pct":   _pct(r.get("op_margin")),
                    "net_margin_pct":  _pct(r.get("net_margin")),
                    "rev_growth_pct":  _pct(r.get("rev_growth")),   # 항상 raw 수치 표시
                    "earn_growth_pct": _pct(r.get("earn_growth")),  # 항상 raw 수치 표시
                },
            })

        # ── 6. 종합 점수 내림차순 정렬 ─────────────────────────
        scored.sort(key=lambda x: x["total_score"], reverse=True)

        # 결과 캐시에 저장 (top_n/max_per_sector 필터 전 상태로 보관)
        with _scored_lock:
            _scored_cache[rkey] = (time.time(), (total, scored))

    # ── 7. 섹터별 상한 적용 → top_n 선별 ──────────────────
    # 캐시 히트·미스 공통 경로. 캐시 데이터 변형을 막기 위해 shallow copy 사용.
    if max_per_sector is not None:
        sector_counts: dict[str, int] = {}
        top = []
        for item in scored:
            sec = item["sector"]
            cnt = sector_counts.get(sec, 0)
            if cnt < max_per_sector:
                top.append({**item})
                sector_counts[sec] = cnt + 1
            if len(top) >= top_n:
                break
    else:
        top = [{**item} for item in scored[:top_n]]

    for rank, item in enumerate(top, 1):
        item["rank"] = rank

    print(f"[Screener] 완료 — 점수화 {len(scored)}개, 상위 {len(top)}개 반환 "
          f"(섹터제한 {max_per_sector if max_per_sector else '없음'})")
    result = {
        "market":            market,
        "total_evaluated":   total,
        "scored":            len(scored),
        "top_n":             top_n,
        "growth_correction": growth_correction,
        "max_per_sector":    max_per_sector,
        "results":           top,
    }
    if universe_cache_note:
        result["universe_cache_note"] = universe_cache_note
    return result


# ═══════════════════════════════════════════════
# 9-1. _attach_sell_trend_note  (매도 규칙 공용 헬퍼)
# ═══════════════════════════════════════════════

def _sell_trend_note_for(ticker: str) -> str | None:
    """
    단일 종목의 매도용 추세 노트를 계산한다.
    (기존 _attach_sell_trend_note 내부 로직과 동일. 지표 조회 실패 시 None.)
    """
    ind = get_indicators(ticker)
    if "error" in ind:
        return None

    close, ma20 = ind.get("close"), ind.get("ma20")
    rsi = ind.get("rsi")
    macd_hist = ind.get("macd_hist")

    if macd_hist is not None and macd_hist > 0:
        return "🟢 반등 신호 — 손절 보류 검토"
    elif rsi is not None and rsi < 30:
        return "🟠 과매도 구간 — 반등 가능성도 있음"
    elif close is not None and ma20 is not None and close > ma20:
        return "🟢 단기 추세 개선 중"
    else:
        return "🔴 하락 지속"


def _attach_sell_trend_note(candidates: list[dict]) -> None:
    """
    각 후보 dict에 get_indicators 기반 추세 정보를 trend_note 필드로 붙인다.
    분류 단계에서 이미 trend_note가 계산된 후보는 재조회하지 않는다 (중복 API 호출 방지).
    """
    for c in candidates:
        if "trend_note" in c:
            continue
        c["trend_note"] = _sell_trend_note_for(c["ticker"])


# ═══════════════════════════════════════════════
# 10. evaluate_sell_rules
# ═══════════════════════════════════════════════

def evaluate_sell_rules() -> dict:
    """
    보유 종목을 손절·익절 규칙에 비춰 "팔아야 할 후보"를 분류한다.

    ⚠️ 실제 매도 주문을 내지 않는다. 판단/제안만 한다 (dry_run 성격).
    ⚠️ 미래 예측 아님. 규칙상 분류이며 실제 매도 여부는 사람이 직접 결정한다.

    임계값 상수 (함수 상단에서 수정 가능)
    ----------------------------------------
    STOP_LOSS_PCT   : 수익률 ≤ 이 값이면 손절 후보
    HARD_FLOOR_PCT  : 수익률 ≤ 이 값이면 추세 무관 무조건 손절 후보
    TAKE_PROFIT_PCT : 수익률 ≥ 이 값이면 익절 후보

    Returns
    -------
    dict
        {
            "as_of": str,
            "rules": {
                "stop_loss_pct":   float,   # 손절 임계값 (예: -10.0)
                "hard_floor_pct":  float,   # 하드 플로어 임계값 (예: -15.0) — 도달 시 추세 무관 무조건 손절 후보
                "take_profit_pct": float,   # 익절 임계값 (예:  20.0)
            },
            "stop_loss": [
                {
                    "ticker":          str,
                    "name":            str,
                    "profit_loss_pct": float,
                    "reason":          str,
                    "action":          str,
                    "market":          str,   # "KR" | "US"
                    "currency":        str,   # "KRW" | "USD"
                    "trend_note":      str | None,  # get_indicators 기반 추세 정보 (손절 구간에서는 보류 판단에 사용됨)
                }
            ],
            "stop_loss_deferred": [...],  # 같은 구조 — 손절 규칙 도달했으나 반등 신호/과매도로 그날 보류
            "take_profit": [...],   # 같은 구조 (trend_note 포함), 수익률 높은 순 정렬
            "hold": {
                "count":    int,
                "domestic": int,
                "overseas": int,
            },
            "summary": {
                "total_holdings":          int,
                "stop_loss_count":         int,
                "stop_loss_deferred_count": int,
                "take_profit_count":       int,
                "hold_count":              int,
            },
            "disclaimer": str,
        }
        실패 시: {"error": "..."}
    """
    # ── 규칙 임계값 ─────────────────────────────────────────────
    STOP_LOSS_PCT   = -10.0   # 수익률 <= 이 값 → 손절 후보
    HARD_FLOOR_PCT  = -15.0   # 도달 시 추세 무관 무조건 손절 후보 (2단계 확정 수치)
    TAKE_PROFIT_PCT =  20.0   # 수익률 >= 이 값 → 익절 후보

    # ── 1. 잔고 조회 ─────────────────────────────────────────────
    balance = get_kis_balance()
    if "error" in balance:
        return {"error": f"잔고 조회 실패: {balance['error']}"}

    holdings = balance.get("holdings", [])
    if not holdings:
        return {"error": "보유 종목이 없습니다."}

    # ── 2. 규칙 분류 ─────────────────────────────────────────────
    stop_loss:          list[dict] = []
    stop_loss_deferred: list[dict] = []
    take_profit:        list[dict] = []
    hold:               list[dict] = []

    for h in holdings:
        pct      = h.get("profit_loss_pct")
        ticker   = h.get("ticker", "")
        name     = h.get("name", ticker)
        market   = h.get("market", "?")
        currency = h.get("currency", "?")

        if pct is None:
            continue

        entry = {
            "ticker":          ticker,
            "name":            name,
            "profit_loss_pct": pct,
            "market":          market,
            "currency":        currency,
        }

        if pct <= HARD_FLOOR_PCT:
            # 하드 플로어: 추세와 무관하게 무조건 손절 후보 (2단계 확정 수치)
            entry["reason"] = (
                f"하드 플로어: 수익률 {pct:+.2f}% ≤ {HARD_FLOOR_PCT:+.1f}% — 추세 무관 무조건 매도 후보"
            )
            entry["action"] = "손절 후보 (하드 플로어)"
            stop_loss.append(entry)
        elif pct <= STOP_LOSS_PCT:
            # 손절 구간(-15% 초과 ~ -10% 이하): 추세를 먼저 보고 보류 여부 결정
            note = _sell_trend_note_for(ticker)
            entry["trend_note"] = note
            if note is not None and ("반등 신호" in note or "과매도" in note):
                # 그날 손절 보류 — 상태 저장 없음. 매 실행마다 최신 지표로 재판단되므로
                # 다음 실행에서 신호가 사라지면 자연히 손절 후보로 복귀한다.
                entry["reason"] = (
                    f"손절 규칙 도달(수익률 {pct:+.2f}% ≤ {STOP_LOSS_PCT:+.1f}%)이지만 "
                    f"추세({note}) 기준으로 오늘은 손절 보류 (2단계 확정 수치)"
                )
                entry["action"] = "손절 보류"
                stop_loss_deferred.append(entry)
            else:
                # 지표 조회 실패(None) 포함 — 근거 없이 보류하지 않고 규칙대로 손절 후보 유지
                entry["reason"] = (
                    f"손절 규칙: 수익률 {pct:+.2f}% ≤ {STOP_LOSS_PCT:+.1f}%"
                )
                entry["action"] = "손절 후보"
                stop_loss.append(entry)
        elif pct >= TAKE_PROFIT_PCT:
            entry["reason"] = (
                f"익절 규칙: 수익률 {pct:+.2f}% ≥ +{TAKE_PROFIT_PCT:.1f}%"
            )
            entry["action"] = "익절 후보"
            take_profit.append(entry)
        else:
            entry["reason"] = (
                f"보유 유지: 수익률 {pct:+.2f}%"
                f" ({STOP_LOSS_PCT:+.1f}% ~ +{TAKE_PROFIT_PCT:.1f}% 범위)"
            )
            entry["action"] = "보유 유지"
            hold.append(entry)

    # ── 3. 정렬 (손절: 수익률 낮은 순, 익절: 수익률 높은 순) ──
    stop_loss.sort(key=lambda x: x["profit_loss_pct"])
    stop_loss_deferred.sort(key=lambda x: x["profit_loss_pct"])
    take_profit.sort(key=lambda x: x["profit_loss_pct"], reverse=True)

    # ── 3-1. 추세 정보 부착 (후보 제외 아님, 참고 정보만 추가) ──
    _attach_sell_trend_note(stop_loss)
    _attach_sell_trend_note(take_profit)

    # ── 4. 보유 유지 국내/해외 집계 ─────────────────────────────
    hold_kr = sum(1 for h in hold if h["market"] == "KR")
    hold_us = sum(1 for h in hold if h["market"] == "US")

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "rules": {
            "stop_loss_pct":   STOP_LOSS_PCT,
            "hard_floor_pct":  HARD_FLOOR_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
        },
        "stop_loss":          stop_loss,
        "stop_loss_deferred": stop_loss_deferred,
        "take_profit":        take_profit,
        "hold": {
            "count":    len(hold),
            "domestic": hold_kr,
            "overseas": hold_us,
        },
        "summary": {
            "total_holdings":          len(holdings),
            "stop_loss_count":         len(stop_loss),
            "stop_loss_deferred_count": len(stop_loss_deferred),
            "take_profit_count":       len(take_profit),
            "hold_count":              len(hold),
        },
        "disclaimer": (
            "이 분류는 규칙 기반 참고 정보이며 투자 권유가 아닙니다. "
            "실제 매도 여부는 본인이 판단·결정하세요. "
            "미래 가격을 예측하지 않습니다."
        ),
    }


# ═══════════════════════════════════════════════
# 10-1. _attach_trend_warning  (매수 규칙 A/B 공용 헬퍼)
# ═══════════════════════════════════════════════

def _attach_trend_warning(candidates: list[dict]) -> None:
    """
    각 후보 dict에 get_indicators 기반 추세 경고를 trend_warning 필드로 붙인다.
    (경고만 붙이고 후보에서 제외하지 않는다. 미래 예측 아님 — 현재 상태만 기술.)
    """
    for c in candidates:
        ind = get_indicators(c["ticker"])
        if "error" in ind:
            c["trend_warning"] = None
            continue

        warnings = []
        close, ma20, ma60 = ind.get("close"), ind.get("ma20"), ind.get("ma60")
        if close is not None and ma20 is not None and ma60 is not None \
                and close < ma20 and close < ma60:
            warnings.append("⚠️ 하락 추세 — 진입 타이밍 주의")

        macd_hist = ind.get("macd_hist")
        if macd_hist is not None and macd_hist < 0:
            warnings.append("⚠️ 모멘텀 약세")

        rsi = ind.get("rsi")
        if rsi is not None and rsi < 30:
            warnings.append("🟠 과매도 구간 (반등 가능성도, 추가 하락 가능성도 있음)")

        c["trend_warning"] = " / ".join(warnings) if warnings else None


# ═══════════════════════════════════════════════
# 11. evaluate_buy_rule_A  (점수 집중 방식)
# ═══════════════════════════════════════════════

def evaluate_buy_rule_A(market: str = "ALL", universe_limit: int | None = None) -> dict:
    """
    [매수 규칙 A — 점수 집중] 스크리닝 점수 상위 종목을 매수 후보로 제안한다.

    ⚠️ 실제 매수 주문을 내지 않는다. 판단/제안만 한다 (dry_run 성격).
    ⚠️ 미래 예측 아님. "지금 점수가 높다"는 사실만 기술한다.

    임계값 상수 (함수 상단에서 수정 가능)
    ----------------------------------------
    BUY_SCORE_MIN  : 이 값 이상인 종목만 후보에 포함 (0~100)
    MAX_CANDIDATES : 반환할 최대 후보 수

    Parameters
    ----------
    market         : 'KR' / 'US' / 'ALL'
    universe_limit : 유니버스에서 평가할 최대 종목 수 (None=전체, 속도 조절용)

    Returns
    -------
    dict
        {
            "rule"        : "A — 점수 집중",
            "as_of"       : str,
            "params"      : dict,
            "held_tickers": list,
            "candidates"  : [
                {
                    "ticker"     : str,
                    "name"       : str,
                    "score"      : float,   # total_score (0~100)
                    "sector"     : str,
                    "market"     : str,     # "KR" | "US"
                    "reason"     : "점수 상위",
                    "key_metrics": dict,
                    "trend_warning": str | None,  # get_indicators 기반 현재 추세 경고 (하락 추세는 보류 판단에 사용됨)
                }
            ],
            "count"     : int,
            "excluded"  : list,   # 규칙상 제외/보류된 후보와 사유 (흑자전환★ 제외, 하락 추세 보류)
            "excluded_count": int,
            "disclaimer": str,
        }
        실패 시: {"error": "..."}
    """
    # ── 규칙 임계값 ─────────────────────────────────────────────
    BUY_SCORE_MIN  = 80    # 점수 이 값 미만이면 후보 제외
    MAX_CANDIDATES = 5     # 최대 반환 후보 수

    # ── 1. 잔고 조회 (보유 종목 제외용) ─────────────────────────
    balance = get_kis_balance()
    if "error" in balance:
        return {"error": f"잔고 조회 실패: {balance['error']}"}

    held_tickers = {h["ticker"] for h in balance.get("holdings", [])}

    # ── 2. 스크리닝 실행 ─────────────────────────────────────────
    screened = screen_stocks(
        market=market,
        top_n=50,
        universe_limit=universe_limit,
        max_per_sector=None,   # 섹터 제한 없이 점수 순으로만
    )
    if "error" in screened:
        return {"error": f"스크리닝 실패: {screened['error']}"}

    # ── 3. 필터: 점수 기준 + 미보유 ─────────────────────────────
    excluded: list[dict] = []   # 규칙상 제외/보류된 후보 (사유 기록 — Phase 0 관찰 데이터)
    candidates: list[dict] = []
    for item in screened.get("results", []):
        if item["total_score"] < BUY_SCORE_MIN:
            break   # results는 점수 내림차순 — 이후는 모두 기준 미달
        if item["ticker"] in held_tickers:
            continue
        if item.get("is_turnaround"):
            # 흑자전환★ 종목 매수 제외 (2단계 확정 수치 — SK스퀘어 점수 왜곡 사례의 코드화)
            # 루프 안에서 걸러지므로 빈 자리는 다음 순위 종목이 자동으로 채운다.
            excluded.append({
                "ticker": item["ticker"],
                "name":   item["name"],
                "score":  item["total_score"],
                "sector": item["sector"],
                "market": "KR" if _is_korean(item["ticker"]) else "US",
                "reason": "흑자전환★ 제외 — 성장률 기저효과로 점수 왜곡 가능",
            })
            continue
        candidates.append({
            "ticker":      item["ticker"],
            "name":        item["name"],
            "score":       item["total_score"],
            "sector":      item["sector"],
            "market":      "KR" if _is_korean(item["ticker"]) else "US",
            "reason":      "점수 상위",
            "key_metrics": item["key_metrics"],
        })
        if len(candidates) >= MAX_CANDIDATES:
            break

    _attach_trend_warning(candidates)

    # 하락 추세 후보 매수 보류 (2단계 확정 수치)
    # 빈 자리를 다음 순위로 채우지 않는다 — 하락장에는 후보가 적은 것이 자연스럽다 (설계 결정).
    _kept: list[dict] = []
    for c in candidates:
        tw = c.get("trend_warning")
        if tw and "하락 추세" in tw:
            c["reason"] = "하락 추세 — 매수 보류"
            excluded.append(c)
        else:
            _kept.append(c)
    candidates = _kept

    return {
        "rule":  "A — 점수 집중",
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "buy_score_min":  BUY_SCORE_MIN,
            "max_candidates": MAX_CANDIDATES,
            "market":         market,
            "universe_limit": universe_limit,
        },
        "held_tickers":   sorted(held_tickers),
        "candidates":     candidates,
        "count":          len(candidates),
        "excluded":       excluded,
        "excluded_count": len(excluded),
        "universe_cache_note": screened.get("universe_cache_note"),
        "disclaimer": (
            "이 목록은 규칙 기반 참고 정보이며 투자 권유가 아닙니다. "
            "실제 매수 여부는 본인이 판단·결정하세요. 미래 가격을 예측하지 않습니다."
        ),
    }


# ═══════════════════════════════════════════════
# 12. evaluate_buy_rule_B  (분산 채우기 방식)
# ═══════════════════════════════════════════════

def evaluate_buy_rule_B(market: str = "ALL", universe_limit: int | None = None) -> dict:
    """
    [매수 규칙 B — 분산 채우기] 포트폴리오에서 비중이 낮은 섹터를
    스크리닝 점수 상위 종목으로 보강할 후보를 제안한다.

    ⚠️ 실제 매수 주문을 내지 않는다. 판단/제안만 한다 (dry_run 성격).
    ⚠️ 미래 예측 아님. "현재 섹터 비중이 낮다"는 사실만 기술한다.

    섹터 비중은 yfinance 기준으로 계산한다 (screen_stocks와 동일 출처 → 섹터명 일치).
    "부족 섹터" 정의: 스크리닝 유니버스에 등장하는 섹터 중 내 포트폴리오 비중이
    SECTOR_UNDERWEIGHT_PCT 미만인 섹터.

    임계값 상수 (함수 상단에서 수정 가능)
    ----------------------------------------
    BUY_SCORE_MIN          : 점수 기준 (0~100)
    MAX_CANDIDATES         : 반환할 최대 후보 수
    SECTOR_UNDERWEIGHT_PCT : 이 값 미만인 섹터를 "부족 섹터"로 분류

    Parameters
    ----------
    market         : 'KR' / 'US' / 'ALL'
    universe_limit : 유니버스에서 평가할 최대 종목 수 (None=전체, 속도 조절용)

    Returns
    -------
    dict
        {
            "rule"             : "B — 분산 채우기",
            "as_of"            : str,
            "params"           : dict,
            "portfolio_sectors": {섹터명: weight_pct},   # 현재 내 섹터 비중 (%)
            "deficit_sectors"  : [str],                  # 부족 섹터 목록
            "held_tickers"     : list,
            "candidates"       : [
                {
                    "ticker"     : str,
                    "name"       : str,
                    "score"      : float,
                    "sector"     : str,
                    "market"     : str,
                    "reason"     : "부족섹터 OO 보강",
                    "key_metrics": dict,
                    "trend_warning": str | None,  # get_indicators 기반 현재 추세 경고 (하락 추세는 보류 판단에 사용됨)
                }
            ],
            "count"    : int,
            "excluded" : list,   # 규칙상 제외/보류된 후보와 사유 (흑자전환★ 제외, 하락 추세 보류)
            "excluded_count": int,
            "disclaimer": str,
        }
        실패 시: {"error": "..."}
    """
    import yfinance as yf

    # ── 규칙 임계값 ─────────────────────────────────────────────
    BUY_SCORE_MIN          = 80    # 점수 이 값 미만이면 후보 제외
    MAX_CANDIDATES         = 5     # 최대 반환 후보 수
    SECTOR_UNDERWEIGHT_PCT = 10.0  # 이 값 미만인 섹터 → 부족 섹터

    # ── 1. 잔고 조회 ─────────────────────────────────────────────
    balance = get_kis_balance()
    if "error" in balance:
        return {"error": f"잔고 조회 실패: {balance['error']}"}

    holdings = balance.get("holdings", [])
    if not holdings:
        return {"error": "보유 종목이 없어 섹터 분산 분석을 수행할 수 없습니다."}

    held_tickers = {h["ticker"] for h in holdings}

    # ── 2. 포트폴리오 섹터 비중 (yfinance 기준 — screen_stocks와 동일 출처) ──
    # USD 종목은 KRW로 환산해 비중을 통일
    usd_krw = balance.get("fx", {}).get("usd_krw") or 1.0
    sector_eval: dict[str, float] = {}

    for h in holdings:
        ev = h.get("eval_amount", 0) or 0
        if h.get("currency") == "USD":
            ev = ev * usd_krw
        t = h["ticker"]
        try:
            info = yf.Ticker(_yf_ticker(t)).info
            sec  = info.get("sector") or info.get("industry") or "기타/미분류"
        except Exception:
            sec = "기타/미분류"
        sector_eval[sec] = sector_eval.get(sec, 0) + ev

    total_ev = sum(sector_eval.values())
    if total_ev <= 0:
        return {"error": "포트폴리오 평가금액 합산이 0 이하입니다. 잔고를 확인하세요."}

    portfolio_sector_pct = {
        sec: round(ev / total_ev * 100, 1)
        for sec, ev in sorted(sector_eval.items(), key=lambda x: -x[1])
    }

    # ── 3. 스크리닝 실행 ─────────────────────────────────────────
    screened = screen_stocks(
        market=market,
        top_n=100,
        universe_limit=universe_limit,
        max_per_sector=None,
    )
    if "error" in screened:
        return {"error": f"스크리닝 실패: {screened['error']}"}

    # 유니버스에 등장하는 섹터 목록
    universe_sectors = {item["sector"] for item in screened.get("results", [])}

    # ── 4. 부족 섹터: 유니버스 섹터 중 내 비중 < SECTOR_UNDERWEIGHT_PCT ──
    deficit_sectors: set[str] = {
        sec for sec in universe_sectors
        if portfolio_sector_pct.get(sec, 0.0) < SECTOR_UNDERWEIGHT_PCT
    }

    if not deficit_sectors:
        return {
            "rule":  "B — 분산 채우기",
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "params": {
                "buy_score_min":          BUY_SCORE_MIN,
                "max_candidates":         MAX_CANDIDATES,
                "sector_underweight_pct": SECTOR_UNDERWEIGHT_PCT,
                "market":                 market,
                "universe_limit":         universe_limit,
            },
            "portfolio_sectors": portfolio_sector_pct,
            "deficit_sectors":   [],
            "held_tickers":      sorted(held_tickers),
            "candidates":        [],
            "count":             0,
            "excluded":          [],
            "excluded_count":    0,
            "message": (
                f"유니버스 내 모든 섹터의 포트폴리오 비중이 "
                f"{SECTOR_UNDERWEIGHT_PCT}% 이상입니다. 현재 분산 상태가 양호합니다."
            ),
            "disclaimer": (
                "이 분석은 참고 정보이며 투자 권유가 아닙니다. "
                "실제 매수 여부는 본인이 판단·결정하세요."
            ),
        }

    # ── 5. 후보 필터: 부족 섹터 + 점수 기준 + 미보유 ─────────────
    excluded: list[dict] = []   # 규칙상 제외/보류된 후보 (사유 기록 — Phase 0 관찰 데이터)
    candidates: list[dict] = []
    for item in screened.get("results", []):
        if item["total_score"] < BUY_SCORE_MIN:
            break   # 점수 내림차순 — 이후는 모두 기준 미달
        if item["ticker"] in held_tickers:
            continue
        if item["sector"] not in deficit_sectors:
            continue
        if item.get("is_turnaround"):
            # 흑자전환★ 종목 매수 제외 (2단계 확정 수치 — SK스퀘어 점수 왜곡 사례의 코드화)
            # 루프 안에서 걸러지므로 빈 자리는 다음 순위 종목이 자동으로 채운다.
            excluded.append({
                "ticker": item["ticker"],
                "name":   item["name"],
                "score":  item["total_score"],
                "sector": item["sector"],
                "market": "KR" if _is_korean(item["ticker"]) else "US",
                "reason": "흑자전환★ 제외 — 성장률 기저효과로 점수 왜곡 가능",
            })
            continue
        candidates.append({
            "ticker":      item["ticker"],
            "name":        item["name"],
            "score":       item["total_score"],
            "sector":      item["sector"],
            "market":      "KR" if _is_korean(item["ticker"]) else "US",
            "reason":      f"부족섹터 {item['sector']} 보강",
            "key_metrics": item["key_metrics"],
        })
        if len(candidates) >= MAX_CANDIDATES:
            break

    _attach_trend_warning(candidates)

    # 하락 추세 후보 매수 보류 (2단계 확정 수치)
    # 빈 자리를 다음 순위로 채우지 않는다 — 하락장에는 후보가 적은 것이 자연스럽다 (설계 결정).
    _kept: list[dict] = []
    for c in candidates:
        tw = c.get("trend_warning")
        if tw and "하락 추세" in tw:
            c["reason"] = "하락 추세 — 매수 보류"
            excluded.append(c)
        else:
            _kept.append(c)
    candidates = _kept

    return {
        "rule":  "B — 분산 채우기",
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "buy_score_min":          BUY_SCORE_MIN,
            "max_candidates":         MAX_CANDIDATES,
            "sector_underweight_pct": SECTOR_UNDERWEIGHT_PCT,
            "market":                 market,
            "universe_limit":         universe_limit,
        },
        "portfolio_sectors": portfolio_sector_pct,
        "deficit_sectors":   sorted(deficit_sectors),
        "held_tickers":      sorted(held_tickers),
        "candidates":        candidates,
        "count":             len(candidates),
        "excluded":          excluded,
        "excluded_count":    len(excluded),
        "universe_cache_note": screened.get("universe_cache_note"),
        "disclaimer": (
            "이 목록은 규칙 기반 참고 정보이며 투자 권유가 아닙니다. "
            "실제 매수 여부는 본인이 판단·결정하세요. 미래 가격을 예측하지 않습니다."
        ),
    }


# ═══════════════════════════════════════════════
# 13. check_guardrails  (자동매매 가드레일 검사)
# ═══════════════════════════════════════════════

def check_guardrails(
    ticker: str,
    order_amount_krw: int,
    *,
    side: str = "BUY",
    market: str | None = None,
    sector: str | None = None,
    accumulated_krw: int = 0,
    ticker_accumulated_krw: int = 0,
    sector_accumulated_krw: int = 0,
    daily_trades: int = 0,
    daily_pnl_pct: float = 0.0,
    cumulative_pnl_pct: float = 0.0,
    now: datetime | None = None,
) -> dict:
    """
    자동매매 주문 전 가드레일(안전장치)을 순서대로 검사한다.
    실제 주문은 내지 않는다. "통과/차단 + 사유"만 반환한다.

    Parameters
    ----------
    ticker                 : str   종목코드 (한국 6자리 / 미국 티커)
    order_amount_krw       : int   이번 주문 금액 (원화 기준)
    side                   : str   "BUY"(기본) | "SELL".
                                   결정 2: 매도는 금액 한도 면제 (거래시간/kill switch만 검사)
    market                 : str   "KR" | "US" | None — None이면 ticker에서 자동 감지
    sector                 : str   섹터명. 없으면 섹터 비중 검사를 건너뜀.
    accumulated_krw        : int   해당 규칙의 오늘 누적 매수 금액(규칙별, 이번 주문 제외, 원화)
                                   — 결정 3: 금액 한도는 규칙별로 각각 적용
    ticker_accumulated_krw : int   해당 규칙의 이 종목 오늘 누적 매수 금액(규칙별, 이번 주문 제외)
    sector_accumulated_krw : int   해당 규칙의 이 섹터 오늘 누적 매수 금액(규칙별, 이번 주문 제외)
    daily_trades           : int   오늘 매수 주문 횟수 (A+B 합산 — 결정 1, 이번 주문 제외)
    daily_pnl_pct          : float 오늘 자동매매 손익률 (%)
    cumulative_pnl_pct     : float 누적 손익률 (%)
    now                    : datetime  테스트용 시각 주입. None이면 KST 현재 시각 사용.
                             naive datetime은 KST로 간주하고, tz가 있으면 KST로 변환한다.

    검사 순서 (하나라도 차단되면 즉시 반환)
    ----------------------------------------
    0. 거래일     : 주말이면 차단. US는 뉴욕 현지 날짜 기준으로 판단.
    1. 거래시간   : KR 09:00~12:00 (KST) / US 09:30~12:30 (뉴욕 현지시간 — 서머타임 자동 반영)
    2. 자동매매한도: 누적 + 이번 ≤ 3,000만 원
    3. 1종목비중  : 이 종목 누적 + 이번 ≤ 한도의 20% (600만 원)
    4. 1섹터비중  : 이 섹터 누적 + 이번 ≤ 한도의 40% (1,200만 원)
    5. 하루거래횟수: 오늘 거래 횟수 + 1 ≤ 5회
    6. kill switch: 일일 손익 ≤ -5% 또는 누적 손익 ≤ -15%

    Returns
    -------
    dict
        {
            "passed"     : bool,         # True = 모든 검사 통과
            "blocked_by" : str | None,   # 차단된 검사 이름 (통과 시 None)
            "reason"     : str | None,   # 차단 사유 (통과 시 None)
            "checks"     : [             # 검사별 상세
                {"name": str, "passed": bool, "detail": str}
            ],
        }
        실패 시: {"error": "..."}
    """
    from datetime import time as _time

    # ── 임계값 상수 ────────────────────────────────────────────────
    LIMIT_KRW           = 30_000_000   # 자동매매 규칙별 한도 (3,000만 원 — 결정 3: 금액은 규칙별, 횟수만 A+B 합산)
    TICKER_WEIGHT_PCT   = 20           # 1종목 최대 비중 (%)
    SECTOR_WEIGHT_PCT   = 40           # 1섹터 최대 비중 (%)
    MAX_DAILY_TRADES    = 5            # 하루 최대 거래 횟수
    KILL_DAILY_PCT      = -5.0         # 일일 kill switch 손익률 (%)
    KILL_CUMULATIVE_PCT = -15.0        # 누적 kill switch 손익률 (%)

    # 거래 허용 시간 (한국시간 기준, 장 초반 3시간)
    KR_OPEN  = _time(9,  0)    # 한국장 시작
    KR_CLOSE = _time(12, 0)    # 한국장 검사 종료
    # 미국장은 뉴욕 현지시간으로 직접 판정한다 — 서머타임 자동 반영.
    # KST 환산: 서머타임 기간 22:30~01:30 / 해제 기간 23:30~02:30
    US_OPEN_NY  = _time(9, 30)    # 미국장 시작 (뉴욕 현지시간)
    US_CLOSE_NY = _time(12, 30)   # 미국장 검사 종료 (뉴욕 현지시간, 장 초반 3시간)

    # ── 파생 상수 ──────────────────────────────────────────────────
    TICKER_LIMIT_KRW = LIMIT_KRW * TICKER_WEIGHT_PCT // 100   # 6,000,000
    SECTOR_LIMIT_KRW = LIMIT_KRW * SECTOR_WEIGHT_PCT // 100   # 12,000,000

    # ── 입력 검증 ─────────────────────────────────────────────────
    ticker = ticker.strip()
    if not ticker:
        return {"error": "ticker가 비어 있습니다."}
    if not isinstance(order_amount_krw, (int, float)) or order_amount_krw <= 0:
        return {"error": f"order_amount_krw는 양수여야 합니다. 입력: {order_amount_krw}"}

    side = side.upper().strip()
    if side not in ("BUY", "SELL"):
        return {"error": f"side는 'BUY' 또는 'SELL' 이어야 합니다. 입력: '{side}'"}

    if market is None:
        market = "KR" if _is_korean(ticker) else "US"
    market = market.upper().strip()
    if market not in ("KR", "US"):
        return {"error": f"market은 'KR' 또는 'US' 이어야 합니다. 입력: '{market}'"}

    # ── 현재 시각 (항상 KST 기준) ─────────────────────────────────
    try:
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
    except ImportError:
        from datetime import timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))

    if now is None:
        now_dt = datetime.now(kst)
    elif now.tzinfo is None:
        now_dt = now.replace(tzinfo=kst)   # naive → KST로 간주
    else:
        now_dt = now.astimezone(kst)       # tz 포함 → KST로 변환
    now_t = now_dt.time()

    # US: 뉴욕 현지시간으로 변환 (서머타임 자동 반영).
    # 시간대 로드에 실패하면 근사치로 판정하지 않고 중단한다 (fail-safe).
    now_ny = None
    if market == "US":
        try:
            from zoneinfo import ZoneInfo
            now_ny = now_dt.astimezone(ZoneInfo("America/New_York"))
        except Exception as e:
            return {
                "error": (
                    "미국 동부 시간대(America/New_York) 로드에 실패했습니다. "
                    f"근사치로 거래시간을 판정할 수 없어 중단합니다: {e}"
                )
            }

    checks: list[dict] = []

    def _check(name: str, passed: bool, detail: str) -> bool:
        checks.append({"name": name, "passed": passed, "detail": detail})
        return passed

    def _block(by: str, reason: str) -> dict:
        return {"passed": False, "blocked_by": by, "reason": reason, "checks": checks}

    # ── 0. 거래일 (주말 차단) ────────────────────────────────────
    # US는 뉴욕 현지 날짜 기준으로 판단 (KST 새벽이라도 뉴욕은 전날 낮)
    session_day = now_dt if market == "KR" else now_ny
    _WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
    session_weekday = session_day.weekday()   # 0=월, 6=일
    is_trading_day  = session_weekday < 5
    detail = (
        f"세션 기준일 {session_day.strftime('%Y-%m-%d')}"
        f"({_WEEKDAY_KO[session_weekday]}{', 뉴욕 현지' if market == 'US' else ''}) — "
        f"{'거래일(월~금)' if is_trading_day else '주말'}"
    )
    if not _check("거래일", is_trading_day, detail):
        return _block("거래일", f"주말은 거래일이 아닙니다 — {detail}")

    # ── 1. 거래 시간 ─────────────────────────────────────────────
    if market == "KR":
        in_hours = KR_OPEN <= now_t <= KR_CLOSE
        detail = (
            f"한국장 허용 시간{'내' if in_hours else '외'} "
            f"(허용: {KR_OPEN.strftime('%H:%M')}~{KR_CLOSE.strftime('%H:%M')} KST, "
            f"현재: {now_t.strftime('%H:%M')})"
        )
    else:  # US — 뉴욕 현지시간 기준 (서머타임 자동 반영)
        ny_t = now_ny.time()
        in_hours = US_OPEN_NY <= ny_t <= US_CLOSE_NY
        detail = (
            f"미국장 허용 시간{'내' if in_hours else '외'} "
            f"(허용: {US_OPEN_NY.strftime('%H:%M')}~{US_CLOSE_NY.strftime('%H:%M')} 뉴욕 현지시간, "
            f"현재: 뉴욕 {ny_t.strftime('%H:%M')} / KST {now_t.strftime('%H:%M')})"
        )
    if not _check("거래시간", in_hours, detail):
        return _block("거래시간", detail)

    # ── 2~4. 금액 검사 3종 — 매도(SELL)는 면제 (결정 2) ──────────
    if side == "SELL":
        _check("자동매매한도", True, "매도 주문 — 금액 한도 면제 (결정 2) — 검사 생략")
        _check("1종목비중",   True, "매도 주문 — 금액 한도 면제 (결정 2) — 검사 생략")
        _check("1섹터비중",   True, "매도 주문 — 금액 한도 면제 (결정 2) — 검사 생략")
    else:
        # ── 2. 자동매매 한도 ─────────────────────────────────────
        total_after = accumulated_krw + order_amount_krw
        within_limit = total_after <= LIMIT_KRW
        detail = (
            f"누적 {accumulated_krw:,}원 + 이번 {order_amount_krw:,}원 = "
            f"{total_after:,}원 / 한도 {LIMIT_KRW:,}원"
        )
        if not _check("자동매매한도", within_limit, detail):
            return _block("자동매매한도", f"자동매매 한도 초과 — {detail}")

        # ── 3. 1종목 비중 ─────────────────────────────────────────
        ticker_after = ticker_accumulated_krw + order_amount_krw
        within_ticker = ticker_after <= TICKER_LIMIT_KRW
        detail = (
            f"{ticker} 누적 {ticker_accumulated_krw:,}원 + 이번 {order_amount_krw:,}원 = "
            f"{ticker_after:,}원 / 종목 한도 {TICKER_LIMIT_KRW:,}원 "
            f"(전체 한도의 {TICKER_WEIGHT_PCT}%)"
        )
        if not _check("1종목비중", within_ticker, detail):
            return _block("1종목비중", f"1종목 비중 {TICKER_WEIGHT_PCT}% 초과 — {detail}")

        # ── 4. 1섹터 비중 ─────────────────────────────────────────
        if sector is not None:
            sector_after = sector_accumulated_krw + order_amount_krw
            within_sector = sector_after <= SECTOR_LIMIT_KRW
            detail = (
                f"섹터 '{sector}' 누적 {sector_accumulated_krw:,}원 + 이번 {order_amount_krw:,}원 = "
                f"{sector_after:,}원 / 섹터 한도 {SECTOR_LIMIT_KRW:,}원 "
                f"(전체 한도의 {SECTOR_WEIGHT_PCT}%)"
            )
            if not _check("1섹터비중", within_sector, detail):
                return _block("1섹터비중", f"1섹터 비중 {SECTOR_WEIGHT_PCT}% 초과 — {detail}")
        else:
            _check("1섹터비중", True, "섹터 미입력 — 검사 생략")

    # ── 5. 하루 거래 횟수 ─────────────────────────────────────────
    trades_after = daily_trades + 1
    within_trades = trades_after <= MAX_DAILY_TRADES
    detail = (
        f"기존 {daily_trades}회 + 이번 1회 = {trades_after}회 / "
        f"일일 한도 {MAX_DAILY_TRADES}회"
    )
    if not _check("하루거래횟수", within_trades, detail):
        return _block("하루거래횟수", f"하루 거래 횟수 초과 — {detail}")

    # ── 6. Kill switch ────────────────────────────────────────────
    kill_daily = daily_pnl_pct   <= KILL_DAILY_PCT
    kill_cum   = cumulative_pnl_pct <= KILL_CUMULATIVE_PCT
    kill_hit   = kill_daily or kill_cum

    reasons = []
    if kill_daily:
        reasons.append(f"일일 손익 {daily_pnl_pct:+.2f}% ≤ {KILL_DAILY_PCT:+.1f}%")
    if kill_cum:
        reasons.append(f"누적 손익 {cumulative_pnl_pct:+.2f}% ≤ {KILL_CUMULATIVE_PCT:+.1f}%")

    if kill_hit:
        kill_detail = " / ".join(reasons)
        _check("kill_switch", False, kill_detail)
        return _block("kill_switch", f"Kill switch 발동 — {kill_detail}")
    else:
        kill_detail = (
            f"일일 {daily_pnl_pct:+.2f}% (임계값 {KILL_DAILY_PCT:+.1f}%), "
            f"누적 {cumulative_pnl_pct:+.2f}% (임계값 {KILL_CUMULATIVE_PCT:+.1f}%)"
        )
        _check("kill_switch", True, kill_detail)

    return {"passed": True, "blocked_by": None, "reason": None, "checks": checks}


# ═══════════════════════════════════════════════
# 14. log_trade / get_trade_log / summarize_trades_by_rule
# ═══════════════════════════════════════════════

def log_trade(
    rule_tag: str,
    ticker: str,
    side: str,
    qty: int,
    price: float,
    reason: str,
    sector: str | None = None,
    source_rule: str | None = None,
) -> dict:
    """
    거래 1건을 TRADE_LOG_PATH(trade_log.json)에 기록한다.

    Parameters
    ----------
    rule_tag : str   "A" | "B" | "MANUAL" | "SELL"
                     (SELL = 매도 규칙의 자동 매도 기록 전용)
    ticker   : str   종목코드 (한국 6자리 / 미국 티커)
    side     : str   "BUY" | "SELL"
    qty      : int   수량 (1 이상)
    price    : float 체결 단가
    reason   : str   기록 사유 (자유 텍스트)
    sector   : str | None  섹터명 (선택). get_auto_trade_stats_today의
                           by_sector 집계에 쓰인다. 없으면 None으로 저장.
    source_rule : str | None
                  SELL 태그 매도가 어느 규칙(A/B)의 보유분을 판 것인지
                  귀속시키는 필드. 매도 기록 시 반드시 전달할 것
                  (A/B 손익 비교와 kill switch 규칙별 기록에 필요).
                  허용값: None 또는 "A"/"B".

    Returns
    -------
    dict  {"logged": True, "entry": {...}}
          실패 시: {"error": "..."}
    """
    import json
    import os

    rule_tag = rule_tag.upper().strip()
    if rule_tag not in _VALID_RULE_TAGS:
        return {
            "error": (
                f"잘못된 rule_tag: '{rule_tag}'. "
                f"허용값: {sorted(_VALID_RULE_TAGS)}"
            )
        }

    side = side.upper().strip()
    if side not in ("BUY", "SELL"):
        return {"error": f"side는 'BUY' 또는 'SELL' 이어야 합니다. 입력: '{side}'"}

    if source_rule is not None:
        source_rule = source_rule.upper().strip()
        if source_rule not in ("A", "B"):
            return {
                "error": (
                    f"잘못된 source_rule: '{source_rule}'. "
                    "None 또는 'A'/'B'만 허용합니다."
                )
            }

    if not isinstance(qty, int) or qty < 1:
        return {"error": f"qty는 1 이상 정수여야 합니다. 입력: {qty}"}

    # 타임스탬프 KST 명시 — check_guardrails·get_auto_trade_stats_today와 같은 ZoneInfo 패턴
    try:
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
    except ImportError:
        from datetime import timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))

    entry = {
        # tz 포함 isoformat은 "+09:00" 접미사가 붙지만
        # get_auto_trade_stats_today의 startswith(날짜) 필터에는 영향 없다.
        "timestamp": datetime.now(kst).isoformat(timespec="seconds"),
        "rule_tag":  rule_tag,
        "ticker":    ticker.strip(),
        "side":      side,
        "qty":       qty,
        "price":     price,
        "reason":    reason,
        "sector":    sector,
        # SELL 태그 매도가 어느 규칙(A/B)의 보유분을 판 것인지 귀속시키는 필드.
        # 매도 기록 시 반드시 전달할 것 (A/B 손익 비교와 kill switch 규칙별 기록에 필요).
        "source_rule": source_rule,
    }

    # 기존 장부 읽기 — 손상된 장부는 절대 덮어쓰지 않는다 (fail-safe)
    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
    except FileNotFoundError:
        records = []  # 첫 기록이므로 정상
    except json.JSONDecodeError as exc:
        return {"error": f"trade_log.json 파싱 실패 — 장부가 손상됐을 수 있어 기록을 중단합니다. "
                         f"파일을 직접 확인·복구한 뒤 다시 시도하세요: {exc}"}
    if not isinstance(records, list):
        return {"error": f"trade_log.json 파싱 실패 — 장부가 손상됐을 수 있어 기록을 중단합니다. "
                         f"파일을 직접 확인·복구한 뒤 다시 시도하세요: 최상위가 list가 아님 ({type(records).__name__})"}

    records.append(entry)

    # 원자적 쓰기: 임시 파일에 먼저 쓰고 os.replace로 교체 (쓰다가 죽어도 기존 파일 보존)
    tmp_path = TRADE_LOG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, TRADE_LOG_PATH)
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return {"error": f"로그 파일 쓰기 실패: {exc}"}

    return {"logged": True, "entry": entry}


def get_trade_log(rule_tag: str | None = None) -> dict:
    """
    거래 로그를 반환한다.

    Parameters
    ----------
    rule_tag : str | None
        None이면 전체, 지정하면 해당 규칙 것만 필터링.
        허용값: "A" | "B" | "MANUAL" | "SELL"

    Returns
    -------
    dict  {"rule_tag": str | None, "count": int, "trades": [...]}
          실패 시: {"error": "..."}
    """
    import json

    if rule_tag is not None:
        rule_tag = rule_tag.upper().strip()
        if rule_tag not in _VALID_RULE_TAGS:
            return {
                "error": (
                    f"잘못된 rule_tag: '{rule_tag}'. "
                    f"허용값: {sorted(_VALID_RULE_TAGS)}"
                )
            }

    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            records = []
    except FileNotFoundError:
        records = []
    except json.JSONDecodeError as exc:
        return {"error": f"로그 파일 파싱 실패: {exc}"}
    except Exception as exc:
        return {"error": f"로그 파일 읽기 실패: {exc}"}

    if rule_tag is not None:
        records = [r for r in records if r.get("rule_tag") == rule_tag]

    return {
        "rule_tag": rule_tag,
        "count":    len(records),
        "trades":   records,
    }


def summarize_trades_by_rule() -> dict:
    """
    거래 로그를 rule_tag별(A / B / MANUAL / SELL)로 묶어 요약한다.
    SELL = 매도 규칙 자동매도(손절 -10% / 익절 +20%) 태그로, 집계 대상에 포함된다.

    Returns
    -------
    dict
        {
            "total_trades": int,
            "by_rule": {
                "<rule_tag>": {
                    "trades":      int,
                    "buy_count":   int,
                    "sell_count":  int,
                    "tickers":     list[str],   # 중복 제거, 정렬
                }
            }
        }
        실패 시: {"error": "..."}
    """
    log = get_trade_log()
    if "error" in log:
        return log

    summary: dict[str, dict] = {
        tag: {"trades": 0, "buy_count": 0, "sell_count": 0, "tickers": set()}
        for tag in _VALID_RULE_TAGS
    }

    for trade in log["trades"]:
        tag  = trade.get("rule_tag", "")
        side = trade.get("side", "")
        if tag not in summary:
            continue
        summary[tag]["trades"] += 1
        if side == "BUY":
            summary[tag]["buy_count"]  += 1
        elif side == "SELL":
            summary[tag]["sell_count"] += 1
        summary[tag]["tickers"].add(trade.get("ticker", ""))

    # set → 정렬 리스트
    by_rule = {
        tag: {
            "trades":     v["trades"],
            "buy_count":  v["buy_count"],
            "sell_count": v["sell_count"],
            "tickers":    sorted(v["tickers"]),
        }
        for tag, v in summary.items()
    }

    return {
        "total_trades": log["count"],
        "by_rule":      by_rule,
    }


def get_auto_trade_stats_today(rule_tag: str) -> dict:
    """
    오늘(KST) 자동매매 규칙별 누적치를 거래 로그에서 집계한다. (읽기 전용)

    check_guardrails가 요구하는 "오늘 누적치" 입력을 조립 단계에서
    손으로 계산하다 실수하는 것(매도 금액 합산, 어제 거래 포함 등)을
    막기 위한 함수다. 주문 관련 코드는 일절 없다.

    집계 기준
    ---------
    - "오늘" 판단은 KST 기준 날짜다 (timestamp가 오늘 날짜로 시작하는 기록만).
    - daily_trades    : 오늘 이 규칙의 거래 횟수 (BUY + SELL 모두).
    - daily_buy_trades: 매수 횟수만, 일 5회 상한 입력용 (설계 결정 1).
    - accumulated_krw : rule_tag가 "A"/"B"면 매수(BUY)만 qty × price 합산
                        (가드레일 한도는 "산 금액" 기준이므로 매도는 제외).
                        rule_tag가 "SELL"이면 매도(SELL)만 합산
                        (감시·기록용 — 횟수 상한에는 미사용, 로드맵 설계 결정 1 참조).
    - 미국 종목(6자리 코드가 아닌 티커)의 price는 USD이므로
      _fetch_usd_krw_rate()로 KRW 환산한다. 오늘 미국 매수가 있는데
      환율 조회가 실패하면 근사치를 쓰지 않고 {"error": "..."}를 반환한다
      (가드레일 입력이 부정확하면 안 되므로).

    Parameters
    ----------
    rule_tag : str  "A" | "B" | "SELL" 만 허용 (규칙별 한도가 각각이므로)

    Returns
    -------
    dict
        {
            "date":            "YYYY-MM-DD" (KST),
            "rule_tag":        str,
            "daily_trades":    int,
            "accumulated_krw": int,
            "by_ticker":       {ticker: 누적 KRW},
            "by_sector":       {sector: 누적 KRW},  # sector 미기록은 "기타/미분류"
            "usd_krw":         float | None,        # 환산에 쓴 환율 (미국 매수 없으면 None)
        }
        실패 시: {"error": "..."}

    check_guardrails 연결 예시
    --------------------------
        stats = get_auto_trade_stats_today("A")
        if "error" in stats:
            ...  # 집계 실패 시 주문 진행 금지
        result = check_guardrails(
            ticker, order_amount_krw,
            sector=sector,
            accumulated_krw=stats["accumulated_krw"],
            ticker_accumulated_krw=stats["by_ticker"].get(ticker, 0),
            sector_accumulated_krw=stats["by_sector"].get(sector, 0),
            daily_trades=stats["daily_trades"],
        )
    """
    rule_tag = rule_tag.upper().strip()
    if rule_tag not in ("A", "B", "SELL"):
        return {
            "error": (
                f"잘못된 rule_tag: '{rule_tag}'. "
                "get_auto_trade_stats_today는 'A', 'B', 'SELL'만 허용합니다."
            )
        }

    # 오늘 날짜 (KST 기준) — check_guardrails와 같은 ZoneInfo 패턴
    try:
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
    except ImportError:
        from datetime import timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")

    log = get_trade_log(rule_tag)
    if "error" in log:
        return log

    todays = [
        t for t in log["trades"]
        if str(t.get("timestamp", "")).startswith(today_str)
    ]
    target_side = "SELL" if rule_tag == "SELL" else "BUY"
    trades_to_sum = [t for t in todays if t.get("side") == target_side]

    # 미국 거래가 있으면 환율 필요 — 실패 시 근사치 대신 에러
    usd_krw = None
    if any(not _is_korean(str(t.get("ticker", ""))) for t in trades_to_sum):
        usd_krw = _fetch_usd_krw_rate()
        if usd_krw is None:
            return {
                "error": (
                    "오늘 미국 종목 매수 기록이 있는데 USD/KRW 환율 조회에 "
                    "실패했습니다. 누적 금액을 정확히 계산할 수 없어 집계를 "
                    "중단합니다. (가드레일 입력이 부정확하면 안 됨)"
                )
            }

    accumulated_krw = 0
    by_ticker: dict[str, int] = {}
    by_sector: dict[str, int] = {}

    for t in trades_to_sum:
        ticker = str(t.get("ticker", ""))
        amount = t.get("qty", 0) * t.get("price", 0)
        if not _is_korean(ticker):
            amount *= usd_krw
        amount = int(round(amount))

        accumulated_krw += amount
        by_ticker[ticker] = by_ticker.get(ticker, 0) + amount
        sector = t.get("sector") or "기타/미분류"
        by_sector[sector] = by_sector.get(sector, 0) + amount

    return {
        "date":            today_str,
        "rule_tag":        rule_tag,
        "daily_trades":    len(todays),
        "daily_buy_trades": sum(1 for t in todays if t.get("side") == "BUY"),
        "accumulated_krw": accumulated_krw,
        "by_ticker":       by_ticker,
        "by_sector":       by_sector,
        "usd_krw":         usd_krw,
    }


def get_combined_auto_trade_stats_today() -> dict:
    """
    오늘(KST) 규칙 A와 B의 누적치를 합산해 반환한다. (읽기 전용)

    가드레일 입력 사용 기준 (설계 결정 1·3)
    ----------------------------------------
    - 설계 결정 1: 하루 거래 횟수 상한(5회)은 "매수"만, A+B 합산으로 센다
      → 가드레일 daily_trades 입력은 이 함수의 daily_buy_trades(합산)를 쓴다.
    - 설계 결정 3: 금액 한도(총 3,000만·종목 600만·섹터 1,200만)는 규칙별로
      각각 적용한다 (로드맵 자금 배분: 규칙별 각 3,000만, A+B 총 6,000만)
      → 가드레일 금액 입력(accumulated_krw 등)은 per_rule[rule_tag]에서 가져온다.
    - 최상위 합산치(accumulated_krw·by_ticker·by_sector)는 감시·기록용이다.
      가드레일 금액 입력으로 쓰지 않는다 (쓰면 A/B가 예산 경쟁을 해 실험이 깨짐).

    조립 계층(auto_trader.py)에서 A·B를 각각 집계해 손으로 더하다
    실수하는 것을 막기 위한 함수다. 주문 코드 없음.

    fail-safe: A와 B 중 한쪽이라도 집계에 실패하면 합산을 중단하고
    {"error": "..."}를 반환한다 (부정확한 가드레일 입력 금지).

    Returns
    -------
    dict
        {
            "date":             "YYYY-MM-DD" (KST),
            "rule_tag":         "A+B",
            "daily_buy_trades": int,                 # A+B 매수 횟수 합
            "accumulated_krw":  int,                 # A+B 매수 금액 합
            "by_ticker":        {ticker: 누적 KRW},  # 키별 병합 합산
            "by_sector":        {sector: 누적 KRW},  # 키별 병합 합산
            "usd_krw":          float | None,        # A 우선, 없으면 B
            "per_rule":         {"A": 원본 dict, "B": 원본 dict},
        }
        실패 시: {"error": "..."}
    """
    per_rule = {}
    for tag in ("A", "B"):
        stats = get_auto_trade_stats_today(tag)
        if "error" in stats:
            return {"error": f"규칙 {tag} 집계 실패 → 합산 중단: {stats['error']}"}
        per_rule[tag] = stats

    a, b = per_rule["A"], per_rule["B"]

    by_ticker: dict[str, int] = dict(a["by_ticker"])
    for ticker, amount in b["by_ticker"].items():
        by_ticker[ticker] = by_ticker.get(ticker, 0) + amount

    by_sector: dict[str, int] = dict(a["by_sector"])
    for sector, amount in b["by_sector"].items():
        by_sector[sector] = by_sector.get(sector, 0) + amount

    return {
        "date":             a["date"],
        "rule_tag":         "A+B",
        "daily_buy_trades": a["daily_buy_trades"] + b["daily_buy_trades"],
        "accumulated_krw":  a["accumulated_krw"] + b["accumulated_krw"],
        "by_ticker":        by_ticker,
        "by_sector":        by_sector,
        "usd_krw":          a["usd_krw"] if a["usd_krw"] is not None else b["usd_krw"],
        "per_rule":         per_rule,
    }


# ═══════════════════════════════════════════════
# 킬 스위치 부품 (0단계 — 계산·상태 기록 전용, 주문 코드 없음)
# ═══════════════════════════════════════════════

# 킬 스위치 손익률 분모 (확정 설계 — 변경 금지)
# 차단 판정은 A+B 합산 1개 (분모 6,000만 원 고정).
# 규칙별(A/B) 손익률은 기록·모니터링용이며 차단 판정에는 쓰지 않는다 (분모 각 3,000만 원).
_KS_TOTAL_BASE_KRW = 60_000_000
_KS_RULE_BASE_KRW = 30_000_000
_KS_DAILY_LIMIT_PCT = -5.0        # check_guardrails KILL_DAILY_PCT와 동일 값
_KS_CUMULATIVE_LIMIT_PCT = -15.0  # check_guardrails KILL_CUMULATIVE_PCT와 동일 값
_KS_RESET_CONFIRM_PHRASE = "누적 킬스위치를 해제합니다"
# 킬 스위치 집계 대상 태그 — MANUAL은 사람 직접 거래라 자동매매 손익에서 제외
_KS_AUTO_TAGS = ("A", "B", "SELL")


def _resolve_kst_now(now: datetime | None) -> datetime:
    """now 인자를 KST 기준 aware datetime으로 정규화한다.
    naive는 KST로 간주, tz가 있으면 KST로 변환 (check_guardrails와 동일 규약)."""
    try:
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
    except ImportError:
        from datetime import timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))
    if now is None:
        return datetime.now(kst)
    if now.tzinfo is None:
        return now.replace(tzinfo=kst)
    return now.astimezone(kst)


def get_auto_trade_positions() -> dict:
    """
    거래 로그를 처음부터 재생해 자동매매분(rule_tag A/B/SELL)의
    순보유 포지션과 실현손익을 계산한다. (읽기 전용 — 주문 코드 없음)

    계산 방식
    ---------
    - 이동평균법: BUY 시 평단 = (기존 평단×기존수량 + 체결가×수량) ÷ 합계수량.
      SELL 시 실현손익 += (체결가 − 평단) × 수량, 수량만 차감 (평단 유지).
    - 귀속: BUY는 rule_tag("A"/"B")의 포지션에 더하고,
      rule_tag="SELL"인 매도는 entry의 source_rule("A"/"B") 포지션에서 차감한다.
    - source_rule이 없거나 None인 SELL은 규칙별(by_rule) 계산에서 제외하고
      warnings에 기록하되, 합산(combined) 계산에는 포함한다
      (합산 포지션은 규칙 구분 없이 전체 BUY/SELL 재생).
    - MANUAL 거래는 자동매매분이 아니므로 전부 제외한다.
    - 통화: 한국 종목 실현손익은 realized_pnl_krw(KRW), 미국 종목은
      realized_pnl_usd(USD)로 따로 누적한다. 각 SELL 시점의 환율을 알 수 없으므로
      KRW 일괄 환산은 calc_auto_trade_pnl()이 현재 환율로 수행한다.

    fail-safe: 매도 수량이 보유 수량을 초과(oversell)하면 로그 데이터 불일치로
    보고 {"error": "..."}를 반환한다 — 킬 스위치 입력이 부정확해지면 안 되므로
    부분 결과로 계속 진행하지 않는다.

    Returns
    -------
    dict
        {
            "combined": {"positions": {ticker: {"qty": int, "avg_price": float,
                                                "market": "KR"|"US"}},
                         "realized_pnl_krw": float, "realized_pnl_usd": float},
            "by_rule":  {"A": {...같은 구조...}, "B": {...}},
            "warnings": [str, ...],
        }
        실패 시: {"error": "..."}
        로그 파일이 없으면 정상 (빈 포지션, 실현손익 0).
    """
    log = get_trade_log()
    if "error" in log:
        return log

    def _new_book() -> dict:
        return {"positions": {}, "realized_pnl_krw": 0.0, "realized_pnl_usd": 0.0}

    books = {"combined": _new_book(), "A": _new_book(), "B": _new_book()}
    warnings_list: list[str] = []

    def _apply(book: dict, entry: dict, label: str) -> str | None:
        """entry 1건을 book에 반영한다. oversell이면 에러 메시지 문자열 반환."""
        ticker = str(entry.get("ticker", "")).strip()
        side = str(entry.get("side", "")).upper()
        qty = entry.get("qty", 0)
        price = entry.get("price", 0)
        market = "KR" if _is_korean(ticker) else "US"
        pos = book["positions"].get(ticker)
        if side == "BUY":
            if pos is None:
                pos = {"qty": 0, "avg_price": 0.0, "market": market}
                book["positions"][ticker] = pos
            total_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["avg_price"] * pos["qty"] + price * qty) / total_qty
            pos["qty"] = total_qty
        else:  # SELL
            held = pos["qty"] if pos else 0
            if qty > held:
                return (f"oversell: [{label}] {ticker} 매도 {qty}주 > 보유 {held}주 "
                        f"(timestamp={entry.get('timestamp')})")
            pnl = (price - pos["avg_price"]) * qty
            if market == "KR":
                book["realized_pnl_krw"] += pnl
            else:
                book["realized_pnl_usd"] += pnl
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del book["positions"][ticker]
        return None

    for entry in log["trades"]:
        tag = str(entry.get("rule_tag", "")).upper()
        if tag not in _KS_AUTO_TAGS:
            continue  # MANUAL 등 — 자동매매분이 아니므로 제외
        side = str(entry.get("side", "")).upper()
        ticker = str(entry.get("ticker", "")).strip()
        if side not in ("BUY", "SELL"):
            warnings_list.append(
                f"알 수 없는 side='{entry.get('side')}' 기록 제외: "
                f"{ticker} (timestamp={entry.get('timestamp')})"
            )
            continue

        # 합산(combined): 규칙 구분 없이 전체 BUY/SELL 재생
        err = _apply(books["combined"], entry, "합산")
        if err:
            return {"error": f"거래 로그 불일치({err}) — 킬 스위치 입력이 "
                             "부정확해지므로 전체 계산을 중단합니다."}

        # 규칙별(by_rule) 귀속
        if side == "BUY":
            rule = tag if tag in ("A", "B") else None
            if rule is None:
                warnings_list.append(
                    f"rule_tag='SELL'인 BUY 기록 — 규칙별 계산에서 제외 (합산에는 포함): "
                    f"{ticker} (timestamp={entry.get('timestamp')})"
                )
        else:
            if tag in ("A", "B"):
                rule = tag
            else:  # tag == "SELL" → source_rule로 귀속
                src = entry.get("source_rule")
                rule = src.upper().strip() if isinstance(src, str) else None
                if rule not in ("A", "B"):
                    rule = None
                    warnings_list.append(
                        f"source_rule이 없거나 잘못된 SELL(source_rule={src!r}) — "
                        f"규칙별 계산에서 제외 (합산에는 포함): "
                        f"{ticker} (timestamp={entry.get('timestamp')})"
                    )
        if rule is not None:
            err = _apply(books[rule], entry, f"규칙 {rule}")
            if err:
                return {"error": f"거래 로그 불일치({err}) — 킬 스위치 입력이 "
                                 "부정확해지므로 전체 계산을 중단합니다."}

    # float 노이즈 제거
    for book in books.values():
        for pos in book["positions"].values():
            pos["avg_price"] = round(pos["avg_price"], 4)
        book["realized_pnl_krw"] = round(book["realized_pnl_krw"], 4)
        book["realized_pnl_usd"] = round(book["realized_pnl_usd"], 4)

    return {
        "combined": books["combined"],
        "by_rule": {"A": books["A"], "B": books["B"]},
        "warnings": warnings_list,
    }


def calc_auto_trade_pnl(*, now: datetime | None = None,
                        price_overrides: dict[str, float] | None = None) -> dict:
    """
    자동매매분(rule_tag A/B/SELL)의 현재 손익을 계산한다. (읽기 전용 — 주문 코드 없음)

    킬 스위치 판정(update_kill_switch_state)의 입력을 만드는 함수다.
    분모는 확정 설계값을 쓴다:
    - 합산(combined) 손익률 분모: 60,000,000원 (A+B 합산 — 차단 판정용)
    - 규칙별(A/B) 손익률 분모:   30,000,000원 (기록·모니터링용, 차단 판정 미사용)

    Parameters
    ----------
    now             : datetime | None
                      테스트용 시각 주입. None이면 KST 현재 시각.
                      naive는 KST로 간주 (check_guardrails와 동일 규약).
    price_overrides : dict[str, float] | None
                      테스트 전용. 지정된 티커는 get_quote 대신 이 값을
                      현재가로 사용한다 (미국 종목은 USD 단위).

    fail-safe
    ---------
    - 보유 종목 현재가를 하나라도 못 구하면 {"error": "..."} — 근사치 금지.
    - 미국 종목 보유/실현손익/당일 거래가 있는데 환율 조회에 실패하면
      {"error": "..."} (get_auto_trade_stats_today와 같은 철학).

    Returns
    -------
    dict
        {
            "as_of":               str,    # KST isoformat
            "eval_krw":            float,  # 현재 평가액 합계 (합산)
            "realized_pnl_krw":    float,  # 실현손익 (USD분은 현재 환율로 환산 포함)
            "unrealized_pnl_krw":  float,  # 평가손익 = Σ (현재가 − 평단) × 수량
            "cumulative_pnl_krw":  float,  # 누적손익 = 실현 + 평가
            "cumulative_pnl_pct":  float,  # 누적손익 ÷ 60,000,000 × 100
            "today_buy_krw":       float,  # 오늘(KST) 매수금액 합 (A/B/SELL 태그만)
            "today_sell_krw":      float,  # 오늘(KST) 매도금액 합 (A/B/SELL 태그만)
            "by_rule": {"A": {"cumulative_pnl_krw", "cumulative_pnl_pct"}, "B": {...}},
            "warnings": [str, ...],
        }
        실패 시: {"error": "..."}
    """
    now_dt = _resolve_kst_now(now)
    today_str = now_dt.strftime("%Y-%m-%d")

    positions = get_auto_trade_positions()
    if "error" in positions:
        return positions
    combined = positions["combined"]
    by_rule = positions["by_rule"]

    # 현재가 조회 (price_overrides 우선) — 하나라도 실패하면 전체 중단
    tickers = set(combined["positions"])
    for book in by_rule.values():
        tickers |= set(book["positions"])
    prices: dict[str, float] = {}
    for ticker in sorted(tickers):
        if price_overrides is not None and ticker in price_overrides:
            prices[ticker] = float(price_overrides[ticker])
            continue
        quote = get_quote(ticker)
        if "error" in quote:
            return {"error": f"현재가 조회 실패({ticker}): {quote['error']} — "
                             "킬 스위치 입력이 부정확하면 안 되므로 계산을 중단합니다."}
        if quote.get("close") is None:
            return {"error": f"현재가 조회 실패({ticker}): close 없음 — "
                             "킬 스위치 입력이 부정확하면 안 되므로 계산을 중단합니다."}
        prices[ticker] = float(quote["close"])

    # 오늘(KST, now 기준) 자동매매분 거래
    log = get_trade_log()
    if "error" in log:
        return log
    todays = [
        t for t in log["trades"]
        if str(t.get("rule_tag", "")).upper() in _KS_AUTO_TAGS
        and str(t.get("timestamp", "")).startswith(today_str)
        and str(t.get("side", "")).upper() in ("BUY", "SELL")
    ]

    # 환율 필요 여부 — 미국 보유/실현손익/당일 거래가 하나라도 있으면 필요
    def _needs_fx(book: dict) -> bool:
        return (book["realized_pnl_usd"] != 0
                or any(p["market"] == "US" for p in book["positions"].values()))

    needs_fx = (_needs_fx(combined)
                or any(_needs_fx(b) for b in by_rule.values())
                or any(not _is_korean(str(t.get("ticker", ""))) for t in todays))
    usd_krw = None
    if needs_fx:
        usd_krw = _fetch_usd_krw_rate()
        if usd_krw is None:
            return {
                "error": (
                    "미국 종목 보유/실현손익/당일 거래가 있는데 USD/KRW 환율 조회에 "
                    "실패했습니다. 근사치 없이 계산을 중단합니다 "
                    "(킬 스위치 입력이 부정확하면 안 됨)."
                )
            }

    def _book_pnl_krw(book: dict) -> tuple[float, float, float]:
        """(평가액, 실현손익, 평가손익) — 전부 KRW."""
        eval_krw = 0.0
        unrealized = 0.0
        for ticker, pos in book["positions"].items():
            fx = usd_krw if pos["market"] == "US" else 1.0
            eval_krw += prices[ticker] * pos["qty"] * fx
            unrealized += (prices[ticker] - pos["avg_price"]) * pos["qty"] * fx
        realized = book["realized_pnl_krw"]
        if book["realized_pnl_usd"]:
            realized += book["realized_pnl_usd"] * usd_krw
        return eval_krw, realized, unrealized

    eval_krw, realized_krw, unrealized_krw = _book_pnl_krw(combined)
    cumulative_krw = realized_krw + unrealized_krw

    today_buy = 0.0
    today_sell = 0.0
    for t in todays:
        amount = t.get("qty", 0) * t.get("price", 0)
        if not _is_korean(str(t.get("ticker", ""))):
            amount *= usd_krw
        if str(t.get("side", "")).upper() == "BUY":
            today_buy += amount
        else:
            today_sell += amount

    rule_out = {}
    for tag in ("A", "B"):
        _, r_realized, r_unrealized = _book_pnl_krw(by_rule[tag])
        r_cum = r_realized + r_unrealized
        rule_out[tag] = {
            "cumulative_pnl_krw": round(r_cum, 2),
            "cumulative_pnl_pct": round(r_cum / _KS_RULE_BASE_KRW * 100, 4),
        }

    return {
        "as_of":              now_dt.isoformat(timespec="seconds"),
        "eval_krw":           round(eval_krw, 2),
        "realized_pnl_krw":   round(realized_krw, 2),
        "unrealized_pnl_krw": round(unrealized_krw, 2),
        "cumulative_pnl_krw": round(cumulative_krw, 2),
        "cumulative_pnl_pct": round(cumulative_krw / _KS_TOTAL_BASE_KRW * 100, 4),
        "today_buy_krw":      round(today_buy, 2),
        "today_sell_krw":     round(today_sell, 2),
        "by_rule":            rule_out,
        "warnings":           positions["warnings"],
    }


def _load_kill_switch_state() -> dict:
    """
    킬 스위치 상태 파일을 로드한다. 파일이 없으면 기본 구조 반환 (첫 사용 — 정상).
    파싱 실패면 {"error": ...} — 손상된 상태를 기본값으로 덮어쓰면 누적 발동
    기록이 사라질 수 있으므로 절대 새로 만들지 않는다 (fail-safe).
    """
    import json

    try:
        with open(KILL_SWITCH_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {
            "daily": {"date": None, "snapshot_eval_krw": 0.0,
                      "snapshot_today_buy_krw": 0.0, "snapshot_today_sell_krw": 0.0,
                      "triggered": False, "pnl_pct_at_trigger": None,
                      "triggered_at": None},
            "cumulative": {"triggered": False, "pnl_pct_at_trigger": None,
                           "triggered_at": None, "resets": []},
            "last_check": None,
        }
    except json.JSONDecodeError as exc:
        return {"error": f"킬 스위치 상태 파일 파싱 실패 — 상태가 손상됐을 수 있어 "
                         f"중단합니다. 파일을 직접 확인·복구하세요: {exc}"}
    if not isinstance(state, dict) or "daily" not in state or "cumulative" not in state:
        return {"error": "킬 스위치 상태 파일 구조 이상 — 상태가 손상됐을 수 있어 "
                         "중단합니다. 파일을 직접 확인·복구하세요."}
    return state


def _save_kill_switch_state(state: dict) -> str | None:
    """원자적 저장 (log_trade와 같은 tmp+replace 패턴). 실패 시 에러 메시지 반환."""
    import json
    import os

    tmp_path = KILL_SWITCH_STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, KILL_SWITCH_STATE_PATH)
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return f"킬 스위치 상태 파일 쓰기 실패: {exc}"
    return None


def update_kill_switch_state(*, now: datetime | None = None,
                             price_overrides: dict[str, float] | None = None) -> dict:
    """
    킬 스위치 상태를 갱신하고 check_guardrails 입력값을 반환한다. (주문 코드 없음)

    동작: calc_auto_trade_pnl()로 손익 계산 → 발동 판정(래칫) →
    KILL_SWITCH_STATE_PATH에 영속화 → 판정용 손익률 반환.

    래칫(ratchet) 방식 — 확정 설계
    ------------------------------
    - 차단 판정은 A+B 합산 1개: 일일 -5.0% / 누적 -15.0% (분모 6,000만 원 고정).
    - 한번 발동하면 손익이 회복돼도 발동 당시 값을 계속 반환한다.
      일일 발동은 KST 날짜가 바뀌면 자동 리셋되고,
      누적 발동은 reset_kill_switch()로만 해제된다.
    - 반환되는 daily_pnl_pct/cumulative_pnl_pct는 래칫 적용 후 값이다 —
      check_guardrails에 그대로 넣으면 발동 상태에서 자연히 차단된다.
    - 규칙별(A/B) 손익률은 기록·모니터링용으로만 반환하며 차단 판정에 쓰지 않는다.

    일일 손익 산식 (스냅샷 방식)
    ----------------------------
    KST 날짜가 바뀐 첫 호출에서 평가액과 당일 매수/매도 누계를 스냅샷하고,
    일일 손익 = (평가액 + 당일 매도 증가분) − (스냅샷 평가액 + 당일 매수 증가분).
    (스냅샷 시점 이전의 당일 거래 효과를 일일 손익에서 제외하기 위함)

    ⚠ 반환값에 "error"가 있으면 호출자는 주문을 전부 중단해야 한다.
      손익을 계산할 수 없으면 킬 스위치가 발동 상태인지 알 수 없으므로
      보수적으로 전면 중단이 맞다.

    Returns
    -------
    dict
        {
            "daily_pnl_pct":        float,  # 래칫 적용 후 값
            "cumulative_pnl_pct":   float,  # 래칫 적용 후 값
            "daily_triggered":      bool,
            "cumulative_triggered": bool,
            "by_rule":              {"A": pct, "B": pct},  # 기록·모니터링용
            "as_of":                str,
        }
        실패 시: {"error": "..."}
    """
    pnl = calc_auto_trade_pnl(now=now, price_overrides=price_overrides)
    if "error" in pnl:
        return pnl

    state = _load_kill_switch_state()
    if "error" in state:
        return state

    now_dt = _resolve_kst_now(now)
    today_str = now_dt.strftime("%Y-%m-%d")
    as_of = pnl["as_of"]

    # KST 날짜가 바뀌면 일일 섹션 자동 리셋 (파일 첫 생성 포함)
    daily = state["daily"]
    if daily.get("date") != today_str:
        daily = {
            "date":                    today_str,
            "snapshot_eval_krw":       pnl["eval_krw"],
            "snapshot_today_buy_krw":  pnl["today_buy_krw"],
            "snapshot_today_sell_krw": pnl["today_sell_krw"],
            "triggered":               False,
            "pnl_pct_at_trigger":      None,
            "triggered_at":            None,
        }
        state["daily"] = daily

    # 일일 손익 — 스냅샷 이후의 순변화만 반영
    daily_pnl_krw = (
        (pnl["eval_krw"] + (pnl["today_sell_krw"] - daily["snapshot_today_sell_krw"]))
        - (daily["snapshot_eval_krw"] + (pnl["today_buy_krw"] - daily["snapshot_today_buy_krw"]))
    )
    daily_pnl_pct = round(daily_pnl_krw / _KS_TOTAL_BASE_KRW * 100, 4)
    cumulative_pnl_pct = pnl["cumulative_pnl_pct"]

    # 래칫 판정 — 이미 발동돼 있으면 발동 당시 값으로 대체
    if daily["triggered"]:
        daily_pnl_pct = daily["pnl_pct_at_trigger"]
    elif daily_pnl_pct <= _KS_DAILY_LIMIT_PCT:
        daily["triggered"] = True
        daily["pnl_pct_at_trigger"] = daily_pnl_pct
        daily["triggered_at"] = as_of

    cumulative = state["cumulative"]
    if cumulative["triggered"]:
        cumulative_pnl_pct = cumulative["pnl_pct_at_trigger"]
    elif cumulative_pnl_pct <= _KS_CUMULATIVE_LIMIT_PCT:
        cumulative["triggered"] = True
        cumulative["pnl_pct_at_trigger"] = cumulative_pnl_pct
        cumulative["triggered_at"] = as_of

    by_rule_pct = {tag: pnl["by_rule"][tag]["cumulative_pnl_pct"] for tag in ("A", "B")}
    state["last_check"] = {
        "at":                 as_of,
        "daily_pnl_pct":      daily_pnl_pct,       # 래칫 적용 후 값 (반환값과 동일)
        "cumulative_pnl_pct": cumulative_pnl_pct,  # 래칫 적용 후 값 (반환값과 동일)
        "by_rule":            by_rule_pct,
    }

    err = _save_kill_switch_state(state)
    if err:
        return {"error": err}

    return {
        "daily_pnl_pct":        daily_pnl_pct,
        "cumulative_pnl_pct":   cumulative_pnl_pct,
        "daily_triggered":      daily["triggered"],
        "cumulative_triggered": cumulative["triggered"],
        "by_rule":              by_rule_pct,
        "as_of":                as_of,
    }


def reset_kill_switch(confirm: str) -> dict:
    """
    누적 킬 스위치 발동을 수동으로 해제한다.

    - confirm이 정확히 "누적 킬스위치를 해제합니다" 여야만 해제한다 (오조작 방지).
    - **일일 발동은 이 함수로 해제할 수 없다.** 일일 발동은 KST 날짜가 바뀌면
      update_kill_switch_state()가 자동 리셋하는 것이 유일한 해제 경로다.
    - 해제 이력은 cumulative.resets에 발동 당시 손익률과 함께 남긴다.

    Returns
    -------
    dict  {"reset": True, "cumulative": {...해제 후 상태...}}
          실패 시: {"error": "..."}
    """
    import os

    if confirm != _KS_RESET_CONFIRM_PHRASE:
        return {"error": "확인 문구 불일치 — 해제하지 않음"}

    if not os.path.exists(KILL_SWITCH_STATE_PATH):
        return {"error": "킬 스위치 상태 파일이 없습니다 — 해제할 발동이 없습니다."}
    state = _load_kill_switch_state()
    if "error" in state:
        return state

    cumulative = state["cumulative"]
    if not cumulative.get("triggered"):
        return {"error": "누적 킬 스위치가 발동 상태가 아닙니다 — 해제할 것이 없습니다."}

    now_dt = _resolve_kst_now(None)
    cumulative.setdefault("resets", []).append({
        "at":                   now_dt.isoformat(timespec="seconds"),
        "pnl_pct_before_reset": cumulative.get("pnl_pct_at_trigger"),
    })
    cumulative["triggered"] = False
    cumulative["pnl_pct_at_trigger"] = None
    cumulative["triggered_at"] = None

    err = _save_kill_switch_state(state)
    if err:
        return {"error": err}
    return {"reset": True, "cumulative": cumulative}


# ═══════════════════════════════════════════════
# 직접 실행 테스트
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import json
    import os
    import sys

    # "./venv/bin/python tools.py killswitch" → 오프라인 테스트(거래 로그 + 킬 스위치
    # T1~T6)까지만 실행하고 종료한다. 이후 테스트는 네트워크·대화형 입력이 필요하다.
    _KS_ONLY = "killswitch" in sys.argv[1:]

    def _pp(label: str, data: dict):
        print(f"\n{'='*55}")
        print(f"  {label}")
        print(f"{'='*55}")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    # ── 거래 로그 테스트 ─────────────────────────────────────────
    print("\n" + "="*55)
    print("  📒 거래 로그 테스트 (log_trade / get_trade_log / summarize)")
    print("="*55)

    # 실제 trade_log.json을 절대 건드리지 않도록 테스트 전용 임시 파일로 교체
    _ORIG_TRADE_LOG_PATH = TRADE_LOG_PATH
    TRADE_LOG_PATH = str(_BASE_DIR / "trade_log_TEST.json")
    try:
        # 깨끗한 상태에서 시작 (이전 테스트 로그 삭제)
        if os.path.exists(TRADE_LOG_PATH):
            os.remove(TRADE_LOG_PATH)
            print(f"\n[초기화] 기존 {TRADE_LOG_PATH} 삭제")

        # 1) 로그 기록 5건
        _pp("log_trade A/매수 — 삼성전자",
            log_trade("A", "005930", "BUY", 5, 75000, "Rule A: RSI 저점, 점수 8/10"))
        _pp("log_trade A/매수 — AAPL",
            log_trade("A", "AAPL",   "BUY", 2, 210.5, "Rule A: MACD 골든크로스"))
        _pp("log_trade B/매수 — SK하이닉스",
            log_trade("B", "000660", "BUY", 3, 180000, "Rule B: 반도체 섹터 비중 부족"))
        _pp("log_trade MANUAL/매수 — MSFT",
            log_trade("MANUAL", "MSFT", "BUY", 1, 440.0, "추천 직원 제안 — AI 섹터 보강"))
        _pp("log_trade SELL/매도 — 삼성전자 (손절 시나리오)",
            log_trade("SELL", "005930", "SELL", 5, 67500, "매도 규칙: 손절 -10% 도달"))

        # 2) 전체 조회
        _pp("get_trade_log() — 전체 (5건 기대)", get_trade_log())

        # 3) Rule A만 필터링
        _pp("get_trade_log('A') — Rule A만 (2건 기대)", get_trade_log("A"))

        # 4) 규칙별 요약
        _pp("summarize_trades_by_rule()", summarize_trades_by_rule())

        # 5) 잘못된 rule_tag → 에러 확인
        _pp("log_trade 잘못된 rule_tag 'C' → 에러 기대",
            log_trade("C", "005930", "BUY", 1, 75000, "테스트"))
        _pp("get_trade_log 잘못된 rule_tag 'X' → 에러 기대",
            get_trade_log("X"))
    finally:
        # 테스트가 예외로 죽어도 임시 파일 정리 + 원래 경로 복구
        if os.path.exists(TRADE_LOG_PATH):
            os.remove(TRADE_LOG_PATH)
        TRADE_LOG_PATH = _ORIG_TRADE_LOG_PATH

    print("\n" + "="*55)
    print("  ✅ 거래 로그 테스트 완료")
    print("="*55)

    # ── 킬 스위치 부품 테스트 (T1~T6) ─────────────────────────────
    print("\n" + "="*55)
    print("  🔴 킬 스위치 부품 테스트 (T1~T6)")
    print("="*55)

    # 실제 trade_log.json / kill_switch_state.json을 절대 건드리지 않도록
    # 테스트 전용 임시 파일로 교체하고 try/finally로 원복+삭제한다.
    # 네트워크 무관: 현재가는 전부 price_overrides, 시각은 전부 now 주입.
    _ORIG_TRADE_LOG_PATH2 = TRADE_LOG_PATH
    _ORIG_KS_STATE_PATH = KILL_SWITCH_STATE_PATH
    TRADE_LOG_PATH = str(_BASE_DIR / "trade_log_TEST.json")
    KILL_SWITCH_STATE_PATH = str(_BASE_DIR / "kill_switch_state_TEST.json")

    _ks_failed: list[str] = []

    def _check(label: str, cond: bool):
        print(f"  {'✅' if cond else '❌'} {label}")
        if not cond:
            _ks_failed.append(label)

    def _rm_test_files():
        for _p in (TRADE_LOG_PATH, KILL_SWITCH_STATE_PATH):
            if os.path.exists(_p):
                os.remove(_p)

    def _write_test_log(entries: list[dict]):
        """타임스탬프를 고정하기 위해 log_trade를 거치지 않고 직접 기록 (테스트 전용)."""
        with open(TRADE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

    def _test_entry(ts, tag, ticker, side, qty, price, source_rule=None):
        return {"timestamp": ts, "rule_tag": tag, "ticker": ticker, "side": side,
                "qty": qty, "price": price, "reason": "킬 스위치 테스트",
                "sector": None, "source_rule": source_rule}

    try:
        # ── T1: 로그 없음 → 빈 포지션, 손익 0, 발동 없음
        print("\n[T1] 로그 없음 → 빈 포지션·손익 0·발동 없음")
        _rm_test_files()
        _t1_pos = get_auto_trade_positions()
        _pp("T1 get_auto_trade_positions()", _t1_pos)
        _check("T1 합산 포지션 빈 값", _t1_pos.get("combined", {}).get("positions") == {})
        _check("T1 실현손익 0", _t1_pos.get("combined", {}).get("realized_pnl_krw") == 0)
        _t1_ks = update_kill_switch_state(now=datetime(2026, 7, 15, 10, 0),
                                          price_overrides={})
        _pp("T1 update_kill_switch_state()", _t1_ks)
        _check("T1 손익 0",
               _t1_ks.get("daily_pnl_pct") == 0 and _t1_ks.get("cumulative_pnl_pct") == 0)
        _check("T1 발동 없음",
               _t1_ks.get("daily_triggered") is False
               and _t1_ks.get("cumulative_triggered") is False)

        # ── T2: A 2회 매수(이동평균 평단) 후 source_rule="A" 매도
        print("\n[T2] A 2회 매수(이동평균) 후 source_rule='A' 매도")
        _rm_test_files()
        log_trade("A", "005930", "BUY", 10, 70000, "T2 매수1")
        log_trade("A", "005930", "BUY", 10, 80000, "T2 매수2")
        log_trade("SELL", "005930", "SELL", 5, 90000, "T2 익절 매도", source_rule="A")
        _t2_bad = log_trade("SELL", "005930", "SELL", 1, 90000,
                            "T2 잘못된 source_rule", source_rule="C")
        _check("T2 source_rule='C' → error", "error" in _t2_bad)
        _t2_pos = get_auto_trade_positions()
        _pp("T2 get_auto_trade_positions()", _t2_pos)
        _t2_a = _t2_pos["by_rule"]["A"]
        _check("T2 이동평균 평단 75,000 (=(70,000×10+80,000×10)÷20)",
               _t2_a["positions"].get("005930", {}).get("avg_price") == 75000)
        _check("T2 잔여 수량 15주", _t2_a["positions"].get("005930", {}).get("qty") == 15)
        _check("T2 실현손익 +75,000 (=(90,000−75,000)×5)",
               _t2_a["realized_pnl_krw"] == 75000)
        _check("T2 합산도 동일 (15주·+75,000)",
               _t2_pos["combined"]["positions"].get("005930", {}).get("qty") == 15
               and _t2_pos["combined"]["realized_pnl_krw"] == 75000)
        _check("T2 warnings 없음", _t2_pos["warnings"] == [])

        # ── T3: 일일 -5% 초과 손실 → 발동 + 래칫
        print("\n[T3] 일일 -5% 초과 손실 → 발동, 가격 회복해도 래칫 유지")
        _rm_test_files()
        _write_test_log([_test_entry("2026-07-10T10:00:00+09:00",
                                     "A", "005930", "BUY", 100, 600000)])
        _t3_1 = update_kill_switch_state(now=datetime(2026, 7, 15, 10, 0),
                                         price_overrides={"005930": 600000})
        _check("T3 스냅샷 직후 발동 없음", _t3_1.get("daily_triggered") is False)
        _t3_2 = update_kill_switch_state(now=datetime(2026, 7, 15, 10, 30),
                                         price_overrides={"005930": 560000})
        _pp("T3 폭락(평가 −400만 = −6.67%) 후", _t3_2)
        _check("T3 daily_triggered=True", _t3_2.get("daily_triggered") is True)
        _check("T3 daily_pnl_pct = -6.6667", _t3_2.get("daily_pnl_pct") == -6.6667)
        _t3_3 = update_kill_switch_state(now=datetime(2026, 7, 15, 11, 0),
                                         price_overrides={"005930": 600000})
        _pp("T3 가격 완전 회복 후 재호출 (래칫 확인)", _t3_3)
        _check("T3 래칫: 회복해도 발동 유지", _t3_3.get("daily_triggered") is True)
        _check("T3 래칫: 발동 당시 값(-6.6667) 반환", _t3_3.get("daily_pnl_pct") == -6.6667)

        # ── T4: 누적 -15% → 발동, reset_kill_switch 검증 (T3 상태에서 계속)
        print("\n[T4] 누적 -15% → 발동, reset_kill_switch 문구 검증")
        _t4_1 = update_kill_switch_state(now=datetime(2026, 7, 15, 11, 30),
                                         price_overrides={"005930": 500000})
        _pp("T4 폭락(누적 −1,000만 = −16.67%) 후", _t4_1)
        _check("T4 cumulative_triggered=True", _t4_1.get("cumulative_triggered") is True)
        _check("T4 cumulative_pnl_pct = -16.6667",
               _t4_1.get("cumulative_pnl_pct") == -16.6667)
        _t4_bad = reset_kill_switch("아무말")
        _pp("T4 reset_kill_switch('아무말') → error 기대", _t4_bad)
        _check("T4 잘못된 문구 → 해제 안 됨", "error" in _t4_bad)
        _t4_ok = reset_kill_switch("누적 킬스위치를 해제합니다")
        _pp("T4 정확한 문구 → 해제", _t4_ok)
        _check("T4 해제됨", _t4_ok.get("reset") is True)
        with open(KILL_SWITCH_STATE_PATH, "r", encoding="utf-8") as f:
            _t4_state = json.load(f)
        _check("T4 cumulative.triggered=False 저장 확인",
               _t4_state["cumulative"]["triggered"] is False)
        _check("T4 resets 이력 1건 (발동 당시 값 -16.6667 보존)",
               len(_t4_state["cumulative"]["resets"]) == 1
               and _t4_state["cumulative"]["resets"][0]["pnl_pct_before_reset"] == -16.6667)

        # ── T5: source_rule 없는 SELL → 경고+규칙별 제외+합산 포함 / oversell → error
        print("\n[T5] source_rule 없는 SELL / oversell")
        _write_test_log([
            _test_entry("2026-07-10T10:00:00+09:00", "A", "005930", "BUY", 10, 10000),
            # source_rule 없음 → 규칙별 제외, 합산 포함
            _test_entry("2026-07-11T10:00:00+09:00", "SELL", "005930", "SELL", 4, 12000),
        ])
        _t5_pos = get_auto_trade_positions()
        _pp("T5 get_auto_trade_positions()", _t5_pos)
        _check("T5 warnings 1건", len(_t5_pos.get("warnings", [])) == 1)
        _check("T5 규칙별 제외: A는 10주·실현 0 그대로",
               _t5_pos["by_rule"]["A"]["positions"].get("005930", {}).get("qty") == 10
               and _t5_pos["by_rule"]["A"]["realized_pnl_krw"] == 0)
        _check("T5 합산 포함: 6주 + 실현 +8,000",
               _t5_pos["combined"]["positions"].get("005930", {}).get("qty") == 6
               and _t5_pos["combined"]["realized_pnl_krw"] == 8000)
        _write_test_log([
            _test_entry("2026-07-10T10:00:00+09:00", "A", "005930", "BUY", 10, 10000),
            _test_entry("2026-07-11T10:00:00+09:00", "SELL", "005930", "SELL",
                        100, 12000, source_rule="A"),  # 보유 10주 초과 매도
        ])
        _t5_over = get_auto_trade_positions()
        _pp("T5 oversell → error 기대", _t5_over)
        _check("T5 oversell → error", "error" in _t5_over)

        # ── T6: 다음 날 → 일일 섹션 자동 리셋 (상태 파일은 T4 이후 그대로)
        print("\n[T6] 다음 날(now+1일) → 일일 섹션 자동 리셋")
        _write_test_log([_test_entry("2026-07-10T10:00:00+09:00",
                                     "A", "005930", "BUY", 100, 600000)])  # T3 로그 복원
        _t6 = update_kill_switch_state(now=datetime(2026, 7, 16, 10, 0),
                                       price_overrides={"005930": 600000})
        _pp("T6 update_kill_switch_state(다음 날)", _t6)
        _check("T6 일일 발동 자동 리셋 (triggered=False)",
               _t6.get("daily_triggered") is False)
        _check("T6 일일 손익 0 (새 스냅샷 기준)", _t6.get("daily_pnl_pct") == 0)
        with open(KILL_SWITCH_STATE_PATH, "r", encoding="utf-8") as f:
            _t6_state = json.load(f)
        _check("T6 새 스냅샷 날짜 2026-07-16", _t6_state["daily"]["date"] == "2026-07-16")
        _check("T6 새 스냅샷 평가액 6,000만",
               _t6_state["daily"]["snapshot_eval_krw"] == 60000000)
    finally:
        # 테스트가 예외로 죽어도 임시 파일 정리 + 원래 경로 복구
        _rm_test_files()
        TRADE_LOG_PATH = _ORIG_TRADE_LOG_PATH2
        KILL_SWITCH_STATE_PATH = _ORIG_KS_STATE_PATH

    print("\n" + "="*55)
    if _ks_failed:
        print(f"  ❌ 킬 스위치 부품 테스트 실패 {len(_ks_failed)}건:")
        for _f in _ks_failed:
            print(f"     - {_f}")
    else:
        print("  ✅ 킬 스위치 부품 테스트 (T1~T6) 전부 통과")
    print("="*55)

    if _KS_ONLY:
        print("\n(인자 'killswitch' — 이후 네트워크·대화형 테스트는 건너뜁니다)")
        raise SystemExit(1 if _ks_failed else 0)

    # ── 가드레일 검사 테스트 ──────────────────────────────────────
    print("\n" + "="*55)
    print("  🛡  check_guardrails 테스트 (8가지 케이스)")
    print("="*55)

    # 시각 픽스처: 한국 장중(10:00) / 장 외(15:00)
    # 날짜를 평일(월요일)로 고정 — 주말에 테스트를 돌려도 거래일 검사에 걸리지 않도록
    _KR_INDAY  = datetime(2026, 7, 13, 10, 0)   # 2026-07-13(월) 10:00
    _KR_CLOSED = datetime(2026, 7, 13, 15, 0)   # 2026-07-13(월) 15:00

    # ── 케이스 1: 모든 조건 통과 → "통과"
    _pp(
        "케이스 1 — 거래시간 OK + 한도 OK + 비중 OK + kill switch 안 걸림 → 통과",
        check_guardrails(
            "005930", 3_000_000,
            sector="반도체",
            accumulated_krw=10_000_000,      # 누적 1000만 + 이번 300만 = 1300만 ≤ 3000만 OK
            ticker_accumulated_krw=2_000_000, # 200만 + 300만 = 500만 ≤ 600만 OK
            sector_accumulated_krw=5_000_000, # 500만 + 300만 = 800만 ≤ 1200만 OK
            daily_trades=2,                   # 2 + 1 = 3회 ≤ 5회 OK
            daily_pnl_pct=-1.0,               # -1.0% > -5.0% OK
            cumulative_pnl_pct=-3.0,          # -3.0% > -15.0% OK
            now=_KR_INDAY,
        )
    )

    # ── 케이스 2: 거래 시간 밖 → "차단(시간 외)"
    _pp(
        "케이스 2 — 한국장 거래시간 밖(15:00) → 차단",
        check_guardrails(
            "005930", 1_000_000,
            now=_KR_CLOSED,
        )
    )

    # ── 케이스 3: 1종목 비중 20% 초과 → "차단(1종목비중)"
    # ticker_accumulated 5,500,000 + 이번 1,000,000 = 6,500,000 > 한도 6,000,000
    _pp(
        "케이스 3 — 1종목 비중 초과(5,500,000 + 1,000,000 = 6,500,000 > 6,000,000) → 차단",
        check_guardrails(
            "005930", 1_000_000,
            sector="반도체",
            accumulated_krw=10_000_000,
            ticker_accumulated_krw=5_500_000,
            sector_accumulated_krw=5_000_000,
            daily_trades=2,
            daily_pnl_pct=-1.0,
            cumulative_pnl_pct=-3.0,
            now=_KR_INDAY,
        )
    )

    # ── 케이스 4: kill switch 발동(일일 -5.2%) → "차단(kill_switch)"
    _pp(
        "케이스 4 — kill switch 발동(일일 -5.2% ≤ -5.0%) → 차단",
        check_guardrails(
            "005930", 1_000_000,
            sector="반도체",
            accumulated_krw=5_000_000,
            ticker_accumulated_krw=1_000_000,
            sector_accumulated_krw=2_000_000,
            daily_trades=2,
            daily_pnl_pct=-5.2,
            cumulative_pnl_pct=-3.0,
            now=_KR_INDAY,
        )
    )

    # ── 케이스 5: 미국장 서머타임 기간 — KST 23:00 = 뉴욕 월 10:00 → 통과
    _pp(
        "케이스 5 — US 서머타임(KST 2026-07-13 23:00 = 뉴욕 월 10:00) → 통과",
        check_guardrails(
            "AAPL", 1_000_000,
            now=datetime(2026, 7, 13, 23, 0),
        )
    )

    # ── 케이스 6: 서머타임 해제 기간 — KST 22:50 = 뉴욕 08:50 (개장 전) → 차단
    # 구 코드(KST 22:30 고정)였다면 통과됐을 시각
    _pp(
        "케이스 6 — US 서머타임 해제(KST 2026-01-12 22:50 = 뉴욕 08:50 개장 전) → 차단",
        check_guardrails(
            "AAPL", 1_000_000,
            now=datetime(2026, 1, 12, 22, 50),
        )
    )

    # ── 케이스 7: 서머타임 해제 기간 — KST 23:40 = 뉴욕 09:40 → 통과
    _pp(
        "케이스 7 — US 서머타임 해제(KST 2026-01-12 23:40 = 뉴욕 09:40) → 통과",
        check_guardrails(
            "AAPL", 1_000_000,
            now=datetime(2026, 1, 12, 23, 40),
        )
    )

    # ── 케이스 8: KST 토요일 새벽이지만 뉴욕은 금 11:30 → 통과
    # 세션 기준일이 2026-07-17(금, 뉴욕 현지)로 표시돼야 함
    _pp(
        "케이스 8 — KST 토 00:30 = 뉴욕 금 11:30 (세션 기준일 금요일) → 통과",
        check_guardrails(
            "AAPL", 1_000_000,
            now=datetime(2026, 7, 18, 0, 30),
        )
    )

    print("\n" + "="*55)
    print("  ⬆  가드레일 테스트 완료. 이후는 기존 테스트.")
    print("="*55)

    # ── 매수 규칙 A vs B 비교 ──────────────────────────────────
    # universe_limit=50: 속도 우선 (KR 시총 상위 50개 근사)
    # 전체 유니버스를 쓰려면 universe_limit=None (느림)
    ULIMIT = 50

    print("\n" + "=" * 55)
    print("  📊 매수 규칙 비교: Rule A vs Rule B")
    print(f"  (universe_limit={ULIMIT}, market='ALL')")
    print("=" * 55)

    print("\n▶ [11] evaluate_buy_rule_A — 점수 집중 방식")
    result_a = evaluate_buy_rule_A(market="ALL", universe_limit=ULIMIT)
    _pp("evaluate_buy_rule_A()", result_a)

    print("\n▶ [12] evaluate_buy_rule_B — 분산 채우기 방식")
    result_b = evaluate_buy_rule_B(market="ALL", universe_limit=ULIMIT)
    _pp("evaluate_buy_rule_B()", result_b)

    # ── 나란히 요약 출력 ──────────────────────────────────────
    print("\n" + "=" * 55)
    print("  🔍 Rule A vs B 비교 요약")
    print("=" * 55)

    def _fmt_candidates(result: dict) -> list[str]:
        lines = []
        for c in result.get("candidates", []):
            lines.append(
                f"  {c['market']} | {c['name']} ({c['ticker']}) "
                f"| 점수 {c['score']} | {c['sector']} | {c['reason']}"
            )
        return lines or ["  (후보 없음)"]

    print(f"\n[Rule A — 점수 집중]  후보 {result_a.get('count', 0)}개")
    for line in _fmt_candidates(result_a):
        print(line)

    print(f"\n[Rule B — 분산 채우기]  후보 {result_b.get('count', 0)}개")
    if "portfolio_sectors" in result_b:
        print("  현재 포트폴리오 섹터 비중:")
        for sec, pct in result_b.get("portfolio_sectors", {}).items():
            marker = " ◀ 부족" if sec in result_b.get("deficit_sectors", []) else ""
            print(f"    {sec}: {pct}%{marker}")
        print(f"  부족 섹터: {result_b.get('deficit_sectors', [])}")
    for line in _fmt_candidates(result_b):
        print(line)

    print("\n" + "=" * 55)
    print("  ⚠️  위 목록은 참고용이며 투자 권유가 아닙니다.")
    print("=" * 55)

    print("\n▶ [10] evaluate_sell_rules — 매도 규칙 분류")
    _pp("evaluate_sell_rules()", evaluate_sell_rules())

    print("\n▶ [0-B] place_kis_order — dry_run 테스트")

    # 정상 케이스: 삼성전자 시장가 매수 1주 (dry_run)
    _pp("place_kis_order('005930', 'BUY', 1, 'MARKET', dry_run=True)",
        place_kis_order("005930", "BUY", 1, "MARKET", dry_run=True))

    # 정상 케이스: 지정가 매수 1주 (dry_run)
    _pp("place_kis_order('005930', 'BUY', 1, 'LIMIT', price=60000, dry_run=True)",
        place_kis_order("005930", "BUY", 1, "LIMIT", price=60000, dry_run=True))

    # 안전장치 테스트: 금액 상한 초과 (60,000원 × 20주 = 120만원)
    _pp("안전장치: 금액 상한 초과 — LIMIT 60000 × 20주",
        place_kis_order("005930", "BUY", 20, "LIMIT", price=60000, dry_run=True))

    # 안전장치 테스트: 해외 종목 입력 거부
    _pp("안전장치: 해외 종목 거부 — AAPL",
        place_kis_order("AAPL", "BUY", 1, "MARKET", dry_run=True))

    print("\n▶ [0-B-2] propose_and_confirm_order — 주문서+승인 흐름 테스트")
    print("  (터미널에서 'y' 또는 'n' 을 입력해 흐름을 확인합니다.)")
    print("  ※ 'n' 입력 시 실제 주문 없이 취소 흐름만 확인합니다.\n")
    _pp("propose_and_confirm_order 결과",
        propose_and_confirm_order("005930", "BUY", 1, "LIMIT", price=60000))

    print("\n▶ [0] get_kis_token")
    tok = get_kis_token()
    if "error" in tok:
        _pp("get_kis_token() — 실패", tok)
    else:
        # 토큰 값은 일부만 표시 (보안)
        safe = tok.copy()
        safe["access_token"] = safe["access_token"][:20] + "..." if safe.get("access_token") else None
        _pp("get_kis_token()", safe)

    print("\n▶ [1] get_quote")
    _pp("get_quote('005930')", get_quote("005930"))
    _pp("get_quote('AAPL')",   get_quote("AAPL"))

    print("\n▶ [2] get_indicators")
    _pp("get_indicators('005930')", get_indicators("005930"))
    _pp("get_indicators('AAPL')",   get_indicators("AAPL"))

    print("\n▶ [3] get_fundamentals")
    _pp("get_fundamentals('005930')", get_fundamentals("005930"))
    _pp("get_fundamentals('AAPL')",   get_fundamentals("AAPL"))

    print("\n▶ [4] get_macro")
    _pp("get_macro()", get_macro())

    print("\n▶ [5] search_news")
    _pp("search_news('005930')", search_news("005930"))
    _pp("search_news('AAPL')",   search_news("AAPL"))

    print("\n▶ [6-B] get_benchmark_comparison")
    _pp("get_benchmark_comparison()", get_benchmark_comparison())

    print("\n▶ [6] get_portfolio_analysis")
    sample_holdings = [
        {"ticker": "005930", "weight": 0.4},
        {"ticker": "035420", "weight": 0.2},
        {"ticker": "AAPL",   "weight": 0.25},
        {"ticker": "MSFT",   "weight": 0.15},
    ]
    _pp("get_portfolio_analysis([삼성전자, NAVER, AAPL, MSFT])",
        get_portfolio_analysis(sample_holdings))
