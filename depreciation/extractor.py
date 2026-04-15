"""
depreciation/extractor.py
감가상각비·무형자산상각비 추출 (EBITDA용)

목적:
  EBITDA 계산에 필요한 **전체 비용 기준** 감가상각비·무형자산상각비를 추출한다.

설계 원칙:
  - 재무항목 추출 모듈(financial/)과 완전 독립
  - 본 모듈 실패 시 재무항목 추출 결과에 영향 없음
  - dart_api/xml_utils.py의 유틸 함수만 재사용 (상태 공유 없음)

워크플로우 (AI-선택, Python-검증):
  1. DART XML 다운로드 + 파싱
  2. [Stage 1] 현금흐름표(CF)에서 Python 키워드 매칭으로 추출 (AI 불필요)
  3. [Stage 2] CF에서 못 찾은 항목 → 주석(NOTES) fallback
     - Step A: Python이 후보 테이블 수집
     - Step B: AI가 올바른 테이블/행을 선택 (위치만 반환)
     - Step C: Python이 AI가 지목한 위치에서 값을 읽고 검증
"""

from functools import lru_cache
from pathlib import Path
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from dart_api.xml_utils import (
    download_dart_document as _download_dart_document,
    normalize_title as _normalize_title,
    parse_dart_xml as _parse_dart_xml,
    xml_table_to_rows as _xml_table_to_rows,
)


# ── 상수 ──────────────────────────────────────────────────────────────────────

# 감가상각비 관련 키워드 (테이블 필터링용)
_DEPRECIATION_KEYWORDS = ["감가상각"]

# 제외할 테이블 키워드 (누계액·이연법인세 등 당기 비용이 아닌 맥락)
_EXCLUDE_TABLE_KEYWORDS = [
    "누계액", "누계", "기초잔액", "기말잔액",
    "취득원가",          # 유형자산 현황 테이블 (감가상각누계액 포함)
    "이연법인세",        # 이연법인세 내역
]

# 단위 승수 매핑
_UNIT_MULTIPLIERS = {
    "원":    1,
    "천원":  1_000,
    "백만원": 1_000_000,
    "억원":  100_000_000,
}

_FS_SCOPE_TITLE_KEYWORDS = (
    "재무상태표",
    "손익계산서",
    "포괄손익계산서",
    "자본변동표",
    "현금흐름표",
    "재무제표주석",
)

# 재무제표 본문(statement) 루트 제목 — 주석 안의 동명 소제목과 구분하기 위한 exact-match set
# 주석 내부 소제목(예: "33.현금흐름표(연결)")은 접두어 제거 후에도 이 set에 포함되지 않는다.
_FS_STMT_ROOT_CORES = frozenset({
    "연결재무상태표", "재무상태표", "별도재무상태표",
    "연결손익계산서", "손익계산서", "별도손익계산서",
    "연결포괄손익계산서", "포괄손익계산서", "별도포괄손익계산서",
    "연결자본변동표", "자본변동표", "별도자본변동표",
    "연결현금흐름표", "현금흐름표", "별도현금흐름표",
})

# 주석 루트 제목 core
_NOTES_ROOT_CORES = frozenset({
    "연결재무제표주석", "별도재무제표주석", "재무제표주석",
    "연결주석", "별도주석", "주석",
})


# ── 내부 유틸 ─────────────────────────────────────────────────────────────────

def _detect_unit_multiplier(tag: Tag) -> int:
    """
    테이블 태그 앞의 '(단위 : XXX)' 패턴에서 단위 승수를 감지한다.

    테이블 바로 앞 형제 태그들(최대 5개)을 역순으로 탐색하여
    가장 가까운 단위 표시를 찾는다.

    Args:
        tag: BeautifulSoup Table 태그

    Returns:
        단위 승수 (기본값: 1 = 원)
    """
    # 앞쪽 형제 태그 5개까지 탐색
    node = tag
    for _ in range(5):
        node = node.find_previous_sibling()
        if node is None:
            break
        text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node)
        m = re.search(r"단위\s*[:：]\s*(천원|백만원|억원|원)", text)
        if m:
            return _UNIT_MULTIPLIERS.get(m.group(1), 1)
    return 1  # 기본값: 원


def _get_section_title(tag: Tag) -> str:
    """
    테이블 앞에 있는 주석 섹션 제목을 추출한다.

    예: "29. 판매비와 관리비", "31. 현금흐름표 관련 추가정보"

    Args:
        tag: BeautifulSoup Table 태그

    Returns:
        섹션 제목 문자열 (찾지 못하면 빈 문자열)
    """
    node = tag
    for _ in range(10):
        node = node.find_previous_sibling()
        if node is None:
            break
        text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node)
        # "숫자. 제목" 패턴 매칭 (주석 번호)
        m = re.match(r"(\d+\.?\s*.{2,40})", text)
        if m:
            return m.group(1).strip()
    return ""


def _parse_number(text: str) -> Optional[float]:
    """
    금액 문자열을 float로 변환한다. 괄호는 음수.

    Args:
        text: 금액 문자열 (예: "4,656,590", "(1,234)", "-")

    Returns:
        float 또는 None (숫자가 아닌 경우)
    """
    text = text.strip()
    if not text or text in ("-", "–", "—", ""):
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = text.replace(",", "").replace(" ", "")
    try:
        val = float(text)
        return -val if negative else val
    except ValueError:
        return None


def _infer_document_scope(soup: BeautifulSoup) -> Optional[bool]:
    """
    문서 전체가 연결 전용/별도 전용인지 추론한다.

    Returns:
        True  -> 연결 전용 문서로 보임
        False -> 별도 전용 문서로 보임
        None  -> 연결/별도가 혼재하거나 판별 불가
    """
    saw_consol = False
    saw_separate = False

    for title_tag in soup.find_all("title"):
        norm = _normalize_title(title_tag.get_text(strip=True))
        if not any(kw in norm for kw in _FS_SCOPE_TITLE_KEYWORDS):
            continue

        if "연결" in norm:
            saw_consol = True
        else:
            saw_separate = True

    if saw_consol and not saw_separate:
        return True
    if saw_separate and not saw_consol:
        return False
    return None


def _strip_title_prefix(norm_title: str) -> str:
    """
    제목 정규화 문자열에서 선두 번호/기호 접두어를 제거한다.

    예:
      "3.연결재무제표주석" -> "연결재무제표주석"
      "4-5.현금흐름표" -> "현금흐름표"
    """
    return re.sub(r"^[\d\-]+\.", "", norm_title)


def _resolve_notes_scope(
    norm_title: str,
    document_scope: Optional[bool],
    current_scope: Optional[bool] = None,
    in_notes: bool = False,
    strict_scope: bool = True,
    has_explicit_consol_root: bool = False,
    has_explicit_separate_root: bool = False,
) -> tuple[Optional[bool], bool]:
    """
    주석 루트 제목의 연결/별도 범위를 해석한다.

    Returns:
        (scope, is_notes_root)
        scope: True=연결, False=별도, None=문서 전체 범위에 위임/판단 불가
    """
    title_core = _strip_title_prefix(norm_title)

    if title_core in ("연결재무제표주석", "연결주석"):
        return True, True
    if title_core in ("별도재무제표주석", "별도주석"):
        return False, True
    if title_core == "재무제표주석":
        # 사업보고서 strict 모드에서는 '재무제표 주석'을 별도 주석 루트로 본다.
        if strict_scope:
            return False, True
        if has_explicit_consol_root and not has_explicit_separate_root:
            return False, True
        if has_explicit_separate_root and not has_explicit_consol_root:
            return True, True
        return current_scope if current_scope is not None else document_scope, True
    if title_core == "주석":
        # 같은 notes 구간 내부에서 반복되는 generic 제목은 현재 범위를 유지한다.
        if in_notes:
            return current_scope, True
        if has_explicit_consol_root and not has_explicit_separate_root:
            return False, True
        if has_explicit_separate_root and not has_explicit_consol_root:
            return True, True
        return current_scope if current_scope is not None else document_scope, True
    return None, False


def _detect_current_column(rows: list[list[str]]) -> Optional[int]:
    """
    테이블에서 '당기' 컬럼 인덱스를 탐지한다.

    우선순위:
      1. 셀에 "당기" 포함 (전기/전전기 제외)
      2. "제 N 기" 패턴 — 가장 큰 N이 당기
      3. "20XX" 연도 패턴 — 가장 최신 연도가 당기
      4. "합계"/"총계" fallback
      5. 실패 시 None

    머리 5행을 탐색 대상으로 한다 (DART CF는 종종 preamble 행이 있음).
    """
    header_rows = rows[:5]
    # rowspan으로 인해 헤더 행이 데이터 행보다 짧을 수 있으므로
    # 최대 컬럼 수를 기준으로 offset을 보정한다.
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
                    # 서브 헤더가 당기 헤더보다 넓으면 colspan 병합 상태
                    # → end_col 제한을 풀고 첫 번째 "합계"를 찾는다
                    if len(sub_header) > len(header):
                        search_end = len(sub_header)
                    else:
                        search_end = end_col
                    for cj in range(ci + 1, min(search_end, len(sub_header))):
                        snorm = sub_header[cj].replace(" ", "").strip()
                        if snorm in ("합계", "총계"):
                            # rowspan으로 빠진 셀 수만큼 보정
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

    # 4. 합계/총계 fallback
    for header in header_rows:
        for ci, cell in enumerate(header):
            norm = cell.replace(" ", "").strip()
            if norm in ("합계", "총계"):
                return ci

    return None


def _has_exclude_keywords(rows: list[list[str]]) -> bool:
    """
    제외 키워드가 포함된 테이블인지 확인한다.

    감가상각누계액, 이연법인세 등은 당기 비용이 아니므로 제외.

    Args:
        rows: 2D 테이블 행 리스트

    Returns:
        True이면 제외 대상
    """
    # 첫 3행(헤더)에서 제외 키워드 확인
    header_text = " ".join(" ".join(r) for r in rows[:3])
    return any(kw in header_text for kw in _EXCLUDE_TABLE_KEYWORDS)


# ── 강한 시그널 탐지 ──────────────────────────────────────────────────────────

# 정확 매칭 대상 라벨 (행 첫 셀이 이 값과 정확히 일치해야 함)
_EXACT_DEPR_LABELS = {"감가상각비", "감가상각비용"}
_EXACT_AMORT_LABELS = {"무형자산상각비", "무형자산상각비용", "무형자산상각"}

# 섹션 제목 블랙리스트 (부분값 테이블)
# 타이틀이 감지된 경우에만 보조 필터로 사용
_PARTIAL_TITLE_KEYWORDS = [
    "유형자산", "투자부동산", "사용권자산", "리스자산",
    "판매비", "판관비", "관리비",
    "기능별",
]


def _row_has_number(row: list[str]) -> bool:
    """행에 실제 숫자 셀이 하나라도 있는지 확인한다."""
    for cell in row[1:]:
        if _parse_number(cell) is not None:
            return True
    return False


def _normalize_row_label(label: str) -> str:
    """
    행 라벨을 정규화한다.

    - 공백/전각공백/특수 공백 제거
    - 선두 번호 접두어 제거 ("1)", "1.", "(1)", "①" 등)
    """
    norm = label.replace(" ", "").replace("\u3000", "").replace("\u00a0", "").strip()
    # 선두 번호 제거
    norm = re.sub(r"^[\(\[]?[\d①-⑳]{1,3}[\)\]]?[\.\s]*", "", norm)
    return norm


def _detect_depreciation_signals(rows: list[list[str]]) -> dict:
    """
    테이블에서 감가상각비 관련 '강한 시그널'을 탐지한다.

    수작업 워크플로우 기준:
      - 정확한 '감가상각비' 행 + 숫자 존재 → 회사 전체 기준 테이블일 가능성 높음
      - 정확한 '무형자산상각비' 행 + 숫자 존재 → 분리 기재 쌍의 일부
      - '감가상각비 및 무형자산상각비' 합산 행 + 숫자 존재 → 합산 기재 테이블

    Returns:
        {
            "has_exact_depr": bool,     # 정확 감가상각비 행 존재
            "has_exact_amort": bool,    # 정확 무형자산상각비 행 존재
            "has_combined_row": bool,   # 합산 행 존재
            "has_separate_pair": bool,  # 분리 기재 쌍 존재 (depr + amort)
        }
    """
    has_exact_depr = False
    has_exact_amort = False
    has_combined_row = False

    for row in rows:
        if not row:
            continue
        label = _normalize_row_label(row[0])

        # 누계액 행은 스킵
        if "누계" in label:
            continue

        # 합산 행 검사 ("감가상각비및무형자산상각비")
        if "감가상각비" in label and "무형자산상각비" in label:
            if _row_has_number(row):
                has_combined_row = True
            continue

        # 정확 감가상각비 매칭
        if label in _EXACT_DEPR_LABELS:
            if _row_has_number(row):
                has_exact_depr = True
            continue

        # 정확 무형자산상각비 매칭
        if label in _EXACT_AMORT_LABELS:
            if _row_has_number(row):
                has_exact_amort = True
            continue

    return {
        "has_exact_depr":    has_exact_depr,
        "has_exact_amort":   has_exact_amort,
        "has_combined_row":  has_combined_row,
        "has_separate_pair": has_exact_depr and has_exact_amort,
    }


# ── 주석 테이블 수집 ──────────────────────────────────────────────────────────

def _collect_depreciation_tables(
    soup: BeautifulSoup,
    fs_div: str = "CFS",
    strict_scope: bool = True,
    debug_trace: Optional[list[str]] = None,
) -> list[dict]:
    """
    주석 섹션에서 감가상각 관련 테이블을 수집한다.

    사업보고서에는 '연결재무제표 주석'과 '재무제표 주석'(별도)이 모두 포함되어 있다.
    fs_div에 따라 올바른 주석 섹션에서만 테이블을 수집한다.

    각 테이블에 대해:
      - 섹션 제목
      - 단위 승수
      - 파싱된 행 데이터
      - AI용 텍스트
    를 포함하는 딕셔너리 리스트를 반환한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS"=연결재무제표 주석, "OFS"=별도재무제표 주석

    Returns:
        [{"title": str, "unit": int, "rows": [[str]], "text": str}, ...]
    """
    want_consol = (fs_div == "CFS")
    document_scope = _infer_document_scope(soup)
    title_norms = [_normalize_title(tag.get_text(strip=True)) for tag in soup.find_all("title")]
    title_cores = [_strip_title_prefix(norm) for norm in title_norms]
    has_explicit_consol_root = any(core in ("연결재무제표주석", "연결주석") for core in title_cores)
    has_explicit_separate_root = any(core in ("별도재무제표주석", "별도주석") for core in title_cores)
    if debug_trace is not None:
        debug_trace.append(
            f"[NOTES] title roots scan: explicit_consol={has_explicit_consol_root}, "
            f"explicit_separate={has_explicit_separate_root}, document_scope={document_scope}, "
            f"strict_scope={strict_scope}"
        )

    # 주석 섹션 시작 위치 찾기
    in_notes = False
    current_notes_scope: Optional[bool] = None
    matched_tables: list[dict] = []
    unscoped_tables: list[dict] = []
    all_notes_tables: list[dict] = []
    has_scoped_notes_root = False

    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
            title_core = _strip_title_prefix(norm)
            notes_scope, is_notes_root = _resolve_notes_scope(
                norm,
                document_scope,
                current_scope=current_notes_scope,
                in_notes=in_notes,
                strict_scope=strict_scope,
                has_explicit_consol_root=has_explicit_consol_root,
                has_explicit_separate_root=has_explicit_separate_root,
            )
            if debug_trace is not None and ("주석" in norm or "현금흐름표" in norm or "재무제표" in norm):
                debug_trace.append(
                    f"[NOTES] title encountered: raw={raw!r}, norm={norm!r}, "
                    f"is_notes_root={is_notes_root}, resolved_scope={notes_scope}, in_notes_before={in_notes}"
                )

            if is_notes_root:
                in_notes = True
                current_notes_scope = notes_scope
                if notes_scope is not None:
                    has_scoped_notes_root = True
            elif title_core in _FS_STMT_ROOT_CORES:
                # 재무제표 본문 루트(연결/별도 재무상태표·현금흐름표 등) 진입 → 주석 종료
                # 주석 내부의 "33.현금흐름표(연결)" 같은 소제목은 title_core에 괄호가 남아
                # 이 set에 포함되지 않으므로 in_notes를 종료하지 않는다.
                in_notes = False
                current_notes_scope = None

        elif tag.name == "table" and in_notes:
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue
            if debug_trace is not None:
                labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                debug_trace.append(
                    f"[NOTES] table seen in notes scope={current_notes_scope}: labels={labels}"
                )

            # 감가상각 키워드 포함 여부 확인 (1차 필터)
            full_text = " ".join(" ".join(r) for r in rows)
            if not any(kw in full_text for kw in _DEPRECIATION_KEYWORDS):
                if debug_trace is not None:
                    debug_trace.append("[NOTES] -> 감가상각 키워드 없음, 스킵")
                continue

            # 제외 대상 필터링 (누계액/이연법인세 등)
            if _has_exclude_keywords(rows):
                if debug_trace is not None:
                    debug_trace.append("[NOTES] -> 제외 키워드 포함, 스킵")
                continue

            # 강한 시그널 탐지 (2차 필터)
            # 정확한 '감가상각비' 행 또는 합산 행이 숫자와 함께 없으면 스킵
            # (유형자산 변동/기능별 배분/판관비 부분값 등 걸러냄)
            signals = _detect_depreciation_signals(rows)
            if not (signals["has_exact_depr"] or signals["has_combined_row"]):
                if debug_trace is not None:
                    preview = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[NOTES] -> 정확 감가상각비/합산 행 없음, 스킵 (labels={preview})"
                    )
                continue

            title = _get_section_title(tag)

            # 섹션 제목 블랙리스트 (3차 필터, 보조)
            # 타이틀이 감지된 경우에만 적용. 자산별/기능별 부분값 테이블 제외.
            if title and any(kw in title for kw in _PARTIAL_TITLE_KEYWORDS):
                if debug_trace is not None:
                    debug_trace.append(
                        f"[NOTES] -> 섹션 제목 블랙리스트 매칭({title}), 스킵"
                    )
                continue

            unit = _detect_unit_multiplier(tag)

            # AI용 텍스트 생성
            text_lines = []
            if title:
                text_lines.append(f"[섹션: {title}]")
            unit_label = {1: "원", 1000: "천원", 1_000_000: "백만원"}.get(unit, f"{unit}원")
            text_lines.append(f"(단위: {unit_label})")
            for row in rows:
                text_lines.append(" | ".join(row))

            entry = {
                "title":   title,
                "unit":    unit,
                "rows":    rows,
                "text":    "\n".join(text_lines),
                "tag":     tag,  # 원본 태그 (디버깅용)
                "signals": signals,
            }

            all_notes_tables.append(entry)

            if current_notes_scope is None:
                unscoped_tables.append(entry)
            elif want_consol == current_notes_scope:
                matched_tables.append(entry)

    if not strict_scope:
        if debug_trace is not None:
            debug_trace.append(f"[NOTES] strict off -> all_notes_tables {len(all_notes_tables)}개 반환")
        return all_notes_tables

    if matched_tables:
        if debug_trace is not None:
            debug_trace.append(f"[NOTES] matched_tables {len(matched_tables)}개 반환")
        return matched_tables

    # 문서 전체가 단일 재무제표 기준이면 범위 미표시 주석도 해당 기준으로 간주한다.
    if unscoped_tables and document_scope is not None and want_consol == document_scope:
        if debug_trace is not None:
            debug_trace.append(f"[NOTES] unscoped_tables {len(unscoped_tables)}개를 document_scope 기준으로 반환")
        return unscoped_tables

    # 주석 루트에 연결/별도 구분이 아예 없었던 문서만 전체 fallback 허용.
    if not has_scoped_notes_root:
        if debug_trace is not None:
            debug_trace.append(
                f"[NOTES] scoped root 없음 -> unscoped={len(unscoped_tables)}, all={len(all_notes_tables)} 반환"
            )
        return unscoped_tables if unscoped_tables else all_notes_tables

    # 연결/별도 루트가 명시된 문서에서는 다른 범위 주석으로 fallback 하지 않는다.
    if debug_trace is not None:
        debug_trace.append("[NOTES] strict scope에서 반환 가능한 주석 테이블 없음")
    return []


# ── AI 테이블/행 선택 ────────────────────────────────────────────────────────

_DEPRECIATION_AI_GUIDE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "depreciation_ai_guide.md"


@lru_cache(maxsize=1)
def _load_depreciation_ai_guide() -> str:
    """감가상각 AI 선택 규칙 문서를 로드한다."""
    try:
        text = _DEPRECIATION_AI_GUIDE_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    except FileNotFoundError:
        pass
    except OSError:
        pass

    return (
        "# 감가상각 선택 규칙\n"
        "- 회사 전체 기준 값이 있는 테이블/행을 선택한다.\n"
        "- 비용의 성격별 분류 또는 현금흐름표 조정항목을 우선한다.\n"
        "- 판매비와관리비, 기능별 배분, 특정 자산 감가상각, 누계액은 제외한다.\n"
        "- 숫자가 아닌 위치(table_id, row_id)만 반환한다.\n"
    )


def _format_tables_for_ai(tables: list[dict]) -> str:
    """AI가 근거를 인용할 수 있도록 테이블/행 번호를 포함한 텍스트를 만든다."""
    chunks: list[str] = []

    for idx, tbl in enumerate(tables, start=1):
        table_id = f"T{idx}"
        title = tbl.get("title") or "-"
        unit = tbl.get("unit", 1)
        unit_label = {1: "원", 1000: "천원", 1_000_000: "백만원", 100_000_000: "억원"}.get(unit, f"{unit}원")

        lines = [
            f"[TABLE {table_id}]",
            f"[SECTION] {title}",
            f"[UNIT] {unit_label}",
        ]

        for row_idx, row in enumerate(tbl.get("rows", []), start=1):
            row_text = " | ".join(row)
            lines.append(f"[ROW R{row_idx}] {row_text}")

        chunks.append("\n".join(lines))

    return "\n\n".join(chunks)


def _build_ai_prompt(tables_text: str, guide_text: str, hint: str = "") -> str:
    """감가상각비 위치 선택용 AI 프롬프트를 생성한다."""
    return (
        "다음은 이 프로젝트에서 반드시 따라야 하는 감가상각/무형자산상각비 선택 규칙 문서다.\n"
        "규칙 문서를 먼저 읽고, 그 기준에 맞게만 판단해라.\n\n"
        f"{guide_text}\n\n"
        f"{hint}"
        "아래는 한국 기업 감사보고서의 주석(Notes)에서 감가상각 관련 테이블을 발췌한 것이다.\n"
        "EBITDA 계산에 필요한 **전체 비용 기준(회사 전체)** 감가상각비와 무형자산상각비가 있는\n"
        "**테이블과 행의 위치**를 선택해라. 숫자를 직접 읽지 마라.\n\n"
        "## 주의사항\n"
        "- '비용의 성격별 분류' 또는 '현금흐름표 조정항목' 기준의 **전체(회사 전체)** 감가상각비를 찾아라.\n"
        "- **'판매비와 관리비' 주석의 감가상각비는 판관비 내 부분값이므로 절대 선택하지 마라.**\n"
        "- 특정 자산(유형자산, 투자부동산, 사용권자산)만의 감가상각은 부분값이므로 선택하지 마라.\n"
        "- 감가상각누계액(누적값)은 당기 비용이 아니므로 선택하지 마라.\n"
        "- 이연법인세 관련 감가상각비는 완전히 다른 맥락이므로 선택하지 마라.\n"
        "- 분리 기재(감가상각비, 무형자산상각비 각각 별도 행)가 있으면 분리를 선택해라.\n"
        "- 합산 기재만 있으면 합산을 선택하고 combined=true로 표시해라.\n"
        "- 찾을 수 없으면 null로 표시해라.\n\n"
        "JSON으로만 응답해라:\n"
        "```json\n"
        '{\n'
        '  "depreciation": {"table_id": "Tn", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"},\n'
        '  "amortization": {"table_id": "Tn", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"},\n'
        '  "combined": false\n'
        '}\n'
        "```\n\n"
        f"주석 테이블:\n---\n{tables_text}\n---"
    )


def _ai_select_depreciation(tables: list[dict]) -> dict:
    """
    AI(Gemini)에게 감가상각비·무형자산상각비의 올바른 위치를 선택하게 한다.

    AI는 숫자를 반환하지 않고, 테이블/행의 위치만 반환한다.

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {
            "depreciation": {"table_id": "Tn", "row_id": "Rn", "row_label": str, "reason": str} | None,
            "amortization": {"table_id": "Tn", "row_id": "Rn", "row_label": str, "reason": str} | None,
            "combined": bool,
        }

    Raises:
        RuntimeError: Gemini API 호출 또는 응답 파싱 실패
    """
    from config import get_gemini_api_key
    from ai_module.gemini_parser import _get_client, _generate, _parse_json

    client = _get_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY 미설정")

    # 테이블 텍스트 병합 (토큰 절약: 8000자 제한)
    combined_text = _format_tables_for_ai(tables)
    if len(combined_text) > 8000:
        combined_text = combined_text[:8000]

    # 분리 기재 쌍이 있는 테이블을 힌트로 표시
    separate_pair_tables = [
        f"T{idx}"
        for idx, tbl in enumerate(tables, start=1)
        if tbl.get("signals", {}).get("has_separate_pair")
    ]
    hint = ""
    if separate_pair_tables:
        hint = (
            "## 중요 힌트: 분리 기재 쌍 탐지됨\n"
            f"다음 테이블에는 '감가상각비'와 '무형자산상각비'가 각각 별도 행으로 기재되어 있다: "
            f"{', '.join(separate_pair_tables)}\n"
            "분리 기재가 합산 기재보다 우선이므로 이 중 하나를 우선 선택해라. "
            "단, 이 테이블이 판관비/유형자산/기능별 배분 등 부분값 테이블이면 제외해라.\n\n"
        )

    guide_text = _load_depreciation_ai_guide()
    prompt = _build_ai_prompt(combined_text, guide_text, hint=hint)
    raw = _generate(client, prompt)
    parsed = _parse_json(raw)
    if parsed is None:
        raise RuntimeError(f"AI 응답 JSON 파싱 실패: {raw[:300]}")

    result: dict = {"depreciation": None, "amortization": None, "combined": False}

    for key in ("depreciation", "amortization"):
        val = parsed.get(key)
        if isinstance(val, dict) and val.get("table_id") and val.get("row_id"):
            result[key] = {
                "table_id": str(val["table_id"]).strip(),
                "row_id": str(val["row_id"]).strip(),
                "row_label": str(val.get("row_label", "")).strip(),
                "reason": str(val.get("reason", "")).strip(),
            }

    combined = parsed.get("combined")
    if isinstance(combined, bool):
        result["combined"] = combined
    else:
        # combined 플래그가 없으면 depreciation의 row_label로 추론
        depr = result.get("depreciation")
        if isinstance(depr, dict):
            label = depr.get("row_label", "").replace(" ", "")
            if "감가상각비" in label and "무형자산상각비" in label:
                result["combined"] = True

    if result["combined"]:
        result["amortization"] = None

    return result


# ── AI 선택 위치에서 Python 값 검증 추출 ─────────────────────────────────────

def _extract_value_at_position(
    tables: list[dict],
    table_id: str,
    row_id: str,
    debug_trace: Optional[list[str]] = None,
) -> Optional[float]:
    """
    AI가 지목한 테이블/행 위치에서 당기 값을 읽는다.

    Args:
        tables:   _collect_depreciation_tables() 반환값
        table_id: "T1", "T2", ... (1-based)
        row_id:   "R1", "R2", ... (1-based)

    Returns:
        원(KRW) 단위로 변환된 값, 또는 None
    """
    # table_id에서 인덱스 추출 (T1 -> 0, T2 -> 1, ...)
    m = re.match(r"T(\d+)", table_id)
    if not m:
        if debug_trace is not None:
            debug_trace.append(f"[VERIFY] 잘못된 table_id: {table_id}")
        return None
    table_idx = int(m.group(1)) - 1

    if table_idx < 0 or table_idx >= len(tables):
        if debug_trace is not None:
            debug_trace.append(f"[VERIFY] table_id {table_id} 범위 초과 (테이블 {len(tables)}개)")
        return None

    tbl = tables[table_idx]
    rows = tbl["rows"]
    unit = tbl["unit"]

    # row_id에서 인덱스 추출 (R1 -> 0, R2 -> 1, ...)
    m = re.match(r"R(\d+)", row_id)
    if not m:
        if debug_trace is not None:
            debug_trace.append(f"[VERIFY] 잘못된 row_id: {row_id}")
        return None
    row_idx = int(m.group(1)) - 1

    if row_idx < 0 or row_idx >= len(rows):
        if debug_trace is not None:
            debug_trace.append(f"[VERIFY] row_id {row_id} 범위 초과 (행 {len(rows)}개)")
        return None

    target_row = rows[row_idx]

    # 당기 컬럼 인덱스 탐지 (당기/제N기/연도/합계)
    current_col = _detect_current_column(rows)

    # 당기 컬럼에서 값 추출
    if current_col is not None and current_col < len(target_row):
        val = _parse_number(target_row[current_col])
        if val is not None:
            result = val * unit
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] {table_id}/{row_id}: col={current_col}, raw={target_row[current_col]}, "
                    f"unit={unit}, result={result}"
                )
            return result

    # 당기 컬럼 미판별 → 첫 번째 숫자 컬럼 사용
    for ci in range(1, len(target_row)):
        val = _parse_number(target_row[ci])
        if val is not None:
            result = val * unit
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] {table_id}/{row_id}: fallback col={ci}, raw={target_row[ci]}, "
                    f"unit={unit}, result={result}"
                )
            return result

    if debug_trace is not None:
        debug_trace.append(f"[VERIFY] {table_id}/{row_id}: 값 추출 실패, row={target_row}")
    return None


def _resolve_actual_row_label(
    tables: list[dict],
    table_id: str,
    row_id: str,
) -> Optional[str]:
    """AI가 지목한 위치의 실제 행 라벨을 반환한다."""
    m = re.match(r"T(\d+)", table_id)
    if not m:
        return None
    ti = int(m.group(1)) - 1
    if ti < 0 or ti >= len(tables):
        return None
    rows = tables[ti].get("rows", [])
    m = re.match(r"R(\d+)", row_id)
    if not m:
        return None
    ri = int(m.group(1)) - 1
    if ri < 0 or ri >= len(rows) or not rows[ri]:
        return None
    return _normalize_row_label(rows[ri][0])


def _verify_ai_selection(
    tables: list[dict],
    ai_selection: dict,
    debug_trace: Optional[list[str]] = None,
) -> dict[str, Optional[float]]:
    """
    AI가 선택한 위치에서 Python이 값을 읽어 최종 결과를 만든다.

    AI가 지목한 행의 실제 라벨이 허용 키워드와 일치하는지 검증하여
    사용권자산상각비 등 오인식을 방지한다.

    Args:
        tables:       _collect_depreciation_tables() 반환값
        ai_selection: _ai_select_depreciation() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None, "combined": bool}
    """
    result: dict[str, Optional[float]] = {"감가상각비": None, "무형자산상각비": None}
    combined = ai_selection.get("combined", False)

    # 감가상각비 추출 (라벨 검증 포함)
    depr_sel = ai_selection.get("depreciation")
    if isinstance(depr_sel, dict):
        actual_label = _resolve_actual_row_label(
            tables, depr_sel["table_id"], depr_sel["row_id"]
        )
        # 합산 행이면 라벨에 감가상각비+무형자산상각비 둘 다 포함
        is_combined_label = (
            actual_label is not None
            and "감가상각비" in actual_label
            and "무형자산상각비" in actual_label
        )
        if actual_label is not None and (actual_label in _EXACT_DEPR_LABELS or is_combined_label):
            val = _extract_value_at_position(
                tables, depr_sel["table_id"], depr_sel["row_id"], debug_trace
            )
            result["감가상각비"] = val
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] 감가상각비: table={depr_sel['table_id']}, row={depr_sel['row_id']}, "
                    f"actual_label='{actual_label}', value={val}"
                )
        else:
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] 감가상각비 라벨 불일치: AI선택={depr_sel.get('row_label')!r}, "
                    f"actual='{actual_label}', 허용={_EXACT_DEPR_LABELS} → null 처리"
                )

    # 무형자산상각비 추출 (합산이 아닌 경우만, 라벨 검증 포함)
    if not combined:
        amort_sel = ai_selection.get("amortization")
        if isinstance(amort_sel, dict):
            actual_label = _resolve_actual_row_label(
                tables, amort_sel["table_id"], amort_sel["row_id"]
            )
            if actual_label is not None and actual_label in _EXACT_AMORT_LABELS:
                val = _extract_value_at_position(
                    tables, amort_sel["table_id"], amort_sel["row_id"], debug_trace
                )
                result["무형자산상각비"] = val
                if debug_trace is not None:
                    debug_trace.append(
                        f"[VERIFY] 무형자산상각비: table={amort_sel['table_id']}, row={amort_sel['row_id']}, "
                        f"actual_label='{actual_label}', value={val}"
                    )
            else:
                if debug_trace is not None:
                    debug_trace.append(
                        f"[VERIFY] 무형자산상각비 라벨 불일치: AI선택={amort_sel.get('row_label')!r}, "
                        f"actual='{actual_label}', 허용={_EXACT_AMORT_LABELS} → null 처리"
                    )

    result["combined"] = combined
    return result


# ── 현금흐름표(CF) 추출 ───────────────────────────────────────────────────────

# 감사보고서 등에서 개별 재무제표 <title> 없이 테이블만 나열된 경우
# 내용 기반으로 CF 테이블을 판별하기 위한 마커 (첫 10행 라벨 검사)
_CF_CONTENT_MARKERS = ("영업활동으로인한현금흐름", "영업활동현금흐름")


def _table_looks_like_cf(rows: list[list[str]]) -> bool:
    """테이블 내용이 현금흐름표인지 판별한다 (title 없이 content 기반)."""
    for row in rows[:10]:
        if not row:
            continue
        label = row[0].replace(" ", "").strip()
        if any(marker in label for marker in _CF_CONTENT_MARKERS):
            return True
    return False


def _find_cf_tables_by_fs_type(
    soup: BeautifulSoup,
    fs_div: str = "CFS",
    strict_scope: bool = True,
    debug_trace: Optional[list[str]] = None,
) -> list[list[list[str]]]:
    """
    사업보고서에서 연결/별도 구분에 맞는 현금흐름표 테이블을 반환한다.

    DART 사업보고서는 '연결 현금흐름표'와 '현금흐름표' 가 모두 포함되어 있다.
    fs_div가 CFS이면 '연결' 이 포함된 CF를, OFS이면 '연결' 이 없는 CF를 선택한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS" 또는 "OFS"

    Returns:
        해당 구분에 맞는 CF 테이블의 rows 리스트
    """
    want_consol = (fs_div == "CFS")
    document_scope = _infer_document_scope(soup)
    matched_tables: list[list[list[str]]] = []
    all_cf_tables: list[list[list[str]]] = []

    current_scope: Optional[bool] = None
    is_cf_section = False
    in_notes = False

    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
            title_core = _strip_title_prefix(norm)

            # 주석 루트 진입 → 이후의 "현금흐름표" 소제목은 CF가 아닌 주석 소제목으로 취급
            if title_core in _NOTES_ROOT_CORES:
                in_notes = True
                is_cf_section = False
                current_scope = None
                if debug_trace is not None:
                    debug_trace.append(
                        f"[CF] notes root encountered: raw={raw!r}, in_notes=True"
                    )
                continue

            # 재무제표 본문 루트 진입 → 주석 종료
            if title_core in _FS_STMT_ROOT_CORES:
                in_notes = False
                is_cf_section = ("현금흐름표" in title_core)
                if is_cf_section:
                    if "연결" in title_core:
                        current_scope = True
                    elif "별도" in title_core:
                        current_scope = False
                    else:
                        current_scope = document_scope if document_scope is not None else False
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] title encountered: raw={raw!r}, norm={norm!r}, resolved_scope={current_scope}"
                        )
                else:
                    current_scope = None
                continue

            # 주석 내부의 "현금흐름표" 소제목은 무시 (두산퓨얼셀 "33. 현금흐름표 (연결)" 케이스)
            if in_notes:
                is_cf_section = False
                if debug_trace is not None and "현금흐름표" in norm:
                    debug_trace.append(
                        f"[CF] skipped notes subsection: raw={raw!r} (in_notes=True)"
                    )
                continue

            # 그 외 제목: non-root 현금흐름표(예: "2-4. 연결 현금흐름표")
            if "현금흐름표" in norm:
                is_cf_section = True
                if "연결" in norm:
                    current_scope = True
                elif "별도" in norm:
                    current_scope = False
                else:
                    current_scope = document_scope if document_scope is not None else False
                if debug_trace is not None:
                    debug_trace.append(
                        f"[CF] title encountered: raw={raw!r}, norm={norm!r}, resolved_scope={current_scope}"
                    )
            else:
                is_cf_section = False
                current_scope = None

        elif tag.name == "table" and (is_cf_section or not in_notes):
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue

            # title 기반 CF 섹션 확인, 또는 content 기반 fallback
            if is_cf_section or _table_looks_like_cf(rows):
                if not is_cf_section and debug_trace is not None:
                    labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[CF] content-based CF 감지 (title 없음): labels={labels}"
                    )
                all_cf_tables.append(rows)
                if debug_trace is not None:
                    labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[CF] table seen scope={current_scope}: labels={labels}"
                    )

                if current_scope is None or want_consol == current_scope:
                    matched_tables.append(rows)

    if not strict_scope:
        if debug_trace is not None:
            debug_trace.append(f"[CF] strict off -> all_cf_tables {len(all_cf_tables)}개 반환")
        return all_cf_tables

    # 구분에 맞는 테이블이 없으면 전체 CF 테이블 반환 (fallback)
    if debug_trace is not None:
        debug_trace.append(
            f"[CF] strict on -> matched={len(matched_tables)}, all={len(all_cf_tables)}"
        )
    return matched_tables if matched_tables else all_cf_tables


def _extract_from_cf(
    soup: BeautifulSoup,
    fs_div: str = "CFS",
    strict_scope: bool = True,
    debug_trace: Optional[list[str]] = None,
) -> dict[str, Optional[float]]:
    """
    현금흐름표(CF) 섹션에서 감가상각비·무형자산상각비를 추출한다.

    현금흐름표의 "영업활동" 조정 항목에는 전체 비용 기준 감가상각비가
    별도 행으로 기재되므로, 주석보다 신뢰도가 높다.
    단위는 재무제표와 동일(원)이므로 보정이 불필요하다.

    fs_div에 따라 연결/별도 현금흐름표를 구분하여 올바른 테이블에서 추출한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS"=연결, "OFS"=별도

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None, "combined": bool}
    """
    cf_tables = _find_cf_tables_by_fs_type(soup, fs_div, strict_scope=strict_scope, debug_trace=debug_trace)
    if debug_trace is not None:
        debug_trace.append(
            f"[CF] 후보 테이블 {len(cf_tables)}개 (fs_div={fs_div}, strict_scope={strict_scope})"
        )

    result: dict[str, Optional[float]] = {"감가상각비": None, "무형자산상각비": None}
    combined = False

    for idx, rows in enumerate(cf_tables, start=1):
        # 감가상각 키워드가 포함된 CF 테이블만 대상
        full_text = " ".join(" ".join(r) for r in rows)
        if "감가상각" not in full_text:
            continue
        if debug_trace is not None:
            labels = ", ".join(row[0].strip() for row in rows[:5] if row)
            debug_trace.append(f"[CF] 감가상각 키워드 포함 테이블 #{idx}: {labels}")

        # ── 당기 컬럼 인덱스 탐지 (당기/제N기/연도) ──
        current_col = _detect_current_column(rows)

        if debug_trace is not None:
            debug_trace.append(f"[CF] 테이블 #{idx}: 당기 컬럼 인덱스={current_col}")

        def _get_current_val(row: list[str]) -> tuple[Optional[float], Optional[int], Optional[str]]:
            """행에서 당기 금액과 그 출처 컬럼/원본 셀을 반환한다."""
            if current_col is not None and current_col < len(row):
                raw = row[current_col]
                val = _parse_number(raw)
                if val is not None:
                    return val, current_col, raw
            # 당기 컬럼 미판별 시 폴백: 첫 번째 숫자
            for ci, cell in enumerate(row[1:], start=1):
                val = _parse_number(cell)
                if val is not None:
                    return val, ci, cell
            return None, None, None

        for row_idx, row in enumerate(rows):
            if not row:
                continue
            label = row[0].replace(" ", "").strip()

            # "감가상각비및무형자산상각비" 합산 항목 감지
            if "감가상각비" in label and "무형자산상각비" in label and "누계" not in label:
                if result["감가상각비"] is not None:
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} combined '{label}' 발견했으나 이미 값 존재 → 스킵"
                        )
                    continue
                val, col, raw = _get_current_val(row)
                if val is not None:
                    result["감가상각비"] = val
                    combined = True
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} combined 매칭: label='{label}', col={col}, "
                            f"raw='{raw}' → {val}"
                        )
                continue

            # 감가상각비: 정확 매칭
            if label == "감가상각비" or label == "감가상각비용":
                if result["감가상각비"] is not None:
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} '감가상각비' 재발견, 첫 값 유지 → 스킵"
                        )
                else:
                    val, col, raw = _get_current_val(row)
                    if val is not None:
                        result["감가상각비"] = val
                        if debug_trace is not None:
                            debug_trace.append(
                                f"[CF] #{idx}/R{row_idx+1} 감가상각비 매칭: col={col}, raw='{raw}' → {val}"
                            )

            # 무형자산상각비
            if label in ("무형자산상각비", "무형자산상각비용", "무형자산상각"):
                if result["무형자산상각비"] is not None:
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} '무형자산상각비' 재발견, 첫 값 유지 → 스킵"
                        )
                else:
                    val, col, raw = _get_current_val(row)
                    if val is not None:
                        result["무형자산상각비"] = val
                        if debug_trace is not None:
                            debug_trace.append(
                                f"[CF] #{idx}/R{row_idx+1} 무형자산상각비 매칭: col={col}, raw='{raw}' → {val}"
                            )

    result["combined"] = combined
    if debug_trace is not None:
        debug_trace.append(
            f"[CF] Python 추출 결과: 감가상각비={result.get('감가상각비')}, "
            f"무형자산상각비={result.get('무형자산상각비')}, combined={combined}"
        )
    return result


# ── 공개 API ──────────────────────────────────────────────────────────────────

def extract_depreciation(
    rcept_no: str,
    fs_div: str = "CFS",
    strict_scope: bool = True,
) -> dict:
    """
    DART 보고서에서 감가상각비·무형자산상각비를 추출한다.

    워크플로우: AI-선택, Python-검증
      Stage 1: CF에서 Python 키워드 매칭 (AI 불필요)
      Stage 2: 주석에서 AI가 위치 선택 → Python이 해당 위치에서 값 검증 추출

    Args:
        rcept_no: DART 접수번호
        fs_div:   "CFS"=연결, "OFS"=별도
        strict_scope:
            True  -> 사업보고서(경로A)처럼 연결/별도 범위를 엄격히 구분
            False -> 감사보고서/연결감사보고서(경로B)처럼 문서 전체를 사용

    Returns:
        {
            "items": {"감가상각비": float|None, "무형자산상각비": float|None},
            "source": "cf" | "cf+notes" | "notes" | "error",
            "error": str|None,
            "tables_found": int,
            "ai_selection": dict|None,
            "combined": bool,
            "trace": list[str],
        }
    """
    base = {
        "items":         {"감가상각비": None, "무형자산상각비": None},
        "source":        "error",
        "error":         None,
        "tables_found":  0,
        "ai_selection":  None,
        "combined":      False,
        "trace":         [],
    }
    trace: list[str] = [
        f"[START] rcept_no={rcept_no}, fs_div={fs_div}, strict_scope={strict_scope}",
    ]

    # 1. XML 다운로드 및 파싱
    try:
        xml_bytes = _download_dart_document(rcept_no)
        if xml_bytes is None:
            trace.append("[LOAD] XML 다운로드 실패")
            return {**base, "error": "XML 다운로드 실패", "trace": trace}
        soup = _parse_dart_xml(xml_bytes)
        if soup is None:
            trace.append("[LOAD] XML 파싱 실패")
            return {**base, "error": "XML 파싱 실패", "trace": trace}
        trace.append(
            f"[LOAD] XML 파싱 성공, document_scope={_infer_document_scope(soup)}"
        )
    except Exception as e:
        trace.append(f"[LOAD] 문서 로드 실패: {e}")
        return {**base, "error": f"문서 로드 실패: {e}", "trace": trace}

    # 2. [Stage 1] 현금흐름표(CF)에서 추출 (AI 불필요, 가장 신뢰도 높음)
    try:
        cf_result = _extract_from_cf(soup, fs_div=fs_div, strict_scope=strict_scope, debug_trace=trace)
    except Exception as e:
        trace.append(f"[CF] 추출 예외: {e}")
        cf_result = {"감가상각비": None, "무형자산상각비": None, "combined": False}

    cf_combined = cf_result.pop("combined", False)
    trace.append(f"[CF] combined={cf_combined}")

    if cf_result.get("감가상각비") is not None and (cf_result.get("무형자산상각비") is not None or cf_combined):
        # CF에서 충분히 찾음 → 즉시 반환
        trace.append("[FINAL] CF 결과만으로 종료")
        return {
            **base,
            "items":    {k: v for k, v in cf_result.items() if k != "combined"},
            "source":   "cf",
            "combined": cf_combined,
            "trace":    trace,
        }

    # 3. [Stage 2] CF에서 못 찾은 항목 → 주석(NOTES) fallback

    # Step A: 테이블 수집 (Python)
    try:
        tables = _collect_depreciation_tables(
            soup,
            fs_div=fs_div,
            strict_scope=strict_scope,
            debug_trace=trace,
        )
        trace.append(f"[NOTES] 후보 테이블 {len(tables)}개")
        for idx, tbl in enumerate(tables, start=1):
            title = tbl.get("title") or "-"
            signals = tbl.get("signals") or {}
            marks = []
            if signals.get("has_separate_pair"):
                marks.append("SEPARATE_PAIR")
            elif signals.get("has_exact_depr"):
                marks.append("depr")
            if signals.get("has_combined_row"):
                marks.append("COMBINED")
            mark_str = f"[{'/'.join(marks)}] " if marks else ""
            labels = ", ".join(row[0].strip() for row in tbl.get("rows", [])[:5] if row)
            trace.append(f"[NOTES] T{idx} {mark_str}title={title} / labels={labels}")
    except Exception as e:
        tables = []
        base["error"] = f"테이블 수집 실패: {e}"
        trace.append(f"[NOTES] 테이블 수집 실패: {e}")

    base["tables_found"] = len(tables)

    if not tables:
        trace.append("[NOTES] 후보 테이블이 없어 주석 추출 생략")
        # CF에서 부분적으로 찾은 것이 있으면 반환
        if cf_result.get("감가상각비") is not None or cf_result.get("무형자산상각비") is not None:
            trace.append("[FINAL] CF 부분 결과 반환")
            return {
                **base,
                "items":    {k: v for k, v in cf_result.items() if k != "combined"},
                "source":   "cf",
                "combined": cf_combined,
                "trace":    trace,
            }
        trace.append("[FINAL] 추출 결과 없음")
        return {**base, "trace": trace}

    # Step B: AI 테이블/행 선택
    ai_selection = None
    notes_result: dict[str, Optional[float]] = {"감가상각비": None, "무형자산상각비": None, "combined": False}

    try:
        ai_selection = _ai_select_depreciation(tables)
        base["ai_selection"] = ai_selection
        trace.append(
            f"[NOTES] AI 선택 결과: depreciation={ai_selection.get('depreciation')}, "
            f"amortization={ai_selection.get('amortization')}, combined={ai_selection.get('combined')}"
        )

        # Step C: Python이 AI 지목 위치에서 값 검증 추출
        notes_result = _verify_ai_selection(tables, ai_selection, debug_trace=trace)
        trace.append(
            f"[NOTES] 검증 추출 결과: 감가상각비={notes_result.get('감가상각비')}, "
            f"무형자산상각비={notes_result.get('무형자산상각비')}, combined={notes_result.get('combined')}"
        )
    except Exception as e:
        base["error"] = f"AI 선택 실패: {e}"
        trace.append(f"[NOTES] AI 선택 실패: {e}")

    # Step D: CF + Notes 병합 (CF 우선)
    notes_combined = notes_result.get("combined", False)
    final: dict[str, Optional[float]] = {}
    for key in ("감가상각비", "무형자산상각비"):
        cf_val = cf_result.get(key)
        notes_val = notes_result.get(key)
        final[key] = cf_val if cf_val is not None else notes_val

    is_combined = cf_combined or notes_combined

    # source 결정
    cf_found = any(cf_result.get(k) is not None for k in ("감가상각비", "무형자산상각비"))
    notes_found = any(notes_result.get(k) is not None for k in ("감가상각비", "무형자산상각비"))
    if cf_found and notes_found:
        source = "cf+notes"
    elif cf_found:
        source = "cf"
    elif notes_found:
        source = "notes"
    else:
        source = "error"

    trace.append(
        f"[FINAL] source={source}, combined={is_combined}, "
        f"감가상각비={final.get('감가상각비')}, 무형자산상각비={final.get('무형자산상각비')}"
    )

    return {
        **base,
        "items":    final,
        "source":   source,
        "combined": is_combined,
        "trace":    trace,
    }
