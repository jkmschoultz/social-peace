import random

from social_peace.config import Config
from social_peace.pipeline.selectors import _pick_range, _weighted_choice


def test_config_loads_and_validates():
    cfg = Config.load()
    assert cfg.render["resolution"] == [1080, 1920]
    assert cfg.templates, "templates.yaml should have entries"
    assert cfg.raw["overlay_text"]


def test_pick_range_scalar_and_list():
    rng = random.Random(0)
    assert _pick_range(rng, 3) == 3
    for _ in range(50):
        v = _pick_range(rng, [2, 4])
        assert 2 <= v <= 4


def test_weighted_choice_respects_weight():
    rng = random.Random(1)
    items = [{"name": "a", "weight": 0.0001}, {"name": "b", "weight": 1000}]
    picks = [_weighted_choice(rng, items)["name"] for _ in range(200)]
    assert picks.count("b") > picks.count("a")
