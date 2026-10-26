from config_loader import load


def test_parses_mapping():
    assert load("a: 1") == {"a": 1}
