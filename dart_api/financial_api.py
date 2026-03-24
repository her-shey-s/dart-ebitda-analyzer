"""
dart_api/financial_api.py
경로A: DART 재무제표 API 직접 호출

사업보고서가 존재하는 기업(주로 상장사)에 대해
'단일회사 전체 재무제표' API와 '주요계정' API를 호출하여
재무 데이터를 딕셔너리로 반환한다.
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


def fetch_full_financial_statement(
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str = "CFS",
) -> list[dict]:
    """
    단일회사 전체 재무제표 API를 호출하여 원시 계정 데이터를 반환한다.

    Args:
        corp_code:   DART 기업 고유코드
        bsns_year:   사업연도 (예: "2023")
        reprt_code:  보고서 코드 (예: "11011" = 사업보고서)
        fs_div:      재무제표 구분 ("CFS"=연결, "OFS"=별도)

    Returns:
        계정별 딕셔너리 리스트 (account_id, account_nm, thstrm_amount 등 포함)

    Raises:
        requests.HTTPError: API 호출 실패 시
        ValueError: 데이터 없음 또는 API 오류 코드 반환 시
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

    if data.get("status") != "000":
        raise ValueError(f"재무제표 API 오류: {data.get('message', '알 수 없음')} (status={data.get('status')})")

    return data.get("list", [])


def fetch_major_accounts(
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str = "CFS",
) -> list[dict]:
    """
    주요계정 API를 호출한다. 교차검증에 사용.

    Args:
        corp_code:   DART 기업 고유코드
        bsns_year:   사업연도
        reprt_code:  보고서 코드
        fs_div:      재무제표 구분

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

    if data.get("status") != "000":
        raise ValueError(f"주요계정 API 오류: {data.get('message', '알 수 없음')}")

    return data.get("list", [])


def extract_target_items(
    raw_list: list[dict],
    year: str,
    prior_year: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """
    전체 재무제표 원시 리스트에서 config.FINANCIAL_ITEMS에 정의된 항목만 추출한다.

    Args:
        raw_list:   fetch_full_financial_statement() 반환값
        year:       당기 사업연도 (예: "2023")
        prior_year: 전기 사업연도 (비교용, 없으면 None)

    Returns:
        항목명 → 금액(원) 딕셔너리
        예: {"매출액": 1234567890, "영업이익": 98765432, ...}
    """
    # account_id 기준 인덱스 구축
    code_to_row: dict[str, dict] = {}
    for row in raw_list:
        acc_id = row.get("account_id", "")
        if acc_id:
            code_to_row[acc_id] = row

    result: dict[str, Optional[float]] = {}
    for item in FINANCIAL_ITEMS:
        dart_code = item["dart_code"]
        row = code_to_row.get(dart_code)
        result[item["name"]] = _parse_amount(row, "thstrm_amount") if row else None

    return result


def _parse_amount(row: dict, field: str) -> Optional[float]:
    """
    DART API 금액 문자열("1,234,567" 또는 "-1,234,567")을 float으로 변환한다.

    Args:
        row:   계정 딕셔너리
        field: 필드명 (예: "thstrm_amount")

    Returns:
        float 금액 또는 None (빈 값 / 변환 불가)
    """
    raw = row.get(field, "")
    if not raw or raw.strip() in ("", "-", "N/A"):
        return None
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return None


def get_financial_data_path_a(
    corp_code: str,
    year: int,
    reprt_code: str,
) -> dict:
    """
    경로A 메인 함수: 연결 우선 → 실패 시 별도 재무제표로 폴백하여 데이터를 반환한다.

    Args:
        corp_code:   DART 기업 고유코드
        year:        사업연도
        reprt_code:  보고서 코드

    Returns:
        {
            "items":    항목명 → 금액 딕셔너리,
            "fs_div":   실제 사용된 재무제표 구분 ("CFS" or "OFS"),
            "raw_list": 원시 계정 리스트 (교차검증용),
        }
    """
    year_str = str(year)

    for div in (FS_DIV["consolidated"], FS_DIV["separate"]):
        try:
            raw = fetch_full_financial_statement(corp_code, year_str, reprt_code, div)
            if raw:
                items = extract_target_items(raw, year_str)
                return {"items": items, "fs_div": div, "raw_list": raw}
        except ValueError:
            continue

    return {"items": {}, "fs_div": None, "raw_list": []}
