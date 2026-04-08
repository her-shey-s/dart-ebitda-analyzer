"""
utils/analysis_logger.py
분석 파이프라인 디버그 로거

회사/연도별 분석 과정을 타임스탬프와 함께 기록하여
어디서 멈추거나 느려지는지 추적할 수 있게 한다.
"""

import time
from typing import Callable, Optional


class AnalysisLogger:
    """
    단일 (회사, 연도) 분석 과정을 타임스탬프와 함께 기록한다.

    사용법:
        logger = AnalysisLogger("삼성전자", 2023)
        logger.log("REPORT", "보고서 탐색 시작")
        ...
        logger.log("REPORT", "보고서 발견: 사업보고서 (2023.12)")
        lines = logger.get_lines()
    """

    def __init__(self, corp_name: str, year: int) -> None:
        self._corp_name = corp_name
        self._year = year
        self._start = time.perf_counter()
        self._lines: list[str] = []
        self.log("START", f"=== {corp_name} {year} 분석 시작 ===")

    def log(self, tag: str, message: str) -> None:
        """타임스탬프가 포함된 로그 한 줄을 추가한다."""
        elapsed = time.perf_counter() - self._start
        self._lines.append(f"[+{elapsed:>7.3f}s] [{tag:<10s}] {message}")

    def elapsed(self) -> float:
        """시작 이후 경과 시간(초)을 반환한다."""
        return time.perf_counter() - self._start

    def get_lines(self) -> list[str]:
        """수집된 전체 로그 라인 리스트를 반환한다."""
        return list(self._lines)

    def as_log_fn(self) -> Callable[[str, str], None]:
        """서브모듈에 전달할 log_fn 콜백을 반환한다."""
        return self.log


def format_amount(val: Optional[float]) -> str:
    """금액을 읽기 쉬운 문자열로 변환한다 (로그 출력용)."""
    if val is None:
        return "None"
    if abs(val) >= 1e8:
        return f"{val / 1e8:,.1f}억"
    return f"{val:,.0f}"
