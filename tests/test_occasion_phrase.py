import pytest

from app.models.occasion import OccasionType, occasion_phrase


_FALLBACK = "your special day"


@pytest.mark.parametrize("ot", [t for t in OccasionType if t is not OccasionType.CUSTOM])
def test_every_non_custom_occasion_has_dedicated_phrase(ot: OccasionType) -> None:
    assert occasion_phrase(ot) != _FALLBACK, (
        f"{ot.value} falls through to fallback — extend _OCCASION_PHRASES "
        "or the friend voucher template will say 'your special day' for it."
    )


def test_custom_uses_label_when_present() -> None:
    assert occasion_phrase(OccasionType.CUSTOM, "housewarming") == "housewarming"


def test_custom_falls_back_when_label_blank() -> None:
    assert occasion_phrase(OccasionType.CUSTOM, "  ") == _FALLBACK
    assert occasion_phrase(OccasionType.CUSTOM, None) == _FALLBACK
