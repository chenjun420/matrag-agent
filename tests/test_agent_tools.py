"""tools 单元测试：read_bundle / plan / derive（离线）。"""

from __future__ import annotations

import json

import pytest

from matrag_agent.tools import TOOL_REGISTRY, derive, plan, read_bundle


def test_tool_registry_exposes_expected() -> None:
    assert set(TOOL_REGISTRY) == {"read_bundle", "plan", "derive"}
    assert all(callable(fn) for fn in TOOL_REGISTRY.values())


def test_read_bundle_reads_file(tmp_path) -> None:
    (tmp_path / "PA601.bundle.json").write_text(
        json.dumps({"product_code": "PA601", "requirements": []}), encoding="utf-8"
    )
    out = read_bundle("PA601", exports_dir=tmp_path)
    assert out["product_code"] == "PA601"


def test_read_bundle_missing_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        read_bundle("NOPE", exports_dir=tmp_path)


def test_read_bundle_non_object_raises(tmp_path) -> None:
    (tmp_path / "X.bundle.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        read_bundle("X", exports_dir=tmp_path)


class _StubClient:
    """记录入参、返回固定 payload 的 stub，用于隔离网络。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def plan_review(self, bundle: dict) -> dict:
        self.calls.append(bundle)
        return self.payload


def test_plan_normalizes_fields() -> None:
    stub = _StubClient({"yaml": "steps: []", "pending": [{"id": 1}], "gaps": ["g1"]})
    out = plan({"product_code": "PA601"}, client=stub)
    assert out["plan_yaml"] == "steps: []"
    assert out["plan_pending"] == [{"id": 1}]
    assert out["plan_gaps"] == ["g1"]


def test_plan_passes_bundle_through() -> None:
    stub = _StubClient({})
    plan({"product_code": "PA601"}, client=stub)
    assert stub.calls == [{"product_code": "PA601"}]


def test_plan_missing_fields_default_empty() -> None:
    out = plan({}, client=_StubClient({}))
    assert out == {"plan_yaml": "", "plan_gaps": [], "plan_pending": [], "warnings": []}


def test_plan_surfaces_warnings() -> None:
    """B.3（D16）：planner warnings 必须透出，供三类分离解读。"""
    stub = _StubClient({"warnings": ["资源冲突: 通道 1 被占用"]})
    out = plan({}, client=stub)
    assert out["warnings"] == ["资源冲突: 通道 1 被占用"]


def test_derive_placeholder_marks_not_implemented() -> None:
    out = derive("V = I * R", {"I": 1.0, "R": 2.0})
    assert out["status"] == "not_implemented"
    assert out["expression"] == "V = I * R"
    assert out["variables"] == {"I": 1.0, "R": 2.0}
    assert out["value"] is None
