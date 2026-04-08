"""
dart_api/notes_parser.py
모듈B: 감가상각비·무형자산상각비 추출 (EBITDA용)

목적:
  EBITDA 계산에 필요한 **전체 비용 기준** 감가상각비·무형자산상각비를 추출한다.

설계 원칙:
  - 기존 모듈A(재무상태표·손익계산서 추출)와 완전 독립
  - 본 모듈 실패 시 모듈A 결과에 영향 없음
  - html_parser.py의 유틸 함수만 재사용 (상태 공유 없음)

워크플로우:
  1. DART XML 다운로드 + 파싱
  2. [1차] 현금흐름표(CF)에서 Python 키워드 매칭으로 추출 (AI 불필요)
  3. [2차] CF에서 못 찾은 항목 → 주석(NOTES) fallback (Python + AI 교차검증)
"""

from functools import lru_cache
from pathlib import Path
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from dart_api.html_parser import (
    _download_dart_document,
    _normalize_title,
    _parse_dart_xml,
    _xml_table_to_rows,
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


def _is_period_table(rows: list[list[str]]) -> bool:
    """
    당기/전기 컬럼 구조인 테이블인지 판별한다.

    당기 비용을 담는 테이블은 보통 "당기"/"전기" 또는 "당기말"/"전기말" 헤더를 갖는다.

    Args:
        rows: 2D 테이블 행 리스트

    Returns:
        True이면 당기/전기 구조
    """
    if not rows:
        return False
    # 첫 2행(헤더)에서 "당기" 포함 여부 확인
    header_text = " ".join(" ".join(r) for r in rows[:2])
    return "당기" in header_text


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


def _extract_depreciation_from_rows(
    rows: list[list[str]],
    unit_multiplier: int = 1,
) -> dict[str, Optional[float]]:
    """
    테이블 행에서 감가상각비·무형자산상각비의 당기 값을 추출한다.

    "감가상각비 및 무형자산상각비" 처럼 합산 항목인 경우:
      - 감가상각비에 해당 값을 기입
      - 무형자산상각비는 None
      - "combined" 플래그를 True로 설정

    Args:
        rows:            2D 테이블 행 리스트
        unit_multiplier: 단위 승수 (1000 = 천원)

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None, "combined": bool}
    """
    result: dict[str, Optional[float]] = {}
    combined = False

    # 가로형 표 대응:
    # 첫 행(또는 둘째 행)에 비용 항목이 열 헤더로 나열되고,
    # 그 아래 행에 당기/전기 금액이 배치되는 경우가 있다.
    header_rows = rows[:2] if len(rows) >= 2 else rows[:1]
    header_targets: dict[str, int] = {}
    for header in header_rows:
        for ci, cell in enumerate(header):
            label = cell.replace(" ", "").strip()
            if "감가상각비" in label and "무형자산상각비" in label and "누계" not in label:
                header_targets["combined"] = ci
            elif label in ("감가상각비", "감가상각비용"):
                header_targets["depr"] = ci
            elif label in ("무형자산상각비", "무형자산상각비용", "무형자산상각"):
                header_targets["amort"] = ci

    if header_targets:
        for key, ci in header_targets.items():
            for row in rows[1:]:
                if ci >= len(row):
                    continue
                val = _parse_number(row[ci])
                if val is None:
                    continue
                if key == "combined":
                    result["감가상각비"] = val * unit_multiplier
                    combined = True
                elif key == "depr":
                    result["감가상각비"] = val * unit_multiplier
                elif key == "amort":
                    result["무형자산상각비"] = val * unit_multiplier
                break

    # 우선순위:
    # 1. 당기 컬럼
    # 2. 합계 컬럼 (비용의 성격별 분류 표)
    # 3. 첫 번째 숫자 컬럼
    current_col = 1

    selected_by_header = False
    for header in header_rows:
        if len(header) <= 1:
            continue
        for ci, cell in enumerate(header):
            label = cell.replace(" ", "").strip()
            if "당기" in label and "전기" not in label:
                current_col = ci
                selected_by_header = True
                break
        if selected_by_header:
            break

    if not selected_by_header:
        for header in header_rows:
            if len(header) <= 1:
                continue
            for ci, cell in enumerate(header):
                label = cell.replace(" ", "").strip()
                if label in ("합계", "총계"):
                    current_col = ci
                    selected_by_header = True
                    break
            if selected_by_header:
                break

    for row in rows:
        if not row:
            continue
        label = row[0].replace(" ", "").strip()

        if current_col >= len(row):
            for ci in range(1, len(row)):
                if _parse_number(row[ci]) is not None:
                    current_col = ci
                    break

        # "감가상각비및무형자산상각비" 합산 항목 감지
        if "감가상각비" in label and "무형자산상각비" in label and "누계" not in label:
            val = _parse_number(row[current_col]) if current_col < len(row) else None
            if val is not None:
                result["감가상각비"] = val * unit_multiplier
                combined = True
            continue

        # 감가상각비 매칭 (단, "사용권자산의 감가상각비", "감가상각누계액" 등 제외)
        if "감가상각비" in label and "누계" not in label:
            # "감가상각비" 정확 매칭 우선
            if label in ("감가상각비", "감가상각비용"):
                val = _parse_number(row[current_col]) if current_col < len(row) else None
                if val is not None:
                    result["감가상각비"] = val * unit_multiplier

        # 무형자산상각비 매칭
        if "무형자산상각비" in label or "무형자산상각" in label:
            if "누계" not in label and "감가상각" not in label:
                val = _parse_number(row[current_col]) if current_col < len(row) else None
                if val is not None:
                    result["무형자산상각비"] = val * unit_multiplier

    result["combined"] = combined
    return result


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

    # 주석이 아닌 섹션 키워드 (이들이 나오면 주석 섹션 종료)
    _NON_NOTES_KEYWORDS = ["재무상태표", "손익계산서", "포괄손익계산서", "자본변동표", "현금흐름표"]

    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
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
            elif any(kw in norm for kw in _NON_NOTES_KEYWORDS):
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

            # 감가상각 키워드 포함 여부 확인
            full_text = " ".join(" ".join(r) for r in rows)
            if not any(kw in full_text for kw in _DEPRECIATION_KEYWORDS):
                if debug_trace is not None:
                    debug_trace.append("[NOTES] -> 감가상각 키워드 없음, 스킵")
                continue

            # 제외 대상 필터링
            if _has_exclude_keywords(rows):
                if debug_trace is not None:
                    debug_trace.append("[NOTES] -> 제외 키워드 포함, 스킵")
                continue

            title = _get_section_title(tag)
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
                "title":  title,
                "unit":   unit,
                "rows":   rows,
                "text":   "\n".join(text_lines),
                "tag":    tag,  # 원본 태그 (디버깅용)
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


# ── Python 추출 ───────────────────────────────────────────────────────────────

def _python_extract_depreciation(tables: list[dict]) -> dict[str, Optional[float]]:
    """
    Python으로 감가상각비·무형자산상각비를 추출한다.

    당기/전기 구조 테이블에서 값을 추출하고,
    여러 테이블에서 발견되면 최대값을 선택한다.
    (전체 비용 기준 값이 부분 값보다 항상 크거나 같으므로)

    합산/분리 처리 규칙:
      - 분리된 값(감가상각비, 무형자산상각비 각각)이 존재하면 분리 값 사용
      - 합산 값만 있으면 합산 값 사용하고 무형자산상각비는 None
      - 합산 값과 별도 무형자산상각비가 동시에 존재하는 경우 이중계산 방지:
        분리된 감가상각비가 있으면 분리 값 우선, 없으면 합산 값 사용하되 무형자산상각비는 None

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None, "combined": bool}
    """
    # 전체 비용 기준 테이블과 부분 테이블(판관비 등)을 분리
    _PARTIAL_SECTION_KEYWORDS = ["판매비", "판관비", "관리비"]

    # 분리된 항목 (합산이 아닌 개별 감가상각비/무형자산상각비)
    separate_depr: list[float] = []
    separate_amort: list[float] = []
    # 합산 항목 ("감가상각비 및 무형자산상각비")
    combined_depr: list[float] = []
    has_combined = False

    for tbl in tables:
        rows = tbl["rows"]
        unit = tbl["unit"]
        title = tbl.get("title", "")

        # 판관비 등 부분 테이블은 제외
        is_partial = any(kw in title for kw in _PARTIAL_SECTION_KEYWORDS)
        if is_partial:
            continue

        extracted = _extract_depreciation_from_rows(rows, unit)

        if extracted.get("combined"):
            has_combined = True
            if extracted.get("감가상각비") is not None:
                combined_depr.append(extracted["감가상각비"])
        else:
            if extracted.get("감가상각비") is not None:
                separate_depr.append(extracted["감가상각비"])
            if extracted.get("무형자산상각비") is not None:
                separate_amort.append(extracted["무형자산상각비"])

    # 분리된 감가상각비/무형자산상각비가 모두 있으면 분리 값 우선 사용
    if separate_depr and separate_amort:
        return {
            "감가상각비":    max(separate_depr),
            "무형자산상각비": max(separate_amort),
            "combined":     False,
        }

    # 합산 값이 있으면 합산 우선 사용
    if combined_depr:
        return {
            "감가상각비":    max(combined_depr),
            "무형자산상각비": None,  # 합산이므로 무형자산상각비 별도 기재 불가
            "combined":     True,
        }

    # 분리된 감가상각비만 있는 경우
    if separate_depr:
        return {
            "감가상각비":    max(separate_depr),
            "무형자산상각비": None,
            "combined":     False,
        }

    # 감가상각비 없이 무형자산상각비만 있는 경우
    return {
        "감가상각비":    None,
        "무형자산상각비": max(separate_amort) if separate_amort else None,
        "combined":     False,
    }


# ── AI 추출 ───────────────────────────────────────────────────────────────────

_DEPRECIATION_AI_GUIDE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "depreciation_ai_guide.md"


@lru_cache(maxsize=1)
def _load_depreciation_ai_guide() -> str:
    """감가상각 AI 추출 규칙 문서를 로드한다."""
    try:
        text = _DEPRECIATION_AI_GUIDE_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    except FileNotFoundError:
        pass
    except OSError:
        pass

    return (
        "# 감가상각 추출 규칙\n"
        "- 회사 전체 기준 값을 우선한다.\n"
        "- 비용의 성격별 분류 또는 현금흐름표 조정항목을 우선한다.\n"
        "- 판매비와관리비, 기능별 배분, 특정 자산 감가상각, 누계액은 제외한다.\n"
        "- '감가상각비 및 무형자산상각비' 합산 표기는 감가상각비에만 기록하고 무형자산상각비는 null로 둔다.\n"
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


def _normalize_ai_source_meta(value) -> dict[str, str]:
    """AI 응답의 source 메타데이터를 로그용 dict로 정규화한다."""
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for key in ("table_id", "section", "row_id", "row_label", "reason"):
        raw = value.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            normalized[key] = text
    return normalized


def _infer_ai_combined(meta: dict[str, str] | None) -> bool:
    """AI 출처 메타데이터만으로 합산 공시 여부를 추론한다."""
    if not isinstance(meta, dict):
        return False

    for key in ("row_label", "reason"):
        text = str(meta.get(key, "")).replace(" ", "")
        if "감가상각비" in text and "무형자산상각비" in text:
            return True
        if "합산공시" in text:
            return True
    return False


def _has_separate_notes_pair(result: dict[str, Optional[float]] | None) -> bool:
    """주석 추출 결과가 감가/무형 분리 기재 쌍인지 판별한다."""
    if not isinstance(result, dict):
        return False
    return (
        not result.get("combined")
        and result.get("감가상각비") is not None
        and result.get("무형자산상각비") is not None
    )


def _format_ai_source_log(item_name: str, meta: dict[str, str]) -> str:
    """AI가 고른 출처를 한 줄 로그로 포맷한다."""
    if not meta:
        return f"[NOTES] AI 출처({item_name}): 미기재"

    parts = []
    if meta.get("table_id"):
        parts.append(f"table={meta['table_id']}")
    if meta.get("section"):
        parts.append(f"section={meta['section']}")
    if meta.get("row_id"):
        parts.append(f"row={meta['row_id']}")
    if meta.get("row_label"):
        parts.append(f"label={meta['row_label']}")
    if meta.get("reason"):
        parts.append(f"reason={meta['reason']}")

    return f"[NOTES] AI 출처({item_name}): " + ", ".join(parts)


def _log_selected_ai_sources(
    trace: list[str],
    ai_result: dict[str, Optional[float]],
    final_result: dict[str, Optional[float]],
) -> None:
    """최종 채택된 항목에 대해 AI가 제시한 출처를 로그에 남긴다."""
    sources = ai_result.get("sources") if isinstance(ai_result, dict) else None
    if not isinstance(sources, dict):
        return

    for key in ("감가상각비", "무형자산상각비"):
        ai_val = ai_result.get(key)
        final_val = final_result.get(key)
        if ai_val is None or final_val is None or ai_val != final_val:
            continue
        meta = sources.get(key)
        if isinstance(meta, dict):
            trace.append(_format_ai_source_log(key, meta))


def _build_ai_prompt(tables_text: str, guide_text: str) -> str:
    """감가상각비 추출용 AI 프롬프트를 생성한다."""
    return (
        "다음은 이 프로젝트에서 반드시 따라야 하는 감가상각/무형자산상각비 추출 규칙 문서다.\n"
        "규칙 문서를 먼저 읽고, 그 기준에 맞게만 판단해라.\n\n"
        f"{guide_text}\n\n"
        "아래는 한국 기업 감사보고서의 주석(Notes)에서 감가상각 관련 테이블을 발췌한 것이다.\n"
        "EBITDA 계산에 필요한 **전체 비용 기준(회사 전체)** 감가상각비와 무형자산상각비를 추출해라.\n\n"
        "## 주의사항\n"
        "- '비용의 성격별 분류' 또는 '현금흐름표 조정항목' 기준의 **전체(회사 전체)** 감가상각비를 찾아라.\n"
        "- **'판매비와 관리비' 주석의 감가상각비는 판관비 내 부분값이므로 절대 선택하지 마라.**\n"
        "- 특정 자산(유형자산, 투자부동산, 사용권자산)만의 감가상각은 부분값이므로 선택하지 마라.\n"
        "- 감가상각누계액(누적값)은 당기 비용이 아니므로 선택하지 마라.\n"
        "- 이연법인세 관련 감가상각비는 완전히 다른 맥락이므로 선택하지 마라.\n"
        "- '감가상각비 및 무형자산상각비'로 합산 표기된 경우: 감가상각비에 해당 값, 무형자산상각비는 null.\n"
        "- 각 테이블 앞에 표시된 '(단위: XXX)'를 반드시 확인하고, 최종 답은 **원(KRW) 단위**로 변환해라.\n"
        "- 답을 고를 때 반드시 [TABLE Tn], [ROW Rn] 표기를 그대로 인용해 출처를 남겨라.\n"
        "- reason에는 왜 그 행이 전체 비용 기준이라고 판단했는지 짧게 적어라.\n"
        "- 찾을 수 없으면 null로 표시해라.\n\n"
        "JSON으로만 응답해라:\n"
        '{"감가상각비": 숫자또는null, "무형자산상각비": 숫자또는null, "combined": true또는false, '
        '"sources": {"감가상각비": {"table_id": "Tn", "section": "섹션명", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"}, '
        '"무형자산상각비": {"table_id": "Tn", "section": "섹션명", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"}}}\n\n'
        f"주석 테이블:\n---\n{tables_text}\n---"
    )


def _ai_extract_depreciation(tables: list[dict]) -> dict[str, Optional[float]]:
    """
    AI(Gemini)로 감가상각비·무형자산상각비를 추출한다.

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None, "combined": bool, "sources": {...}}

    Raises:
        RuntimeError: Gemini API 호출 또는 응답 파싱 실패
    """
    from config import GEMINI_API_KEY
    from gemini_parser import _get_client, _generate, _parse_json

    client = _get_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY 미설정")

    # 테이블 텍스트 병합 (토큰 절약: 8000자 제한)
    combined = _format_tables_for_ai(tables)
    if len(combined) > 8000:
        combined = combined[:8000]

    guide_text = _load_depreciation_ai_guide()
    prompt = _build_ai_prompt(combined, guide_text)
    raw = _generate(client, prompt)
    parsed = _parse_json(raw)
    if parsed is None:
        raise RuntimeError(f"AI 응답 JSON 파싱 실패: {raw[:300]}")

    result: dict[str, Optional[float]] = {}
    for key in ("감가상각비", "무형자산상각비"):
        val = parsed.get(key)
        try:
            result[key] = float(val) if val is not None else None
        except (TypeError, ValueError):
            result[key] = None

    sources = parsed.get("sources")
    if isinstance(sources, dict):
        result["sources"] = {
            "감가상각비": _normalize_ai_source_meta(sources.get("감가상각비")),
            "무형자산상각비": _normalize_ai_source_meta(sources.get("무형자산상각비")),
        }

    combined = parsed.get("combined")
    if isinstance(combined, bool):
        result["combined"] = combined
    else:
        result["combined"] = _infer_ai_combined((result.get("sources") or {}).get("감가상각비"))

    if result.get("combined"):
        result["무형자산상각비"] = None

    return result


# ── 교차검증 ──────────────────────────────────────────────────────────────────

def _cross_validate(
    python_result: dict[str, Optional[float]],
    ai_result: dict[str, Optional[float]],
    rel_tol: float = 0.05,
) -> dict[str, Optional[float]]:
    """
    Python과 AI 결과를 교차검증하여 최종 값을 결정한다.

    규칙:
      - 둘 다 None → None
      - 하나만 값 있음 → 있는 쪽 채택
      - 둘 다 값 있고 일치(5% 이내) → AI 값 채택
      - 둘 다 값 있고 불일치 → AI 값 채택 (AI가 맥락 판단 가능)

    Args:
        python_result: Python 추출 결과
        ai_result:     AI 추출 결과
        rel_tol:       상대 허용 오차

    Returns:
        최종 {"감가상각비": float|None, "무형자산상각비": float|None}
    """
    final: dict[str, Optional[float]] = {}

    for key in ("감가상각비", "무형자산상각비"):
        py_val = python_result.get(key)
        ai_val = ai_result.get(key)

        if py_val is None and ai_val is None:
            final[key] = None
        elif py_val is None:
            final[key] = ai_val
        elif ai_val is None:
            final[key] = py_val
        else:
            # 둘 다 값 있음 → AI 우선 (맥락 판단 능력)
            final[key] = ai_val

    return final


# ── 현금흐름표(CF) 추출 ───────────────────────────────────────────────────────

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

    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            norm = _normalize_title(raw)
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

        elif tag.name == "table" and is_cf_section:
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue

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
        {"감가상각비": float|None, "무형자산상각비": float|None}
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

        for row in rows:
            if not row:
                continue
            label = row[0].replace(" ", "").strip()

            # "감가상각비및무형자산상각비" 합산 항목 감지
            if "감가상각비" in label and "무형자산상각비" in label and "누계" not in label:
                for cell in row[1:]:
                    val = _parse_number(cell)
                    if val is not None:
                        result["감가상각비"] = val
                        combined = True
                        break
                continue

            # 감가상각비: 정확 매칭 ("감가상각비" == label)
            if label == "감가상각비" or label == "감가상각비용":
                for cell in row[1:]:
                    val = _parse_number(cell)
                    if val is not None:
                        result["감가상각비"] = val
                        break

            # 무형자산상각비
            if label in ("무형자산상각비", "무형자산상각비용", "무형자산상각"):
                for cell in row[1:]:
                    val = _parse_number(cell)
                    if val is not None:
                        result["무형자산상각비"] = val
                        break

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

    모듈A와 완전 독립적으로 동작한다.
    실패 시 items 값이 None으로 설정되며, 예외를 발생시키지 않는다.

    Args:
        rcept_no: DART 접수번호
        fs_div:   "CFS"=연결, "OFS"=별도 (CF 테이블 선택에 사용)
        strict_scope:
            True  -> 사업보고서(경로A)처럼 연결/별도 범위를 엄격히 구분
            False -> 감사보고서/연결감사보고서(경로B)처럼 문서 전체를 사용

    Returns:
        {
            "items": {"감가상각비": float|None, "무형자산상각비": float|None},
            "source": "ai" | "python" | "cross_validated" | "error",
            "error": str|None,
            "tables_found": int,
            "python_result": dict,
            "ai_result": dict|None,
        }
    """
    base = {
        "items":         {"감가상각비": None, "무형자산상각비": None},
        "source":        "error",
        "error":         None,
        "tables_found":  0,
        "python_result": {"감가상각비": None, "무형자산상각비": None},
        "ai_result":     None,
        "combined":      False,  # "감가상각비 및 무형자산상각비" 합산 여부
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

    # 2. [1차] 현금흐름표(CF)에서 추출 시도 (AI 불필요, 가장 신뢰도 높음)
    try:
        cf_result = _extract_from_cf(soup, fs_div=fs_div, strict_scope=strict_scope, debug_trace=trace)
    except Exception as e:
        trace.append(f"[CF] 추출 예외: {e}")
        cf_result = {"감가상각비": None, "무형자산상각비": None}

    cf_combined = cf_result.pop("combined", False)
    trace.append(f"[CF] combined={cf_combined}")

    if cf_result.get("감가상각비") is not None and (cf_result.get("무형자산상각비") is not None or cf_combined):
        # CF에서 찾음 → 즉시 반환
        trace.append("[FINAL] CF 결과만으로 종료")
        return {
            **base,
            "items":        {k: v for k, v in cf_result.items() if k != "combined"},
            "source":       "cf",
            "python_result": cf_result,
            "combined":     cf_combined,
            "trace":        trace,
        }

    # 3. [2차] CF에서 못 찾은 항목 → 주석(NOTES) fallback
    try:
        tables = _collect_depreciation_tables(
            soup,
            fs_div=fs_div,
            strict_scope=strict_scope,
            debug_trace=trace,
        )
        trace.append(f"[NOTES] 후보 테이블 {len(tables)}개")
        for idx, tbl in enumerate(tables[:5], start=1):
            title = tbl.get("title") or "-"
            labels = ", ".join(row[0].strip() for row in tbl.get("rows", [])[:5] if row)
            trace.append(f"[NOTES] 테이블 #{idx}: title={title} / labels={labels}")
    except Exception as e:
        tables = []
        base["error"] = f"테이블 수집 실패: {e}"
        trace.append(f"[NOTES] 테이블 수집 실패: {e}")

    base["tables_found"] = len(tables)

    # 주석 Python 추출
    try:
        notes_python = _python_extract_depreciation(tables) if tables else {"감가상각비": None, "무형자산상각비": None}
        trace.append(
            f"[NOTES] Python 추출 결과: 감가상각비={notes_python.get('감가상각비')}, "
            f"무형자산상각비={notes_python.get('무형자산상각비')}, combined={notes_python.get('combined')}"
        )
    except Exception as e:
        trace.append(f"[NOTES] Python 추출 예외: {e}")
        notes_python = {"감가상각비": None, "무형자산상각비": None}

    # 주석 AI 추출
    notes_ai = None
    if tables:
        try:
            notes_ai = _ai_extract_depreciation(tables)
            base["ai_result"] = notes_ai
            trace.append(
                f"[NOTES] AI 추출 결과: 감가상각비={notes_ai.get('감가상각비')}, "
                f"무형자산상각비={notes_ai.get('무형자산상각비')}, combined={notes_ai.get('combined')}"
            )
        except Exception as e:
            base["error"] = f"AI 추출 실패: {e}"
            trace.append(f"[NOTES] AI 추출 실패: {e}")
    else:
        trace.append("[NOTES] 후보 테이블이 없어 AI 추출 생략")

    # 주석 교차검증
    ai_combined = bool(notes_ai.get("combined")) if isinstance(notes_ai, dict) else False
    python_has_separate_pair = _has_separate_notes_pair(notes_python)

    if notes_ai:
        notes_final = _cross_validate(notes_python, notes_ai)
        if python_has_separate_pair and ai_combined:
            notes_final = {
                "감가상각비": notes_python.get("감가상각비"),
                "무형자산상각비": notes_python.get("무형자산상각비"),
            }
            trace.append("[NOTES] Python 분리 기재 우선: AI가 합산 공시를 선택했지만 주석에 분리 기재 쌍이 있어 Python 결과를 유지")
        trace.append(
            f"[NOTES] 교차검증 결과: 감가상각비={notes_final.get('감가상각비')}, "
            f"무형자산상각비={notes_final.get('무형자산상각비')}"
        )
        _log_selected_ai_sources(trace, notes_ai, notes_final)
    else:
        notes_final = notes_python

    # 합산 표기에서 감가상각비를 사용한 경우, AI가 무형자산상각비만 따로 채워도
    # 혼합 기재하지 않도록 무형자산상각비를 비운다.
    if (notes_python.get("combined") or ai_combined) and not python_has_separate_pair:
        notes_final["무형자산상각비"] = None
        if notes_python.get("combined") and ai_combined:
            trace.append("[NOTES] Python/AI 모두 combined=True 이므로 무형자산상각비를 None으로 고정")
        elif ai_combined:
            trace.append("[NOTES] AI combined=True 이므로 무형자산상각비를 None으로 고정")
        else:
            trace.append("[NOTES] Python combined=True 이므로 무형자산상각비를 None으로 고정")

    # CF 결과와 주석 결과 병합 (CF 우선)
    final: dict[str, Optional[float]] = {}
    for key in ("감가상각비", "무형자산상각비"):
        final[key] = cf_result.get(key) or notes_final.get(key)

    notes_combined = notes_python.get("combined", False) if isinstance(notes_python, dict) else False
    if python_has_separate_pair:
        notes_combined = False
        ai_combined = False
    is_combined = cf_combined or notes_combined or ai_combined

    source = "cf" if all(cf_result.get(k) is not None for k in ("감가상각비", "무형자산상각비")) else \
             "cf+notes" if any(cf_result.get(k) is not None for k in ("감가상각비", "무형자산상각비")) else \
             "notes"

    base["python_result"] = {
        "cf": cf_result,
        "notes": notes_python,
    }
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
