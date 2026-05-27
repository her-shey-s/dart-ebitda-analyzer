"""
Shared helpers for value-level extraction metadata.

The app still exposes the legacy ``items`` mapping for UI and validation, but
new extraction code should also carry ``item_details`` so every final number can
be traced back to its source row/cell and confidence level.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional


def empty_item_detail(
    name: str,
    *,
    source: Optional[dict[str, Any]] = None,
    confidence: str = "missing",
    value_state: str = "missing",
    flags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Return an empty detail record for an item that was not extracted."""
    return {
        "name": name,
        "value": None,
        "raw_value": None,
        "unit_multiplier": None,
        "normalized_value_unit": "KRW",
        "source": source or {},
        "confidence": confidence,
        "value_state": value_state,
        "flags": list(flags or []),
    }


def make_item_detail(
    name: str,
    *,
    value: Optional[float],
    raw_value: Any = None,
    unit_multiplier: Optional[int] = 1,
    source: Optional[dict[str, Any]] = None,
    confidence: str = "verified",
    value_state: Optional[str] = None,
    flags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build a serializable detail record for one extracted item."""
    if value is None and confidence == "verified":
        confidence = "missing"
    return {
        "name": name,
        "value": float(value) if value is not None else None,
        "raw_value": None if raw_value is None else str(raw_value),
        "unit_multiplier": unit_multiplier,
        "normalized_value_unit": "KRW",
        "source": source or {},
        "confidence": confidence,
        "value_state": value_state or ("extracted" if value is not None else "missing"),
        "flags": unique_flags(flags or []),
    }


def empty_item_details(item_names: list[str]) -> dict[str, dict[str, Any]]:
    """Return an empty detail mapping for all item names."""
    return {name: empty_item_detail(name) for name in item_names}


def details_to_items(details: dict[str, dict[str, Any]]) -> dict[str, Optional[float]]:
    """Project rich item details back to the legacy item -> numeric value map."""
    return {
        name: detail.get("value") if isinstance(detail, dict) else None
        for name, detail in details.items()
    }


def details_from_items(
    items: dict[str, Optional[float]],
    *,
    source: Optional[dict[str, Any]] = None,
    confidence: str = "unverified_legacy_value",
    flags: Optional[list[str]] = None,
) -> dict[str, dict[str, Any]]:
    """Wrap a legacy numeric item map when row/cell metadata is not available."""
    result: dict[str, dict[str, Any]] = {}
    for name, value in items.items():
        if value is None:
            result[name] = empty_item_detail(
                name,
                source=deepcopy(source) if source else None,
                flags=list(flags or []),
            )
        else:
            result[name] = make_item_detail(
                name,
                value=value,
                raw_value=None,
                unit_multiplier=None,
                source=deepcopy(source) if source else None,
                confidence=confidence,
                flags=list(flags or []),
            )
    return result


def reconcile_details_with_final_items(
    details: dict[str, dict[str, Any]],
    final_items: dict[str, Optional[float]],
    *,
    ai_comparison: Optional[dict[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
    """
    Keep original details where the final value stayed the same.

    If AI comparison changed or filled a numeric value without a cell-level
    source, mark that value explicitly as unverified AI output.
    """
    result = deepcopy(details)
    ai_source = (ai_comparison or {}).get("source")
    disagreements = (ai_comparison or {}).get("disagreements") or {}

    for name, final_value in final_items.items():
        current = result.get(name)
        current_value = current.get("value") if isinstance(current, dict) else None
        if same_numeric_value(current_value, final_value):
            continue

        if final_value is None:
            result[name] = empty_item_detail(
                name,
                source={
                    "source_type": "ai_comparison",
                    "ai_source": ai_source,
                    "previous_source": current.get("source") if isinstance(current, dict) else None,
                },
                flags=["final_value_removed_by_ai_comparison"],
            )
            continue

        result[name] = make_item_detail(
            name,
            value=final_value,
            raw_value=None,
            unit_multiplier=None,
            source={
                "source_type": "ai_comparison",
                "ai_source": ai_source,
                "disagreement": _stringify_disagreement(disagreements.get(name)),
            },
            confidence="unverified_ai_value",
            flags=["ai_numeric_without_source"],
        )

    return result


def add_detail_flag(detail: dict[str, Any], flag: str) -> None:
    """Add one flag to a detail record, preserving order and uniqueness."""
    flags = list(detail.get("flags") or [])
    if flag not in flags:
        flags.append(flag)
    detail["flags"] = flags


def annotate_report_source(
    details: dict[str, dict[str, Any]],
    *,
    rcept_no: str,
    report_nm: str,
    path: str,
    report_type: str,
) -> None:
    """Attach report-level source metadata to every detail in-place."""
    for detail in details.values():
        if not isinstance(detail, dict):
            continue
        source = detail.setdefault("source", {})
        source.setdefault("rcept_no", rcept_no)
        source.setdefault("report_nm", report_nm)
        source.setdefault("path", path)
        source.setdefault("report_type", report_type)


def same_numeric_value(a: Optional[float], b: Optional[float]) -> bool:
    """Compare numeric values with a tiny tolerance for float round trips."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= max(1.0, abs(float(a)), abs(float(b))) * 1e-12


def unique_flags(flags: list[str]) -> list[str]:
    """Return flags with duplicates removed while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for flag in flags:
        if flag and flag not in seen:
            seen.add(flag)
            result.append(flag)
    return result


def _stringify_disagreement(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, tuple):
        return " vs ".join("-" if v is None else f"{float(v):.0f}" for v in value)
    return str(value)
