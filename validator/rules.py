"""
validator/rules.py
재무 데이터 검증 — 회계 항등식

회계 항등식 검증 (비용 0원):
    총자산 = 총부채 + 자본총계
    매출총이익 = 매출액 - 매출원가
    영업이익 ≤ 매출총이익 (판관비는 항상 양수)

AI 독립 추출 교차검증은 경로A·B 모두 gemini_parser.py에서 처리한다.

공통 반환 형식:
    {
        "is_valid": bool,
        "checks": [
            {
                "rule":     규칙 설명 문자열,
                "expected": 기대값,
                "actual":   실제값,
                "diff":     차이(절대값),
                "passed":   bool,
                "severity": "critical" | "warning" | "info",
            }, ...
        ],
        "flags": [실패한 checks의 요약 문자열 리스트],
    }
"""

from typing import Optional


# ── 공차(허용 오차) ───────────────────────────────────────────────────────
# 1원 또는 0.01% 중 큰 쪽: 소액 고정 허용 + 대액 비율 허용
_ABS_TOL = 1.0        # 원 단위 절대 허용
_REL_TOL = 0.0001     # 0.01% 상대 허용


def _tolerance(base: float) -> float:
    """base 크기에 맞는 허용 오차를 반환한다."""
    return max(_ABS_TOL, abs(base) * _REL_TOL)


def _passes(diff: float, base: float) -> bool:
    """abs(diff) ≤ 허용 오차이면 True."""
    return abs(diff) <= _tolerance(base)


# ── 1단계: 회계 항등식 검증 ────────────────────────────────────────────────

def _check_balance_sheet_identity(items: dict) -> dict:
    """
    총자산 = 총부채 + 자본총계 검증.

    severity: critical (회계 기초 항등식 위반은 데이터 오류 가능성)
    """
    rule = "회계항등식: 총자산 = 총부채 + 자본총계"
    assets     = items.get("총자산")
    liabilities = items.get("총부채")
    equity     = items.get("자본총계")

    if any(v is None for v in (assets, liabilities, equity)):
        return _skipped(rule, "critical", "필요 항목(총자산·총부채·자본총계) 중 None 존재")

    expected = liabilities + equity
    diff = assets - expected
    passed = _passes(diff, assets)
    return {
        "rule":     rule,
        "expected": expected,
        "actual":   assets,
        "diff":     abs(diff),
        "passed":   passed,
        "severity": "critical",
    }


def _check_gross_profit_identity(items: dict) -> dict:
    """
    매출총이익 = 매출액 - 매출원가 검증.

    severity: critical
    """
    rule = "회계항등식: 매출총이익 = 매출액 - 매출원가"
    gross   = items.get("매출총이익")
    revenue = items.get("매출액")
    cogs    = items.get("매출원가")

    if any(v is None for v in (gross, revenue, cogs)):
        return _skipped(rule, "critical", "필요 항목(매출총이익·매출액·매출원가) 중 None 존재")

    expected = revenue - cogs
    diff = gross - expected
    passed = _passes(diff, gross if gross != 0 else expected)
    return {
        "rule":     rule,
        "expected": expected,
        "actual":   gross,
        "diff":     abs(diff),
        "passed":   passed,
        "severity": "critical",
    }


def _check_operating_income_le_gross_profit(items: dict) -> dict:
    """
    영업이익 ≤ 매출총이익 검증.
    (영업이익 = 매출총이익 - 판관비, 판관비 ≥ 0이어야 함)

    severity: warning (판관비 환입 등 예외 존재)
    """
    rule = "논리검증: 영업이익 ≤ 매출총이익"
    op_income = items.get("영업이익")
    gross     = items.get("매출총이익")

    if any(v is None for v in (op_income, gross)):
        return _skipped(rule, "warning", "필요 항목(영업이익·매출총이익) 중 None 존재")

    passed = op_income <= gross
    diff = max(op_income - gross, 0)   # 위반 시 초과분, 통과 시 0
    return {
        "rule":     rule,
        "expected": f"영업이익({op_income:,.0f}) ≤ 매출총이익({gross:,.0f})",
        "actual":   op_income,
        "diff":     diff,
        "passed":   passed,
        "severity": "warning",
    }


def check_accounting_identities(items: dict[str, Optional[float]]) -> list[dict]:
    """
    1단계: 세 가지 회계 규칙을 모두 검증한다.

    Args:
        items: 항목명 → 금액 딕셔너리 (financial_api / html_parser 반환값)

    Returns:
        check 딕셔너리 리스트 (통과/실패 모두 포함)
    """
    return [
        _check_balance_sheet_identity(items),
        _check_gross_profit_identity(items),
        _check_operating_income_le_gross_profit(items),
    ]


# ── 메인 검증 함수 ────────────────────────────────────────────────────────

def validate(items: dict[str, Optional[float]]) -> dict:
    """
    회계 항등식 검증을 실행하고 통합 결과를 반환한다.

    AI 독립 추출 교차검증은 경로A·B 모두 gemini_parser.py에서 별도 처리한다.

    Args:
        items: 항목명 → 금액 딕셔너리 (필수)

    Returns:
        {
            "is_valid": bool,          # critical 실패 없으면 True
            "checks":   [check, ...],  # 모든 검증 항목 (통과·실패·스킵 포함)
            "flags":    [str, ...],    # 실패한 검증의 요약 메시지 리스트
        }
    """
    checks: list[dict] = []

    # 회계 항등식
    checks.extend(check_accounting_identities(items))

    # 플래그: 실패한 항목의 요약 메시지
    flags = [_check_to_flag(c) for c in checks if not c["passed"]]

    # is_valid: critical 실패가 없으면 True
    is_valid = not any(
        not c["passed"] and c["severity"] == "critical"
        for c in checks
    )

    return {
        "is_valid": is_valid,
        "checks":   checks,
        "flags":    flags,
    }


# ── 내부 유틸 ──────────────────────────────────────────────────────────────

def _skipped(rule: str, severity: str, reason: str) -> dict:
    """데이터 부족으로 스킵된 검증 항목을 반환한다."""
    return {
        "rule":     rule,
        "expected": None,
        "actual":   None,
        "diff":     None,
        "passed":   True,   # 스킵은 통과로 처리 (데이터 부족은 오류가 아님)
        "severity": "info",
        "note":     reason,
    }


def _check_to_flag(check: dict) -> str:
    """실패한 check 딕셔너리를 요약 문자열로 변환한다."""
    rule = check["rule"]
    sev  = check["severity"].upper()
    diff = check.get("diff")

    if diff is not None:
        return f"[{sev}] {rule} | 차이={diff:,.0f}원"
    return f"[{sev}] {rule}"
