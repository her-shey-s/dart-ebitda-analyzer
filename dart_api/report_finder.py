"""
dart_api/report_finder.py
사업보고서 / 감사보고서 탐색 로직

특정 기업·연도에 대해 어떤 보고서가 DART에 공시되어 있는지 탐색하고,
경로A(사업보고서 → 재무제표 API)와 경로B(감사보고서 → HTML 파싱) 중
어느 쪽으로 데이터를 수집할지 결정한다.

탐색 우선순위:
  1. 사업보고서  (pblntf_detail_ty=A001) → path="A"
  2. 연결감사보고서 (pblntf_detail_ty=F002) → path="B"
  3. 감사보고서    (pblntf_detail_ty=F001) → path="B"

정정보고서가 있으면 접수일 기준 최신 본(정정본)을 우선 선택한다.
"""

import re
from typing import Optional

import requests

from config import DART_API_KEY, DART_ENDPOINTS, REQUEST_TIMEOUT


# ── 공시 목록 API용 코드 (list.json) ─────────────────────────────────────
# ※ 재무제표 API의 reprt_code(11011 등)와 별개임
_SEARCH_ORDER = [
    {
        "report_type":    "annual",
        "pblntf_detail_ty": "A001",   # 사업보고서
        "path":           "A",
        "reprt_code":     "11011",    # 재무제표 API 호출 시 사용
    },
    {
        "report_type":    "audit_consol",
        "pblntf_detail_ty": "F002",   # 연결감사보고서
        "path":           "B",
        "reprt_code":     None,
    },
    {
        "report_type":    "audit_separate",
        "pblntf_detail_ty": "F001",   # 감사보고서
        "path":           "B",
        "reprt_code":     None,
    },
]


def find_report(corp_code: str, year: int) -> Optional[dict]:
    """
    특정 기업·사업연도의 보고서를 탐색하여 데이터 수집 경로를 결정한다.

    탐색 순서: 사업보고서 → 연결감사보고서 → 감사보고서
    각 유형에서 가장 최근 접수된 공시(정정본 포함)를 선택한다.

    사업연도 Y의 보고서는 Y+1년 상반기까지 제출되므로
    검색 범위를 {year}0101 ~ {year+1}0630으로 설정한다.

    Args:
        corp_code: DART 기업 고유코드 (8자리)
        year:      사업연도 (예: 2023)

    Returns:
        보고서가 발견되면:
        {
            "path":        "A" | "B",
            "rcept_no":    접수번호,
            "report_type": "annual" | "audit_consol" | "audit_separate",
            "report_nm":   보고서명,
            "rcept_dt":    접수일자 (YYYYMMDD),
            "reprt_code":  재무제표 API용 코드 (경로A만, 경로B는 None),
        }
        보고서를 찾지 못하면 None.

    Raises:
        requests.HTTPError: API 호출 자체가 실패한 경우
    """
    bgn_de = f"{year}0101"
    end_de = f"{year + 1}0630"

    for spec in _SEARCH_ORDER:
        item = _fetch_latest_disclosure(
            corp_code=corp_code,
            bgn_de=bgn_de,
            end_de=end_de,
            pblntf_detail_ty=spec["pblntf_detail_ty"],
        )
        if item is None:
            continue

        # 보고서명에서 사업연도 추출하여 요청 연도와 일치 여부 검증
        # 예: "감사보고서 (2024.12)" → 2024 ≠ 2025(요청) → 스킵
        report_year = _extract_year_from_report_nm(item["report_nm"])
        if report_year is not None and report_year != year:
            continue

        return {
            "path":        spec["path"],
            "rcept_no":    item["rcept_no"],
            "report_type": spec["report_type"],
            "report_nm":   item["report_nm"],
            "rcept_dt":    item["rcept_dt"],
            "reprt_code":  spec["reprt_code"],
        }

    return None


def _fetch_latest_disclosure(
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_detail_ty: str,
) -> Optional[dict]:
    """
    DART 공시 목록 API를 호출하여 해당 유형의 가장 최근 공시 1건을 반환한다.

    정정보고서는 원본보다 접수일이 늦으므로 rcept_dt 내림차순 정렬로
    자동으로 우선 선택된다.

    Args:
        corp_code:          DART 기업 고유코드
        bgn_de:             검색 시작일 (YYYYMMDD)
        end_de:             검색 종료일 (YYYYMMDD)
        pblntf_detail_ty:   공시상세유형코드 (예: A001, F001, F002)

    Returns:
        {rcept_no, report_nm, rcept_dt} 딕셔너리 또는 None

    Raises:
        requests.HTTPError: HTTP 레벨 오류 시
    """
    params = {
        "crtfc_key":        DART_API_KEY,
        "corp_code":        corp_code,
        "bgn_de":           bgn_de,
        "end_de":           end_de,
        "pblntf_detail_ty": pblntf_detail_ty,
        "page_count":       10,   # 정정본 포함해서 여유있게 조회
    }

    resp = requests.get(
        DART_ENDPOINTS["disclosure_list"],
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    # status 013 = 조회된 데이터가 없음
    if data.get("status") != "000" or not data.get("list"):
        return None

    # 접수일(rcept_dt) 내림차순 → 가장 최근 제출(정정본 우선)
    items = sorted(data["list"], key=lambda x: x.get("rcept_dt", ""), reverse=True)
    latest = items[0]

    return {
        "rcept_no":  latest["rcept_no"],
        "report_nm": latest["report_nm"],
        "rcept_dt":  latest["rcept_dt"],
    }


def _extract_year_from_report_nm(report_nm: str) -> Optional[int]:
    """
    보고서명에서 사업연도를 추출한다.

    DART 보고서명 패턴: "감사보고서 (2024.12)", "사업보고서 (2023.12)" 등

    Args:
        report_nm: DART 보고서명 문자열

    Returns:
        사업연도(int) 또는 None (패턴 없으면 검증 생략)
    """
    m = re.search(r"\((\d{4})\.\d{2}\)", report_nm)
    return int(m.group(1)) if m else None


def get_document_index(rcept_no: str) -> list[dict]:
    """
    접수번호로 보고서 원문 문서 목록(index)을 가져온다.

    경로B에서 재무제표 HTML URL을 찾기 위해 사용한다.

    Args:
        rcept_no: 공시 접수번호

    Returns:
        문서 목록 딕셔너리 리스트 (title, url 포함).
        조회 실패 시 빈 리스트.

    Raises:
        requests.HTTPError: HTTP 레벨 오류 시
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
    문서 목록에서 재무제표가 포함된 HTML 문서 URL을 반환한다.

    우선순위:
      1. 제목에 "재무제표", "재무상태표", "손익계산서" 포함
      2. 제목에 "감사보고서" 포함
      3. 첫 번째 문서

    Args:
        doc_index: get_document_index() 반환값

    Returns:
        HTML 문서 URL 문자열 또는 None
    """
    if not doc_index:
        return None

    keyword_groups = [
        ["재무제표", "재무상태표", "손익계산서"],
        ["감사보고서"],
    ]

    for keywords in keyword_groups:
        for doc in doc_index:
            title = doc.get("title", "")
            if any(kw in title for kw in keywords):
                return doc.get("url")

    return doc_index[0].get("url")
