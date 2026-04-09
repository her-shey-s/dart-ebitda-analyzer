"""
financial/api_extractor.py
경로A: DART 재무제표 API 직접 호출

사업보고서가 존재하는 기업에 대해
  단일회사 전체 재무제표(fnlttSinglAcntAll) API를 호출하여
  config.FINANCIAL_ITEMS에 정의된 항목을 추출한다.

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


_VALID_FS_DIVS = {"CFS", "OFS"}


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


def _infer_fs_div(raw_list: list[dict], requested_div: str) -> str:
    """
    API 응답 행들에서 실제 재무제표 구분(CFS/OFS)을 추론한다.

    DART가 요청한 fs_div와 다른 기준의 데이터를 돌려주는 경우가 있어,
    응답 행의 fs_div 값을 우선 신뢰한다. 유효한 값이 없으면 요청값을 사용한다.
    """
    counts: dict[str, int] = {}
    for row in raw_list:
        div = (row.get("fs_div") or "").strip().upper()
        if div in _VALID_FS_DIVS:
            counts[div] = counts.get(div, 0) + 1

    if counts:
        return max(counts, key=counts.get)
    return requested_div


def _extract_by_id_then_nm(
    raw_list: list[dict],
    dart_code: str,
    keywords: list[str],
    exclude_keywords: list[str] | None = None,
    negate_keywords: list[str] | None = None,
) -> Optional[float]:
    """
    단일 항목을 두 단계로 매칭하여 당기 금액을 반환한다.

    1단계: account_id == dart_code 정확 일치
    2단계: account_nm에 keywords 중 하나가 포함 (account_id 미매칭 시)
           exclude_keywords가 포함된 라인은 제외
           negate_keywords에 해당하는 키워드로 매칭된 경우 양수 값을 음수로 반전

    Args:
        raw_list:        전체 재무제표 원시 리스트
        dart_code:       DART account_id (IFRS 코드)
        keywords:        account_nm 폴백 매칭 키워드
        exclude_keywords: 제외할 account_nm 키워드 목록 (선택)
        negate_keywords:  부호 반전이 필요한 키워드 목록 (선택)

    Returns:
        float 금액 또는 None
    """
    negate_set = set(negate_keywords) if negate_keywords else set()
    exclude_set = set(exclude_keywords) if exclude_keywords else set()

    # 1단계: account_id 매칭 (DART가 부호를 정확히 기록하므로 반전 불필요)
    for row in raw_list:
        if (row.get("account_id") or "").strip() == dart_code:
            val = _parse_amount(row)
            if val is not None:
                return val

    # 2단계: account_nm 키워드 매칭 (폴백)
    for row in raw_list:
        nm = (row.get("account_nm") or "").replace(" ", "")
        if exclude_set and any(ex_kw in nm for ex_kw in exclude_set):
            continue
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
            exclude_keywords=item.get("exclude_keywords"),
            negate_keywords=item.get("negate_keywords"),
        )
    return result


# ── API 원시 데이터 → AI용 텍스트 ─────────────────────────────────────────

# 재무제표 유형별 sj_div 코드 → AI 텍스트 라벨
_SJ_DIV_LABELS: dict[str, str] = {
    "BS":  "BS",   # 재무상태표
    "IS":  "IS",   # 손익계산서
    "CIS": "IS",   # 포괄손익계산서 → IS로 통합 (매출액·영업이익 등 포함)
    "CF":  "CF",   # 현금흐름표
    "SCE": "SCE",  # 자본변동표
}


def format_raw_for_ai(raw_list: list[dict], max_chars: int = 8000) -> str:
    """
    전체재무제표 API 원시 행 리스트를 AI 추출용 텍스트로 변환한다.

    경로B의 _extract_table_text_for_ai()와 동일한 역할.
    각 행을 "[재무제표유형] 계정명: 금액" 형식으로 변환하여
    AI가 독립적으로 항목을 추출할 수 있게 한다.

    Args:
        raw_list: fetch_full_financial_statement() 반환값
        max_chars: 최대 문자 수 (기본 8000)

    Returns:
        AI에게 전달할 텍스트 문자열
    """
    sections: dict[str, list[str]] = {}

    for row in raw_list:
        sj_div = (row.get("sj_div") or "").strip().upper()
        label = _SJ_DIV_LABELS.get(sj_div, "")
        if not label:
            continue

        nm = (row.get("account_nm") or "").strip()
        if not nm:
            continue

        amount = (row.get("thstrm_amount") or "").strip()
        if not amount or amount in ("-", ""):
            continue

        sections.setdefault(label, []).append(f"{nm}: {amount}")

    lines: list[str] = []
    for sec in ("BS", "IS", "CF", "SCE"):
        if sec in sections:
            lines.append(f"[{sec}]")
            lines.extend(sections[sec])

    text = "\n".join(lines)
    return text[:max_chars] if len(text) > max_chars else text


# ── 경로A 메인 함수 ────────────────────────────────────────────────────────

def get_financial_data_path_a(
    corp_code: str,
    year: int,
    reprt_code: str,
    log_fn=None,
) -> dict:
    """
    경로A 메인 함수: 전체 재무제표를 조회하고 AI 독립 추출로 교차검증한다.

    1. CFS/OFS 각각 조회 가능한지 확인
    2. 둘 다 있으면 연결(CFS) 우선
    3. 연결이 없으면 별도(OFS) 사용
    4. AI 독립 추출 + Python 결과 비교 + 불일치 시 AI 판정

    Args:
        corp_code:   DART 기업 고유코드
        year:        사업연도 (int)
        reprt_code:  보고서 코드 (report_finder에서 전달, 예: "11011")

    Returns:
        {
            "items":          {항목명: 금액(float|None), ...},
            "fs_div":         "CFS" | "OFS" | None,
            "error":          오류 메시지 문자열 (정상이면 None),
            "ai_comparison":  AI 비교 결과 딕셔너리 | None,
        }
    """
    import time as _time
    _log = log_fn or (lambda tag, msg: None)

    bsns_year = str(year)
    empty = {"items": {}, "fs_div": None, "error": None, "ai_comparison": None}

    last_error = ""
    available_statements: dict[str, dict] = {}

    # 1. 어떤 재무제표 구분이 실제로 존재하는지 확인
    for requested_div in (FS_DIV["consolidated"], FS_DIV["separate"]):
        _log("DATA_A", f"  {requested_div} 재무제표 API 호출 중...")
        t0 = _time.perf_counter()
        try:
            raw = fetch_full_financial_statement(corp_code, bsns_year, reprt_code, requested_div)
        except (ValueError, requests.RequestException) as e:
            elapsed = _time.perf_counter() - t0
            last_error = str(e)
            _log("DATA_A", f"  {requested_div} 실패 ({elapsed:.2f}초): {e}")
            continue
        elapsed = _time.perf_counter() - t0
        actual_div = _infer_fs_div(raw, requested_div)
        _log("DATA_A", f"  {requested_div} 성공 ({elapsed:.2f}초): {len(raw)}행, actual_div={actual_div}")
        available_statements[actual_div] = {
            "raw": raw,
            "requested_div": requested_div,
        }

    # 2. 연결 우선, 없으면 별도
    selected_div = None
    for candidate in (FS_DIV["consolidated"], FS_DIV["separate"]):
        if candidate in available_statements:
            selected_div = candidate
            break

    if selected_div is None:
        empty["error"] = last_error or "재무제표 데이터를 가져올 수 없습니다."
        _log("DATA_A", f"  사용 가능한 재무제표 없음: {last_error}")
        return empty

    _log("DATA_A", f"  선택된 재무제표: {selected_div}")
    selected = available_statements[selected_div]
    items = extract_target_items(selected["raw"])
    matched = sum(1 for v in items.values() if v is not None)
    _log("DATA_A", f"  Python 추출: {len(items)}개 항목 중 {matched}개 매칭")

    # 3. AI 독립 추출 + Python 결과 비교 (GEMINI_API_KEY가 있을 때만)
    ai_comparison = None
    try:
        from config import GEMINI_API_KEY
        if GEMINI_API_KEY:
            table_text = format_raw_for_ai(selected["raw"])
            if table_text.strip():
                _log("AI", "  AI 비교 추출 시작...")
                t0 = _time.perf_counter()
                from ai_module.gemini_parser import extract_with_ai_comparison
                ai_comparison = extract_with_ai_comparison(table_text, items, log_fn=log_fn)
                elapsed = _time.perf_counter() - t0
                _log("AI", f"  AI 비교 완료 ({elapsed:.2f}초): source={ai_comparison.get('source')}, calls={ai_comparison.get('ai_calls')}")
                items = ai_comparison["items"]  # 최종 결과 사용
    except Exception as e:
        _log("AI", f"  AI 비교 실패: {e}")

    return {
        "items":          items,
        "fs_div":         selected_div,
        "error":          None,
        "ai_comparison":  ai_comparison,
    }
