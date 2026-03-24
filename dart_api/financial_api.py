"""
dart_api/financial_api.py
경로A: DART 재무제표 API 직접 호출

사업보고서가 존재하는 기업에 대해
  1. 단일회사 전체 재무제표(fnlttSinglAcntAll) — 주 데이터 소스
  2. 단일회사 주요계정(fnlttSinglAcnt)          — 교차검증 소스
두 API를 호출하여 config.FINANCIAL_ITEMS에 정의된 항목을 추출한다.

계정 매칭 전략 (두 단계):
  1차: account_id (IFRS 코드) 정확 일치
  2차: account_nm (한글 계정명) 키워드 포함 일치  ← account_id가 없거나 미매칭 시 폴백
"""

from typing import Optional

import requests

from config import (
    DART_API_KEY,
    DART_ENDPOINTS,
    FINANCIAL_ITEMS,
    FS_DIV,
    REQUEST_TIMEOUT,
)


# ── 주요계정 API account_nm → FINANCIAL_ITEMS name 매핑 ──────────────────
# fnlttSinglAcnt가 반환하는 고정 계정명을 우리 항목명으로 변환
_MAJOR_ACCT_NAME_MAP: dict[str, str] = {
    "자산총계":   "총자산",
    "부채총계":   "총부채",
    "자본총계":   "자본총계",
    "매출액":     "매출액",
    "영업이익":   "영업이익",
    "당기순이익": "당기순이익",
}


# ── 내부 유틸 ──────────────────────────────────────────────────────────────

def _parse_amount(row: dict, field: str = "thstrm_amount") -> Optional[float]:
    """
    DART API 금액 문자열을 float으로 변환한다.

    처리:
      - 쉼표 제거: "1,234,567" → 1234567.0
      - 음수 처리: "-1,234,567" → -1234567.0
      - 빈값/대시: None 반환

    Args:
        row:   계정 딕셔너리
        field: 금액 필드명 (기본값: "thstrm_amount" = 당기)

    Returns:
        float 또는 None
    """
    raw = (row.get(field) or "").strip()
    if not raw or raw in ("-", "N/A", ""):
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _build_id_index(raw_list: list[dict]) -> dict[str, dict]:
    """account_id → row 인덱스를 빌드한다."""
    idx: dict[str, dict] = {}
    for row in raw_list:
        acc_id = (row.get("account_id") or "").strip()
        if acc_id:
            # 같은 account_id가 여러 번 나올 수 있을 때 첫 번째 우선
            idx.setdefault(acc_id, row)
    return idx


def _extract_by_id_then_nm(
    raw_list: list[dict],
    dart_code: str,
    keywords: list[str],
    negate_keywords: list[str] | None = None,
) -> Optional[float]:
    """
    단일 항목을 두 단계로 매칭하여 당기 금액을 반환한다.

    1단계: account_id == dart_code 정확 일치
    2단계: account_nm에 keywords 중 하나가 포함 (account_id 미매칭 시)
           negate_keywords에 해당하는 키워드로 매칭된 경우 양수 값을 음수로 반전

    Args:
        raw_list:        전체 재무제표 원시 리스트
        dart_code:       DART account_id (IFRS 코드)
        keywords:        account_nm 폴백 매칭 키워드
        negate_keywords: 부호 반전이 필요한 키워드 목록 (선택)

    Returns:
        float 금액 또는 None
    """
    negate_set = set(negate_keywords) if negate_keywords else set()

    # 1단계: account_id 매칭 (DART가 부호를 정확히 기록하므로 반전 불필요)
    for row in raw_list:
        if (row.get("account_id") or "").strip() == dart_code:
            val = _parse_amount(row)
            if val is not None:
                return val

    # 2단계: account_nm 키워드 매칭 (폴백)
    for row in raw_list:
        nm = (row.get("account_nm") or "").replace(" ", "")
        for kw in keywords:
            if kw in nm:
                val = _parse_amount(row)
                if val is not None:
                    if kw in negate_set and val > 0:
                        val = -val
                    return val

    return None


# ── API 호출 함수 ──────────────────────────────────────────────────────────

def fetch_full_financial_statement(
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str = "CFS",
) -> list[dict]:
    """
    단일회사 전체 재무제표 API(fnlttSinglAcntAll)를 호출한다.

    Args:
        corp_code:   DART 기업 고유코드
        bsns_year:   사업연도 문자열 (예: "2023")
        reprt_code:  보고서 코드 (예: "11011")
        fs_div:      "CFS"=연결, "OFS"=별도

    Returns:
        계정별 딕셔너리 리스트

    Raises:
        requests.HTTPError: HTTP 오류
        ValueError: API 상태 코드가 000이 아닐 때 (데이터 없음 포함)
    """
    params = {
        "crtfc_key":  DART_API_KEY,
        "corp_code":  corp_code,
        "bsns_year":  bsns_year,
        "reprt_code": reprt_code,
        "fs_div":     fs_div,
    }
    resp = requests.get(DART_ENDPOINTS["financial_stmt"], params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    if status != "000":
        raise ValueError(
            f"전체재무제표 API 오류 [{fs_div}]: {data.get('message', '')} (status={status})"
        )

    items = data.get("list", [])
    if not items:
        raise ValueError(f"전체재무제표 데이터 없음 [{fs_div}]")

    return items


def fetch_major_accounts(
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str = "CFS",
) -> list[dict]:
    """
    단일회사 주요계정 API(fnlttSinglAcnt)를 호출한다. 교차검증에 사용.

    Args:
        corp_code:   DART 기업 고유코드
        bsns_year:   사업연도 문자열
        reprt_code:  보고서 코드
        fs_div:      "CFS"=연결, "OFS"=별도

    Returns:
        주요계정 딕셔너리 리스트

    Raises:
        requests.HTTPError, ValueError
    """
    params = {
        "crtfc_key":  DART_API_KEY,
        "corp_code":  corp_code,
        "bsns_year":  bsns_year,
        "reprt_code": reprt_code,
        "fs_div":     fs_div,
    }
    resp = requests.get(DART_ENDPOINTS["major_account"], params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    if status != "000":
        raise ValueError(
            f"주요계정 API 오류 [{fs_div}]: {data.get('message', '')} (status={status})"
        )

    return data.get("list", [])


# ── 항목 추출 함수 ────────────────────────────────────────────────────────

def extract_target_items(raw_list: list[dict]) -> dict[str, Optional[float]]:
    """
    전체 재무제표 원시 리스트에서 FINANCIAL_ITEMS에 정의된 항목을 추출한다.

    account_id 정확 일치 → account_nm 키워드 폴백 순서로 매칭한다.

    Args:
        raw_list: fetch_full_financial_statement() 반환값

    Returns:
        항목명 → 금액(원, float) 딕셔너리.
        찾지 못한 항목은 None.
    """
    result: dict[str, Optional[float]] = {}
    for item in FINANCIAL_ITEMS:
        result[item["name"]] = _extract_by_id_then_nm(
            raw_list,
            dart_code=item["dart_code"],
            keywords=item["keywords"],
            negate_keywords=item.get("negate_keywords"),
        )
    return result


def extract_major_account_items(major_list: list[dict]) -> dict[str, Optional[float]]:
    """
    주요계정 원시 리스트에서 교차검증용 항목을 추출한다.

    주요계정 API는 account_nm 기준으로 고정된 항목을 반환하므로
    _MAJOR_ACCT_NAME_MAP을 통해 우리 항목명으로 변환한다.

    Args:
        major_list: fetch_major_accounts() 반환값

    Returns:
        항목명 → 금액 딕셔너리 (매핑된 항목만 포함)
    """
    result: dict[str, Optional[float]] = {}
    for row in major_list:
        nm = (row.get("account_nm") or "").strip()
        our_name = _MAJOR_ACCT_NAME_MAP.get(nm)
        if our_name:
            result[our_name] = _parse_amount(row)
    return result


# ── 경로A 메인 함수 ────────────────────────────────────────────────────────

def get_financial_data_path_a(
    corp_code: str,
    year: int,
    reprt_code: str,
) -> dict:
    """
    경로A 메인 함수: 전체 재무제표 + 주요계정을 조회하여 통합 결과를 반환한다.

    연결(CFS) 우선 시도 → 데이터 없으면 별도(OFS) 폴백.
    주요계정 API도 같은 fs_div로 호출하여 교차검증 데이터를 제공한다.

    Args:
        corp_code:   DART 기업 고유코드
        year:        사업연도 (int)
        reprt_code:  보고서 코드 (report_finder에서 전달, 예: "11011")

    Returns:
        {
            "items":       {항목명: 금액(float|None), ...},   # 주 데이터
            "cross_check": {항목명: 금액(float|None), ...},   # 교차검증용
            "fs_div":      "CFS" | "OFS" | None,              # 실제 사용 구분
            "error":       오류 메시지 문자열 (정상이면 None),
        }
    """
    bsns_year = str(year)
    empty = {"items": {}, "cross_check": {}, "fs_div": None, "error": None}

    last_error = ""
    for div in (FS_DIV["consolidated"], FS_DIV["separate"]):
        # ── 전체 재무제표 ──────────────────────────────────────────────────
        try:
            raw = fetch_full_financial_statement(corp_code, bsns_year, reprt_code, div)
        except (ValueError, requests.RequestException) as e:
            last_error = str(e)
            continue

        items = extract_target_items(raw)

        # ── 주요계정 (교차검증, 실패해도 무시) ────────────────────────────
        cross_check: dict[str, Optional[float]] = {}
        try:
            major_raw = fetch_major_accounts(corp_code, bsns_year, reprt_code, div)
            cross_check = extract_major_account_items(major_raw)
        except Exception:
            pass  # 교차검증 실패는 주 데이터에 영향 없음

        return {
            "items":       items,
            "cross_check": cross_check,
            "fs_div":      div,
            "error":       None,
        }

    # CFS/OFS 모두 실패
    empty["error"] = last_error or "재무제표 데이터를 가져올 수 없습니다."
    return empty
