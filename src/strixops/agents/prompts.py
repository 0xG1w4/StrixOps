"""System prompts for root and child agents.

All static prompt text lives in editable ``prompt_parts/*.md`` files next to
this module (loaded per call — edits take effect on the next scan with no
rebuild), with the module-level defaults below as fallback when a file is
missing or unreadable. The console's /skills page edits those files.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from strixops import skills as skill_registry
from strixops.engine.scanconfig import ScanSpec, authorized_target_line, multi_target_instruction

PROMPT_PARTS_DIR = Path(__file__).parent / "prompt_parts"

_DEFAULTS: dict[str, str] = {}

_LANGUAGE_FILES = {"zh": "language_zh.md", "en": "language_en.md"}
_FROZEN_PARTS: ContextVar[dict[str, str] | None] = ContextVar("strixops_prompt_parts", default=None)


@contextmanager
def use_prompt_parts(parts: dict[str, str]):
    token = _FROZEN_PARTS.set(parts)
    try:
        yield
    finally:
        _FROZEN_PARTS.reset(token)


def load_part(name: str, default: str = "") -> str:
    """Read a prompt part file; fall back to the embedded default."""
    frozen = _FROZEN_PARTS.get()
    if frozen is not None:
        return frozen.get(name, default).strip()
    path = PROMPT_PARTS_DIR / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    except OSError:
        pass
    return default.strip() if default else ""


def _part(name: str) -> str:
    content = load_part(name, _DEFAULTS.get(name, ""))
    if not content:
        raise ValueError(f"Required prompt part is missing, empty or unreadable: {name}")
    return content


def _environment_part() -> str:
    """Keep pre-environment snapshots intact without reading newer live text."""
    frozen = _FROZEN_PARTS.get()
    if frozen is not None and "environment.md" not in frozen:
        return ""
    return _part("environment.md")


def list_prompt_parts() -> list[dict]:
    """Inventory for the console editor."""
    out: list[dict] = []
    for path in sorted(PROMPT_PARTS_DIR.glob("*.md")):
        out.append(
            {
                "name": path.stem,
                "file": path.name,
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
            }
        )
    return out


def read_prompt_part(name: str) -> str | None:
    path = (PROMPT_PARTS_DIR / f"{name}.md").resolve()
    if not path.is_relative_to(PROMPT_PARTS_DIR.resolve()) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def write_prompt_part(name: str, content: str) -> bool:
    path = (PROMPT_PARTS_DIR / f"{name}.md").resolve()
    if not path.is_relative_to(PROMPT_PARTS_DIR.resolve()):
        return False
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def language_instruction(report_language: str) -> str:
    part = _LANGUAGE_FILES.get("zh" if report_language.startswith("zh") else "en")
    if part:
        return _part(part)
    return ""


def engagement_context(spec: ScanSpec | None) -> str:
    """Immutable assignment essentials, independent of parent conversation history."""
    if spec is None:
        return (
            "ENGAGEMENT CONTEXT UNAVAILABLE\n"
            "No verified target scope or access information was supplied. Do not probe until "
            "your parent supplies the authorized scope and operator constraints."
        )
    sections = [
        _part("scope_frame.md").format(targets=authorized_target_line(spec)),
        "EXECUTION CONTEXT\n"
        "exec_command starts in the local isolated sandbox, not on a target host. "
        "Initial verified target host: none. Initial verified remote session: none. "
        "Before attributing command output to a target, verify the active remote session, "
        "hostname/address and effective identity; record that evidence. Tool/session handles "
        "from another agent's history are background and must be revalidated before reuse.",
    ]
    if coordination := multi_target_instruction(spec):
        sections.append(coordination)
    if spec.scan_type == "internal":
        if spec.socks5_proxy:
            sections.append(
                "DECLARED ACCESS TYPE: socks5_network\n"
                f"SOCKS5 proxy: {spec.socks5_proxy}\n"
                "This provides a network transport only; it does not establish a shell, "
                "a compromised host, credentials or privileges. Tools still execute in the "
                "sandbox. Verify routing and supported protocols before using it for "
                "in-scope network requests. Any later remote shell requires separate verification."
            )
        if spec.gsocket_key:
            sections.append(
                "DECLARED ACCESS TYPE: gsocket_claimed_shell\n"
                f"Operator-provided GSocket key: {spec.gsocket_key}\n"
                "A remote shell is claimed but not yet verified. Confirm the remote service "
                "mode, connection, execution host, effective identity and session before "
                "treating it as shell access. A key alone establishes no capability. "
                "Adding -p to a shell connection does not automatically create a SOCKS proxy; "
                "SOCKS requires a separately confirmed matching remote service mode. "
                "If connection verification fails, report the blocker; local sandbox commands "
                "are not an on-host fallback. Do not replace the supplied relay/key."
            )
        if not spec.socks5_proxy and not spec.gsocket_key:
            sections.append(
                "DECLARED ACCESS TYPE: unspecified\n"
                "No shell, tunnel or credentials were supplied. Establish what network "
                "access is actually available within scope before selecting a technique."
            )
        if spec.crypto:
            sections.append(
                "CRYPTO MODE: assess cryptocurrency-related assets within the same target scope "
                "and operator constraints. Record exposure; do not transfer assets."
            )
    else:
        sections.append("DECLARED ACCESS TYPE: web_network; no remote shell has been established.")
    sections.append(
        "OPERATOR INSTRUCTIONS AND CONSTRAINTS\n"
        + (spec.instruction_text.strip() or "No additional operator instructions were supplied.")
    )
    return "\n\n".join(sections)


def root_instructions(spec: ScanSpec) -> str:
    sections: list[str] = [
        _part("root_directive.md"),
        _part("root_orchestration.md"),
        engagement_context(spec),
        _part("internal_mode.md").format(extras="") if spec.scan_type == "internal" else _part("web_mode.md"),
    ]
    sections += [
        _part("tooling.md"),
        _environment_part(),
        language_instruction(spec.report_language),
        _part("common_tail.md"),
    ]
    prompt = "\n\n".join(s for s in sections if s)
    return _with_skills(prompt, skill_registry.default_root_skills(spec.scan_type))


def child_instructions(task: str, spec: ScanSpec | None = None, skills: list[str] | None = None) -> str:
    internal_extra = (
        " / create_internal_finding for internal discoveries — report each distinct discovery promptly; "
        "datasets may use complete attachments"
        if spec is not None and spec.scan_type == "internal"
        else ""
    )
    sections = [
        _part("child_frame.md").format(task=task, internal_extra=internal_extra),
        engagement_context(spec),
        _part("tooling.md"),
        _environment_part(),
        language_instruction(spec.report_language) if spec is not None else "",
        _part("common_tail.md"),
    ]
    prompt = "\n\n".join(s for s in sections if s)
    # Skills the parent requested via create_agent(skills=[...]) are injected
    # into the child's prompt up front (mirroring the reference behavior —
    # recording them without injecting left the child without the playbook).
    required = skill_registry.default_child_skills(spec.scan_type) if spec is not None else []
    return _with_skills(prompt, [*required, *(skills or [])])


def _with_skills(prompt: str, skill_names: list[str]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for name in skill_names:
        canonical_id = skill_registry.canonical_skill_id(name)
        if canonical_id in seen:
            continue
        content = skill_registry.load_skill_markdown(canonical_id)
        if not content:
            raise skill_registry.SkillResolutionError(
                f"Required skill is empty or unavailable: {canonical_id}"
            )
        parts.append(f"===== SKILL: {canonical_id} =====\n{content}")
        seen.add(canonical_id)
    if not parts:
        return prompt
    return prompt + "\n\n" + "\n\n".join(parts)
