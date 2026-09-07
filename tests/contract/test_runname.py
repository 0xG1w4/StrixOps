"""Run-name generation must bind under the platform's resolver oracle."""

from __future__ import annotations

import pytest

from strixops.platform.runname import generate_run_name, slugify_for_run_name

from .oracles import looks_like_run_dir, run_name_matches

# (target, scan_type, safe_name-ish values the platform derives) — mirrors
# how supervisor.run_name_matches is called with the task's safe name and
# the raw target.
TARGET_CORPUS = [
    ("https://example.com", "web", "example_com"),
    ("https://example.com", "web", "example.com"),
    ("http://sub.example.com:8080", "web", "sub_example_com_8080"),
    ("https://api.victim.test", "web", "api_victim_test"),
    ("10.10.10.5", "internal", "10_10_10_5"),
    ("192.168.1.100", "internal", "192_168_1_100"),
    ("corp-dc01.internal.lan", "internal", "corp-dc01_internal_lan"),
    (
        "https://a-very-long-subdomain-name-that-exceeds-thirty-two-characters.example.com",
        "web",
        "a-very-long-subdomain-name-that-exceeds-thir",
    ),
]


@pytest.mark.parametrize(("target", "scan_type", "safe_name"), TARGET_CORPUS)
def test_generated_name_matches_platform_resolver(target: str, scan_type: str, safe_name: str) -> None:
    for _ in range(20):  # random suffix each call — match must hold for all
        name = generate_run_name(target, scan_type)
        assert looks_like_run_dir(name), name
        assert run_name_matches(name, safe_name, target), f"{name} not matched by ({safe_name!r}, {target!r})"


def test_slug_rules() -> None:
    assert slugify_for_run_name("Example.COM") == "example-com"
    assert slugify_for_run_name("sub.example.com:8080") == "sub-example-com-8080"
    assert slugify_for_run_name("---") == "pentest"
    assert len(slugify_for_run_name("a" * 100)) <= 32
    assert slugify_for_run_name("a" * 40) == "a" * 32


def test_url_with_port_slug() -> None:
    name = generate_run_name("http://sub.example.com:8080", "web")
    # netloc includes the port; run name joins slug and hex suffix with '_'
    assert name.startswith("sub-example-com-8080_")
    assert run_name_matches(name, "sub_example_com_8080", "http://sub.example.com:8080")
