from __future__ import annotations

import pytest

from scrapeyard.common.paths import safe_join, safe_path_part


def test_safe_path_part_accepts_regular_names() -> None:
    assert safe_path_part("demo-job_1.2") == "demo-job_1.2"


@pytest.mark.parametrize(
    "value",
    ["", "   ", ".", "..", "../x", "x/y", r"x\y", "x\x00y", "x\ny", "x\ty", "x\x7fy"],
)
def test_safe_path_part_rejects_unsafe_components(value: str) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        safe_path_part(value, label="project")


def test_safe_path_part_error_does_not_echo_rejected_value() -> None:
    with pytest.raises(ValueError) as exc_info:
        safe_path_part("secret-token\n", label="project")

    assert "secret-token" not in str(exc_info.value)


def test_safe_path_part_rejects_oversized_components() -> None:
    with pytest.raises(ValueError, match="at most 255 bytes"):
        safe_path_part("x" * 256, label="project")


def test_safe_join_validates_each_component(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        safe_join(tmp_path, "project", "../job")
