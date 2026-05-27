"""
financial/doc_extractor.py
경로B: DART 감사보고서 문서 파싱

DART의 document.xml API로 보고서 ZIP을 다운로드하고
DART XML 포맷(dart4.xsd)의 재무제표 테이블을 파싱하여
항목별 금액을 추출한다.

DART 문서 포맷 특징:
  - ZIP 속 XML 파일 (dart3.xsd / dart4.xsd)
  - TABLE / TR / TD·TU·TE 태그 (HTML 유사)
  - 금액은 KRW 기반 숫자 문자열이며, 테이블별 단위(원/천원/백만원/억원)가
    캡션이나 헤더에 명시된다. 추출 후 ``utils.units``로 원 단위로 환산한다.
  - 음수는 괄호 표기 (예: (20,283,010,877))
  - 항목명에 주석 참조 포함 (예: "I. 매출액<주석20>")
  - 항목명에 전각 공백 혼재 (예: "자      산      총      계")
"""

import re
import warnings
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from config import FINANCIAL_ITEMS, MAX_HTML_SIZE_MB, REQUEST_TIMEOUT
from dart_api.xml_utils import (
    download_dart_document as _download_dart_document,
    normalize_title as _normalize_title,
    parse_dart_xml as _parse_dart_xml,
    xml_table_to_rows as _xml_table_to_rows,
)
from utils.units import (
    detect_unit_multiplier as _detect_unit_multiplier,
    detect_unit_from_rows as _detect_unit_from_rows,
    detect_unit_in_text as _detect_unit_in_text,
    unit_label as _unit_label,
)
from financial.extraction_result import (
    details_to_items,
    empty_item_detail,
    make_item_detail,
    reconcile_details_with_final_items,
)

# 재무상태표 판별 키워드
_BS_KEYWORDS = {"자산총계", "부채총계", "자본총계", "유동자산", "비유동자산"}
# 손익계산서 판별 키워드
_IS_KEYWORDS = {"매출액", "영업이익", "당기순이익", "매출총이익", "영업수익", "연결당기순이익"}


# ── 전처리 함수 ────────────────────────────────────────────────────────────

def normalize_label(text: str) -> str:
    """
    재무제표 항목명을 정규화하여 keywords 매칭에 사용할 형태로 변환한다.

    처리 순서:
      1. DART 주석 참조 제거: <주석XX>, (주석XX) 등 양쪽 형식
      2. 모든 공백 제거 (스페이스·탭·nbsp·전각공백·제로폭공백 등)
      3. 선두 로마자 번호 + 마침표 제거 (I., II., III. 등, 대소문자)
      4. 선두 아라비아 숫자 번호 + 마침표 제거 (1., 2. 등)
      5. 선두 한글 목차 번호 + 마침표 제거 (가., 나. 등)
      6. 선두 괄호형 번호 제거 ((1), (2) 등)
      7. 앞뒤 마침표 정리

    Examples:
        "I. 매출액<주석20>"         → "매출액"
        "매출원가(주석15,16,21)"    → "매출원가"
        "자      산      총      계" → "자산총계"
        "III. 영 업 이 익"           → "영업이익"
        "V. 영업이익(손실)"          → "영업이익(손실)"
        "(2)영업이익(손실)"          → "영업이익(손실)"
    """
    # 1. DART 주석 참조 제거 — 여러 표기 형식 처리
    #    꺾쇠형: <주석3,16> / 괄호형: (주석15,16,21), (주23,31)
    text = re.sub(r"<주석?\s*[\d,\s]+>", "", text)
    text = re.sub(r"\(주석?\s*[\d,\s]+\)", "", text)
    # 2. 모든 공백 변종 제거
    text = re.sub(r"[\s\u3000\xa0\u00a0\u200b\u200c\u200d\ufeff]+", "", text)
    # 3. 선두 로마자 번호 + 마침표 (ASCII: I., II. 등 / 전각 유니코드: Ⅰ Ⅱ … Ⅻ)
    text = re.sub(r"^[\u2160-\u216B]+\.?", "", text)   # 전각 Unicode Roman (Ⅰ–Ⅻ)
    text = re.sub(r"^[IVXivx]+\.", "", text)            # ASCII Roman
    # 4. 선두 아라비아 숫자 + 마침표
    text = re.sub(r"^\d+\.", "", text)
    # 5. 선두 한글 목차 번호 + 마침표
    text = re.sub(r"^[가-힣]\.", "", text)
    # 6. 선두 괄호형 번호
    text = re.sub(r"^\(\d+\)", "", text)
    # 7. 앞뒤 마침표
    text = text.strip(".")
    return text


def _to_float(value) -> Optional[float]:
    """
    금액 셀 값을 float으로 변환한다.

    처리:
      - 쉼표 제거: "1,234,567" → 1234567.0
      - 괄호 음수: "(20,283,010)" → -20283010.0
      - 대시(-), 빈값 → None
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "N/A", ""):
        return None
    text = text.replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _is_numeric_cell(val: str) -> bool:
    """셀 값이 금액 숫자인지 판별한다."""
    return _to_float(val) is not None


# ── DART XML 다운로드 및 파싱 ──────────────────────────────────────────────

def _build_section_table_map(
    soup: BeautifulSoup,
) -> dict[str, list[tuple]]:
    """
    TITLE 태그 위치를 기준으로 각 섹션에 속하는 테이블을 분류한다.

    DART XML 구조:
      <TITLE>재 무 상 태 표</TITLE>  (pos A)
      <TABLE> ... </TABLE>           ← BS 테이블
      <TITLE>손 익 계 산 서</TITLE>  (pos B)
      <TABLE> ... </TABLE>           ← IS 테이블
      ...

    각 테이블은 자신 바로 앞에 있는 TITLE의 섹션에 배정된다.

    Returns:
        {"BS": [(tag, rows, scope), ...], "IS": [...], "CF": [...], "NOTES": [...]}
        tag는 단위 감지에 사용되고, scope는 "CFS"/"OFS"/None (직전 재무제표 섹션
        헤더 기준 연결/별도 구간)이다.
    """
    section_key_map = {
        "재무상태표": "BS",
        "손익계산서": "IS",
        "포괄손익계산서": "IS",
        "자본변동표": "EQ",
        "현금흐름표": "CF",
        "주석": "NOTES",
    }

    # 모든 TABLE 태그를 순회하면서 직전 TITLE의 섹션에 배정
    # soup의 descendants 순서 = 문서 순서이므로, TITLE과 TABLE을 함께 순회
    result: dict[str, list[tuple]] = {
        "BS": [], "IS": [], "CF": [], "EQ": [], "NOTES": [],
    }

    current_section: str | None = None
    current_scope: str | None = None
    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
            scope = _title_scope(norm)
            if scope is not None:
                current_scope = scope
            for section_name, key in section_key_map.items():
                if section_name in norm:
                    current_section = key
                    break
        elif tag.name == "table" and current_section is not None:
            rows = _xml_table_to_rows(tag)
            if rows:
                result[current_section].append((tag, rows, current_scope))

    return result


# 연결/별도 스코프 판별에 쓰는 합계성 행 라벨 (정규화 후)
_SCOPE_TOTAL_LABELS = {
    "자산총계", "부채총계", "자본총계",
    "매출액", "영업수익", "매출총이익", "영업이익", "당기순이익",
}
# 재무제표 본문 타이틀로 인정하는 키워드 (섹션 헤더 "연결재무제표"는 제외)
_STATEMENT_TITLE_KEYWORDS = ("재무상태표", "손익계산서", "포괄손익계산서", "현금흐름표")


def _title_scope(norm_title: str) -> Optional[str]:
    """
    TITLE이 재무제표 섹션 경계이면 그 연결/별도 스코프("CFS"/"OFS")를, 아니면 None.

    재무제표 섹션 경계는 본문 타이틀(재무상태표 등) 또는 섹션 헤더("2.연결재무제표",
    "4.재무제표")처럼 "재무제표"를 포함하는 타이틀이다. 경계가 아닌 타이틀은 스코프
    구간을 바꾸지 않으므로 None을 돌려준다(_has_populated_consolidated_statements와 동일).
    """
    is_fs_section = (
        any(k in norm_title for k in _STATEMENT_TITLE_KEYWORDS)
        or ("재무제표" in norm_title)
    )
    if not is_fs_section:
        return None
    return "CFS" if "연결" in norm_title else "OFS"


def _classify_tables_with_scope(soup: BeautifulSoup) -> dict[str, list[tuple]]:
    """
    TITLE 기반 섹션 분류가 실패한 문서를 위한 키워드 기반 fallback.

    문서 순서대로 모든 테이블을 키워드로 BS/IS 분류하면서, 직전 재무제표 섹션
    헤더 기준 연결/별도 스코프를 함께 기록한다.

    Returns:
        {"BS": [(tag, rows, scope), ...], "IS": [...]}
    """
    out: dict[str, list[tuple]] = {"BS": [], "IS": []}
    current_scope: Optional[str] = None
    for tag in soup.descendants:
        if tag.name == "title":
            scope = _title_scope(_normalize_title(tag.get_text(strip=True)))
            if scope is not None:
                current_scope = scope
        elif tag.name == "table":
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue
            cls = _classify_table(rows)
            if cls in ("BS", "IS"):
                out[cls].append((tag, rows, current_scope))
    return out


def _filter_tables_by_scope(tables: list[tuple], fs_div: Optional[str]) -> list[tuple]:
    """
    연결/별도 혼재 문서에서 해결된 fs_div 스코프의 테이블만 남긴다.

    해당 스코프의 테이블이 하나라도 있으면 그 스코프로 제한하고(다른 스코프·스코프
    미상 테이블 제외), 없으면 원본 그대로 둔다. 단일 스코프/스코프 미상 문서(별도
    감사보고서 등)를 과도하게 비우지 않기 위함이다.
    """
    if not fs_div:
        return tables
    matching = [t for t in tables if len(t) > 2 and t[2] == fs_div]
    return matching if matching else tables


def _table_has_meaningful_amount(rows: list[list[str]]) -> bool:
    """합계성 행(자산총계·매출액 등)에 0이 아닌 실제 금액이 있으면 True."""
    for row in rows:
        if not row:
            continue
        if normalize_label(row[0]) in _SCOPE_TOTAL_LABELS:
            for cell in row[1:]:
                v = _to_float(cell)
                if v is not None and abs(v) > 0:
                    return True
    return False


def _has_populated_consolidated_statements(soup: BeautifulSoup) -> bool:
    """
    연결 재무제표 섹션에 '실제 데이터가 채워진' 표가 있는지 확인한다.

    사업보고서 본문은 별도 전용 회사도 "2.연결재무제표"·"3.연결재무제표주석" 같은
    빈 섹션 헤더 타이틀을 포함한다. 따라서 타이틀 문자열만으로 연결을 판정하면
    별도 전용 회사를 연결로 오판한다. 섹션 헤더로 연결/별도 구간을 구분한 뒤,
    연결 구간 안에 합계성 금액이 채워진 표가 있을 때만 연결로 본다.
    """
    in_consolidated = False
    for tag in soup.descendants:
        if tag.name == "title":
            norm = _normalize_title(tag.get_text(strip=True))
            has_stmt = any(k in norm for k in _STATEMENT_TITLE_KEYWORDS)
            is_fs_section = has_stmt or ("재무제표" in norm)
            if not is_fs_section:
                continue  # 본문과 무관한 타이틀은 구간을 바꾸지 않음
            in_consolidated = "연결" in norm
        elif tag.name == "table" and in_consolidated:
            rows = _xml_table_to_rows(tag)
            if rows and _table_has_meaningful_amount(rows):
                return True
    return False


def _infer_path_b_fs_div(
    soup: BeautifulSoup,
    report_type: Optional[str] = None,
) -> str:
    """
    경로B 문서의 재무제표 기준(CFS/OFS)을 추론한다.

    report_finder가 이미 연결/별도 보고서 유형을 식별했으면 그 값을 우선 사용한다.
    그 외(사업보고서 본문 등)에는 연결 재무제표가 '데이터와 함께' 실재할 때만
    CFS로 판정한다. 빈 연결 섹션 헤더만 있는 별도 전용 회사는 OFS로 본다.
    """
    if report_type == "audit_consol":
        return "CFS"
    if report_type == "audit_separate":
        return "OFS"

    if _has_populated_consolidated_statements(soup):
        return "CFS"
    return "OFS"


def _classify_table(rows: list[list[str]]) -> str:
    """
    행 데이터로 테이블 유형을 분류한다. (fallback용)

    TITLE 기반 섹션 분류가 실패했을 때 키워드 기반으로 분류한다.

    Args:
        rows: _xml_table_to_rows() 반환값

    Returns:
        "BS" (재무상태표) | "IS" (손익계산서) | "unknown"
    """
    labels = {normalize_label(row[0]) for row in rows if row}
    if labels & _BS_KEYWORDS:
        return "BS"
    if labels & _IS_KEYWORDS:
        return "IS"
    return "unknown"


# ── 당기 컬럼 탐지 ────────────────────────────────────────────────────────

def _detect_current_column(rows: list[list[str]]) -> Optional[int]:
    """
    테이블 헤더에서 당기(현재 기간) 컬럼 인덱스를 탐지한다.

    우선순위:
      1. "당기" 포함 (전기/전전기 제외)
      2. "제 N 기" 패턴 — 가장 큰 N이 당기
      3. "20XX" 연도 패턴 — 가장 최신 연도가 당기

    머리 5행을 탐색 대상으로 한다.
    """
    header_rows = rows[:5]
    max_cols = max((len(r) for r in rows), default=0)

    # 1. "당기" 직접 매칭
    for header in header_rows:
        for ci, cell in enumerate(header):
            norm = cell.replace(" ", "").strip()
            if "당기" in norm and "전기" not in norm and "전전기" not in norm:
                # 복합 헤더 대응 (비용의 성격별 분류 등):
                # "당기" 아래 매출원가/판관비/합계 하위 컬럼이 있으면 "합계" 우선
                end_col = len(header)
                for cj in range(ci + 1, len(header)):
                    cnorm = header[cj].replace(" ", "").strip()
                    if "전기" in cnorm or "전전기" in cnorm:
                        end_col = cj
                        break
                for sub_header in header_rows:
                    if len(sub_header) > len(header):
                        search_end = len(sub_header)
                    else:
                        search_end = end_col
                    for cj in range(ci + 1, min(search_end, len(sub_header))):
                        snorm = sub_header[cj].replace(" ", "").strip()
                        if snorm in ("합계", "총계"):
                            offset = max_cols - len(sub_header)
                            return cj + max(offset, 0)
                return ci

    # 2. "제 N 기" 패턴 — 가장 큰 N
    gi_candidates: list[tuple[int, int]] = []
    for header in header_rows:
        for ci, cell in enumerate(header):
            m = re.search(r"제\s*(\d+)\s*기", cell)
            if m:
                gi_candidates.append((int(m.group(1)), ci))
    if gi_candidates:
        gi_candidates.sort(key=lambda x: (-x[0], x[1]))
        return gi_candidates[0][1]

    # 3. 20XX 연도 — 가장 최신
    year_candidates: list[tuple[int, int]] = []
    for header in header_rows:
        for ci, cell in enumerate(header):
            for m in re.finditer(r"(20\d{2})", cell):
                year_candidates.append((int(m.group(1)), ci))
    if year_candidates:
        year_candidates.sort(key=lambda x: (-x[0], x[1]))
        return year_candidates[0][1]

    return None


# ── 항목 추출 ──────────────────────────────────────────────────────────────

def _detect_table_unit_info(table_tag, rows: list[list[str]]) -> tuple[int, str]:
    """Return (unit_multiplier, confidence_label) for a parsed table."""
    unit = _detect_unit_multiplier(table_tag, fallback=0)
    if unit:
        return unit, "detected_from_tag"
    unit = _detect_unit_from_rows(rows, fallback=0)
    if unit:
        return unit, "detected_from_rows"
    return 1, "assumed_won"


def _column_label(rows: list[list[str]], col_idx: int) -> Optional[str]:
    """Build a compact human-readable label for a table column."""
    parts: list[str] = []
    for row in rows[:5]:
        if col_idx >= len(row):
            continue
        cell = re.sub(r"\s+", " ", row[col_idx]).strip()
        if not cell or _to_float(cell) is not None:
            continue
        if cell not in parts:
            parts.append(cell)
    return " / ".join(parts) if parts else None


def _make_doc_detail(
    item_name: str,
    *,
    value: Optional[float],
    raw_value: str | None,
    unit_multiplier: int,
    unit_confidence: str,
    statement: str,
    table_id: str,
    row_idx: int,
    row_label: str,
    normalized_label: str,
    col_idx: Optional[int],
    column_label: Optional[str],
    confidence: str,
    flags: Optional[list[str]] = None,
    value_state: Optional[str] = None,
    table_scope: Optional[str] = None,
) -> dict:
    detail_flags = list(flags or [])
    if unit_confidence == "assumed_won":
        detail_flags.append("unit_assumed_won")
        if confidence == "verified":
            confidence = "low_confidence"
    return make_item_detail(
        item_name,
        value=value,
        raw_value=raw_value,
        unit_multiplier=unit_multiplier,
        source={
            "source_type": "document_xml",
            "statement": statement,
            "fs_scope": table_scope,
            "table_id": table_id,
            "row_id": f"R{row_idx + 1}",
            "row_label": row_label,
            "normalized_row_label": normalized_label,
            "column_id": f"C{col_idx + 1}" if col_idx is not None else None,
            "column_label": column_label,
            "unit_confidence": unit_confidence,
        },
        confidence=confidence,
        value_state=value_state,
        flags=detail_flags,
    )


def find_item_detail_in_table(
    item_name: str,
    rows: list[list[str]],
    keywords: list[str],
    negate_keywords: list[str] | None = None,
    unit_multiplier: int = 1,
    unit_confidence: str = "unknown",
    force_positive: bool = False,
    statement: str = "",
    table_id: str = "",
    table_scope: Optional[str] = None,
) -> Optional[dict]:
    """Find one item in rows and return a rich detail record."""
    if not rows:
        return None

    keyword_set = set(keywords)
    negate_set = set(negate_keywords) if negate_keywords else set()

    header = rows[0] if rows else []
    skip_cols = {
        ci for ci, cell in enumerate(header)
        if "주석" in re.sub(r"\s+", "", cell)
    }

    current_col = _detect_current_column(rows)

    for row_idx, row in enumerate(rows):
        if not row:
            continue
        norm = normalize_label(row[0])
        if norm not in keyword_set:
            continue

        base_flags: list[str] = ["matched_by_normalized_label"]

        if current_col is not None and current_col < len(row):
            raw = row[current_col]
            val = _to_float(raw)
            flags = list(base_flags)
            confidence = "verified"
            value_state = "extracted"
            if val is None:
                val = 0.0
                confidence = "low_confidence"
                value_state = "zero_assumed_from_non_numeric_current_cell"
                flags.append("non_numeric_current_cell_zero_assumed")
            val = val * unit_multiplier
            if force_positive and val < 0:
                val = abs(val)
                flags.append("force_positive")
            if norm in negate_set and val > 0:
                val = -val
                flags.append("negated_by_label")
            return _make_doc_detail(
                item_name,
                value=val,
                raw_value=raw,
                unit_multiplier=unit_multiplier,
                unit_confidence=unit_confidence,
                statement=statement,
                table_id=table_id,
                row_idx=row_idx,
                row_label=row[0],
                normalized_label=norm,
                col_idx=current_col,
                column_label=_column_label(rows, current_col),
                confidence=confidence,
                flags=flags,
                value_state=value_state,
                table_scope=table_scope,
            )

        for ci in range(1, len(row)):
            if ci in skip_cols:
                continue
            raw = row[ci]
            val = _to_float(raw)
            if val is None:
                continue
            flags = base_flags + ["current_column_not_detected", "first_numeric_column_used"]
            val = val * unit_multiplier
            if force_positive and val < 0:
                val = abs(val)
                flags.append("force_positive")
            if norm in negate_set and val > 0:
                val = -val
                flags.append("negated_by_label")
            return _make_doc_detail(
                item_name,
                value=val,
                raw_value=raw,
                unit_multiplier=unit_multiplier,
                unit_confidence=unit_confidence,
                statement=statement,
                table_id=table_id,
                row_idx=row_idx,
                row_label=row[0],
                normalized_label=norm,
                col_idx=ci,
                column_label=_column_label(rows, ci),
                confidence="low_confidence",
                flags=flags,
                table_scope=table_scope,
            )

    return None

def find_item_in_table(
    rows: list[list[str]],
    keywords: list[str],
    negate_keywords: list[str] | None = None,
    unit_multiplier: int = 1,
    force_positive: bool = False,
) -> Optional[float]:
    """
    행 리스트에서 keywords와 일치하는 항목명을 찾아 당기 금액을 반환한다.

    - 첫 번째 컬럼이 항목명, 이후 컬럼 중 당기 컬럼의 값을 반환
    - 당기 컬럼을 헤더에서 탐지하고, 탐지 실패 시 첫 번째 숫자 컬럼 폴백
    - 당기 컬럼의 값이 '-'이면 0.0 반환 (항목이 존재하지만 값이 0인 경우)
    - normalize_label() 적용 후 keywords와 정확히 일치(==)하는지 비교
      → 부분 일치 사용 안 함: "영업외이익" ≠ "영업이익"
    - negate_keywords: 해당 키워드로 매칭되면 양수 값을 음수로 반전
      (예: "영업손실" → 12억 → -12억)
    - force_positive: 감사보고서에서 비용 항목을 괄호 음수로 표기해도 양수로 반환
    - unit_multiplier: 테이블 단위 승수(1=원, 1000=천원, 1_000_000=백만원, ...).
      반환 값에 곱해 원(KRW) 기준 금액을 돌려준다.

    Args:
        rows:             _xml_table_to_rows() 반환값
        keywords:         config.FINANCIAL_ITEMS의 keywords (정규화된 형태)
        negate_keywords:  부호 반전이 필요한 키워드 목록 (선택)
        unit_multiplier:  단위 승수 (기본 1=원)
        force_positive:   True이면 반환값을 양수로 정규화

    Returns:
        금액(float, 원 단위) 또는 None
    """
    detail = find_item_detail_in_table(
        "_legacy",
        rows,
        keywords,
        negate_keywords=negate_keywords,
        unit_multiplier=unit_multiplier,
        force_positive=force_positive,
    )
    return detail.get("value") if detail else None


def _extract_all_item_details(
    soup: BeautifulSoup,
    fs_div: Optional[str] = None,
) -> dict[str, dict]:
    """
    파싱된 DART XML에서 모든 FINANCIAL_ITEMS를 추출한다.

    1차: TITLE 태그 기반 섹션 경계로 테이블 분류
         - BS 항목: <TITLE>재무상태표 ~ <TITLE>손익계산서 사이 테이블
         - IS 항목: <TITLE>손익계산서 ~ <TITLE>현금흐름표 사이 테이블
    2차: TITLE이 없는 문서를 위한 fallback → 키워드 기반 테이블 분류

    주석 섹션은 제외하지 않음: 추후 감가상각비 등 주석 기반 추출 확장 가능.

    fs_div가 주어지면 연결/별도 혼재 문서에서 그 스코프의 테이블만 읽는다.
    사업보고서 본문은 빈 연결재무제표 섹션과 채워진 별도 재무제표 섹션을 함께
    담을 수 있어, 스코프 필터 없이는 첫 매칭(빈 연결) 표를 읽어 0을 반환한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS"/"OFS" (선택). 혼재 문서에서 읽을 스코프 제한.

    Returns:
        항목명 → item_details 딕셔너리
    """
    result: dict[str, dict] = {
        item["name"]: empty_item_detail(
            item["name"],
            source={"source_type": "document_xml", "statement": item["fs_type"]},
            flags=["not_found"],
        )
        for item in FINANCIAL_ITEMS
    }

    # 1차: TITLE 태그 기반 섹션 분류 (스코프 포함)
    section_map = _build_section_table_map(soup)
    bs_tables = section_map["BS"]
    is_tables = section_map["IS"]
    cf_tables = section_map["CF"]

    # TITLE 기반 분류 실패 시 (BS/IS 모두 비어있으면) 키워드 fallback (스코프 포함)
    if not bs_tables and not is_tables:
        fallback = _classify_tables_with_scope(soup)
        bs_tables = fallback["BS"]
        is_tables = fallback["IS"]

    # 연결/별도 혼재 문서면 해결된 스코프의 표만 남긴다.
    bs_tables = _filter_tables_by_scope(bs_tables, fs_div)
    is_tables = _filter_tables_by_scope(is_tables, fs_div)
    cf_tables = _filter_tables_by_scope(cf_tables, fs_div)

    def _table_entries(fs_type: str, tables: list[tuple]) -> list[dict]:
        entries: list[dict] = []
        for idx, (tag, rows, scope) in enumerate(tables, start=1):
            unit, unit_confidence = _detect_table_unit_info(tag, rows)
            entries.append({
                "tag": tag,
                "rows": rows,
                "scope": scope,
                "unit": unit,
                "unit_confidence": unit_confidence,
                "statement": fs_type,
                "table_id": f"{fs_type}{idx}",
            })
        return entries

    typed_tables = {
        "BS": _table_entries("BS", bs_tables),
        "IS": _table_entries("IS", is_tables),
        "CF": _table_entries("CF", cf_tables),
    }

    for item in FINANCIAL_ITEMS:
        search_order = typed_tables.get(item["fs_type"], typed_tables["IS"])

        for entry in search_order:
            detail = find_item_detail_in_table(
                item["name"],
                entry["rows"],
                item["keywords"],
                negate_keywords=item.get("negate_keywords"),
                unit_multiplier=entry["unit"],
                unit_confidence=entry["unit_confidence"],
                force_positive=item["name"] == "매출원가",
                statement=entry["statement"],
                table_id=entry["table_id"],
                table_scope=entry["scope"],
            )
            if detail is not None and detail.get("value") is not None:
                result[item["name"]] = detail
                break

    return result


def _extract_all_items(
    soup: BeautifulSoup,
    fs_div: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """
    Backward-compatible wrapper returning item -> numeric value.

    New code should prefer ``_extract_all_item_details``.
    """
    return details_to_items(_extract_all_item_details(soup, fs_div=fs_div))


def _extract_table_text_for_ai(
    soup: BeautifulSoup,
    fs_div: Optional[str] = None,
) -> str:
    """
    BS/IS/CF 테이블의 텍스트를 AI 재추출용으로 압축하여 반환한다.

    TITLE 기반 섹션 분류된 테이블만 포함하여 불필요한 텍스트를 줄인다.
    각 행은 '항목명: 값' 형태로 변환한다. fs_div가 주어지면 Python 추출과 같은
    스코프의 표만 AI에 보여, 연결/별도 혼재 문서에서 AI가 다른 스코프 표를 보고
    엇갈린 판정을 내리지 않도록 한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS"/"OFS" (선택). 혼재 문서에서 포함할 스코프 제한.

    Returns:
        테이블 텍스트 문자열 (최대 8000자)
    """
    section_map = _build_section_table_map(soup)
    fallback = None
    lines: list[str] = []

    for fs_type in ("BS", "IS", "CF"):
        tables = section_map.get(fs_type, [])
        # TITLE 기반이 비어있으면 키워드 fallback (BS/IS만)
        if not tables and fs_type in ("BS", "IS"):
            if fallback is None:
                fallback = _classify_tables_with_scope(soup)
            tables = fallback.get(fs_type, [])

        tables = _filter_tables_by_scope(tables, fs_div)

        for tag, rows, _scope in tables:
            unit = _detect_unit_multiplier(tag)
            if unit == 1:
                unit = _detect_unit_from_rows(rows, fallback=1)
            lines.append(f"[{fs_type}] (단위: {_unit_label(unit)})")
            for row in rows:
                if row and len(row) >= 2:
                    label = normalize_label(row[0])
                    vals  = " | ".join(c for c in row[1:] if c.strip())
                    if label and vals:
                        lines.append(f"{label}: {vals}")
            if len("\n".join(lines)) > 8000:
                break

    return "\n".join(lines)[:8000]


# ── 경로B 메인 함수 ────────────────────────────────────────────────────────

def get_financial_data_path_b(
    rcept_no: str,
    report_type: Optional[str] = None,
    log_fn=None,
) -> dict:
    """
    경로B 메인 함수: 감사보고서 rcept_no로 재무 데이터를 추출한다.

    내부 흐름:
      1. document.xml API로 보고서 ZIP 다운로드
      2. ZIP 내 XML 파일 추출
      3. BeautifulSoup으로 TABLE 파싱
      4. BS/IS 테이블 분류 → Python 항목별 금액 추출
      5. AI 독립 추출 + Python 결과 비교 + 불일치 시 AI 판정

    Args:
        rcept_no:    공시 접수번호 (report_finder.find_report()의 반환값)
        report_type: 보고서 유형 (선택)
        log_fn:      로그 콜백 (tag, message) → None (선택)

    Returns:
        {
            "items":          {항목명: 금액(float|None), ...},
            "fs_div":         "CFS" | "OFS",
            "error":          None | 오류 메시지 문자열,
            "ai_comparison":  AI 비교 결과 딕셔너리 | None,
        }
    """
    import time as _time
    _log = log_fn or (lambda tag, msg: None)

    empty_items = {item["name"]: None for item in FINANCIAL_ITEMS}
    empty_details = {
        item["name"]: empty_item_detail(
            item["name"],
            source={"source_type": "document_xml", "statement": item["fs_type"]},
            flags=["not_extracted"],
        )
        for item in FINANCIAL_ITEMS
    }

    # 1. 다운로드
    _log("DATA_B", f"  document.xml ZIP 다운로드 중 (rcept_no={rcept_no})...")
    t0 = _time.perf_counter()
    xml_bytes = _download_dart_document(rcept_no)
    elapsed = _time.perf_counter() - t0
    if xml_bytes is None:
        _log("DATA_B", f"  다운로드 실패 ({elapsed:.2f}초)")
        return {
            "items": empty_items,
            "item_details": empty_details,
            "fs_div": None, "error": f"document.xml 다운로드 실패 (rcept_no={rcept_no})",
            "ai_comparison": None,
        }
    _log("DATA_B", f"  다운로드 완료 ({elapsed:.2f}초, {len(xml_bytes) / 1024:.0f}KB)")

    # 2. 파싱
    t0 = _time.perf_counter()
    soup = _parse_dart_xml(xml_bytes)
    elapsed = _time.perf_counter() - t0
    if soup is None:
        _log("DATA_B", f"  XML 파싱 실패 ({elapsed:.2f}초)")
        return {
            "items": empty_items,
            "item_details": empty_details,
            "fs_div": None, "error": "DART XML 파싱 실패",
            "ai_comparison": None,
        }
    _log("DATA_B", f"  XML 파싱 완료 ({elapsed:.2f}초)")

    fs_div = _infer_path_b_fs_div(soup, report_type=report_type)
    _log("DATA_B", f"  재무제표 기준: {fs_div} (report_type={report_type})")

    # 3. Python 항목 추출 (해결된 연결/별도 스코프로 테이블 제한)
    t0 = _time.perf_counter()
    item_details = _extract_all_item_details(soup, fs_div=fs_div)
    items = details_to_items(item_details)
    elapsed = _time.perf_counter() - t0
    matched = sum(1 for v in items.values() if v is not None)
    _log("DATA_B", f"  Python 추출 완료 ({elapsed:.2f}초): {len(items)}개 항목 중 {matched}개 매칭")

    # 4. AI 추출 + 비교 (GEMINI_API_KEY가 있을 때만)
    ai_comparison = None
    try:
        from config import get_gemini_api_key
        if get_gemini_api_key():
            table_text = _extract_table_text_for_ai(soup, fs_div=fs_div)
            if table_text.strip():
                _log("AI", "  AI 비교 추출 시작...")
                t0 = _time.perf_counter()
                from ai_module.gemini_parser import extract_with_ai_comparison
                ai_comparison = extract_with_ai_comparison(table_text, items, log_fn=log_fn)
                elapsed = _time.perf_counter() - t0
                _log("AI", f"  AI 비교 완료 ({elapsed:.2f}초): source={ai_comparison.get('source')}, calls={ai_comparison.get('ai_calls')}")
                items = ai_comparison["items"]  # 최종 결과 사용
                item_details = reconcile_details_with_final_items(
                    item_details,
                    items,
                    ai_comparison=ai_comparison,
                )
    except Exception as e:
        _log("AI", f"  AI 비교 실패: {e}")

    return {
        "items":         items,
        "item_details":  item_details,
        "fs_div":        fs_div,
        "error":         None,
        "ai_comparison": ai_comparison,
    }


def is_parse_successful(result: dict[str, Optional[float]], min_found: int = 4) -> bool:
    """
    추출 결과의 성공 여부를 판단한다.

    Args:
        result:    항목명 → 금액 딕셔너리
        min_found: 성공으로 간주할 최소 항목 수

    Returns:
        True이면 파싱 성공, False이면 Gemini 폴백 필요
    """
    found = sum(1 for v in result.values() if v is not None)
    return found >= min_found


# ── HTML 파싱 (레거시 / 폴백용) ──────────────────────────────────────────────

def download_html(url: str) -> Optional[str]:
    """
    URL에서 HTML을 다운로드한다. (레거시 호환 / 오래된 공시 폴백용)

    인코딩 감지 순서: Content-Type 헤더 → HTML meta charset → UTF-8 → EUC-KR
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        content = b""
        limit = MAX_HTML_SIZE_MB * 1024 * 1024
        for chunk in resp.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > limit:
                return None

        return content.decode(_detect_encoding(content), errors="replace")
    except requests.RequestException:
        return None


def _detect_encoding(content: bytes) -> str:
    """HTML bytes에서 인코딩을 추정한다."""
    head = content[:2000].lower()
    if b"charset=utf-8" in head or b'charset="utf-8"' in head:
        return "utf-8"
    if b"euc-kr" in head or b"ks_c_5601" in head or b"cp949" in head:
        return "euc-kr"
    try:
        content.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "euc-kr"


def extract_tables_from_html(html: str) -> list[pd.DataFrame]:
    """HTML에서 pandas DataFrame 테이블 리스트를 추출한다. (레거시 호환)"""
    soup = BeautifulSoup(html, "lxml")
    tables = []
    for tag in soup.find_all("table"):
        try:
            tables.extend(pd.read_html(str(tag), thousands=","))
        except Exception:
            continue
    return tables


def parse_financial_data_from_html(html: str) -> dict[str, Optional[float]]:
    """HTML 재무제표에서 항목을 파싱한다. (레거시 호환 / HTML 전용)

    각 테이블에 단위(원/천원/백만원/억원) 표기가 있으면 감지하여 원 단위로
    환산한다. 감지 실패 시 1(원)을 가정한다.
    """
    soup = BeautifulSoup(html, "lxml")
    tables_dfs = extract_tables_from_html(html)
    table_tags = soup.find_all("table")

    # DataFrame과 table 태그를 zip으로 매칭 (extract_tables_from_html이 같은 순서로 만든다).
    # 단, pd.read_html이 일부 태그를 스킵할 수 있어 갯수 불일치 시 단위는 모두 1로 fallback.
    if len(table_tags) == len(tables_dfs):
        table_units = [_detect_unit_multiplier(t) for t in table_tags]
        for i, (t, mult) in enumerate(zip(table_tags, table_units)):
            if mult == 1:
                # 태그 단위 감지 실패 시 DataFrame 머리 5행으로 폴백
                df = tables_dfs[i]
                rows = df.head(5).astype(str).values.tolist()
                table_units[i] = _detect_unit_from_rows(rows, fallback=1)
    else:
        # 매칭 실패 시 각 DF의 머리 5행만 보고 단위 감지
        table_units = []
        for df in tables_dfs:
            rows = df.head(5).astype(str).values.tolist()
            table_units.append(_detect_unit_from_rows(rows, fallback=1))

    result: dict[str, Optional[float]] = {item["name"]: None for item in FINANCIAL_ITEMS}

    for item in FINANCIAL_ITEMS:
        for df, unit in zip(tables_dfs, table_units):
            if df.empty or df.shape[1] < 2:
                continue
            label_col = df.iloc[:, 0].astype(str)
            # 숫자 컬럼 탐색 (_to_float 기반)
            amount_col = None
            for ci in range(1, df.shape[1]):
                col_vals = df.iloc[:, ci].astype(str)
                if col_vals.apply(lambda v: _to_float(v) is not None).any():
                    amount_col = ci
                    break
            if amount_col is None:
                continue
            keyword_set = set(item["keywords"])
            for idx, label in enumerate(label_col):
                if normalize_label(label) in keyword_set:
                    val = _to_float(df.iloc[idx, amount_col])
                    if val is not None:
                        result[item["name"]] = val * unit
                        break
            if result[item["name"]] is not None:
                break

    return result
