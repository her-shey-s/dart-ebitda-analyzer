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

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from dart_api.html_parser import (
    _build_section_table_map,
    _download_dart_document,
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

    Args:
        rows:            2D 테이블 행 리스트
        unit_multiplier: 단위 승수 (1000 = 천원)

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None}
    """
    result: dict[str, Optional[float]] = {}

    # 당기 컬럼 인덱스 찾기 (보통 "당기"가 포함된 컬럼)
    current_col = 1  # 기본값: 두 번째 컬럼

    if rows and len(rows[0]) > 1:
        for ci, cell in enumerate(rows[0]):
            if "당기" in cell and "전기" not in cell:
                current_col = ci
                break

    for row in rows:
        if not row:
            continue
        label = row[0].replace(" ", "").strip()

        # 감가상각비 매칭 (단, "사용권자산의 감가상각비", "감가상각누계액" 등 제외)
        if "감가상각비" in label and "누계" not in label:
            # "사용권자산의감가상각비" → 부분 감가상각이므로 별도 처리하지 않음
            # "감가상각비" 정확 매칭 우선
            if label in ("감가상각비", "감가상각비용"):
                val = _parse_number(row[current_col]) if current_col < len(row) else None
                if val is not None:
                    result["감가상각비"] = val * unit_multiplier

        # 무형자산상각비 매칭
        if "무형자산상각비" in label or "무형자산상각" in label:
            if "누계" not in label:
                val = _parse_number(row[current_col]) if current_col < len(row) else None
                if val is not None:
                    result["무형자산상각비"] = val * unit_multiplier

    return result


# ── 주석 테이블 수집 ──────────────────────────────────────────────────────────

def _collect_depreciation_tables(soup: BeautifulSoup) -> list[dict]:
    """
    주석 섹션에서 감가상각 관련 테이블을 수집한다.

    각 테이블에 대해:
      - 섹션 제목
      - 단위 승수
      - 파싱된 행 데이터
      - AI용 텍스트
    를 포함하는 딕셔너리 리스트를 반환한다.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        [{"title": str, "unit": int, "rows": [[str]], "text": str}, ...]
    """
    # 주석 섹션 시작 위치 찾기
    in_notes = False
    tables: list[dict] = []

    section_key_map = {
        "재무상태표": False,
        "손익계산서": False,
        "포괄손익계산서": False,
        "자본변동표": False,
        "현금흐름표": False,
        "주석": True,
    }

    for tag in soup.descendants:
        if tag.name == "title":
            raw = tag.get_text(strip=True)
            # 정규화: 공백/특수문자 제거
            norm = re.sub(r"[\s\u3000]+", "", raw)
            for section_name, is_notes in section_key_map.items():
                if section_name in norm:
                    in_notes = is_notes
                    break

        elif tag.name == "table" and in_notes:
            rows = _xml_table_to_rows(tag)
            if not rows:
                continue

            # 감가상각 키워드 포함 여부 확인
            full_text = " ".join(" ".join(r) for r in rows)
            if not any(kw in full_text for kw in _DEPRECIATION_KEYWORDS):
                continue

            # 제외 대상 필터링
            if _has_exclude_keywords(rows):
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

            tables.append({
                "title":  title,
                "unit":   unit,
                "rows":   rows,
                "text":   "\n".join(text_lines),
                "tag":    tag,  # 원본 태그 (디버깅용)
            })

    return tables


# ── Python 추출 ───────────────────────────────────────────────────────────────

def _python_extract_depreciation(tables: list[dict]) -> dict[str, Optional[float]]:
    """
    Python으로 감가상각비·무형자산상각비를 추출한다.

    당기/전기 구조 테이블에서 값을 추출하고,
    여러 테이블에서 발견되면 최대값을 선택한다.
    (전체 비용 기준 값이 부분 값보다 항상 크거나 같으므로)

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None}
    """
    all_depr: list[float] = []
    all_amort: list[float] = []

    for tbl in tables:
        rows = tbl["rows"]
        unit = tbl["unit"]

        # 당기/전기 구조 테이블만 대상
        if not _is_period_table(rows):
            continue

        extracted = _extract_depreciation_from_rows(rows, unit)

        if extracted.get("감가상각비") is not None:
            all_depr.append(extracted["감가상각비"])
        if extracted.get("무형자산상각비") is not None:
            all_amort.append(extracted["무형자산상각비"])

    return {
        "감가상각비":    max(all_depr) if all_depr else None,
        "무형자산상각비": max(all_amort) if all_amort else None,
    }


# ── AI 추출 ───────────────────────────────────────────────────────────────────

def _build_ai_prompt(tables_text: str) -> str:
    """감가상각비 추출용 AI 프롬프트를 생성한다."""
    return (
        "아래는 한국 기업 감사보고서의 주석(Notes)에서 감가상각 관련 테이블을 발췌한 것이다.\n"
        "EBITDA 계산에 필요한 **전체 비용 기준(회사 전체)** 감가상각비와 무형자산상각비를 추출해라.\n\n"
        "## 주의사항\n"
        "- '비용의 성격별 분류' 또는 '판매비와관리비+매출원가 합산' 기준의 전체 감가상각비를 찾아라.\n"
        "- '현금흐름표 조정항목'의 감가상각비도 전체 기준이므로 교차검증에 활용해라.\n"
        "- 특정 자산(유형자산, 투자부동산, 사용권자산)만의 감가상각은 부분값이므로 선택하지 마라.\n"
        "- 감가상각누계액(누적값)은 당기 비용이 아니므로 선택하지 마라.\n"
        "- 이연법인세 관련 감가상각비는 완전히 다른 맥락이므로 선택하지 마라.\n"
        "- 각 테이블 앞에 표시된 '(단위: XXX)'를 반드시 확인하고, 최종 답은 **원(KRW) 단위**로 변환해라.\n"
        "- 찾을 수 없으면 null로 표시해라.\n\n"
        "JSON으로만 응답해라:\n"
        '{"감가상각비": 숫자또는null, "무형자산상각비": 숫자또는null}\n\n'
        f"주석 테이블:\n---\n{tables_text}\n---"
    )


def _ai_extract_depreciation(tables: list[dict]) -> dict[str, Optional[float]]:
    """
    AI(Gemini)로 감가상각비·무형자산상각비를 추출한다.

    Args:
        tables: _collect_depreciation_tables() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None}

    Raises:
        RuntimeError: Gemini API 호출 또는 응답 파싱 실패
    """
    from config import GEMINI_API_KEY
    from gemini_parser import _get_client, _generate, _parse_json

    client = _get_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY 미설정")

    # 테이블 텍스트 병합 (토큰 절약: 8000자 제한)
    combined = "\n\n".join(tbl["text"] for tbl in tables)
    if len(combined) > 8000:
        combined = combined[:8000]

    prompt = _build_ai_prompt(combined)
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

def _extract_from_cf(soup: BeautifulSoup) -> dict[str, Optional[float]]:
    """
    현금흐름표(CF) 섹션에서 감가상각비·무형자산상각비를 추출한다.

    현금흐름표의 "영업활동" 조정 항목에는 전체 비용 기준 감가상각비가
    별도 행으로 기재되므로, 주석보다 신뢰도가 높다.
    단위는 재무제표와 동일(원)이므로 보정이 불필요하다.

    Args:
        soup: _parse_dart_xml() 반환값

    Returns:
        {"감가상각비": float|None, "무형자산상각비": float|None}
    """
    section_map = _build_section_table_map(soup)
    cf_tables = section_map.get("CF", [])

    result: dict[str, Optional[float]] = {"감가상각비": None, "무형자산상각비": None}

    for rows in cf_tables:
        # 감가상각 키워드가 포함된 CF 테이블만 대상
        full_text = " ".join(" ".join(r) for r in rows)
        if "감가상각" not in full_text:
            continue

        for row in rows:
            if not row:
                continue
            label = row[0].replace(" ", "").strip()

            # 감가상각비: 정확 매칭 ("감가상각비" == label)
            if label == "감가상각비" or label == "감가상각비용":
                # CF 테이블은 보통 4~5열: 항목 | 당기금액 | 당기소계 | 전기금액 | 전기소계
                # 첫 번째 숫자 컬럼(당기)을 가져온다
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

    return result


# ── 공개 API ──────────────────────────────────────────────────────────────────

def extract_depreciation(rcept_no: str) -> dict:
    """
    DART 보고서에서 감가상각비·무형자산상각비를 추출한다.

    모듈A와 완전 독립적으로 동작한다.
    실패 시 items 값이 None으로 설정되며, 예외를 발생시키지 않는다.

    Args:
        rcept_no: DART 접수번호

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
    }

    # 1. XML 다운로드 및 파싱
    try:
        xml_bytes = _download_dart_document(rcept_no)
        if xml_bytes is None:
            return {**base, "error": "XML 다운로드 실패"}
        soup = _parse_dart_xml(xml_bytes)
        if soup is None:
            return {**base, "error": "XML 파싱 실패"}
    except Exception as e:
        return {**base, "error": f"문서 로드 실패: {e}"}

    # 2. [1차] 현금흐름표(CF)에서 추출 시도 (AI 불필요, 가장 신뢰도 높음)
    try:
        cf_result = _extract_from_cf(soup)
    except Exception:
        cf_result = {"감가상각비": None, "무형자산상각비": None}

    if cf_result.get("감가상각비") is not None and cf_result.get("무형자산상각비") is not None:
        # CF에서 둘 다 찾음 → 즉시 반환
        return {
            **base,
            "items":        cf_result,
            "source":       "cf",
            "python_result": cf_result,
        }

    # 3. [2차] CF에서 못 찾은 항목 → 주석(NOTES) fallback
    try:
        tables = _collect_depreciation_tables(soup)
    except Exception as e:
        tables = []
        base["error"] = f"테이블 수집 실패: {e}"

    base["tables_found"] = len(tables)

    # 주석 Python 추출
    try:
        notes_python = _python_extract_depreciation(tables) if tables else {"감가상각비": None, "무형자산상각비": None}
    except Exception:
        notes_python = {"감가상각비": None, "무형자산상각비": None}

    # 주석 AI 추출
    notes_ai = None
    if tables:
        try:
            notes_ai = _ai_extract_depreciation(tables)
            base["ai_result"] = notes_ai
        except Exception as e:
            base["error"] = f"AI 추출 실패: {e}"

    # 주석 교차검증
    if notes_ai:
        notes_final = _cross_validate(notes_python, notes_ai)
    else:
        notes_final = notes_python

    # CF 결과와 주석 결과 병합 (CF 우선)
    final: dict[str, Optional[float]] = {}
    for key in ("감가상각비", "무형자산상각비"):
        final[key] = cf_result.get(key) or notes_final.get(key)

    base["python_result"] = {
        "cf": cf_result,
        "notes": notes_python,
    }

    source = "cf" if all(cf_result.get(k) is not None for k in ("감가상각비", "무형자산상각비")) else \
             "cf+notes" if any(cf_result.get(k) is not None for k in ("감가상각비", "무형자산상각비")) else \
             "notes"

    return {
        **base,
        "items":  final,
        "source": source,
    }
