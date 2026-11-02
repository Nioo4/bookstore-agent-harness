import pytest

from app.memory.errors import MemoryValueInvalid
from app.memory.types import normalize_v2_preference_value


def test_v2_preference_value_normalizes_language_names_and_budget() -> None:
    assert normalize_v2_preference_value("PREFERRED_LANGUAGE", "中文") == "zh-CN"
    assert normalize_v2_preference_value("PREFERRED_LANGUAGE", "EN-us") == "en-US"
    assert normalize_v2_preference_value("MAX_USUAL_BUDGET", "5000") == "5000"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PREFERRED_LANGUAGE", "随便"),
        ("MAX_USUAL_BUDGET", "50元"),
        ("MAX_USUAL_BUDGET", "-1"),
    ],
)
def test_v2_preference_value_rejects_values_that_break_runtime_filters(key: str, value: str) -> None:
    with pytest.raises(MemoryValueInvalid):
        normalize_v2_preference_value(key, value)
