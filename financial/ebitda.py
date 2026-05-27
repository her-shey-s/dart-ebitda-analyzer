"""
financial/ebitda.py
EBITDA 파생 계산 — 단일 소스(single source of truth)

EBITDA = 영업이익 + 감가상각비 + 사용권자산상각비 + 무형자산상각비

UI(app.py), 엑셀 출력, 회귀 테스트 하네스가 모두 이 함수를 공유한다.
streamlit 의존성이 없으므로 단위 테스트에서 그대로 import할 수 있다.
"""

from __future__ import annotations

from typing import Optional

# EBITDA 구성요소 항목명 (config.FINANCIAL_ITEMS의 name과 일치)
EBITDA_OPERATING_INCOME = "영업이익"
EBITDA_ADDBACKS = ("감가상각비", "사용권자산상각비", "무형자산상각비")


def compute_ebitda(items: dict) -> Optional[float]:
    """EBITDA = 영업이익 + 감가상각비 + 사용권자산상각비 + 무형자산상각비.

    영업이익이 없으면 EBITDA를 계산할 수 없으므로 None을 반환한다.
    상각비 구성요소는 합산 공시 등으로 None일 수 있으며, 그 경우 0으로 간주해
    더하지 않는다(중복·과대계산 방지).
    """
    op = items.get(EBITDA_OPERATING_INCOME)
    if op is None:
        return None
    total = float(op)
    for key in EBITDA_ADDBACKS:
        v = items.get(key)
        if v is not None:
            total += float(v)
    return total
