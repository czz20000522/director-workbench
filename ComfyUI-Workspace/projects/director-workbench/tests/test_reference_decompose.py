from __future__ import annotations

from tools import reference_decompose


def test_recommended_frame_count_scales_with_duration_and_stays_bounded() -> None:
    assert reference_decompose.recommended_frame_count(10) == 12
    assert reference_decompose.recommended_frame_count(126.967) == 22
    assert reference_decompose.recommended_frame_count(900) == 36
