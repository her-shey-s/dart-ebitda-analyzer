"""
financial/doc_extractor.py
경로B: DART 감사보고서 문서 파싱

DART의 document.xml API로 보고서 ZIP을 다운로드하고
DART XML 포맷(dart4.xsd)의 재무제표 테이블을 파싱하여
항목별 금액을 추출한다.

DART 문서 포맷 특징:
  - ZIP 속 XML 파일 (dart3.xsd / dart4.xsd)
  - TABLE / TR / TD·TU·TE 태그 (HTML 유사)
  - 금액은 원(KRW) 단위의 숫자 문자열, 음수는 괄호 표기 (예: (20,283,010,877))
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
    # 1. DART 주석 참조 제거 — 두 가지 형식 모두 처리
    #    꺾쇠형: <주석3,16>  /  괄호형: (주석15,16,21)
    text = re.sub(r"<주석[\d,\s]+>", "", text)
    text = re.sub(r"\(주석[\d,\s]+\)", "", text)
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
) -> dict[str, list[list[list[str]]]]:
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
        {"BS": [...], "IS": [...], "CF": [...], "NOTES": [...]}
    """
    # 1. 모든 TITLE 태그의 위치 + 섹션명 수집
    title_positions: list[tuple[int, str]] = []  # (source_pos, section_key)
    section_key_map = {
        "재무상태표": "BS",
        "손익계산서": "IS",
        "포괄손익계산서": "IS",
        "자본변동표": "EQ",
        "현금흐름표": "CF",
        "주석": "NOTES",
    }

    for title_tag in soup.find_all("title"):
        raw = title_tag.get_text(strip=True)
        norm = _normalize_title(raw)
        # TITLE 텍스트에 섹션명이 포함되어 있으면 매핑
        for section_name, key in section_key_map.items():
            if section_name in norm:
                # sourcepos 대용: 태그의 문서 내 순서를 유지하기 위해 리스트 순서 사용
                title_positions.append((id(title_tag), key))
                break

    # 2. 모든 TABLE 태그를 순회하면서 직전 TITLE의 섹션에 배정
    #    soup의 descendants 순서 = 문서 순서이므로, TITLE과 TABLE을 함께 순회
    result: dict[str, list[list[list[str]]]] = {
        "BS": [], "IS": [], "CF": [], "EQ": [], "NOTES": [],
    }

    current_section: str | None = None
    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
            for section_name, key in section_key_map.items():
                if section_name in norm:
                    current_section = key
                    break
        elif tag.name == "table" and current_section is not None:
            rows = _xml_table_to_rows(tag)
            if rows:
                result[current_section].append(rows)

    return result


def _infer_path_b_fs_div(
    soup: BeautifulSoup,
    report_type: Optional[str] = None,
) -> str:
    """
    경로B 문서의 재무제표 기준(CFS/OFS)을 추론한다.

    report_finder가 이미 연결/별도 보고서 유형을 식별했으면 그 값을 우선 사용한다.
    """
    if report_type == "audit_consol":
        return "CFS"
    if report_type == "audit_separate":
        return "OFS"

    for title_tag in soup.find_all("title"):
        norm = _normalize_title(title_tag.get_text(strip=True))
        if "연결" in norm and any(kw in norm for kw in ("재무상태표", "손익계산서", "포괄손익계산서", "현금흐름표", "재무제표주석")):
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
                    for cj in range(ci + 1, min(end_col, len(sub_header))):
                        snorm = sub_header[cj].replace(" ", "").strip()
                        if snorm in ("합계", "총계"):
                            return cj
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

def find_item_in_table(
    rows: list[list[str]],
    keywords: list[str],
    negate_keywords: list[str] | None = None,
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

    Args:
        rows:            _xml_table_to_rows() 반환값
        keywords:        config.FINANCIAL_ITEMS의 keywords (정규화된 형태)
        negate_keywords: 부호 반전이 필요한 키워드 목록 (선택)

    Returns:
        금액(float) 또는 None
    """
    if not rows:
        return None

    keyword_set = set(keywords)
    negate_set  = set(negate_keywords) if negate_keywords else set()

    # 주석 컬럼 인덱스 탐지: 헤더 행에 "주석" 포함 → 금액 컬럼 탐색 시 제외
    # 공백 제거 후 비교: DART XML에서 "주  석", "주   석" 등 변종 대응
    header = rows[0] if rows else []
    skip_cols = {
        ci for ci, cell in enumerate(header)
        if "주석" in re.sub(r"\s+", "", cell)
    }

    # 당기 컬럼 탐지
    current_col = _detect_current_column(rows)

    for row in rows:
        if not row:
            continue
        norm = normalize_label(row[0])
        if norm in keyword_set:
            # 당기 컬럼이 탐지된 경우: 해당 컬럼만 참조
            if current_col is not None and current_col < len(row):
                val = _to_float(row[current_col])
                if val is None:
                    # '-' 등 비숫자 → 항목 행이 존재하므로 0 처리
                    val = 0.0
                if norm in negate_set and val > 0:
                    val = -val
                return val

            # 당기 컬럼 미탐지 시 폴백: 첫 번째 숫자 컬럼
            # → 이중 컬럼 구조(소계/상세 행이 다른 col에 값)에서도 동작
            for ci in range(1, len(row)):
                if ci in skip_cols:
                    continue
                val = _to_float(row[ci])
                if val is not None:
                    if norm in negate_set and val > 0:
                        val = -val
                    return val

    return None


def _extract_all_items(soup: BeautifulSoup) -> dict[str, Optional[float]]:
    """
    파싱된 DART XML에서 모든 FINANCIAL_ITEMS를 추출한다.

    1차: TITLE 태그 기반 섹션 경계로 테이블 분류
         - BS 항목: <TITLE>재무상태표 ~ <TITLE>손익계산서 사이 테이블
         - IS 항목: <TITLE>손익계산서 ~ <TITLE>현금흐름표 사이 테이블
    2차: TITLE이 없는 문서를 위한 fallback → 키워드 기반 테이블 분류

    주석 섹션은 제외하지 않음: 추후 감가상각비 등 주석 기반 추출 확장 가능.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        항목명 → 금액(float|None) 딕셔너리
    """
    result: dict[str, Optional[float]] = {item["name"]: None for item in FINANCIAL_ITEMS}

    # 1차: TITLE 태그 기반 섹션 분류
    section_map = _build_section_table_map(soup)
    bs_tables = section_map["BS"]
    is_tables = section_map["IS"]
    cf_tables = section_map["CF"]

    # TITLE 기반 분류 실패 시 (BS/IS 모두 비어있으면) 키워드 fallback
    if not bs_tables and not is_tables:
        for table_tag in soup.find_all("table"):
            rows = _xml_table_to_rows(table_tag)
            if not rows:
                continue
            fs_type = _classify_table(rows)
            if fs_type == "BS":
                bs_tables.append(rows)
            elif fs_type == "IS":
                is_tables.append(rows)

    # fs_type별 테이블 매핑
    tables_by_type = {"BS": bs_tables, "IS": is_tables, "CF": cf_tables}

    for item in FINANCIAL_ITEMS:
        search_order = tables_by_type.get(item["fs_type"], is_tables)

        for rows in search_order:
            val = find_item_in_table(rows, item["keywords"], item.get("negate_keywords"))
            if val is not None:
                result[item["name"]] = val
                break

    return result


def _extract_table_text_for_ai(soup: BeautifulSoup) -> str:
    """
    BS/IS/CF 테이블의 텍스트를 AI 재추출용으로 압축하여 반환한다.

    TITLE 기반 섹션 분류된 테이블만 포함하여 불필요한 텍스트를 줄인다.
    각 행은 '항목명: 값' 형태로 변환한다.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        테이블 텍스트 문자열 (최대 8000자)
    """
    section_map = _build_section_table_map(soup)
    lines: list[str] = []

    for fs_type in ("BS", "IS", "CF"):
        tables = section_map.get(fs_type, [])
        # TITLE 기반이 비어있으면 키워드 fallback (BS/IS만)
        if not tables and fs_type in ("BS", "IS"):
            for table_tag in soup.find_all("table"):
                rows = _xml_table_to_rows(table_tag)
                if rows and _classify_table(rows) == fs_type:
                    tables.append(rows)

        for rows in tables:
            lines.append(f"[{fs_type}]")
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

    # 1. 다운로드
    _log("DATA_B", f"  document.xml ZIP 다운로드 중 (rcept_no={rcept_no})...")
    t0 = _time.perf_counter()
    xml_bytes = _download_dart_document(rcept_no)
    elapsed = _time.perf_counter() - t0
    if xml_bytes is None:
        _log("DATA_B", f"  다운로드 실패 ({elapsed:.2f}초)")
        return {
            "items": empty_items,
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
            "fs_div": None, "error": "DART XML 파싱 실패",
            "ai_comparison": None,
        }
    _log("DATA_B", f"  XML 파싱 완료 ({elapsed:.2f}초)")

    fs_div = _infer_path_b_fs_div(soup, report_type=report_type)
    _log("DATA_B", f"  재무제표 기준: {fs_div} (report_type={report_type})")

    # 3. Python 항목 추출
    t0 = _time.perf_counter()
    items = _extract_all_items(soup)
    elapsed = _time.perf_counter() - t0
    matched = sum(1 for v in items.values() if v is not None)
    _log("DATA_B", f"  Python 추출 완료 ({elapsed:.2f}초): {len(items)}개 항목 중 {matched}개 매칭")

    # 4. AI 추출 + 비교 (GEMINI_API_KEY가 있을 때만)
    ai_comparison = None
    try:
        from config import GEMINI_API_KEY
        if GEMINI_API_KEY:
            table_text = _extract_table_text_for_ai(soup)
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
        "items":         items,
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
    """HTML 재무제표에서 항목을 파싱한다. (레거시 호환 / HTML 전용)"""
    tables = extract_tables_from_html(html)
    result: dict[str, Optional[float]] = {item["name"]: None for item in FINANCIAL_ITEMS}

    for item in FINANCIAL_ITEMS:
        for df in tables:
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
                        result[item["name"]] = val
                        break
            if result[item["name"]] is not None:
                break

    return result
