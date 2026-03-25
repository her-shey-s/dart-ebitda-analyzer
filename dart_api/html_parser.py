"""
dart_api/html_parser.py
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

import io
import re
import warnings
import zipfile
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from config import DART_API_KEY, FINANCIAL_ITEMS, MAX_HTML_SIZE_MB, REQUEST_TIMEOUT


# ── DART document.xml API ──────────────────────────────────────────────────
_DOCUMENT_API_URL = "https://opendart.fss.or.kr/api/document.xml"

# 재무상태표 판별 키워드
_BS_KEYWORDS = {"자산총계", "부채총계", "자본총계", "유동자산", "비유동자산"}
# 손익계산서 판별 키워드
_IS_KEYWORDS = {"매출액", "영업이익", "당기순이익", "매출총이익", "영업수익", "연결당기순이익"}


# ── 전처리 함수 ────────────────────────────────────────────────────────────

def normalize_label(text: str) -> str:
    """
    재무제표 항목명을 정규화하여 keywords 매칭에 사용할 형태로 변환한다.

    처리 순서:
      1. DART 주석 참조 제거: <주석XX>, <주석XX,YY> 등
      2. 모든 공백 제거 (스페이스·탭·nbsp·전각공백·제로폭공백 등)
      3. 선두 로마자 번호 + 마침표 제거 (I., II., III. 등, 대소문자)
      4. 선두 아라비아 숫자 번호 + 마침표 제거 (1., 2. 등)
      5. 선두 한글 목차 번호 + 마침표 제거 (가., 나. 등)
      6. 선두 괄호형 번호 제거 ((1), (2) 등)
      7. 앞뒤 마침표 정리

    Examples:
        "I. 매출액<주석20>"         → "매출액"
        "자      산      총      계" → "자산총계"
        "III. 영 업 이 익"           → "영업이익"
        "V. 영업이익(손실)"          → "영업이익(손실)"
        "(2)영업이익(손실)"          → "영업이익(손실)"
    """
    # 1. DART 주석 참조 제거
    #    형식 A: <주석3,16>  (꺽쇠 태그형)
    #    형식 B: (주석15)    (괄호형 — 식품·제조업 감사보고서에서 자주 사용)
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

def _download_dart_document(rcept_no: str) -> Optional[bytes]:
    """
    DART document.xml API로 보고서 ZIP을 다운로드하고 XML bytes를 반환한다.

    ZIP 안에 XML 파일이 여럿일 경우 가장 큰 파일을 선택한다.

    Args:
        rcept_no: 공시 접수번호

    Returns:
        XML 파일 bytes 또는 None (다운로드/압축 오류 시)
    """
    try:
        resp = requests.get(
            _DOCUMENT_API_URL,
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=REQUEST_TIMEOUT * 2,
            stream=True,
        )
        resp.raise_for_status()

        content = b""
        limit = MAX_HTML_SIZE_MB * 1024 * 1024
        for chunk in resp.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > limit:
                return None

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # 가장 큰 파일 선택 (보통 하나지만 여럿일 수 있음)
            names = zf.namelist()
            target = max(names, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(target)

    except (requests.RequestException, zipfile.BadZipFile, KeyError):
        return None


def _parse_dart_xml(xml_bytes: bytes) -> Optional[BeautifulSoup]:
    """
    DART XML bytes를 BeautifulSoup으로 파싱한다.

    DART XML은 dart4.xsd 커스텀 스키마를 사용하므로 lxml HTML 파서로 처리한다.

    Args:
        xml_bytes: _download_dart_document() 반환값

    Returns:
        BeautifulSoup 객체 또는 None
    """
    try:
        return BeautifulSoup(xml_bytes, "lxml")
    except Exception:
        return None


def _xml_table_to_rows(table_tag) -> list[list[str]]:
    """
    BeautifulSoup table 태그를 문자열 행 리스트로 변환한다.

    DART XML의 td / th / tu(금액셀) / te 모두 처리한다.

    Args:
        table_tag: BeautifulSoup의 <table> 태그

    Returns:
        [[셀1, 셀2, ...], ...] 형태의 행 리스트
    """
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = tr.find_all(["td", "th", "tu", "te"])
        row = [c.get_text(strip=True) for c in cells]
        if any(row):  # 빈 행 제외
            rows.append(row)
    return rows


def _classify_table(rows: list[list[str]]) -> str:
    """
    행 데이터로 테이블 유형을 분류한다.

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


# ── 항목 추출 ──────────────────────────────────────────────────────────────

def find_item_in_table(
    rows: list[list[str]],
    keywords: list[str],
    negate_keywords: list[str] | None = None,
) -> Optional[float]:
    """
    행 리스트에서 keywords와 일치하는 항목명을 찾아 당기 금액을 반환한다.

    - 첫 번째 컬럼이 항목명, 이후 컬럼 중 첫 번째 숫자 컬럼이 당기 금액
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
    header = rows[0] if rows else []
    skip_cols = {ci for ci, cell in enumerate(header) if "주석" in cell}

    # 매칭된 행마다 동적으로 첫 번째 숫자 컬럼을 탐색한다.
    # → 고정 amount_col을 사용하지 않음:
    #   소계 행(자산총계 등)은 col 3에, 상세 행(결손금 등)은 col 2에 값이 있는
    #   이중 컬럼 구조 테이블에서도 각 행이 올바른 값을 찾을 수 있다.
    for row in rows:
        if not row:
            continue
        norm = normalize_label(row[0])
        if norm in keyword_set:
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

    재무상태표(BS) 항목은 BS 테이블에서만, 손익계산서(IS) 항목은 IS 테이블에서만 탐색한다.
    전체 테이블 fallback 없음: 오분류 방지를 위해 유형이 맞는 테이블만 사용한다.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        항목명 → 금액(float|None) 딕셔너리
    """
    result: dict[str, Optional[float]] = {item["name"]: None for item in FINANCIAL_ITEMS}

    # 테이블을 유형별로 분류
    bs_tables: list[list[list[str]]] = []
    is_tables: list[list[list[str]]] = []
    all_tables: list[list[list[str]]] = []

    for table_tag in soup.find_all("table"):
        rows = _xml_table_to_rows(table_tag)
        if not rows:
            continue
        all_tables.append(rows)
        fs_type = _classify_table(rows)
        if fs_type == "BS":
            bs_tables.append(rows)
        elif fs_type == "IS":
            is_tables.append(rows)

    for item in FINANCIAL_ITEMS:
        # fs_type에 맞는 테이블에서만 탐색 (fallback 없음)
        search_order = bs_tables if item["fs_type"] == "BS" else is_tables

        for rows in search_order:
            val = find_item_in_table(rows, item["keywords"], item.get("negate_keywords"))
            if val is not None:
                result[item["name"]] = val
                break

    return result


def _extract_table_text_for_ai(soup: BeautifulSoup) -> str:
    """
    BS/IS 테이블의 텍스트를 AI 재추출용으로 압축하여 반환한다.

    분류된 테이블만 포함하여 불필요한 텍스트를 줄인다.
    각 행은 '항목명: 값' 형태로 변환한다.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        테이블 텍스트 문자열 (최대 4000자)
    """
    lines: list[str] = []
    for table_tag in soup.find_all("table"):
        rows = _xml_table_to_rows(table_tag)
        if not rows:
            continue
        fs_type = _classify_table(rows)
        if fs_type not in ("BS", "IS"):
            continue
        lines.append(f"[{fs_type}]")
        for row in rows:
            if row and len(row) >= 2:
                label = normalize_label(row[0])
                vals  = " | ".join(c for c in row[1:] if c.strip())
                if label and vals:
                    lines.append(f"{label}: {vals}")
        if len("\n".join(lines)) > 4000:
            break
    return "\n".join(lines)[:4000]


# ── 경로B 메인 함수 ────────────────────────────────────────────────────────

def get_financial_data_path_b(rcept_no: str, skip_gemini: bool = False) -> dict:
    """
    경로B 메인 함수: 감사보고서 rcept_no로 재무 데이터를 추출한다.

    내부 흐름:
      1. document.xml API로 보고서 ZIP 다운로드
      2. ZIP 내 XML 파일 추출
      3. BeautifulSoup으로 TABLE 파싱
      4. BS/IS 테이블 분류 → 항목별 금액 추출

    반환 형식은 financial_api.get_financial_data_path_a()와 동일하다.

    Args:
        rcept_no:     공시 접수번호 (report_finder.find_report()의 반환값)
        skip_gemini:  True이면 AI 재추출을 건너뛰고 메타데이터만 반환 (배치 처리용)

    Returns:
        {
            "items":       {항목명: 금액(float|None), ...},
            "cross_check": {},   # 경로B는 교차검증 없음
            "fs_div":      "OFS",  # 감사보고서는 별도 기준
            "error":       None | 오류 메시지 문자열,
            "_pending_extraction": {table_text, missing_items} (skip_gemini=True일 때만),
        }
    """
    # 1. 다운로드
    xml_bytes = _download_dart_document(rcept_no)
    if xml_bytes is None:
        return {
            "items": {item["name"]: None for item in FINANCIAL_ITEMS},
            "cross_check": {},
            "fs_div": None,
            "error": f"document.xml 다운로드 실패 (rcept_no={rcept_no})",
        }

    # 2. 파싱
    soup = _parse_dart_xml(xml_bytes)
    if soup is None:
        return {
            "items": {item["name"]: None for item in FINANCIAL_ITEMS},
            "cross_check": {},
            "fs_div": None,
            "error": "DART XML 파싱 실패",
        }

    # 3. 항목 추출
    items = _extract_all_items(soup)

    # 4. AI 재추출: 못 찾은 항목이 4개 이상이고 GEMINI_API_KEY가 있을 때
    missing = [k for k, v in items.items() if v is None]
    if len(missing) >= 4:
        if skip_gemini:
            # 배치 처리용: Gemini 호출 대신 메타데이터만 반환
            table_text = _extract_table_text_for_ai(soup)
            return {
                "items":       items,
                "cross_check": {},
                "fs_div":      "OFS",
                "error":       None,
                "_pending_extraction": {
                    "table_text":    table_text,
                    "missing_items": missing,
                },
            }
        else:
            try:
                from config import GEMINI_API_KEY
                if GEMINI_API_KEY:
                    from gemini_parser import extract_from_raw_text
                    table_text = _extract_table_text_for_ai(soup)
                    ai_items = extract_from_raw_text(table_text, missing)
                    for k, v in ai_items.items():
                        if v is not None and items.get(k) is None:
                            items[k] = v
            except Exception:
                pass  # AI 재추출 실패는 무시

    return {
        "items":       items,
        "cross_check": {},
        "fs_div":      "OFS",
        "error":       None,
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
