"""Loads report settings from YAML. Requires the third-party PyYAML package."""

import yaml


def load_report_config(text):
    data = yaml.safe_load(text) or {}
    return {
        "title": str(data.get("title", "Weekly report")),
        "currency": str(data.get("currency", "INR")),
    }
