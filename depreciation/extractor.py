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
    download_all_dart_documents as _download_all_dart_documents,
    download_dart_document as _download_dart_document,
    normalize_title as _normalize_title,
    parse_dart_xml as _parse_dart_xml,
    xml_table_to_rows as _xml_table_to_rows,
)
from utils.units import (
    UNIT_MULTIPLIERS as _UNIT_MULTIPLIERS,
    detect_unit_multiplier as _detect_unit_multiplier,
    detect_unit_from_rows as _detect_unit_from_rows,
    unit_label as _unit_label,
)
from financial.extraction_result import (
    add_detail_flag,
    details_from_items,
    empty_item_details,
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

_DEPR_ITEM_NAMES = ["감가상각비", "사용권자산상각비", "무형자산상각비"]


def _build_depreciation_item_details(
    items: dict[str, Optional[float]],
    *,
    rcept_no: str,
    fs_div: str,
    source: str,
    combined: bool = False,
    risk_flags_by_item: Optional[dict[str, list[str]]] = None,
    selected_candidate_type_by_item: Optional[dict[str, str]] = None,
) -> dict:
    """
    Wrap depreciation values in the shared item_details shape.

    risk_flags_by_item / selected_candidate_type_by_item이 주어지면 해당 항목의 detail에
    선택된 표의 위험 플래그와 분류 유형을 부착하고 confidence를 low_confidence로 강등한다.
    AI가 후보 중 위험 플래그가 붙은 표를 골랐을 때 최종 결과를 verified로 두지 않기 위함이다
    (명세 #7).
    """
    details = details_from_items(
        {name: items.get(name) for name in _DEPR_ITEM_NAMES},
        source={
            "source_type": "depreciation_extractor",
            "rcept_no": rcept_no,
            "fs_div": fs_div,
            "depreciation_source": source,
        },
        confidence="verified" if source != "error" else "missing",
        flags=["depreciation_position_in_trace"],
    )
    if combined and details.get("감가상각비", {}).get("value") is not None:
        add_detail_flag(details["감가상각비"], "combined_depreciation_and_amortization")

    for name in _DEPR_ITEM_NAMES:
        detail = details.get(name)
        if not detail or detail.get("value") is None:
            continue
        cand_type = (selected_candidate_type_by_item or {}).get(name)
        if cand_type:
            detail.setdefault("source", {})["candidate_type"] = cand_type
        flags = (risk_flags_by_item or {}).get(name) or []
        if flags:
            for flag in flags:
                add_detail_flag(detail, flag)
            detail["confidence"] = "low_confidence"
    return details


# ── 내부 유틸 ─────────────────────────────────────────────────────────────────

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

    text = text.replace(" ", "").strip()
    # 주석(노트 번호) 컬럼은 "2,4,13,27,28,33"처럼 콤마로 나열돼 숫자처럼 보이지만
    # 금액이 아니다. 콤마가 있으면 천단위 그룹 형식(첫 그룹 1~3자리, 이후 정확히
    # 3자리)일 때만 금액으로 인정한다.
    if "," in text:
        if not re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            return None
        text = text.replace(",", "")
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
            # "당기"가 기간 헤더(당기/당기말/당기초)일 때만 매칭한다. "당기순이익"·
            # "당기손익" 같은 계정 라벨의 "당기"(뒤에 한글이 이어짐)는 제외한다.
            if (
                re.search(r"당기(?:말|초)?(?![가-힣])", norm)
                and "전기" not in norm
                and "전전기" not in norm
            ):
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
    #    "제 37 (당) 기"처럼 (당)/(전) 표기가 끼어드는 헤더도 잡는다.
    gi_candidates: list[tuple[int, int]] = []
    for header in header_rows:
        for ci, cell in enumerate(header):
            m = re.search(r"제\s*(\d+)\s*\(?\s*[당전]?\s*\)?\s*기", cell)
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
_EXACT_ROU_AMORT_LABELS = {
    "사용권자산상각비", "사용권자산상각비용", "사용권자산상각",
    "사용권자산감가상각비", "사용권자산감가상각비용", "사용권자산감가상각",
}
_EXACT_AMORT_LABELS = {"무형자산상각비", "무형자산상각비용", "무형자산상각"}


def _is_intangible_amortization_label(label: str) -> bool:
    """
    무형자산상각비(무형자산 상각/감가상각) 행인지 판정한다.

    회사마다 표기가 갈린다: '무형자산상각비'가 표준이나 일부는 '무형자산감가상각비'로
    적는다(예: 2025 에실로코리아 '26. 비용의 성격별 분류'). 후자는 '무형자산'과 '상각'
    사이에 '감가'가 끼어 '무형자산상각' substring으로는 잡히지 않으므로, '무형자산'과
    '상각'이 함께 있으면 무형자산상각으로 본다. 이렇게 잡아야:
      (a) 감가상각비 합산에서 제외되어 무형자산상각비가 감가상각비에 오합산되지 않고,
      (b) 무형자산상각비 버킷으로 올바로 분리된다.

    '무형자산손상차손'·'무형자산처분손실' 등은 '상각'이 없어 자동 제외된다.

    Args:
        label: _normalize_row_label() 적용된 행 라벨
    """
    if "누계" in label:
        return False
    if "무형자산" not in label:
        return False
    return "상각" in label


def _is_general_depreciation_label(label: str) -> bool:
    """
    '감가상각비' 버킷에 합산할 일반 감가상각 행인지 판정한다.

    '투자부동산 감가상각비'·'유형자산 감가상각비'처럼 사용권/무형이 아닌 감가상각
    항목은 모두 감가상각비에 합쳐야 한다(중복 없이). 사용권/무형은 각자 버킷이고,
    '대손상각비'(감가상각 아님)·'누계액'·합산행은 제외한다.

    Args:
        label: _normalize_row_label() 적용된 행 라벨

    규칙:
      - '감가상각' 또는 '투자부동산상각'을 포함해야 함
        → '대손상각비'·'무형자산상각비'(감가상각 없음) 자동 제외
        → '투자부동산상각비'(2022 STX엔진 등 '감가'가 빠진 단축 표기)는 보조 토큰으로 포함.
          CF 경로(_extract_from_cf)도 동일 토큰으로 감가상각비에 합산한다.
      - '사용권' 포함 → 제외(사용권자산상각비 버킷)
      - 무형자산상각(=무형자산상각비/무형자산감가상각비) → 제외(무형자산상각비 버킷 및
        합산행 '감가상각비및무형자산상각비' 방어)
      - '누계' 포함 → 제외(자산 변동표의 상각누계액)
    """
    if "감가상각" not in label and "투자부동산상각" not in label:
        return False
    if "누계" in label:
        return False
    if "사용권" in label:
        return False
    if _is_intangible_amortization_label(label):
        return False
    return True

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
    # 선두 번호/한글 접두어 반복 제거 ("1.가.감가상각비" 같은 이중 접두어 대응).
    # 한글 접두어는 정상 라벨 첫 글자("감"·"무"·"사" 등)를 잘라먹지 않도록
    # 반드시 구분자(., ), (가) 형태)를 동반할 때만 매칭한다.
    _PREFIX_RE = (
        r"^(?:"
        r"[\(\[]?[\d①-⑳]{1,3}[\)\]]?[\.\s]*"      # 숫자 접두어 (1., (1), ① 등)
        r"|\(\s*[가나다라마바사아자차카타파하]\s*\)[\.\s]*"  # (가) 형태
        r"|[가나다라마바사아자차카타파하][\.\)][\.\s]*"        # 가. / 가) 형태
        r")"
    )
    while True:
        new_norm = re.sub(_PREFIX_RE, "", norm)
        if not new_norm or new_norm == norm:
            break
        norm = new_norm
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
            "has_general_depr": bool,   # 일반 감가상각 행 존재(유형자산/투자부동산 등)
            "has_exact_amort": bool,    # 정확 무형자산상각비 행 존재
            "has_combined_row": bool,   # 합산 행 존재
            "has_separate_pair": bool,  # 분리 기재 쌍 존재 (depr + amort)
        }
    """
    has_exact_depr = False
    has_general_depr = False
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
                has_general_depr = True
            continue

        # 무형자산상각비 매칭('무형자산상각비'·'무형자산감가상각비' 등)
        if _is_intangible_amortization_label(label):
            if _row_has_number(row):
                has_exact_amort = True
            continue

        # 일반 감가상각 행(증권·자산운용 등 금융사 CF 주석은 '감가상각비' 단일 행 없이
        # '유형자산감가상각비'·'투자부동산감가상각비'로 분리 기재한다). 이 행들도
        # 감가상각비 후보 시그널로 인정해야 표가 후보에서 누락되지 않는다.
        if _is_general_depreciation_label(label) and _row_has_number(row):
            has_general_depr = True

    return {
        "has_exact_depr":    has_exact_depr,
        "has_general_depr":  has_general_depr,
        "has_exact_amort":   has_exact_amort,
        "has_combined_row":  has_combined_row,
        "has_separate_pair": has_general_depr and has_exact_amort,
    }


# ── 후보 테이블 구조 분류 (명세 #7) ─────────────────────────────────────────

# 회사 전체 비용을 가리키는 강한 긍정 시그널 (섹션 제목)
_STRONG_POSITIVE_TITLE_HINTS = ("비용의성격별", "성격별비용", "성격별분류")

# 자산 변동표를 가리키는 헤더 컬럼명 (취득원가/누계액/장부금액 등)
_ASSET_MOVEMENT_HEADER_TOKENS = (
    "취득원가", "장부금액", "감가상각누계액", "감액손실누계액",
    "정부보조금", "재평가잉여금",
)
# 자산 변동표의 흐름 컬럼 (기초·취득·처분·대체·기말 등이 한 표에 모이면 변동표)
_ASSET_MOVEMENT_FLOW_TOKENS = ("기초", "취득", "처분", "대체", "기말", "감액", "환입")

# 기능별 배분 표를 가리키는 컬럼/라벨 (판관비, 매출원가, 제조원가, 연구개발비 등)
_FUNCTIONAL_BREAKDOWN_TOKENS = (
    "판매비와관리비", "판매비", "판관비",
    "매출원가", "제조원가", "연구개발비",
)

# 부분 자산군만 다루는 표를 가리키는 제목 토큰 (이미 _PARTIAL_TITLE_KEYWORDS로 1차 걸러지지만
# 제목이 비어 있는 본문 표가 통과할 때를 대비한 보조 시그널)
_PARTIAL_ASSET_CLASS_TOKENS = ("유형자산", "투자부동산", "사용권자산", "리스자산", "무형자산")


def _classify_depreciation_candidate(
    rows: list[list[str]],
    title: Optional[str],
) -> tuple[str, list[str]]:
    """
    감가상각 후보 테이블의 구조 유형(candidate_type)과 위험 플래그(risk_flags)를 분류한다.

    candidate_type 값:
      - "expense_by_nature":     "비용의 성격별 분류" 등 회사 전체 비용 표 — 강한 긍정
      - "asset_movement":        취득원가/감가상각누계액/장부금액 자산 변동표 — 부분값(제외)
      - "functional_breakdown":  판관비/매출원가/제조원가/연구개발비 기능별 배분 — 부분값(제외)
      - "general_depreciation":  위 분류에 속하지 않지만 정확한 감가상각비 행이 있는 일반 후보
      - "unknown":               근거 부족 (호출측에서 risk_flag로 처리)

    risk_flags는 분류 근거(예: asset_movement_headers, functional_breakdown_columns)와
    살아남는 후보의 보조 위험(예: partial_asset_class_title)을 모두 기록한다.
    호출측은 분류된 유형으로 후보에서 제외하거나, 살아남은 표의 risk_flags를 최종 결과
    confidence 강등에 사용한다.
    """
    risk_flags: list[str] = []
    title_norm = (title or "").replace(" ", "")

    # 헤더(상단 4행) 모든 셀을 공백 제거하여 키워드 검색 대상으로 모은다.
    header_cells = [cell for r in rows[:4] for cell in r]
    header_text = " ".join(c.replace(" ", "") for c in header_cells)
    # 기능별 배분 토큰(매출원가/판관비 등)은 '컬럼 헤더'에 등장한다. '비용의 성격별
    # 분류' 표의 행 라벨 '상품매출원가'(0번 컬럼)를 기능별 컬럼으로 오인하지 않도록,
    # 기능별 판정은 0번(라벨) 컬럼을 뺀 셀에서만 검색한다.
    col_header_text = " ".join(
        c.replace(" ", "") for r in rows[:4] for c in r[1:]
    )

    # 1) 자산 변동표: 누계액/취득원가/장부금액류가 함께 등장하거나 흐름 컬럼이 다수
    asset_marker_hits = sum(1 for tok in _ASSET_MOVEMENT_HEADER_TOKENS if tok in header_text)
    flow_marker_hits = sum(1 for tok in _ASSET_MOVEMENT_FLOW_TOKENS if tok in header_text)
    if asset_marker_hits >= 2:
        risk_flags.append("asset_movement_headers")
    if flow_marker_hits >= 3:
        risk_flags.append("asset_movement_flow_columns")
    if asset_marker_hits >= 2 or flow_marker_hits >= 3:
        return "asset_movement", risk_flags

    # 2) 비용의 성격별 분류: 섹션 제목 강한 긍정 → 회사 전체 비용 표.
    #    기능별 배분 판정보다 우선한다('상품매출원가' 같은 행이 있어도 성격별 표다).
    if any(hint in title_norm for hint in _STRONG_POSITIVE_TITLE_HINTS):
        return "expense_by_nature", risk_flags

    # 3) 기능별 배분 표: 컬럼 헤더(0번 라벨 컬럼 제외)에 판관비/매출원가/제조원가 등
    func_hits = sum(1 for tok in _FUNCTIONAL_BREAKDOWN_TOKENS if tok in col_header_text)
    if func_hits >= 1:
        risk_flags.append("functional_breakdown_columns")
        return "functional_breakdown", risk_flags

    # 4) 부분 자산군 제목 (살아남은 후보에 보조 risk_flag로 부착)
    if title_norm and any(tok in title_norm for tok in _PARTIAL_ASSET_CLASS_TOKENS):
        risk_flags.append("partial_asset_class_title")

    return "general_depreciation", risk_flags


# 후보에서 제외할 candidate_type
_EXCLUDED_CANDIDATE_TYPES = {"asset_movement", "functional_breakdown"}


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
            if not (
                signals["has_exact_depr"]
                or signals["has_general_depr"]
                or signals["has_combined_row"]
            ):
                if debug_trace is not None:
                    preview = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[NOTES] -> 감가상각비/합산 행 없음, 스킵 (labels={preview})"
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

            # 4차 필터: 테이블 구조 기반 분류 (명세 #7).
            # 자산 변동표/기능별 배분 표는 부분값이므로 후보에서 제외하고 사유를 trace에 남긴다.
            # 살아남는 후보에는 candidate_type과 risk_flags를 부착해 AI 선택·결과 신뢰도에 사용한다.
            candidate_type, risk_flags = _classify_depreciation_candidate(rows, title)
            if candidate_type in _EXCLUDED_CANDIDATE_TYPES:
                if debug_trace is not None:
                    debug_trace.append(
                        f"[NOTES] -> 구조 분류 {candidate_type} (flags={risk_flags}), 후보 제외 "
                        f"(title={title or '-'})"
                    )
                continue

            # 캡션/부모/헤더 태그에서 단위 감지 실패 시, 표 안의 "(단위: 천원)"
            # 같은 행에서 다시 찾는다(단위 표기가 표 밖이 아니라 표 안에 있는 케이스).
            unit = _detect_unit_multiplier(tag)
            if unit == 1:
                unit = _detect_unit_from_rows(rows, fallback=1)

            # AI용 텍스트 생성
            text_lines = []
            if title:
                text_lines.append(f"[섹션: {title}]")
            text_lines.append(f"[CANDIDATE_TYPE] {candidate_type}")
            unit_label = {1: "원", 1000: "천원", 1_000_000: "백만원"}.get(unit, f"{unit}원")
            text_lines.append(f"(단위: {unit_label})")
            for row in rows:
                text_lines.append(" | ".join(row))

            entry = {
                "title":          title,
                "unit":           unit,
                "rows":           rows,
                "text":           "\n".join(text_lines),
                "tag":            tag,  # 원본 태그 (디버깅용)
                "signals":        signals,
                "candidate_type": candidate_type,
                "risk_flags":     risk_flags,
            }
            if debug_trace is not None:
                debug_trace.append(
                    f"[NOTES] -> 후보 채택 candidate_type={candidate_type} flags={risk_flags} "
                    f"(title={title or '-'})"
                )

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

        candidate_type = tbl.get("candidate_type") or "unknown"
        lines = [
            f"[TABLE {table_id}]",
            f"[SECTION] {title}",
            f"[TYPE] {candidate_type}",
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
        "EBITDA 계산에 필요한 **전체 비용 기준(회사 전체)** 감가상각비·사용권자산상각비·무형자산상각비가 있는\n"
        "**테이블과 행의 위치**를 선택해라. 숫자를 직접 읽지 마라.\n\n"
        "## 주의사항\n"
        "- '비용의 성격별 분류' 또는 '현금흐름표 조정항목' 기준의 **전체(회사 전체)** 감가상각비를 찾아라.\n"
        "- **'판매비와 관리비' 주석의 감가상각비는 판관비 내 부분값이므로 절대 선택하지 마라.**\n"
        "- '유형자산', '투자부동산'의 **자산 내역(취득원가/누계액) 테이블** 안에 있는 감가상각은 부분값이므로 선택하지 마라.\n"
        "- 단, 비용의 성격별 분류·CF 조정항목 테이블에서 회사 전체 비용을 '감가상각비', "
        "'사용권자산상각비', '무형자산상각비'로 분리 기재한 경우에는 각각을 별도 행으로 선택해라.\n"
        "- 감가상각누계액(누적값)은 당기 비용이 아니므로 선택하지 마라.\n"
        "- 이연법인세 관련 감가상각비는 완전히 다른 맥락이므로 선택하지 마라.\n"
        "- 분리 기재(감가상각비/사용권자산상각비/무형자산상각비 각각 별도 행)가 있으면 분리를 선택해라.\n"
        "- 사용권자산상각비는 분리 기재된 경우에만 선택. 회사가 감가상각비에 통합 기재한 경우 null.\n"
        "- 합산 기재('감가상각비 및 무형자산상각비')만 있으면 combined=true로 표시.\n"
        "- 찾을 수 없으면 해당 항목을 null로 표시.\n\n"
        "JSON으로만 응답해라:\n"
        "```json\n"
        '{\n'
        '  "depreciation": {"table_id": "Tn", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"},\n'
        '  "rou_amortization": {"table_id": "Tn", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"},\n'
        '  "amortization": {"table_id": "Tn", "row_id": "Rn", "row_label": "행 라벨", "reason": "선택 이유"},\n'
        '  "combined": false\n'
        '}\n'
        "```\n"
        "값이 없으면 해당 키를 null로 둬라 (예: \"rou_amortization\": null).\n\n"
        f"주석 테이블:\n---\n{tables_text}\n---"
    )


def _ai_select_depreciation(tables: list[dict], log_fn=None) -> dict:
    """
    AI(Gemini)에게 감가상각비·무형자산상각비의 올바른 위치를 선택하게 한다.

    AI는 숫자를 반환하지 않고, 테이블/행의 위치만 반환한다.

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {
            "depreciation":     {"table_id": ..., "row_id": ..., "row_label": str, "reason": str} | None,
            "rou_amortization": {"table_id": ..., "row_id": ..., "row_label": str, "reason": str} | None,
            "amortization":     {"table_id": ..., "row_id": ..., "row_label": str, "reason": str} | None,
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
            "단, 이 테이블이 판관비/유형자산/기능별 배분 등 부분값 테이블이면 제외해라. "
            "같은 테이블에 '사용권자산상각비'가 별도 행으로 있으면 그것도 함께 선택해라.\n\n"
        )

    guide_text = _load_depreciation_ai_guide()
    prompt = _build_ai_prompt(combined_text, guide_text, hint=hint)
    raw = _generate(client, prompt, log_fn=log_fn)
    parsed = _parse_json(raw)
    if parsed is None:
        raise RuntimeError(f"AI 응답 JSON 파싱 실패: {raw[:300]}")

    result: dict = {
        "depreciation":     None,
        "rou_amortization": None,
        "amortization":     None,
        "combined":         False,
    }

    for key in ("depreciation", "rou_amortization", "amortization"):
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
        result["rou_amortization"] = None

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
        # 당기 컬럼이 '-' 등 비숫자이면 해당 항목은 당기에 없는 것 (0 또는 미계상)
        # 전기 값을 가져오면 안 되므로 None 반환
        if debug_trace is not None:
            debug_trace.append(
                f"[VERIFY] {table_id}/{row_id}: col={current_col}, raw='{target_row[current_col]}' → 당기 값 없음 (None)"
            )
        return None

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


def _clean_display_label(label: str) -> str:
    """행 라벨을 표시용으로 정리한다(연속 공백 1칸, 앞 번호 접두 제거).

    비고 문구에 원문 합산 라벨을 그대로 보여주기 위한 것으로, 정규화(공백 제거)와
    달리 '감가상각비 및 무형자산상각비'처럼 사람이 읽기 좋은 형태를 유지한다.
    """
    norm = re.sub(r"\s+", " ", (label or "")).strip()
    # 선두 번호/기호 접두어 제거 ("1.", "(1)", "가." 등)
    norm = re.sub(r"^[\(\[]?[\d①-⑳]{1,3}[\)\]]?[.\s]*", "", norm).strip()
    return norm


def _raw_display_row_label(
    tables: list[dict],
    table_id: str,
    row_id: str,
) -> Optional[str]:
    """AI가 지목한 위치의 원문 행 라벨(표시용, 공백 유지)을 반환한다."""
    m = re.match(r"T(\d+)", table_id or "")
    if not m:
        return None
    ti = int(m.group(1)) - 1
    if ti < 0 or ti >= len(tables):
        return None
    rows = tables[ti].get("rows", [])
    m = re.match(r"R(\d+)", row_id or "")
    if not m:
        return None
    ri = int(m.group(1)) - 1
    if ri < 0 or ri >= len(rows) or not rows[ri]:
        return None
    return _clean_display_label(rows[ri][0])


def _sum_general_depreciation_in_table(
    tables: list[dict],
    table_id: str,
    debug_trace: Optional[list[str]] = None,
) -> Optional[float]:
    """
    AI가 감가상각비로 지목한 '표 전체'에서 일반 감가상각 행을 모두 합산한다.

    한 표 안에 '감가상각비'와 '투자부동산 감가상각비'처럼 여러 감가상각 행이
    분리 기재된 회사가 있다. 사용권/무형은 각자 버킷으로 따로 추출되므로 여기서는
    제외하여 중복 계산을 원천 차단한다. (표 선택 자체는 AI가 수행)

    Returns:
        원(KRW) 단위 합계, 일치 행이 없으면 None
    """
    m = re.match(r"T(\d+)", table_id or "")
    if not m:
        return None
    ti = int(m.group(1)) - 1
    if ti < 0 or ti >= len(tables):
        return None
    tbl = tables[ti]
    rows = tbl["rows"]
    unit = tbl["unit"]
    current_col = _detect_current_column(rows)

    total: Optional[float] = None
    parts: list[tuple[str, float]] = []
    for row in rows:
        if not row:
            continue
        label = _normalize_row_label(row[0])
        if not _is_general_depreciation_label(label):
            continue
        val: Optional[float] = None
        if current_col is not None and current_col < len(row):
            v = _parse_number(row[current_col])
            if v is not None:
                val = v * unit
        if val is None:
            for ci in range(1, len(row)):
                v = _parse_number(row[ci])
                if v is not None:
                    val = v * unit
                    break
        if val is not None:
            total = (total or 0.0) + val
            parts.append((label, val))

    if debug_trace is not None and parts:
        debug_trace.append(
            f"[AGG] 감가상각비 합산({table_id}): {parts} → {total}"
        )
    return total


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
        {"감가상각비": float|None, "사용권자산상각비": float|None,
         "무형자산상각비": float|None, "combined": bool}
    """
    result: dict[str, Optional[float]] = {
        "감가상각비": None,
        "사용권자산상각비": None,
        "무형자산상각비": None,
    }
    combined = ai_selection.get("combined", False)
    combined_label: Optional[str] = None

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
        # 정확 '감가상각비'뿐 아니라 '유형자산감가상각비'·'투자부동산감가상각비' 같은
        # 일반 감가상각 라벨도 허용한다(금융사 CF 주석 분리 기재 대응). 사용권/무형은
        # _is_general_depreciation_label에서 제외되므로 오인식되지 않는다.
        is_general_depr_label = (
            actual_label is not None
            and not is_combined_label
            and _is_general_depreciation_label(actual_label)
        )
        if actual_label is not None and (
            actual_label in _EXACT_DEPR_LABELS or is_combined_label or is_general_depr_label
        ):
            if is_combined_label:
                # 합산 공시('감가상각비 및 무형자산상각비')는 단일 합산값을 그대로 사용
                val = _extract_value_at_position(
                    tables, depr_sel["table_id"], depr_sel["row_id"], debug_trace
                )
            else:
                # 일반 감가상각: 선택된 표 안의 모든 감가상각 행(투자부동산 등)을 합산.
                # 사용권/무형은 제외되므로 중복 계산되지 않는다.
                val = _sum_general_depreciation_in_table(
                    tables, depr_sel["table_id"], debug_trace
                )
                if val is None:
                    val = _extract_value_at_position(
                        tables, depr_sel["table_id"], depr_sel["row_id"], debug_trace
                    )
            result["감가상각비"] = val
            # 합산 공시인 경우, 비고에 표시할 원문 합산 라벨을 보관한다
            # (예: '감가상각비 및 무형자산상각비', '감가상각비와 기타상각비').
            if combined and val is not None:
                combined_label = _raw_display_row_label(
                    tables, depr_sel["table_id"], depr_sel["row_id"]
                )
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] 감가상각비: table={depr_sel['table_id']}, row={depr_sel['row_id']}, "
                    f"actual_label='{actual_label}', value={val}"
                )
        else:
            if debug_trace is not None:
                debug_trace.append(
                    f"[VERIFY] 감가상각비 라벨 불일치: AI선택={depr_sel.get('row_label')!r}, "
                    f"actual='{actual_label}', 허용=감가상각비/일반감가상각/합산 → null 처리"
                )

    # 사용권자산상각비 추출 (합산이 아닌 경우만, 라벨 검증 포함)
    if not combined:
        rou_sel = ai_selection.get("rou_amortization")
        if isinstance(rou_sel, dict):
            actual_label = _resolve_actual_row_label(
                tables, rou_sel["table_id"], rou_sel["row_id"]
            )
            if actual_label is not None and actual_label in _EXACT_ROU_AMORT_LABELS:
                val = _extract_value_at_position(
                    tables, rou_sel["table_id"], rou_sel["row_id"], debug_trace
                )
                result["사용권자산상각비"] = val
                if debug_trace is not None:
                    debug_trace.append(
                        f"[VERIFY] 사용권자산상각비: table={rou_sel['table_id']}, row={rou_sel['row_id']}, "
                        f"actual_label='{actual_label}', value={val}"
                    )
            else:
                if debug_trace is not None:
                    debug_trace.append(
                        f"[VERIFY] 사용권자산상각비 라벨 불일치: AI선택={rou_sel.get('row_label')!r}, "
                        f"actual='{actual_label}', 허용={_EXACT_ROU_AMORT_LABELS} → null 처리"
                    )

    # 무형자산상각비 추출 (합산이 아닌 경우만, 라벨 검증 포함)
    if not combined:
        amort_sel = ai_selection.get("amortization")
        if isinstance(amort_sel, dict):
            actual_label = _resolve_actual_row_label(
                tables, amort_sel["table_id"], amort_sel["row_id"]
            )
            if actual_label is not None and _is_intangible_amortization_label(actual_label):
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
                        f"actual='{actual_label}', 허용=무형자산상각/무형자산감가상각 → null 처리"
                    )

    result["combined"] = combined
    result["combined_label"] = combined_label
    return result


# ── 현금흐름표(CF) 추출 ───────────────────────────────────────────────────────

# 감사보고서 등에서 개별 재무제표 <title> 없이 테이블만 나열된 경우
# 내용 기반으로 CF 테이블을 판별하기 위한 마커 (첫 10행 라벨 검사)
_CF_CONTENT_MARKERS = ("영업활동으로인한현금흐름", "영업활동현금흐름")

# 주석에 있는 "영업으로부터 창출된 현금" (간접법 산출 내역) 표를 식별하는
# 합계성 라벨 마커들. 회사·연도별 변형이 많아 substring으로 느슨하게 잡는다.
#   - "차감전순이익": "법인세차감전순이익" / "법인세비용차감전순이익" 모두 매치
#   - "당기순이익조정": "당기순이익조정을위한가감"
#   - "영업으로부터창출된현금"
_CF_ADJUSTMENT_TOTAL_MARKERS = (
    "차감전순이익",
    "당기순이익조정",
    "영업으로부터창출된현금",
)


def _table_looks_like_cf(rows: list[list[str]]) -> bool:
    """테이블 내용이 현금흐름표인지 판별한다 (title 없이 content 기반)."""
    for row in rows[:10]:
        if not row:
            continue
        label = row[0].replace(" ", "").strip()
        if any(marker in label for marker in _CF_CONTENT_MARKERS):
            return True
    return False


def _table_looks_like_cf_adjustment(rows: list[list[str]]) -> bool:
    """
    주석에 있는 '영업으로부터 창출된 현금'(간접법 산출 내역) 표를 내용으로 판별한다.

    이 표는 보고서마다 <TITLE>로 잡힐 때도 있고(예: 2025년 현대비앤지스틸의
    "34. 영업으로부터 창출된 현금 (연결)"), 단순히 <P> 본문에 텍스트로 적힌 뒤
    표만 이어지는 경우도 있다(예: 같은 회사 2022년 "35. 영업으로부터 ..."). 두
    경우 모두 행 라벨로 식별해 CF 후보에 넣고, Stage 1의 감가상각 행 매칭을 그대로
    재사용한다.

    식별 기준 (false positive 최소화):
      (a) '차감전순이익' / '당기순이익조정' / '영업으로부터창출된현금' 같은
          CF-조정 합계성 라벨이 한 행 이상 등장하고,
      (b) 같은 표 안에 '감가상각'을 포함한 행이 있어야 한다.
    자산 변동표·판관비·비용성격별 등에는 (a) 마커가 등장하지 않아 걸러진다.
    """
    has_total_marker = False
    has_depr_row = False
    for row in rows[:25]:
        if not row:
            continue
        label = row[0].replace(" ", "").strip()
        if not has_total_marker and any(m in label for m in _CF_ADJUSTMENT_TOTAL_MARKERS):
            has_total_marker = True
        if not has_depr_row and "감가상각" in label:
            has_depr_row = True
        if has_total_marker and has_depr_row:
            return True
    return False


def _find_cf_tables_by_fs_type(
    soup: BeautifulSoup,
    fs_div: str = "CFS",
    strict_scope: bool = True,
    debug_trace: Optional[list[str]] = None,
) -> list[tuple[Tag, list[list[str]]]]:
    """
    사업보고서에서 연결/별도 구분에 맞는 현금흐름표 테이블을 반환한다.

    DART 사업보고서는 '연결 현금흐름표'와 '현금흐름표' 가 모두 포함되어 있다.
    fs_div가 CFS이면 '연결' 이 포함된 CF를, OFS이면 '연결' 이 없는 CF를 선택한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS" 또는 "OFS"

    Returns:
        (table_tag, rows) 튜플 리스트. tag는 단위 감지에 사용된다.
    """
    want_consol = (fs_div == "CFS")
    document_scope = _infer_document_scope(soup)
    matched_tables: list[tuple[Tag, list[list[str]]]] = []
    all_cf_tables: list[tuple[Tag, list[list[str]]]] = []

    current_scope: Optional[bool] = None
    # 주석 루트("3. 연결재무제표 주석" 등)에서 추출한 연결/별도 스코프.
    # 주석 안에 흩어진 CF-조정 표(영업으로부터 창출된 현금)에 적용한다.
    notes_scope: Optional[bool] = None
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
                # 주석 루트 자체에서 연결/별도 스코프를 추출해 두면, 그 안의
                # CF-조정 표가 어느 스코프인지 결정할 수 있다.
                if "연결" in title_core:
                    notes_scope = True
                elif "별도" in title_core:
                    notes_scope = False
                else:
                    notes_scope = document_scope if document_scope is not None else False
                if debug_trace is not None:
                    debug_trace.append(
                        f"[CF] notes root encountered: raw={raw!r}, in_notes=True, "
                        f"notes_scope={notes_scope}"
                    )
                continue

            # 재무제표 본문 루트 진입 → 주석 종료
            if title_core in _FS_STMT_ROOT_CORES:
                in_notes = False
                notes_scope = None
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

        elif tag.name == "table":
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue

            # 어떤 경로로 이 표를 CF 후보로 채택했는지 분기:
            #   (a) title 기반 CF 섹션
            #   (b) title 없는 본문에서 content-based CF 감지
            #   (c) 주석 안에 있는 CF-조정 표(영업으로부터 창출된 현금) — 명세 #7/실패누적
            accepted = False
            table_scope: Optional[bool] = None

            if is_cf_section:
                accepted = True
                table_scope = current_scope
            elif not in_notes and _table_looks_like_cf(rows):
                accepted = True
                table_scope = current_scope
                if debug_trace is not None:
                    labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[CF] content-based CF 감지 (title 없음): labels={labels}"
                    )
            elif in_notes and _table_looks_like_cf_adjustment(rows):
                accepted = True
                table_scope = notes_scope
                if debug_trace is not None:
                    labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[CF] notes CF-조정 감지: scope={notes_scope}, labels={labels}"
                    )

            if accepted:
                all_cf_tables.append((tag, rows))
                if debug_trace is not None:
                    labels = ", ".join(row[0].strip() for row in rows[:5] if row)
                    debug_trace.append(
                        f"[CF] table seen scope={table_scope}: labels={labels}"
                    )

                if table_scope is None or want_consol == table_scope:
                    matched_tables.append((tag, rows))

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

    각 CF 테이블의 단위(원/천원/백만원/억원)를 캡션·헤더에서 감지하여
    추출 값을 원 단위로 환산한다. 단위 감지 실패 시 원(=1)로 가정한다.

    fs_div에 따라 연결/별도 현금흐름표를 구분하여 올바른 테이블에서 추출한다.

    Args:
        soup:   _parse_dart_xml() 반환값
        fs_div: "CFS"=연결, "OFS"=별도

    Returns:
        {"감가상각비": float|None, "사용권자산상각비": float|None,
         "무형자산상각비": float|None, "combined": bool}
    """
    cf_tables = _find_cf_tables_by_fs_type(soup, fs_div, strict_scope=strict_scope, debug_trace=debug_trace)
    if debug_trace is not None:
        debug_trace.append(
            f"[CF] 후보 테이블 {len(cf_tables)}개 (fs_div={fs_div}, strict_scope={strict_scope})"
        )

    result: dict[str, Optional[float]] = {
        "감가상각비": None,
        "사용권자산상각비": None,
        "무형자산상각비": None,
    }
    combined = False
    combined_label: Optional[str] = None
    # 일반 감가상각을 합산한 출처 테이블(중복 방지: 다른 테이블 행은 합산하지 않음)
    depr_src_idx: Optional[int] = None

    for idx, (table_tag, rows) in enumerate(cf_tables, start=1):
        # 감가상각 키워드가 포함된 CF 테이블만 대상
        full_text = " ".join(" ".join(r) for r in rows)
        if "감가상각" not in full_text:
            continue
        if debug_trace is not None:
            labels = ", ".join(row[0].strip() for row in rows[:5] if row)
            debug_trace.append(f"[CF] 감가상각 키워드 포함 테이블 #{idx}: {labels}")

        # 테이블 단위 감지 (캡션 → 부모 → 인라인 헤더 순) 후 rows 폴백.
        # 단위를 곱해 모든 결과를 원(KRW) 기준으로 통일한다.
        unit = _detect_unit_multiplier(table_tag)
        if unit == 1:
            unit = _detect_unit_from_rows(rows, fallback=1)
        if debug_trace is not None:
            debug_trace.append(
                f"[CF] 테이블 #{idx}: 단위={_unit_label(unit)} (x{unit})"
            )

        # ── 당기 컬럼 인덱스 탐지 (당기/제N기/연도) ──
        current_col = _detect_current_column(rows)

        if debug_trace is not None:
            debug_trace.append(f"[CF] 테이블 #{idx}: 당기 컬럼 인덱스={current_col}")

        def _get_current_val(row: list[str]) -> tuple[Optional[float], Optional[int], Optional[str]]:
            """행에서 당기 금액과 그 출처 컬럼/원본 셀을 반환한다(단위 보정 포함)."""
            if current_col is not None and current_col < len(row):
                raw = row[current_col]
                val = _parse_number(raw)
                if val is not None:
                    return val * unit, current_col, raw
            # 당기 컬럼 미판별 시 폴백: 첫 번째 숫자
            for ci, cell in enumerate(row[1:], start=1):
                val = _parse_number(cell)
                if val is not None:
                    return val * unit, ci, cell
            return None, None, None

        for row_idx, row in enumerate(rows):
            if not row:
                continue
            # 라벨을 정규화하되, CF 본문에서는 substring으로 관대하게 매칭한다
            # ("가. 감가상각비", "감가상각비용", "감가상각비등" 등 접두/접미 변형 모두 허용).
            label = _normalize_row_label(row[0])

            # 1) 합산 행 ("감가상각...무형자산상각..." 한 줄로 기재) — 가장 먼저 검사
            if "감가상각" in label and "무형자산상각" in label and "누계" not in label:
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
                    combined_label = _clean_display_label(row[0])
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} combined 매칭: label='{label}', col={col}, "
                            f"raw='{raw}' → {val}"
                        )
                continue

            # 2) 사용권자산상각 (감가상각보다 먼저 — "사용권자산감가상각비"와 겹치므로)
            if "사용권자산상각" in label or "사용권자산감가상각" in label:
                if "누계" in label:
                    continue
                if result["사용권자산상각비"] is None:
                    val, col, raw = _get_current_val(row)
                    if val is not None:
                        result["사용권자산상각비"] = val
                        if debug_trace is not None:
                            debug_trace.append(
                                f"[CF] #{idx}/R{row_idx+1} 사용권자산상각 매칭: label='{label}', "
                                f"col={col}, raw='{raw}' → {val}"
                            )
                elif debug_trace is not None:
                    debug_trace.append(
                        f"[CF] #{idx}/R{row_idx+1} '사용권자산상각' 재발견, 첫 값 유지 → 스킵"
                    )
                continue

            # 3) 무형자산상각 (감가상각보다 먼저)
            #    '무형자산감가상각비'(에실로코리아 등)도 무형자산상각으로 잡아야
            #    아래 4) 감가상각 합산에 오합산되지 않는다.
            if _is_intangible_amortization_label(label):
                if "누계" in label:
                    continue
                if result["무형자산상각비"] is None:
                    val, col, raw = _get_current_val(row)
                    if val is not None:
                        result["무형자산상각비"] = val
                        if debug_trace is not None:
                            debug_trace.append(
                                f"[CF] #{idx}/R{row_idx+1} 무형자산상각 매칭: label='{label}', "
                                f"col={col}, raw='{raw}' → {val}"
                            )
                elif debug_trace is not None:
                    debug_trace.append(
                        f"[CF] #{idx}/R{row_idx+1} '무형자산상각' 재발견, 첫 값 유지 → 스킵"
                    )
                continue

            # 4) 감가상각 (가장 마지막 — 위 3개와 겹치지 않은 행만)
            #    한 테이블에 '감가상각비'·'투자부동산 감가상각비'처럼 여러 행이
            #    분리 기재되면 모두 합산한다(사용권/무형은 위에서 이미 분리됨).
            #    "투자부동산상각비"(2022 현대비앤지스틸 등)는 라벨에 '감가'가 빠져
            #    있어 일반 substring 매칭으로는 잡히지 않으므로 보조 토큰으로 본다.
            if "감가상각" in label or "투자부동산상각" in label:
                if "누계" in label:
                    continue
                if combined:
                    # 합산 공시 케이스에서는 별도 일반 감가상각 행을 더하지 않는다.
                    continue
                if depr_src_idx is not None and depr_src_idx != idx:
                    # 다른 CF 테이블(연결/별도 중복 등)의 감가상각은 합산하지 않음
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} '감가상각' 다른 출처 테이블 → 스킵"
                        )
                    continue
                val, col, raw = _get_current_val(row)
                if val is not None:
                    result["감가상각비"] = (result["감가상각비"] or 0.0) + val
                    depr_src_idx = idx
                    if debug_trace is not None:
                        debug_trace.append(
                            f"[CF] #{idx}/R{row_idx+1} 감가상각 합산: label='{label}', "
                            f"col={col}, raw='{raw}' → += {val} (누적 {result['감가상각비']})"
                        )

    result["combined"] = combined
    result["combined_label"] = combined_label
    if debug_trace is not None:
        debug_trace.append(
            f"[CF] Python 추출 결과: 감가상각비={result.get('감가상각비')}, "
            f"사용권자산상각비={result.get('사용권자산상각비')}, "
            f"무형자산상각비={result.get('무형자산상각비')}, combined={combined}"
        )
    return result


# ── 공개 API ──────────────────────────────────────────────────────────────────

def extract_depreciation(
    rcept_no: str,
    fs_div: str = "CFS",
    strict_scope: bool = True,
    log_fn=None,
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
            "items": {"감가상각비": float|None, "사용권자산상각비": float|None,
                      "무형자산상각비": float|None},
            "source": "cf" | "cf+notes" | "notes" | "error",
            "error": str|None,
            "tables_found": int,
            "ai_selection": dict|None,
            "combined": bool,
            "trace": list[str],
        }
    """
    base = {
        "items":         {"감가상각비": None, "사용권자산상각비": None, "무형자산상각비": None},
        "item_details":  empty_item_details(_DEPR_ITEM_NAMES),
        "source":        "error",
        "error":         None,
        "tables_found":  0,
        "ai_selection":  None,
        "combined":      False,
        "combined_label": None,
        "trace":         [],
    }
    trace: list[str] = [
        f"[START] rcept_no={rcept_no}, fs_div={fs_div}, strict_scope={strict_scope}",
    ]

    # 1. XML 다운로드 및 파싱
    #    사업보고서 ZIP은 섹션별 XML이 분리되어 있을 수 있으므로,
    #    가장 큰 파일에서 못 찾으면 나머지 파일도 병합하여 재시도한다.
    try:
        all_docs = _download_all_dart_documents(rcept_no)
        if not all_docs:
            trace.append("[LOAD] XML 다운로드 실패")
            return {**base, "error": "XML 다운로드 실패", "trace": trace}
        soup = _parse_dart_xml(all_docs[0])  # 가장 큰 파일 먼저
        if soup is None:
            trace.append("[LOAD] XML 파싱 실패")
            return {**base, "error": "XML 파싱 실패", "trace": trace}
        trace.append(
            f"[LOAD] XML 파싱 성공 (파일 {len(all_docs)}개 중 최대), "
            f"document_scope={_infer_document_scope(soup)}"
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
    cf_combined_label = cf_result.pop("combined_label", None)
    trace.append(f"[CF] combined={cf_combined}")

    # CF에서 감가상각비 + 무형자산상각비(또는 합산)까지 잡혔으면 즉시 반환.
    # 사용권자산상각비는 분리 기재된 회사에만 있으므로 필수 조건이 아니다 (CF에서 잡혔으면 따라온다).
    if cf_result.get("감가상각비") is not None and (cf_result.get("무형자산상각비") is not None or cf_combined):
        trace.append("[FINAL] CF 결과만으로 종료")
        return {
            **base,
            "items":    {k: v for k, v in cf_result.items() if k != "combined"},
            "item_details": _build_depreciation_item_details(
                cf_result,
                rcept_no=rcept_no,
                fs_div=fs_div,
                source="cf",
                combined=cf_combined,
            ),
            "source":   "cf",
            "combined": cf_combined,
            "combined_label": cf_combined_label,
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
            candidate_type = tbl.get("candidate_type") or "unknown"
            risk_flags = tbl.get("risk_flags") or []
            marks = []
            if signals.get("has_separate_pair"):
                marks.append("SEPARATE_PAIR")
            elif signals.get("has_exact_depr"):
                marks.append("depr")
            if signals.get("has_combined_row"):
                marks.append("COMBINED")
            mark_str = f"[{'/'.join(marks)}] " if marks else ""
            risk_str = f" risk={risk_flags}" if risk_flags else ""
            labels = ", ".join(row[0].strip() for row in tbl.get("rows", [])[:5] if row)
            trace.append(
                f"[NOTES] T{idx} {mark_str}type={candidate_type}{risk_str} "
                f"title={title} / labels={labels}"
            )
    except Exception as e:
        tables = []
        base["error"] = f"테이블 수집 실패: {e}"
        trace.append(f"[NOTES] 테이블 수집 실패: {e}")

    base["tables_found"] = len(tables)

    if not tables:
        trace.append("[NOTES] 후보 테이블이 없어 주석 추출 생략")
        # CF에서 부분적으로 찾은 것이 있으면 반환
        if any(cf_result.get(k) is not None for k in ("감가상각비", "사용권자산상각비", "무형자산상각비")):
            trace.append("[FINAL] CF 부분 결과 반환")
            return {
                **base,
                "items":    {k: v for k, v in cf_result.items() if k != "combined"},
                "item_details": _build_depreciation_item_details(
                    cf_result,
                    rcept_no=rcept_no,
                    fs_div=fs_div,
                    source="cf",
                    combined=cf_combined,
                ),
                "source":   "cf",
                "combined": cf_combined,
                "combined_label": cf_combined_label,
                "trace":    trace,
            }

        # 사업보고서 ZIP에 섹션별 XML이 분리되어 있을 수 있으므로
        # 나머지 파일도 병합하여 재시도
        if len(all_docs) > 1:
            trace.append(f"[RETRY] 최대 파일에서 결과 없음 — 나머지 {len(all_docs)-1}개 파일 병합 재시도")
            combined = b"<html><body>" + b"".join(all_docs) + b"</body></html>"
            soup_all = _parse_dart_xml(combined)
            if soup_all is not None:
                try:
                    cf_result2 = _extract_from_cf(soup_all, fs_div=fs_div, strict_scope=strict_scope, debug_trace=trace)
                except Exception:
                    cf_result2 = {"감가상각비": None, "무형자산상각비": None, "combined": False}
                cf_combined2 = cf_result2.pop("combined", False)
                cf_combined_label2 = cf_result2.pop("combined_label", None)
                if cf_result2.get("감가상각비") is not None and (cf_result2.get("무형자산상각비") is not None or cf_combined2):
                    trace.append("[FINAL] 병합 파일 CF에서 추출 완료")
                    return {
                        **base,
                        "items":    {k: v for k, v in cf_result2.items() if k != "combined"},
                        "item_details": _build_depreciation_item_details(
                            cf_result2,
                            rcept_no=rcept_no,
                            fs_div=fs_div,
                            source="cf",
                            combined=cf_combined2,
                        ),
                        "source":   "cf",
                        "combined": cf_combined2,
                        "combined_label": cf_combined_label2,
                        "trace":    trace,
                    }
                try:
                    tables = _collect_depreciation_tables(
                        soup_all, fs_div=fs_div, strict_scope=strict_scope, debug_trace=trace,
                    )
                    trace.append(f"[RETRY] 병합 파일에서 후보 테이블 {len(tables)}개")
                    base["tables_found"] = len(tables)
                except Exception as e:
                    tables = []
                    trace.append(f"[RETRY] 병합 파일 테이블 수집 실패: {e}")
                # tables가 채워졌으면 아래 AI 선택 단계로 계속 진행

        if not tables:
            trace.append("[FINAL] 추출 결과 없음")
            return {**base, "trace": trace}

    # Step B: AI 테이블/행 선택
    ai_selection = None
    notes_result: dict[str, Optional[float]] = {
        "감가상각비": None,
        "사용권자산상각비": None,
        "무형자산상각비": None,
        "combined": False,
    }

    try:
        ai_selection = _ai_select_depreciation(tables, log_fn=log_fn)
        base["ai_selection"] = ai_selection
        trace.append(
            f"[NOTES] AI 선택 결과: depreciation={ai_selection.get('depreciation')}, "
            f"rou_amortization={ai_selection.get('rou_amortization')}, "
            f"amortization={ai_selection.get('amortization')}, combined={ai_selection.get('combined')}"
        )

        # Step C: Python이 AI 지목 위치에서 값 검증 추출
        notes_result = _verify_ai_selection(tables, ai_selection, debug_trace=trace)
        trace.append(
            f"[NOTES] 검증 추출 결과: 감가상각비={notes_result.get('감가상각비')}, "
            f"사용권자산상각비={notes_result.get('사용권자산상각비')}, "
            f"무형자산상각비={notes_result.get('무형자산상각비')}, combined={notes_result.get('combined')}"
        )
    except Exception as e:
        base["error"] = f"AI 선택 실패: {e}"
        trace.append(f"[NOTES] AI 선택 실패: {e}")

    # Step D: CF + Notes 병합 (CF 우선)
    notes_combined = notes_result.get("combined", False)
    final: dict[str, Optional[float]] = {}
    notes_provenance: dict[str, bool] = {}
    for key in ("감가상각비", "사용권자산상각비", "무형자산상각비"):
        cf_val = cf_result.get(key)
        notes_val = notes_result.get(key)
        final[key] = cf_val if cf_val is not None else notes_val
        # 최종값이 notes 단계에서 나왔는지(CF가 덮어쓰지 않은 경우) 표시 — risk_flags 적용 판단용.
        notes_provenance[key] = cf_val is None and notes_val is not None

    # AI가 선택한 표의 candidate_type/risk_flags를 notes 출처 항목에만 전파한다(명세 #7).
    risk_flags_by_item: dict[str, list[str]] = {}
    selected_candidate_type_by_item: dict[str, str] = {}
    if ai_selection:
        _SEL_KEYS = {
            "감가상각비": "depreciation",
            "사용권자산상각비": "rou_amortization",
            "무형자산상각비": "amortization",
        }
        for item_name, sel_key in _SEL_KEYS.items():
            if not notes_provenance.get(item_name):
                continue
            sel = ai_selection.get(sel_key)
            if not isinstance(sel, dict):
                continue
            t_id = sel.get("table_id")
            if not t_id:
                continue
            for idx, tbl in enumerate(tables, start=1):
                if f"T{idx}" != t_id:
                    continue
                cand_type = tbl.get("candidate_type") or "unknown"
                selected_candidate_type_by_item[item_name] = cand_type
                tbl_flags = list(tbl.get("risk_flags") or [])
                if cand_type == "unknown":
                    tbl_flags.append("candidate_type_unknown")
                if tbl_flags:
                    risk_flags_by_item[item_name] = tbl_flags
                    trace.append(
                        f"[NOTES] 선택된 표 위험 플래그 전파: {item_name} ← T{idx} "
                        f"(type={cand_type}, flags={tbl_flags})"
                    )
                break

    is_combined = cf_combined or notes_combined
    # CF가 합산을 잡았으면 CF 라벨, 아니면 notes 라벨을 비고용으로 사용한다.
    combined_label = cf_combined_label if cf_combined else notes_result.get("combined_label")

    # source 결정
    _track_keys = ("감가상각비", "사용권자산상각비", "무형자산상각비")
    cf_found = any(cf_result.get(k) is not None for k in _track_keys)
    notes_found = any(notes_result.get(k) is not None for k in _track_keys)
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
        f"감가상각비={final.get('감가상각비')}, "
        f"사용권자산상각비={final.get('사용권자산상각비')}, "
        f"무형자산상각비={final.get('무형자산상각비')}"
    )

    return {
        **base,
        "items":    final,
        "item_details": _build_depreciation_item_details(
            final,
            rcept_no=rcept_no,
            fs_div=fs_div,
            source=source,
            combined=is_combined,
            risk_flags_by_item=risk_flags_by_item or None,
            selected_candidate_type_by_item=selected_candidate_type_by_item or None,
        ),
        "source":   source,
        "combined": is_combined,
        "combined_label": combined_label,
        "trace":    trace,
    }
