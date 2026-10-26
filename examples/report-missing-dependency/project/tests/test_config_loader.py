from config_loader import load_report_config


def test_defaults():
    assert load_report_config("") == {"title": "Weekly report", "currency": "INR"}


def test_custom_title():
    assert load_report_config("title: Sales")["title"] == "Sales"
