"""
utils/cache.py
SQLite 기반 캐싱 유틸리티

DART API 호출 결과와 파싱 결과를 SQLite에 저장하여
반복 요청 시 API 비용을 절감한다.

저장 형식: pickle → bytes → BLOB
"""

import pickle
import sqlite3
import time
from typing import Any, Optional

from config import CACHE_DB_PATH


def _get_conn() -> sqlite3.Connection:
    """캐시 DB 연결을 반환하고 테이블이 없으면 생성한다."""
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cache (
            key        TEXT PRIMARY KEY,
            value      BLOB NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_cache(key: str) -> Optional[Any]:
    """
    캐시에서 값을 조회한다.

    Args:
        key: 캐시 키

    Returns:
        캐시된 값 또는 None (없거나 만료된 경우)
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()

        if row is None:
            return None

        value_blob, expires_at = row
        if time.time() > expires_at:
            delete_cache(key)
            return None

        return pickle.loads(value_blob)
    except Exception:
        return None


def set_cache(key: str, value: Any, ttl_hours: float = 24.0) -> None:
    """
    값을 캐시에 저장한다.

    Args:
        key:       캐시 키
        value:     저장할 값 (pickle 가능한 모든 객체)
        ttl_hours: 만료 시간 (시간 단위, 기본 24시간)
    """
    try:
        expires_at = time.time() + ttl_hours * 3600
        blob = pickle.dumps(value)
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, blob, expires_at),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 캐시 오류는 무시 (기능 영향 없음)


def delete_cache(key: str) -> None:
    """
    특정 캐시 항목을 삭제한다.

    Args:
        key: 삭제할 캐시 키
    """
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clear_all_cache() -> None:
    """캐시 테이블의 모든 항목을 삭제한다."""
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM cache")
        conn.commit()
        conn.close()
    except Exception:
        pass


def purge_expired() -> int:
    """
    만료된 캐시 항목을 정리한다.

    Returns:
        삭제된 항목 수
    """
    try:
        conn = _get_conn()
        cursor = conn.execute(
            "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    except Exception:
        return 0


def make_cache_key(*parts: str) -> str:
    """
    여러 문자열 파트를 조합하여 캐시 키를 생성한다.

    Args:
        *parts: 키를 구성하는 문자열들 (예: corp_code, year, fs_div)

    Returns:
        콜론으로 연결된 캐시 키 문자열
    """
    return ":".join(str(p) for p in parts)
