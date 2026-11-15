import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import photolimits


def test_evaluate_ok_and_edges():
    ok = photolimits.evaluate(120, 500)
    assert ok["ok"] is True
    assert ok["too_few"] is False
    assert ok["too_many"] is False
    assert ok["min_photos"] == 30
    assert ok["photo_cap"] == 500


def test_evaluate_too_few():
    result = photolimits.evaluate(12, 500)
    assert result["ok"] is False
    assert result["too_few"] is True
    assert "30" in result["warning"]


def test_evaluate_too_many():
    result = photolimits.evaluate(501, 500)
    assert result["ok"] is False
    assert result["too_many"] is True
    assert "500" in result["warning"]


def test_enforce_raises():
    with pytest.raises(photolimits.PhotoLimitError) as few:
        photolimits.enforce(29, 500)
    assert few.value.code == "too_few"
    with pytest.raises(photolimits.PhotoLimitError) as many:
        photolimits.enforce(600, 500)
    assert many.value.code == "too_many"
    assert photolimits.enforce(30, 500)["ok"] is True


def test_clamp_and_suggested_altitude():
    assert photolimits.clamp_cap(10) == 30
    assert photolimits.clamp_cap(9000) == 5000
    assert photolimits.clamp_cap("nope") == 500
    assert photolimits.suggested_altitude_m(10) == 35.0
    assert photolimits.suggested_altitude_m(None) is None


def test_stockpile_mission_detection():
    assert photolimits.is_stockpile_mission({"kind": "stockpile_counting", "computed": {"photoCount": 80}})
    assert photolimits.is_stockpile_mission({"settings": {"preset": "stockpile"}})
    assert not photolimits.is_stockpile_mission({"settings": {"preset": "findDrone"}})
    assert photolimits.photo_count_of({"computed": {"photoCount": 42}}) == 42
    assert photolimits.photo_count_of({"photos": [1, 2, 3]}) == 3
