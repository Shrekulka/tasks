# braincomua_project/modules/utils.py

"""
Helper utilities for text normalization and price cleaning
used across requests, Selenium, and Playwright scrapers.
"""
import re

def clean_text(text):
    """
    Normalizes whitespace: replaces non-breaking spaces (\xa0)
    and multiple whitespace characters with a single regular space.
    """
    if not text:
        return text
    # Replace non-breaking spaces with regular spaces
    normalized = text.replace("\xa0", " ")
    # Replace any whitespace sequences (newlines, tabs, multiple spaces) with a single regular space
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def clean_price(text):
    """
    Helper function to remove currency symbols and whitespaces from prices.
    Returns digits-only string or None.
    """
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit())
    return cleaned if cleaned else None