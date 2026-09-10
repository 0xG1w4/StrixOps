"""Explicit host rules shared by capture, replay and the task control plane."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def _host(value: str) -> str:
    value = value.rstrip(".").lower()
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("A website hostname is required")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        encoded = value.encode("idna").decode("ascii")
        if len(encoded) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(c.isalnum() or c == "-" for c in label)
            for label in encoded.split(".")
        ):
            raise ValueError("Invalid website hostname") from None
        return encoded


def parse_rule(rule: str) -> tuple[str, str, int | None, bool]:
    rule = rule.strip()
    if not rule or any(c.isspace() or ord(c) < 32 for c in rule):
        raise ValueError("Website rules cannot be empty or contain whitespace")
    explicit = "://" in rule
    parsed = urlsplit(rule if explicit else "//" + rule)
    if explicit and parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https website rules are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Website rules cannot contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use a hostname or origin; path and query filters are not website scope rules")
    name = parsed.hostname or ""
    wildcard = name.startswith("*.")
    name = name[2:] if wildcard else name
    name = _host(name)
    if wildcard:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            pass
        else:
            raise ValueError("IP addresses cannot have wildcard subdomains")
    port = parsed.port
    if explicit and port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme if explicit else "", name, port, wildcard


def normalize_rules(rules: list[str]) -> list[str]:
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Provide at most 100 website rules")
    result = []
    for rule in rules:
        if not isinstance(rule, str):
            raise ValueError("Website rules must be strings")
        scheme, host, port, wildcard = parse_rule(rule)
        authority = f"[{host}]" if ":" in host else host
        value = (scheme + "://" if scheme else "") + ("*." if wildcard else "") + authority
        if port is not None:
            value += f":{port}"
        if value not in result:
            result.append(value)
    return result


def allowed_url(url: str, allow_hosts: list[str], exclude_hosts: list[str] | None = None) -> bool:
    try:
        if any(ord(c) < 32 or c.isspace() for c in url):
            return False
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username is not None:
            return False
        host = _host(parsed.hostname or "")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        def matches(rule: str) -> bool:
            scheme, name, rule_port, wildcard = parse_rule(rule)
            return (
                (host.endswith("." + name) if wildcard else host == name)
                and (not scheme or scheme == parsed.scheme)
                and (rule_port is None or rule_port == port)
            )

        return any(matches(rule) for rule in allow_hosts) and not any(
            matches(rule) for rule in (exclude_hosts or [])
        )
    except (ValueError, UnicodeError, TypeError):
        return False
