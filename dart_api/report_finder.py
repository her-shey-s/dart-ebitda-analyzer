"""
dart_api/report_finder.py
사업보고서 / 감사보고서 탐색 로직

우선순위:
  1. 사업보고서 (연간) → 경로A (financial_api.py)
  2. 연결감사보고서     → 경로B (html_parser.py)
  3. 감사보고서         → 경로B (html_parser.py)
"""

from typing import Optional

import requests

from config import DART_API_KEY, DART_ENDPOINTS, REPORT_CODES, REQUEST_TIMEOUT


def get_latest_report(
    corp_code: str,
    year: int,
    prefer_consolidated: bool = True,
) -> Optional[dict]:
    """
    주어진 기업의 특정 연도 최신 보고서 메타데이터를 반환한다.

    탐색 순서:
      사업보고서(11011) → 연결감사보고서(11004) → 감사보고서(11005)

    Args:
        corp_code:           DART 기업 고유코드
        year:                사업연도 (예: 2023)
        prefer_consolidated: True이면 연결재무제표 우선

    Returns:
        보고서 메타딕셔너리 또는 None
        {
            "rcept_no":    접수번호,
            "report_type": "annual" | "audit_consol" | "audit_separate",
            "rcept_dt":    접수일자,
            "report_nm":   보고서명,
        }
    """
    search_order = [
        ("annual",         REPORT_CODES["annual"]),
        ("audit_consol",   REPORT_CODES["audit_consol"]),
        ("audit_separate", REPORT_CODES["audit_separate"]),
    ]

    for report_type, pblntf_detail_ty in search_order:
        meta = _fetch_disclosure(corp_code, year, pblntf_detail_ty)
        if meta:
            meta["report_type"] = report_type
            return meta

    return None


def _fetch_disclosure(corp_code: str, year: int, pblntf_detail_ty: str) -> Optional[dict]:
    """
    DART 공시 목록 API에서 특정 보고서 유형의 가장 최근 공시를 조회한다.

    Args:
        corp_code:          DART 기업 고유코드
        year:               사업연도
        pblntf_detail_ty:   보고서 유형 코드

    Returns:
        공시 메타딕셔너리 또는 None
    """
    params = {
        "crtfc_key":         DART_API_KEY,
        "corp_code":         corp_code,
        "bgn_de":            f"{year}0101",
        "end_de":            f"{year}1231",
        "pblntf_detail_ty":  pblntf_detail_ty,
        "page_count":        5,
    }
    resp = requests.get(DART_ENDPOINTS["disclosure_list"], params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000" or not data.get("list"):
        return None

    # 접수일 기준 최신 공시 선택
    items = sorted(data["list"], key=lambda x: x.get("rcept_dt", ""), reverse=True)
    item = items[0]
    return {
        "rcept_no":  item["rcept_no"],
        "rcept_dt":  item["rcept_dt"],
        "report_nm": item["report_nm"],
    }


def get_document_index(rcept_no: str) -> list[dict]:
    """
    접수번호로 보고서 원문 문서 목록(index)을 가져온다.

    경로B에서 HTML 원문 URL을 찾기 위해 사용.

    Args:
        rcept_no: 공시 접수번호

    Returns:
        문서 목록 (title, url 포함 딕셔너리 리스트)

    Raises:
        requests.HTTPError: API 오류 시
    """
    params = {"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}
    resp = requests.get(DART_ENDPOINTS["doc_index"], params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        return []

    return data.get("list", [])


def find_financial_statement_doc(doc_index: list[dict]) -> Optional[str]:
    """
    문서 목록에서 재무제표가 포함된 HTML 문서 URL을 찾는다.

    Args:
        doc_index: get_document_index() 반환값

    Returns:
        재무제표 HTML URL 또는 None
    """
    priority_keywords = ["재무제표", "재무상태표", "손익계산서"]
    fallback_keywords = ["감사보고서", "audit"]

    for keywords in (priority_keywords, fallback_keywords):
        for doc in doc_index:
            title = doc.get("title", "").lower()
            if any(kw.lower() in title for kw in keywords):
                return doc.get("url")

    # 키워드 없으면 첫 번째 문서 반환
    return doc_index[0].get("url") if doc_index else None
