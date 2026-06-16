"""
tools.py
========
데이터·지표·뉴스·재무·거시 함수 모음.

규칙 (CLAUDE.md):
- 모든 함수는 dict 반환. 실패 시 {"error": "..."} 반환.
- 한국 종목: 6자리 코드(예: "005930"), 미국: 티커(예: "AAPL").
- 함수 시그니처(이름·인자)는 안정적으로 유지한다.
"""

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
# 직접 실행 테스트
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import json

    def _pp(label: str, data: dict):
        print(f"\n{'='*55}")
        print(f"  {label}")
        print(f"{'='*55}")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

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

    print("\n▶ [6] get_portfolio_analysis")
    sample_holdings = [
        {"ticker": "005930", "weight": 0.4},
        {"ticker": "035420", "weight": 0.2},
        {"ticker": "AAPL",   "weight": 0.25},
        {"ticker": "MSFT",   "weight": 0.15},
    ]
    _pp("get_portfolio_analysis([삼성전자, NAVER, AAPL, MSFT])",
        get_portfolio_analysis(sample_holdings))
