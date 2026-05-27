"""
tests/test_validation_rules.py
검증 상태 모델 회귀 테스트 (명세 #6) — 결정적, 네트워크/AI 불필요

배경:
  과거에는 데이터 부족으로 회계 항등식 검증을 스킵해도 check가 passed=True로
  저장되어, "검증 통과"와 "검증 불가(스킵)"가 한 통계에 섞였다. 명세 #6은
  스킵을 통과로 세지 않고 통과/실패/검증불가를 명확히 분리하도록 요구한다.
"""

from validator.rules import validate

# 회계 항등식이 모두 성립하는 완전한 항목 집합
_VALID = {
    "총자산": 1000.0, "총부채": 400.0, "자본총계": 600.0,
    "매출액": 800.0, "매출원가": 500.0, "매출총이익": 300.0,
    "영업이익": 100.0,
}


def _by_rule_status(result: dict) -> dict[str, str]:
    return {c["rule"]: c["status"] for c in result["checks"]}


def test_all_pass_is_verified():
    r = validate(_VALID)
    assert r["validation_status"] == "verified"
    assert r["is_valid"] is True
    assert r["flags"] == []
    assert r["skipped"] == []
    assert all(c["status"] == "passed" for c in r["checks"])
    assert all(c["passed"] is True for c in r["checks"])


def test_critical_failure_is_failed_validation():
    # 총자산을 깨뜨려 BS 항등식 위반
    bad = {**_VALID, "총자산": 9999.0}
    r = validate(bad)
    assert r["validation_status"] == "failed_validation"
    assert r["is_valid"] is False
    assert len(r["flags"]) == 1            # 실패한 critical 1건만 flag
    assert r["skipped"] == []
    bs = next(c for c in r["checks"] if "총자산 = 총부채" in c["rule"])
    assert bs["status"] == "failed"
    assert bs["passed"] is False


def test_skipped_is_not_counted_as_passed():
    # BS 항목이 전혀 없어 BS 항등식이 스킵된다.
    missing_bs = {
        "매출액": 800.0, "매출원가": 500.0, "매출총이익": 300.0, "영업이익": 100.0,
    }
    r = validate(missing_bs)

    # 핵심: 스킵은 통과(passed=True)로 저장되지 않는다.
    bs = next(c for c in r["checks"] if "총자산 = 총부채" in c["rule"])
    assert bs["status"] == "skipped_missing_data"
    assert bs["passed"] is None
    assert bs["passed"] is not True

    # 스킵은 실패 flag에도 들어가지 않는다.
    assert r["flags"] == []
    # 스킵은 별도 목록으로 노출된다 (필수 항목 누락 가시화).
    assert len(r["skipped"]) == 1
    assert r["skipped"][0]["note"]

    # critical 실패가 없으므로 is_valid는 True지만, 완전 통과는 아니다.
    assert r["is_valid"] is True
    assert r["validation_status"] == "partial"


def test_warning_failure_is_partial_not_verified():
    # 영업이익 > 매출총이익 → warning 실패 (critical 아님)
    items = {**_VALID, "영업이익": 999.0}
    r = validate(items)
    assert r["is_valid"] is True              # warning은 critical이 아니므로 valid
    assert r["validation_status"] == "partial"
    statuses = _by_rule_status(r)
    assert statuses["논리검증: 영업이익 ≤ 매출총이익"] == "failed"


def test_skipped_preserves_rule_severity():
    # 필수 항목 누락 스킵은 원 규칙의 severity를 유지한다(어떤 중요도가 빠졌는지 가시화).
    r = validate({"영업이익": 100.0})
    bs = next(c for c in r["checks"] if "총자산 = 총부채" in c["rule"])
    assert bs["status"] == "skipped_missing_data"
    assert bs["severity"] == "critical"
