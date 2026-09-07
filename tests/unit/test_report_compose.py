"""Executive report composition: platform-format header, sections, languages."""

from __future__ import annotations

import json
from pathlib import Path

from strixops.platform.events import EventWriter
from strixops.report.state import RunState


def _state(tmp_path: Path, language: str) -> RunState:
    events = EventWriter(tmp_path)
    state = RunState(tmp_path, events)
    state.set_scan_config(
        {"target": "https://t.example.com", "scan_type": "web", "report_language": language}
    )
    return state


def _finish(state: RunState, **extra) -> None:
    state.update_final_fields(
        executive_summary="摘要内容",
        methodology="recon → probe → verify",
        technical_analysis="技术细节",
        recommendations="升级组件",
        **extra,
    )


def test_zh_report_platform_format(tmp_path: Path):
    state = _state(tmp_path, "zh-CN")
    state.add_vulnerability_report(
        {"id": "vuln-0001", "title": "反射型XSS", "severity": "medium"},
        agent_id="root",
        agent_name="root agent",
    )
    _finish(
        state,
        overall_severity="Medium",
        severity_rationale="存在可利用的反射型XSS。",
        battle_gains="获取会话伪造能力",
        limitations="未覆盖API层面",
    )
    state.write_executive_report()
    text = (tmp_path / "penetration_test_report.md").read_text(encoding="utf-8")

    assert text.startswith("# 渗透测试报告 - https://t.example.com")
    assert "目标：https://t.example.com" in text
    assert "任务类型：Web应用渗透测试" in text
    assert "报告产生时间：" in text
    assert "Overall Severity: Medium" in text
    assert "Severity Rationale: 存在可利用的反射型XSS。" in text
    assert "\n---\n" in text
    assert "## 执行摘要" in text
    assert "## 本阶段战果整理" in text
    assert "## 本阶段限制与未完成部分" in text
    assert "## 漏洞清单" in text
    assert "**[MEDIUM]** 反射型XSS" in text
    assert "## 修复建议" in text
    # empty optional sections are omitted
    assert "攻击路径与关键进展" not in text


def test_en_report_platform_format(tmp_path: Path):
    state = _state(tmp_path, "en")
    _finish(state, overall_severity="High", severity_rationale="RCE confirmed")
    state.write_executive_report()
    text = (tmp_path / "penetration_test_report.md").read_text(encoding="utf-8")

    assert text.startswith("# Penetration Test Report - https://t.example.com")
    assert "Target: https://t.example.com" in text
    assert "Engagement type: Web application penetration test" in text
    assert "Overall Severity: High" in text
    assert "## Executive Summary" in text
    assert "## Limitations & Unfinished Work" not in text  # omitted when empty


def test_internal_type_label(tmp_path: Path):
    state = RunState(tmp_path, EventWriter(tmp_path))
    state.set_scan_config({"target": "10.0.0.5", "scan_type": "internal", "report_language": "zh-CN"})
    _finish(state)
    state.write_executive_report()
    text = (tmp_path / "penetration_test_report.md").read_text(encoding="utf-8")
    assert "任务类型：内网渗透测试" in text


def test_severity_falls_back_to_findings(tmp_path: Path):
    state = _state(tmp_path, "zh-CN")
    state.add_vulnerability_report(
        {"id": "vuln-0001", "title": "RCE", "severity": "critical"}, agent_id="root", agent_name="root agent"
    )
    _finish(state)  # no overall_severity
    state.write_executive_report()
    text = (tmp_path / "penetration_test_report.md").read_text(encoding="utf-8")
    assert "Overall Severity: Critical" in text


def test_scan_results_recorded_rich(tmp_path: Path):
    state = _state(tmp_path, "zh-CN")
    _finish(state, battle_gains="战果", future_leverage="横向移动")
    state.mark_complete()
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["scan_results"]["battle_gains"] == "战果"
    assert record["scan_results"]["future_leverage"] == "横向移动"


def test_internal_only_critical_finding_sets_report_and_record_severity(tmp_path: Path):
    state = _state(tmp_path, "en")
    state.set_scan_config({"target": "fixture.invalid", "scan_type": "internal", "report_language": "en"})
    state.add_internal_finding(
        {"title": "Validated critical impact", "finding_type": "result", "severity": "Critical"},
        agent_id="root",
        agent_name="root",
    )
    _finish(state)
    state.write_executive_report()
    state.mark_complete()
    assert "Overall Severity: Critical" in (tmp_path / "penetration_test_report.md").read_text()
    assert json.loads((tmp_path / "run.json").read_text())["scan_results"]["overall_severity"] == "critical"


def test_severity_aggregation_considers_both_finding_channels(tmp_path: Path):
    state = _state(tmp_path, "en")
    state.add_vulnerability_report({"title": "High", "severity": "high"}, agent_id="root", agent_name="root")
    state.add_internal_finding(
        {"title": "Critical", "severity": "critical"}, agent_id="root", agent_name="root"
    )
    assert state._derived_severity() == "critical"
