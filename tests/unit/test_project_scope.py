"""Project target-scope policy tests."""

from __future__ import annotations

import pytest

from strixops.console import project_scope


def _restricted(*entries: dict) -> dict:
    return {"mode": "restricted", "entries": list(entries)}


def _domain(value: str, include_subdomains: bool) -> dict:
    return {
        "kind": "domain",
        "value": value,
        "include_subdomains": include_subdomains,
    }


def test_default_scope_is_explicit_any_and_still_validates_target():
    assert project_scope.normalize_scope("*") == project_scope.default_scope()
    assert project_scope.target_is_allowed(project_scope.default_scope(), "https://example.com/a", "web")
    with pytest.raises(project_scope.TargetValidationError):
        project_scope.target_is_allowed(project_scope.default_scope(), "file:///etc/passwd", "web")


def test_missing_scope_does_not_silently_become_unrestricted():
    with pytest.raises(project_scope.ScopeValidationError):
        project_scope.normalize_scope(None)


def test_any_cannot_be_mixed_or_used_in_restricted_mode():
    policy = _restricted(
        {"kind": "any", "value": "*"},
        _domain("example.com", False),
    )
    errors = project_scope.validate_scope(policy)
    assert "the '*' entry must be the only scope entry" in errors
    assert "restricted scope cannot contain the '*' entry" in errors


def test_domain_requires_explicit_subdomain_choice():
    with pytest.raises(project_scope.ScopeValidationError, match="explicit boolean"):
        project_scope.normalize_scope(_restricted({"kind": "domain", "value": "example.com"}))


def test_domain_normalizes_case_root_dot_and_idna():
    policy = project_scope.normalize_scope(_restricted(_domain("BÜCHER.Example.", True)))
    assert policy["entries"] == [
        {
            "kind": "domain",
            "value": "xn--bcher-kva.example",
            "include_subdomains": True,
        }
    ]


def test_domain_matching_is_label_safe_and_subdomains_are_opt_in():
    exact = _restricted(_domain("example.com", False))
    recursive = _restricted(_domain("example.com", True))

    assert project_scope.target_is_allowed(exact, "https://EXAMPLE.com:8443/path", "web")
    assert not project_scope.target_is_allowed(exact, "https://api.example.com", "web")
    assert project_scope.target_is_allowed(recursive, "api.deep.example.com", "web")
    assert not project_scope.target_is_allowed(recursive, "example.com.attacker.test", "web")
    assert not project_scope.target_is_allowed(recursive, "notexample.com", "web")


def test_web_ip_target_can_match_ip_or_cidr():
    policy = _restricted(
        {"kind": "ip", "value": "203.0.113.9"},
        {"kind": "cidr", "value": "10.20.0.0/16"},
    )
    assert project_scope.target_is_allowed(policy, "http://203.0.113.9:8080/", "web")
    assert project_scope.target_is_allowed(policy, "https://10.20.4.8", "web")
    assert not project_scope.target_is_allowed(policy, "https://10.21.4.8", "web")


def test_internal_cidr_must_be_entirely_contained():
    policy = _restricted({"kind": "cidr", "value": "10.20.0.0/16"})
    assert project_scope.target_is_allowed(policy, "10.20.4.0/24", "internal")
    assert not project_scope.target_is_allowed(policy, "10.20.0.0/15", "internal")


def test_ipv6_is_normalized_and_contained():
    policy = _restricted({"kind": "cidr", "value": "2001:db8::/32"})
    decision = project_scope.assert_target_allowed(
        policy,
        "https://[2001:0db8:0000:0000::1]:443/",
        "web",
    )
    assert decision.target.value == "2001:db8::1"
    assert project_scope.target_is_allowed(policy, "2001:db8:2::/48", "internal")


def test_cidr_with_host_bits_is_rejected_instead_of_silently_widened():
    with pytest.raises(project_scope.ScopeValidationError, match="host bits set"):
        project_scope.normalize_scope(_restricted({"kind": "cidr", "value": "10.20.1.9/16"}))
    with pytest.raises(project_scope.TargetValidationError, match="host bits set"):
        project_scope.normalize_target("10.20.1.9/16", "internal")


@pytest.mark.parametrize(
    "target",
    [
        "ftp://example.com/file",
        "https://user:pass@example.com/",
        "https://example.com\\@attacker.test/",
        "https://example.com:99999/",
    ],
)
def test_ambiguous_or_unsupported_web_targets_fail_closed(target: str):
    with pytest.raises(project_scope.TargetValidationError):
        project_scope.evaluate_target("*", target, "web")


def test_out_of_scope_assertion_carries_the_normalized_decision():
    policy = _restricted(_domain("example.com", False))
    with pytest.raises(project_scope.TargetOutOfScopeError) as caught:
        project_scope.assert_target_allowed(policy, "https://other.example", "web")
    assert caught.value.decision.allowed is False
    assert caught.value.decision.target.value == "other.example"


def test_unknown_fields_and_scan_types_are_rejected():
    with pytest.raises(project_scope.ScopeValidationError, match="unknown fields"):
        project_scope.normalize_scope(
            {"mode": "unrestricted", "entries": [{"kind": "any", "value": "*"}], "oops": True}
        )
    with pytest.raises(project_scope.TargetValidationError, match="scan_type"):
        project_scope.normalize_target("example.com", "external")


def test_non_scalar_mode_and_kind_are_reported_as_validation_errors():
    with pytest.raises(project_scope.ScopeValidationError, match="scope.mode"):
        project_scope.normalize_scope({"mode": [], "entries": [{"kind": "any", "value": "*"}]})
    with pytest.raises(project_scope.ScopeValidationError, match="kind must be"):
        project_scope.normalize_scope({"mode": "restricted", "entries": [{"kind": []}]})


@pytest.mark.parametrize(
    "target",
    ["https://example.com../", "fe80::1%en0"],
)
def test_noncanonical_hostname_and_scoped_ipv6_fail_closed(target: str):
    with pytest.raises(project_scope.TargetValidationError):
        project_scope.evaluate_target("*", target, "web")
