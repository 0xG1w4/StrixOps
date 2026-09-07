"""Project target-scope normalization and launch-time containment checks.

The console stores project scope as a small JSON policy.  This module is the
single parser for that policy so API handlers do not grow subtly different
domain/IP matching rules.

Normalized policy shape::

    {
        "schema_version": 1,
        "mode": "restricted",
        "entries": [
            {
                "kind": "domain",
                "value": "example.com",
                "include_subdomains": True,
            },
            {"kind": "cidr", "value": "10.20.0.0/16"},
        ],
    }

``*`` is represented by one ``{"kind": "any", "value": "*"}`` entry
and may never be combined with another entry.  Invalid policies and invalid
targets raise: callers cannot accidentally turn malformed input into an
unrestricted policy by coercing a falsey result.

These checks constrain the target accepted at launch.  They are deliberately
pure (no DNS lookups) and are not a replacement for an execution-time network
guard that validates DNS answers and every outbound connection.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import SplitResult, urlsplit

SCHEMA_VERSION = 1
MAX_SCOPE_ENTRIES = 256

KIND_ANY = "any"
KIND_DOMAIN = "domain"
KIND_IP = "ip"
KIND_CIDR = "cidr"

MODE_UNRESTRICTED = "unrestricted"
MODE_RESTRICTED = "restricted"

SCAN_WEB = "web"
SCAN_INTERNAL = "internal"

_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_POLICY_KEYS = {"schema_version", "mode", "entries"}
_RULE_KEYS = {
    KIND_ANY: {"kind", "value"},
    KIND_DOMAIN: {"kind", "value", "include_subdomains"},
    KIND_IP: {"kind", "value"},
    KIND_CIDR: {"kind", "value"},
}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
TargetKind = Literal["domain", "ip", "cidr"]


class ScopeError(ValueError):
    """Base class for scope-policy and target errors."""


class ScopeValidationError(ScopeError):
    """A policy cannot be normalized safely."""

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class TargetValidationError(ScopeError):
    """A scan target cannot be parsed unambiguously."""


class TargetOutOfScopeError(ScopeError):
    """A valid target is not contained by a valid scope policy."""

    def __init__(self, decision: ScopeDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


@dataclass(frozen=True, slots=True)
class NormalizedTarget:
    """Canonical host or network extracted from a scan target."""

    raw: str
    scan_type: Literal["web", "internal"]
    kind: TargetKind
    value: str


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """Containment result; malformed input raises before this is created."""

    allowed: bool
    target: NormalizedTarget
    matched_rule: dict[str, Any] | None
    reason: str


def default_scope() -> dict[str, Any]:
    """A fresh project's explicit, JSON-serializable ``*`` policy."""
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE_UNRESTRICTED,
        "entries": [{"kind": KIND_ANY, "value": "*"}],
    }


def normalize_scope(raw: object) -> dict[str, Any]:
    """Validate and canonicalize a project scope.

    The literal string ``"*"`` is accepted as an input shorthand.  Missing
    values are not treated as ``*``; project creation/migration must opt into
    the default by calling :func:`default_scope` explicitly.
    """
    if raw == "*":
        return default_scope()
    if not isinstance(raw, dict):
        raise ScopeValidationError(["scope must be an object or the literal '*'"])

    errors: list[str] = []
    unknown_policy_keys = set(raw) - _POLICY_KEYS
    if unknown_policy_keys:
        errors.append(f"scope has unknown fields: {_join_fields(unknown_policy_keys)}")

    supplied_version = raw.get("schema_version", SCHEMA_VERSION)
    if type(supplied_version) is not int or supplied_version != SCHEMA_VERSION:
        errors.append(f"scope.schema_version must be {SCHEMA_VERSION}")

    mode = raw.get("mode")
    if not isinstance(mode, str) or mode not in {MODE_UNRESTRICTED, MODE_RESTRICTED}:
        errors.append("scope.mode must be 'unrestricted' or 'restricted'")

    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        errors.append("scope.entries must be an array")
        raw_entries = []
    elif not raw_entries:
        errors.append("scope.entries must contain at least one entry")
    elif len(raw_entries) > MAX_SCOPE_ENTRIES:
        errors.append(f"scope.entries exceeds the limit of {MAX_SCOPE_ENTRIES}")

    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries[:MAX_SCOPE_ENTRIES]):
        try:
            entries.append(normalize_scope_entry(entry))
        except ScopeValidationError as exc:
            errors.extend(f"scope.entries[{index}]: {message}" for message in exc.errors)

    any_entries = [entry for entry in entries if entry["kind"] == KIND_ANY]
    if any_entries and len(entries) != 1:
        errors.append("the '*' entry must be the only scope entry")
    if mode == MODE_UNRESTRICTED:
        if len(entries) != 1 or not any_entries:
            errors.append("unrestricted scope must contain exactly the '*' entry")
    elif mode == MODE_RESTRICTED and any_entries:
        errors.append("restricted scope cannot contain the '*' entry")

    if errors:
        raise ScopeValidationError(errors)

    # Preserve first occurrence order while eliminating exact duplicates.
    unique: list[dict[str, Any]] = []
    fingerprints: set[tuple[object, ...]] = set()
    for entry in entries:
        fingerprint = (
            entry["kind"],
            entry["value"],
            entry.get("include_subdomains"),
        )
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            unique.append(entry)

    return {"schema_version": SCHEMA_VERSION, "mode": mode, "entries": unique}


def validate_scope(raw: object) -> list[str]:
    """Return validation messages for form/API use without weakening checks."""
    try:
        normalize_scope(raw)
    except ScopeValidationError as exc:
        return list(exc.errors)
    return []


def normalize_scope_entry(raw: object) -> dict[str, Any]:
    """Validate and canonicalize one scope entry."""
    if not isinstance(raw, dict):
        raise ScopeValidationError(["entry must be an object"])

    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _RULE_KEYS:
        raise ScopeValidationError(["kind must be 'any', 'domain', 'ip', or 'cidr'"])

    errors: list[str] = []
    unknown = set(raw) - _RULE_KEYS[kind]
    if unknown:
        errors.append(f"entry has unknown fields: {_join_fields(unknown)}")

    value = raw.get("value")
    if not isinstance(value, str):
        errors.append("value must be a string")
        value = ""
    value = value.strip()

    if kind == KIND_ANY:
        if value != "*":
            errors.append("an 'any' entry value must be '*'")
        normalized: dict[str, Any] = {"kind": KIND_ANY, "value": "*"}
    elif kind == KIND_DOMAIN:
        if "include_subdomains" not in raw or type(raw.get("include_subdomains")) is not bool:
            errors.append("domain include_subdomains must be an explicit boolean")
        try:
            domain = normalize_domain(value)
        except ScopeValidationError as exc:
            errors.extend(exc.errors)
            domain = ""
        normalized = {
            "kind": KIND_DOMAIN,
            "value": domain,
            "include_subdomains": raw.get("include_subdomains")
            if type(raw.get("include_subdomains")) is bool
            else False,
        }
    elif kind == KIND_IP:
        try:
            ip = _parse_ip(value, label="IP scope entry")
        except ScopeValidationError as exc:
            errors.extend(exc.errors)
            ip = None
        normalized = {"kind": KIND_IP, "value": str(ip) if ip is not None else ""}
    else:
        try:
            network = _parse_network(value, label="CIDR scope entry")
        except ScopeValidationError as exc:
            errors.extend(exc.errors)
            network = None
        normalized = {"kind": KIND_CIDR, "value": str(network) if network is not None else ""}

    if errors:
        raise ScopeValidationError(errors)
    return normalized


def normalize_domain(raw: str) -> str:
    """Return a lowercase ASCII/IDNA domain with an optional root dot removed."""
    value = raw.strip()
    if value.endswith("."):
        value = value[:-1]
    if not value:
        raise ScopeValidationError(["domain must not be empty"])
    if value.endswith(".") or ".." in value:
        raise ScopeValidationError(["domain contains an empty label"])
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ScopeValidationError(["domain must not contain whitespace or control characters"])
    if any(token in value for token in ("*", "/", "\\", ":", "@")):
        raise ScopeValidationError(
            ["domain must be a hostname without wildcards, scheme, port, credentials, or path"]
        )
    try:
        ascii_value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ScopeValidationError(["domain is not valid IDNA"]) from exc
    if len(ascii_value) > 253:
        raise ScopeValidationError(["domain is longer than 253 characters"])
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in ascii_value.split(".")):
        raise ScopeValidationError(["domain contains an invalid hostname label"])
    try:
        ipaddress.ip_address(ascii_value)
    except ValueError:
        return ascii_value
    raise ScopeValidationError(["domain entry is an IP address; use kind 'ip'"])


def normalize_target(raw: object, scan_type: str) -> NormalizedTarget:
    """Parse a web or internal scan target into a canonical host/network."""
    normalized_scan_type = str(scan_type).strip().lower()
    if normalized_scan_type not in {SCAN_WEB, SCAN_INTERNAL}:
        raise TargetValidationError("scan_type must be 'web' or 'internal'")
    if not isinstance(raw, str):
        raise TargetValidationError("target must be a string")
    value = raw.strip()
    if not value:
        raise TargetValidationError("target must not be empty")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise TargetValidationError("target must not contain whitespace or control characters")
    if "\\" in value:
        raise TargetValidationError("target must not contain backslashes")

    if normalized_scan_type == SCAN_WEB:
        kind, canonical = _normalize_web_target(value)
    else:
        kind, canonical = _normalize_internal_target(value)
    return NormalizedTarget(
        raw=value,
        scan_type=normalized_scan_type,
        kind=kind,
        value=canonical,
    )


def evaluate_target(scope: object, target: object, scan_type: str) -> ScopeDecision:
    """Evaluate containment, raising for malformed policy or target input."""
    policy = normalize_scope(scope)
    normalized_target = normalize_target(target, scan_type)

    for rule in policy["entries"]:
        if _rule_contains(rule, normalized_target):
            return ScopeDecision(
                allowed=True,
                target=normalized_target,
                matched_rule=dict(rule),
                reason=f"target {normalized_target.value!r} matches scope entry {rule['value']!r}",
            )

    return ScopeDecision(
        allowed=False,
        target=normalized_target,
        matched_rule=None,
        reason=f"target {normalized_target.value!r} is outside the project scope",
    )


def target_is_allowed(scope: object, target: object, scan_type: str) -> bool:
    """Boolean containment result; validation errors intentionally propagate."""
    return evaluate_target(scope, target, scan_type).allowed


def assert_target_allowed(scope: object, target: object, scan_type: str) -> ScopeDecision:
    """Return the matching decision or raise :class:`TargetOutOfScopeError`."""
    decision = evaluate_target(scope, target, scan_type)
    if not decision.allowed:
        raise TargetOutOfScopeError(decision)
    return decision


def _normalize_web_target(value: str) -> tuple[TargetKind, str]:
    # A raw IP literal is unambiguous and avoids treating an unbracketed IPv6
    # address as ``host:port``.
    if "%" not in value:
        try:
            return KIND_IP, str(ipaddress.ip_address(value))
        except ValueError:
            pass

    parsed: SplitResult = (
        urlsplit(value) if value.startswith("//") or "://" in value else urlsplit(f"//{value}")
    )

    scheme = parsed.scheme.lower()
    if scheme and scheme not in {"http", "https"}:
        raise TargetValidationError("web target scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise TargetValidationError("web target must not contain credentials")
    try:
        host = parsed.hostname
        # Accessing .port performs urllib's range and integer validation.
        _ = parsed.port
    except ValueError as exc:
        raise TargetValidationError(f"web target has an invalid host or port: {exc}") from exc
    if not host:
        raise TargetValidationError("web target must contain a hostname or IP address")

    if "%" in host:
        raise TargetValidationError("scoped IPv6 zone identifiers are not supported")
    try:
        return KIND_IP, str(ipaddress.ip_address(host))
    except ValueError:
        try:
            return KIND_DOMAIN, normalize_domain(host)
        except ScopeValidationError as exc:
            raise TargetValidationError(str(exc)) from exc


def _normalize_internal_target(value: str) -> tuple[TargetKind, str]:
    if "%" in value:
        raise TargetValidationError("scoped IPv6 zone identifiers are not supported")
    if any(token in value for token in ("://", "/?", "#", "@")):
        # Slash is valid only as the CIDR separator and is handled below.
        raise TargetValidationError("internal target must be a hostname, IP address, or CIDR")
    if "/" in value:
        try:
            network = _parse_network(value, label="internal target")
        except ScopeValidationError as exc:
            raise TargetValidationError(str(exc)) from exc
        return KIND_CIDR, str(network)
    try:
        return KIND_IP, str(ipaddress.ip_address(value))
    except ValueError:
        try:
            return KIND_DOMAIN, normalize_domain(value)
        except ScopeValidationError as exc:
            raise TargetValidationError(str(exc)) from exc


def _rule_contains(rule: dict[str, Any], target: NormalizedTarget) -> bool:
    kind = rule["kind"]
    if kind == KIND_ANY:
        return True
    if kind == KIND_DOMAIN:
        if target.kind != KIND_DOMAIN:
            return False
        domain = rule["value"]
        return target.value == domain or (
            bool(rule["include_subdomains"]) and target.value.endswith(f".{domain}")
        )
    if kind == KIND_IP:
        rule_ip = ipaddress.ip_address(rule["value"])
        if target.kind == KIND_IP:
            return ipaddress.ip_address(target.value) == rule_ip
        if target.kind == KIND_CIDR:
            network = ipaddress.ip_network(target.value, strict=True)
            return network.num_addresses == 1 and network.network_address == rule_ip
        return False
    if kind == KIND_CIDR:
        allowed_network = ipaddress.ip_network(rule["value"], strict=True)
        if target.kind == KIND_IP:
            target_ip = ipaddress.ip_address(target.value)
            return target_ip.version == allowed_network.version and target_ip in allowed_network
        if target.kind == KIND_CIDR:
            target_network = ipaddress.ip_network(target.value, strict=True)
            return target_network.version == allowed_network.version and target_network.subnet_of(
                allowed_network
            )
    return False


def _parse_ip(value: str, *, label: str) -> IPAddress:
    if not value or "%" in value:
        raise ScopeValidationError([f"{label} must be a plain IPv4 or IPv6 address"])
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise ScopeValidationError([f"{label} is not a valid IPv4 or IPv6 address"]) from exc


def _parse_network(value: str, *, label: str) -> IPNetwork:
    if not value or "/" not in value or "%" in value:
        raise ScopeValidationError([f"{label} must be an IPv4 or IPv6 network in CIDR notation"])
    try:
        # strict=True prevents silently widening 10.20.1.4/16 to 10.20.0.0/16.
        return ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ScopeValidationError(
            [f"{label} is invalid or has host bits set; use the canonical network address"]
        ) from exc


def _join_fields(fields: set[object]) -> str:
    return ", ".join(sorted(str(field) for field in fields))
