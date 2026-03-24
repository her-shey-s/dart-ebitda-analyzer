"""
validator/rules.py
회계 규칙 검증 및 교차 검증

3단계 검증 중 1, 2단계를 담당한다 (비용 0원).
  1단계: 회계 항등식 검증 (자산=부채+자본, 매출총이익=매출-원가 등)
  2단계: 경로A에서 주요계정 API와 전체재무제표 API 수치 비교
"""

from typing import Optional

from config import ACCOUNTING_IDENTITIES


# ── 1단계: 회계 항등식 검증 ────────────────────────────────────────────────

def validate_accounting_identities(
    items: dict[str, Optional[float]],
) -> list[dict]:
    """
    config.ACCOUNTING_IDENTITIES에 정의된 항등식을 검증한다.

    자산=부채+자본:    items["총자산"] ≈ items["총부채"] + items["자본총계"]
    매출총이익 검증:   items["매출총이익"] ≈ items["매출액"] - items["매출원가"]

    Args:
        items: 항목명 → 금액 딕셔너리

    Returns:
        이상이 발견된 경우 플래그 딕셔너리 리스트
        각 딕셔너리: {
            "rule":      규칙 설명 문자열,
            "lhs":       좌변 계산값,
            "rhs":       우변 계산값,
            "diff_rate": 차이 비율,
        }
        이상 없으면 빈 리스트.
    """
    flags = []

    # (좌변 항목 리스트, 우변 항목 리스트, 허용 오차)
    identities = [
        (["총자산"],      ["총부채", "자본총계"],    0.01, "자산 = 부채 + 자본"),
        (["매출총이익"],  ["매출액"],                 0.01, "매출총이익 ≈ 매출액 - 매출원가",
         # 매출원가는 빼는 방향
         ["매출원가"]),
    ]

    # 단순 항목: (lhs 합계, rhs 합계) 비교
    # 자산=부채+자본
    flag = _check_sum_identity(
        items, ["총자산"], ["총부채", "자본총계"], 0.01, "자산 = 부채 + 자본"
    )
    if flag:
        flags.append(flag)

    # 매출총이익 = 매출액 - 매출원가
    flag = _check_difference_identity(
        items, "매출총이익", "매출액", "매출원가", 0.01, "매출총이익 = 매출액 - 매출원가"
    )
    if flag:
        flags.append(flag)

    return flags


def _check_sum_identity(
    items: dict,
    lhs_names: list[str],
    rhs_names: list[str],
    tolerance: float,
    rule_desc: str,
) -> Optional[dict]:
    """
    lhs 합계 ≈ rhs 합계 여부를 검사한다.

    Args:
        items:      항목명 → 금액 딕셔너리
        lhs_names:  좌변 항목명 리스트
        rhs_names:  우변 항목명 리스트
        tolerance:  허용 오차 비율 (예: 0.01 = 1%)
        rule_desc:  규칙 설명 문자열

    Returns:
        이상 감지 시 플래그 딕셔너리, 없으면 None
    """
    lhs_vals = [items.get(n) for n in lhs_names]
    rhs_vals = [items.get(n) for n in rhs_names]

    if any(v is None for v in lhs_vals + rhs_vals):
        return None  # 데이터 부족 → 검증 스킵

    lhs = sum(lhs_vals)
    rhs = sum(rhs_vals)
    base = max(abs(lhs), abs(rhs), 1)
    diff_rate = abs(lhs - rhs) / base

    if diff_rate > tolerance:
        return {
            "rule":      rule_desc,
            "lhs":       lhs,
            "rhs":       rhs,
            "diff_rate": diff_rate,
        }
    return None


def _check_difference_identity(
    items: dict,
    result_name: str,
    minuend_name: str,
    subtrahend_name: str,
    tolerance: float,
    rule_desc: str,
) -> Optional[dict]:
    """
    result ≈ minuend - subtrahend 여부를 검사한다.

    Args:
        items:            항목명 → 금액 딕셔너리
        result_name:      결과 항목명 (예: "매출총이익")
        minuend_name:     피감수 항목명 (예: "매출액")
        subtrahend_name:  감수 항목명 (예: "매출원가")
        tolerance:        허용 오차 비율
        rule_desc:        규칙 설명 문자열

    Returns:
        이상 감지 시 플래그 딕셔너리, 없으면 None
    """
    result = items.get(result_name)
    minuend = items.get(minuend_name)
    subtrahend = items.get(subtrahend_name)

    if any(v is None for v in (result, minuend, subtrahend)):
        return None

    expected = minuend - subtrahend
    base = max(abs(result), abs(expected), 1)
    diff_rate = abs(result - expected) / base

    if diff_rate > tolerance:
        return {
            "rule":      rule_desc,
            "lhs":       result,
            "rhs":       expected,
            "diff_rate": diff_rate,
        }
    return None


# ── 2단계: 교차 검증 ────────────────────────────────────────────────────────

def cross_validate(
    full_stmt_items: dict[str, Optional[float]],
    major_account_items: dict[str, Optional[float]],
    tolerance: float = 0.005,
) -> list[dict]:
    """
    전체재무제표 API 결과와 주요계정 API 결과를 비교하여 불일치를 감지한다.

    Args:
        full_stmt_items:    fetch_full_financial_statement() → extract_target_items() 결과
        major_account_items: fetch_major_accounts() 후 동일 방식으로 추출한 결과
        tolerance:          허용 오차 비율 (기본 0.5%)

    Returns:
        불일치 플래그 딕셔너리 리스트
        각 딕셔너리: {
            "item":      항목명,
            "full_stmt": 전체재무제표 값,
            "major_acc": 주요계정 값,
            "diff_rate": 차이 비율,
        }
    """
    flags = []
    for name in full_stmt_items:
        v1 = full_stmt_items.get(name)
        v2 = major_account_items.get(name)

        if v1 is None or v2 is None:
            continue

        base = max(abs(v1), abs(v2), 1)
        diff_rate = abs(v1 - v2) / base

        if diff_rate > tolerance:
            flags.append({
                "item":      name,
                "full_stmt": v1,
                "major_acc": v2,
                "diff_rate": diff_rate,
            })

    return flags


def summarize_validation(
    identity_flags: list[dict],
    cross_flags: list[dict],
) -> dict:
    """
    1, 2단계 검증 결과를 요약하여 반환한다.

    Args:
        identity_flags: validate_accounting_identities() 반환값
        cross_flags:    cross_validate() 반환값

    Returns:
        {
            "passed":        bool,  # True이면 AI 검증 불필요
            "needs_ai":      bool,
            "all_flags":     [플래그 설명 문자열 리스트],
            "identity_flags": ...,
            "cross_flags":   ...,
        }
    """
    all_flags: list[str] = []

    for f in identity_flags:
        all_flags.append(
            f"[항등식] {f['rule']}: 좌변={f['lhs']:,.0f}, 우변={f['rhs']:,.0f}, "
            f"차이={f['diff_rate']*100:.2f}%"
        )

    for f in cross_flags:
        all_flags.append(
            f"[교차] {f['item']}: 전체재무={f['full_stmt']:,.0f}, 주요계정={f['major_acc']:,.0f}, "
            f"차이={f['diff_rate']*100:.2f}%"
        )

    passed = len(all_flags) == 0
    return {
        "passed":         passed,
        "needs_ai":       not passed,
        "all_flags":      all_flags,
        "identity_flags": identity_flags,
        "cross_flags":    cross_flags,
    }
