"""
tests/test_regression.py
fixture 기반 오프라인 회귀 테스트

tests/fixtures/<id>/ 아래의 모든 fixture를 자동 수집하여, 저장된 원본 입력으로
추출기를 돌린 결과가 golden.json과 일치하는지 검증한다. 네트워크/AI 불필요.

새 fixture 추가:
    python scripts/capture_fixture.py <기업명> <연도> --id <fixture_id>
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import _harness

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _discover_fixtures() -> list[Path]:
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(
        d for d in FIXTURES_DIR.iterdir()
        if d.is_dir() and (d / "manifest.json").is_file() and (d / "golden.json").is_file()
    )


_FIXTURES = _discover_fixtures()


@pytest.mark.parametrize("fixture_dir", _FIXTURES, ids=[d.name for d in _FIXTURES])
def test_fixture_matches_golden(fixture_dir: Path):
    manifest = json.loads((fixture_dir / "manifest.json").read_text("utf-8"))
    golden = json.loads((fixture_dir / "golden.json").read_text("utf-8"))

    actual = _harness.build_result(manifest, fixture_dir)
    diffs = _harness.diff_result(actual, golden)

    assert not diffs, (
        f"[{fixture_dir.name}] golden 불일치 ({len(diffs)}건):\n  " + "\n  ".join(diffs)
    )


def test_at_least_one_fixture_present():
    # fixture 디렉터리가 통째로 비면 회귀 테스트가 조용히 0건 통과하는 것을 막는다.
    assert _FIXTURES, "tests/fixtures/ 에 fixture가 하나도 없습니다."
