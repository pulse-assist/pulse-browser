"""Unit tests for URL policy helpers (no Chrome required)."""

from browser_plugin.policy import host_allowed, is_dangerous, parse_patterns


def test_allowlist_exact_and_wildcard():
    assert host_allowed("https://amazon.com/x", ["amazon.com"])
    assert host_allowed("https://www.amazon.com/x", ["amazon.com"])
    assert host_allowed("https://smile.amazon.com/x", ["*.amazon.com"])
    assert not host_allowed("https://evil.com/", ["amazon.com"])


def test_dangerous_patterns():
    patterns = parse_patterns("checkout, /pay")
    assert is_dangerous("https://shop.test/checkout/1", patterns)
    assert is_dangerous("https://shop.test/pay/now", patterns)
    assert not is_dangerous("https://shop.test/cart", patterns)
