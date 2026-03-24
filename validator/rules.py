"""
validator/rules.py
재무 데이터 3단계 검증

1단계 - 회계 항등식 (비용 0원):
    총자산 = 총부채 + 자본총계
    매출총이익 = 매출액 - 매출원가
    영업이익 ≤ 매출총이익 (판관비는 항상 양수)

2단계 - 교차 검증 (비용 0원, 경로A만):
    전체재무제표(items) vs 주요계정 API(cross_check) 수치 비교
    DART가 CFS 요청에 OFS를 반환하는 알려진 혼동 패턴 감지

3단계 - AI 검증 (최소 비용):
    1·2단계에서 critical 플래그가 달린 건만 gemini_parser.py로 연결
    (현재는 시그니처만 정의)

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


# ── 2단계: 교차 검증 ────────────────────────────────────────────────────────

# DART 주요계정 API가 CFS 요청에 OFS 값을 반환하는 알려진 패턴:
# 여러 항목이 일정 비율(40%~80%)로 일관되게 낮으면 CFS/OFS 혼동으로 간주
_CFS_OFS_MIN_ITEMS = 2       # 패턴 판별에 필요한 최소 불일치 항목 수
_CFS_OFS_RATIO_MIN = 0.20   # 불일치 비율이 이 값 이상이면 CFS/OFS 가능
_CFS_OFS_RATIO_MAX = 0.95   # 이 값을 넘으면 너무 달라서 다른 원인 가능


def _is_cfs_ofs_confusion(ratios: list[float]) -> bool:
    """
    cross_check 값들이 items 값 대비 일정 비율 범위에 있으면
    CFS/OFS 혼동으로 판단한다.

    Args:
        ratios: cross_check[i] / items[i] 비율 리스트

    Returns:
        True이면 CFS/OFS 혼동 가능성
    """
    if len(ratios) < _CFS_OFS_MIN_ITEMS:
        return False
    in_range = [_CFS_OFS_RATIO_MIN < r < _CFS_OFS_RATIO_MAX for r in ratios]
    # 과반수가 같은 비율 범위 안에 있으면 혼동 패턴
    return sum(in_range) >= len(ratios) * 0.5


def check_cross_validation(
    items: dict[str, Optional[float]],
    cross_check: dict[str, Optional[float]],
) -> list[dict]:
    """
    2단계: 전체재무제표(items)와 주요계정 API(cross_check) 수치를 비교한다.

    경로A에서만 유효. cross_check가 비어 있으면 빈 리스트를 반환한다.

    불일치 원인 분류:
      - CFS/OFS 혼동: DART가 CFS 요청에 OFS를 반환하는 알려진 버그 → severity=warning
      - 실제 불일치: 원인 불명의 수치 차이 → severity=critical

    Args:
        items:       전체재무제표에서 추출한 항목 딕셔너리
        cross_check: 주요계정 API에서 추출한 항목 딕셔너리

    Returns:
        check 딕셔너리 리스트
    """
    if not cross_check:
        return []

    mismatches = []
    ratios = []

    for name, v1 in items.items():
        v2 = cross_check.get(name)
        if v1 is None or v2 is None or v1 == 0:
            continue

        diff = abs(v1 - v2)
        if _passes(diff, v1):
            continue  # 허용 오차 이내 → 통과

        mismatches.append((name, v1, v2, diff))
        ratios.append(abs(v2) / abs(v1))

    if not mismatches:
        return []

    # 혼동 패턴 여부 결정
    is_confusion = _is_cfs_ofs_confusion(ratios)
    severity = "warning" if is_confusion else "critical"
    cause = "DART API CFS/OFS 혼동 가능성" if is_confusion else "수치 불일치"

    checks = []
    for name, v1, v2, diff in mismatches:
        checks.append({
            "rule":     f"교차검증: {name} ({cause})",
            "expected": v1,
            "actual":   v2,
            "diff":     diff,
            "passed":   False,
            "severity": severity,
        })

    return checks


# ── 3단계: AI 검증 (시그니처만) ─────────────────────────────────────────────

def validate_with_ai(
    validation_result: dict,
    items: dict[str, Optional[float]],
    corp_name: str,
    year: int,
) -> dict:
    """
    3단계: AI(Gemini Flash)로 검증한다.

    1·2단계에서 critical 플래그가 달린 건만 이 단계를 호출한다.
    현재는 시그니처만 정의되어 있으며, gemini_parser.py 연결 시 구현한다.

    Args:
        validation_result: validate() 반환값 (1·2단계 결과)
        items:             항목명 → 금액 딕셔너리
        corp_name:         기업명 (AI 컨텍스트용)
        year:              사업연도

    Returns:
        validate()와 동일한 형식에 "ai_result" 키 추가된 딕셔너리

    Raises:
        NotImplementedError: 미구현 상태
    """
    raise NotImplementedError(
        "3단계 AI 검증은 gemini_parser.py 연결 후 구현 예정입니다."
    )


# ── 메인 검증 함수 ────────────────────────────────────────────────────────

def validate(
    items: dict[str, Optional[float]],
    cross_check: dict[str, Optional[float]] | None = None,
    path: str = "A",
) -> dict:
    """
    1·2단계 검증을 실행하고 통합 결과를 반환한다.

    Args:
        items:       항목명 → 금액 딕셔너리 (필수)
        cross_check: 교차검증용 딕셔너리 (경로A만 전달, 경로B는 None 또는 {})
        path:        "A" (사업보고서) | "B" (감사보고서)

    Returns:
        {
            "is_valid": bool,          # critical 실패 없으면 True
            "checks":   [check, ...],  # 모든 검증 항목 (통과·실패·스킵 포함)
            "flags":    [str, ...],    # 실패한 검증의 요약 메시지 리스트
        }
    """
    checks: list[dict] = []

    # 1단계: 회계 항등식
    checks.extend(check_accounting_identities(items))

    # 2단계: 교차 검증 (경로A + cross_check 있을 때만)
    if path == "A" and cross_check:
        checks.extend(check_cross_validation(items, cross_check))

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


def needs_ai_validation(result: dict) -> bool:
    """
    3단계 AI 검증이 필요한지 판단한다.

    critical 실패가 있거나 warning이 2개 이상이면 AI 검증 권장.

    Args:
        result: validate() 반환값

    Returns:
        True이면 AI 검증 필요
    """
    critical_fails = sum(
        1 for c in result["checks"]
        if not c["passed"] and c["severity"] == "critical"
    )
    warning_fails = sum(
        1 for c in result["checks"]
        if not c["passed"] and c["severity"] == "warning"
    )
    return critical_fails > 0 or warning_fails >= 2


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
