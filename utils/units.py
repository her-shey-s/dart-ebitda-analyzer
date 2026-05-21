"""
utils/units.py
재무제표/주석 테이블 단위 감지·변환 공용 유틸.

DART 공시는 테이블마다 단위(원·천원·백만원·억원)가 자유롭게 사용되며,
값을 원(KRW) 기준으로 정확히 계산하려면 테이블별 단위 승수를 감지해
추출 값에 곱해줘야 한다. 단위를 놓치면 1,000~1억배의 오차가 발생할 수 있다.
"""

import re
from typing import Optional

from bs4 import Tag


UNIT_MULTIPLIERS: dict[str, int] = {
    "원":    1,
    "천원":  1_000,
    "백만원": 1_000_000,
    "억원":  100_000_000,
}

_UNIT_LABEL_BY_MULTIPLIER: dict[int, str] = {
    1:           "원",
    1_000:       "천원",
    1_000_000:   "백만원",
    100_000_000: "억원",
}

# "단위: 백만원", "단위:천원", "(단위: 원)", "금액단위 : 백만원" 등 변형 대응.
# 콜론은 ASCII/전각 모두 허용, 사이 공백/괄호 허용. KRW도 인식(원과 동일).
_UNIT_RE = re.compile(
    r"(?:단위|금액\s*단위)\s*[:：]?\s*[\(\[]?\s*"
    r"(천\s*원|백\s*만\s*원|억\s*원|원|KRW)\s*[\)\]]?",
    re.IGNORECASE,
)
# 보조 패턴: "단위는 백만원이며" 처럼 콜론 없이 자연어로 표기된 케이스.
# '단위'와 단위 키워드 사이를 최대 12자 임의 문자(개행 제외)로 허용.
_UNIT_LOOSE_RE = re.compile(r"단위.{0,12}?(천\s*원|백\s*만\s*원|억\s*원)")


def _normalize_unit_token(token: str) -> Optional[int]:
    """매칭된 단위 토큰을 승수로 변환한다."""
    if not token:
        return None
    norm = re.sub(r"\s+", "", token).upper()
    if norm == "KRW":
        return 1
    return UNIT_MULTIPLIERS.get(norm)


def detect_unit_in_text(text: str) -> Optional[int]:
    """단일 텍스트 블록에서 단위 표기를 찾아 승수를 반환한다."""
    if not text:
        return None
    m = _UNIT_RE.search(text)
    if m:
        mult = _normalize_unit_token(m.group(1))
        if mult is not None:
            return mult
    m = _UNIT_LOOSE_RE.search(text)
    if m:
        return _normalize_unit_token(m.group(1))
    return None


def detect_unit_multiplier(tag: Tag, fallback: int = 1) -> int:
    """
    테이블 태그 주변/내부에서 단위 승수를 감지한다.

    탐색 순서:
      1. 직전 형제 태그 최대 5개 ('(단위: 백만원)' 캡션 패턴)
      2. 부모 노드의 직전 형제 (캡션이 한 단계 위에 있는 경우)
      3. 테이블 내부 첫 5행의 셀 텍스트 (인라인 표기)

    감지 실패 시 ``fallback`` (기본 1=원) 반환.
    """
    node = tag
    for _ in range(5):
        node = node.find_previous_sibling() if node is not None else None
        if node is None:
            break
        text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node)
        unit = detect_unit_in_text(text)
        if unit is not None:
            return unit

    parent = tag.parent
    sib = parent
    for _ in range(3):
        sib = sib.find_previous_sibling() if sib is not None else None
        if sib is None:
            break
        text = sib.get_text(strip=True) if hasattr(sib, "get_text") else str(sib)
        unit = detect_unit_in_text(text)
        if unit is not None:
            return unit

    for i, tr in enumerate(tag.find_all("tr")):
        if i >= 5:
            break
        for cell in tr.find_all(["td", "th", "tu", "te"]):
            unit = detect_unit_in_text(cell.get_text(strip=True))
            if unit is not None:
                return unit

    return fallback


def detect_unit_from_rows(rows: list[list[str]], fallback: int = 1) -> int:
    """
    테이블의 rows에서만(태그 없이) 단위를 감지한다. 머리 5행 탐색.
    HTML fallback 경로 등 BS4 태그 참조가 없는 경우 사용.
    """
    for row in rows[:5]:
        for cell in row:
            unit = detect_unit_in_text(cell)
            if unit is not None:
                return unit
    return fallback


def unit_label(multiplier: int) -> str:
    """승수를 사람이 읽을 수 있는 단위 라벨로 변환한다."""
    return _UNIT_LABEL_BY_MULTIPLIER.get(multiplier, f"{multiplier}원")
