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

import FinanceDataReader as fdr
import pandas as pd

warnings.filterwarnings("ignore")  # yfinance deprecation 등 노이즈 억제

# ── 피어 종목 견적 캐시 (스레드 안전, TTL 10분) ─────────────────────────
_quote_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 600        # 초
_PEER_RETRIES = 2       # 피어 조회 실패 시 최대 재시도 횟수
_PEER_MIN_COUNT = 2     # 이 수 미만이면 "판별 제한"으로 표시


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


def _fetch_ohlcv(ticker: str, days: int = 200) -> pd.DataFrame | None:
    """OHLCV DataFrame 반환. 실패 시 None."""
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
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════
# 0. KIS 모의투자 인증 (토큰 발급 + 캐싱)
# ═══════════════════════════════════════════════

_KIS_DOMAIN          = "https://openapivts.koreainvestment.com:29443"
_KIS_TOKEN_CACHE_FILE = ".kis_token_cache.json"
_KIS_TOKEN_BUFFER_SEC = 600   # 만료 10분 전에 갱신 트리거

# ── place_kis_order 안전장치 상수 ──────────────────────────────
_KIS_MOCK_ACCOUNT   = "50193730-01"   # 허용된 모의투자 계좌 (하드코딩)
_KIS_ORDER_LIMIT_KRW = 1_000_000      # 1회 주문 금액 상한 (100만 원)

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
        return {"error": f"KIS 토큰 발급 HTTP 오류: {exc} — {body_text}"}
    except Exception as exc:
        return {"error": f"KIS 토큰 발급 실패: {exc}"}

    access_token = data.get("access_token")
    if not access_token:
        return {"error": f"KIS 응답에 access_token 없음: {data}"}

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
        return {"error": f"토큰 획득 실패: {tok['error']}"}

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
            domestic = {"error": f"국내 잔고 API 오류: {msg}"}
    except Exception as exc:
        domestic = {"error": f"국내 잔고 조회 실패: {exc}"}

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
    2. 주문 금액 상한: 1회 ≤ 100만 원.
       - 지정가: price × qty
       - 시장가: get_quote() 현재가 × qty (근사)
       초과 시 즉시 거부.

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
    if order_type == "LIMIT":
        estimated_amount = price * qty
        price_source     = f"지정가({price:,}원)"
    else:
        quote = get_quote(ticker)
        if "error" in quote:
            return {"error": f"시장가 금액 확인을 위한 현재가 조회 실패: {quote['error']}"}
        current_price = quote.get("close")
        if current_price is None:
            return {"error": "현재가 조회 결과에 종가(close)가 없습니다."}
        estimated_amount = int(current_price) * qty
        price_source     = f"현재가 근사({int(current_price):,}원)"

    if estimated_amount > _KIS_ORDER_LIMIT_KRW:
        return {
            "error": (
                f"주문 금액 상한 초과 — 상한: {_KIS_ORDER_LIMIT_KRW:,}원, "
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

    safety_info = {
        "account_ok":    True,
        "amount_ok":     True,
        "limit_krw":     _KIS_ORDER_LIMIT_KRW,
        "estimated_krw": estimated_amount,
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
        return {"error": f"토큰 획득 실패: {tok['error']}"}

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
        return {"error": f"KIS 주문 HTTP 오류: {exc} — {body_text}"}
    except Exception as exc:
        return {"error": f"KIS 주문 요청 실패: {exc}"}

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
    기술적 지표(MA20, MA60, RSI, MACD)를 반환한다.

    Parameters
    ----------
    ticker : str
        한국 6자리 코드 또는 미국 티커.

    Returns
    -------
    dict  {ticker, date, close, ma20, ma60, rsi,
           macd, macd_signal, macd_hist}
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

    return {
        "ticker":      ticker,
        "date":        df.index[-1].strftime("%Y-%m-%d"),
        "close":       _safe_float(close_s.iloc[-1]),
        "ma20":        ma20,
        "ma60":        ma60,
        "rsi":         rsi,
        "macd":        macd_val,
        "macd_signal": macd_signal,
        "macd_hist":   macd_hist,
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
           market_cap, currency}
          실패 시: {"error": "..."}
    """
    import yfinance as yf

    ticker = ticker.strip()
    market = "KR" if _is_korean(ticker) else "US"
    yf_sym = _yf_ticker(ticker)

    try:
        info = yf.Ticker(yf_sym).info
    except Exception as exc:
        return {"error": f"yfinance 조회 실패 ({yf_sym}): {exc}"}

    if not info or info.get("regularMarketPrice") is None:
        # .KS 실패 시 .KQ(코스닥) 재시도
        if market == "KR":
            try:
                yf_sym = ticker + ".KQ"
                info   = yf.Ticker(yf_sym).info
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

    return {
        "ticker":           ticker,
        "market":           market,
        "currency":         currency,
        "per":              _val(per),
        "pbr":              _val(pbr),
        "eps":              _val(eps),
        "revenue":          _val(revenue),
        "operating_income": _val(op_income),
        "net_income":       _val(net_income),
        "debt_ratio":       _val(debt_ratio),      # %
        "market_cap":       _val(market_cap),
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
            return result
        except Exception as e:
            return {"error": f"KR 유니버스 조회 실패: {e}"}

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
            return result
        except Exception as e:
            return {"error": f"US 유니버스 조회 실패: {e}"}

    if market == "KR":
        kr = _fetch_kr()
        if isinstance(kr, dict):
            return kr
        return {"market": "KR", "count": len(kr), "tickers": kr}

    if market == "US":
        us = _fetch_us()
        if isinstance(us, dict):
            return us
        return {"market": "US", "count": len(us), "tickers": us}

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
    return {
        "market":            market,
        "total_evaluated":   total,
        "scored":            len(scored),
        "top_n":             top_n,
        "growth_correction": growth_correction,
        "max_per_sector":    max_per_sector,
        "results":           top,
    }


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
    TAKE_PROFIT_PCT : 수익률 ≥ 이 값이면 익절 후보

    Returns
    -------
    dict
        {
            "as_of": str,
            "rules": {
                "stop_loss_pct":   float,   # 손절 임계값 (예: -10.0)
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
                }
            ],
            "take_profit": [...],   # 같은 구조, 수익률 높은 순 정렬
            "hold": {
                "count":    int,
                "domestic": int,
                "overseas": int,
            },
            "summary": {
                "total_holdings":    int,
                "stop_loss_count":   int,
                "take_profit_count": int,
                "hold_count":        int,
            },
            "disclaimer": str,
        }
        실패 시: {"error": "..."}
    """
    # ── 규칙 임계값 ─────────────────────────────────────────────
    STOP_LOSS_PCT   = -10.0   # 수익률 <= 이 값 → 손절 후보
    TAKE_PROFIT_PCT =  20.0   # 수익률 >= 이 값 → 익절 후보

    # ── 1. 잔고 조회 ─────────────────────────────────────────────
    balance = get_kis_balance()
    if "error" in balance:
        return {"error": f"잔고 조회 실패: {balance['error']}"}

    holdings = balance.get("holdings", [])
    if not holdings:
        return {"error": "보유 종목이 없습니다."}

    # ── 2. 규칙 분류 ─────────────────────────────────────────────
    stop_loss:   list[dict] = []
    take_profit: list[dict] = []
    hold:        list[dict] = []

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

        if pct <= STOP_LOSS_PCT:
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
    take_profit.sort(key=lambda x: x["profit_loss_pct"], reverse=True)

    # ── 4. 보유 유지 국내/해외 집계 ─────────────────────────────
    hold_kr = sum(1 for h in hold if h["market"] == "KR")
    hold_us = sum(1 for h in hold if h["market"] == "US")

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "rules": {
            "stop_loss_pct":   STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
        },
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "hold": {
            "count":    len(hold),
            "domestic": hold_kr,
            "overseas": hold_us,
        },
        "summary": {
            "total_holdings":    len(holdings),
            "stop_loss_count":   len(stop_loss),
            "take_profit_count": len(take_profit),
            "hold_count":        len(hold),
        },
        "disclaimer": (
            "이 분류는 규칙 기반 참고 정보이며 투자 권유가 아닙니다. "
            "실제 매도 여부는 본인이 판단·결정하세요. "
            "미래 가격을 예측하지 않습니다."
        ),
    }


# ═══════════════════════════════════════════════
# 직접 실행 테스트
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import json

    def _pp(label: str, data: dict):
        print(f"\n{'='*55}")
        print(f"  {label}")
        print(f"{'='*55}")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

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
