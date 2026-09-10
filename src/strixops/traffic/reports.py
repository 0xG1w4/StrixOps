"""Immutable request-test report snapshots with explicit evidence provenance."""

from __future__ import annotations

import hashlib
import json
import math

from strixops.traffic.store import new_id, now


def _text(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")


_REASONS = {
    "completed": "測試流程已完成",
    "time_budget_limited": "已使用預留時間收尾，保留部分結果",
    "time_budget_exceeded": "時間預算已用盡，測試未完成",
    "model_error": "模型執行未完成",
    "incomplete_lifecycle": "尚未完成測試結論",
    "cancelled": "測試已取消",
    "partial": "僅完成部分測試",
}


def _completion_reason(job: dict) -> str:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    reason = result.get("completion_reason") or diagnostics.get("reason")
    if isinstance(reason, str) and reason in _REASONS:
        return reason
    if any(
        isinstance(error, str) and error.strip().casefold().startswith("time budget exceeded")
        for error in (job.get("error"), result.get("error"))
    ):
        return "time_budget_exceeded"
    if result.get("partial") is True:
        return "partial"
    return {"completed": "completed", "cancelled": "cancelled"}.get(job.get("status"), "")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1_000_000 or not math.isfinite(value):
        return None
    return float(value)


def _diagnostic_lines(result: dict) -> list[str]:
    diagnostics = result.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return []
    lines, timing, activity = [], [], []
    for field, label in (("elapsed_seconds", "已用"), ("budget_seconds", "上限")):
        value = _number(diagnostics.get(field))
        if value is not None:
            timing.append(f"{label} {value:g} 秒")
    if timing:
        lines.append("- 時間預算：" + "／".join(timing))
    for field, label in (("request_count", "重送"), ("model_rounds", "模型呼叫")):
        value = _number(diagnostics.get(field))
        if value is not None and value.is_integer():
            activity.append(f"{label} {value:g} 次")
    rounds = diagnostics.get("rounds")
    if isinstance(rounds, list):
        durations = [
            value
            for row in rounds[:200]
            if isinstance(row, dict) and (value := _number(row.get("duration_seconds"))) is not None
        ]
        if durations:
            activity.append(f"模型耗時合計 {sum(durations):g} 秒（含中止的呼叫）")
    if activity:
        lines.append("- 執行記錄：" + "；".join(activity))
    phase = {
        "initialization": "初始化",
        "model": "模型",
        "tool": "工具",
        "replay": "重送",
        "wrapup": "收尾",
        "completion": "完成",
    }.get(str(diagnostics.get("phase")), "")
    if phase:
        stage = "收尾／" if diagnostics.get("stage") == "wrapup" and phase != "收尾" else ""
        lines.append(f"- 結束階段：{stage}{phase}")
    return lines


def build_report(task: dict, endpoints: list[dict], jobs: list[dict]) -> dict:
    cutoff = now()
    sources = [
        {
            "id": job["id"],
            "status": job["status"],
            "flow_ids": job["flow_ids"],
            "result": job.get("result"),
            "completion_reason": _completion_reason(job),
            "prompt_sha256": (job.get("prompt_snapshot") or {}).get("sha256"),
        }
        for job in jobs
    ]
    digest = hashlib.sha256(
        json.dumps({"endpoints": endpoints, "tests": sources}, sort_keys=True).encode()
    ).hexdigest()
    lines = [
        f"# {_text(task['name'])} — MCP 測試報告",
        "",
        f"- 產生時間：{cutoff}",
        f"- 任務：`{task['id']}`",
        f"- 資料快照：`{digest}`",
        "",
        "此報告只涵蓋本次已觀察流量及明確執行的測試；未觀察或未測試的網站功能不在結論內。",
        "",
        "## 觀察範圍",
        "",
        "- 允許網站：" + ", ".join(map(_text, task["allow_hosts"])),
        "- 排除網站：" + (", ".join(map(_text, task["exclude_hosts"])) or "無"),
        "",
        "| 方法 | 主機 | 已觀察路徑／推測分組 | 樣本數 | 證據 |",
        "|---|---|---|---:|---|",
    ]
    for endpoint in endpoints:
        lines.append(
            f"| {_text(endpoint['method'])} | {_text(endpoint['host'])} | {_text(endpoint['path'])} | "
            f"{endpoint['count']} | `{endpoint['latest_flow_id']}` |"
        )
    if not endpoints:
        lines.append("| — | — | 尚無捕獲流量 | 0 | — |")
    lines += ["", "## 測試結果", ""]
    if not jobs:
        lines.append("尚未執行 Agent 測試；流量盤點不代表已驗證安全性。")
    for job in reversed(jobs):
        result = job.get("result") or {}
        reason = _completion_reason(job)
        partial = result.get("partial") is True or reason in {
            "partial",
            "time_budget_limited",
            "time_budget_exceeded",
        }
        status = _text(job["status"])
        if reason == "time_budget_exceeded":
            status = "時間預算已用盡（測試未完成，保留部分結果）"
        elif partial and job["status"] == "completed":
            status = "部分完成（已收尾，仍需後續驗證）"
        summary = result.get("summary")
        if not summary or summary in (job.get("error"), result.get("error")):
            summary = _REASONS.get(reason, "測試尚未完成")
        lines += [
            f"### `{job['id']}`",
            "",
            f"- 狀態：{status}",
            "- 原始請求：" + ", ".join(f"`{flow_id}`" for flow_id in job["flow_ids"]),
        ]
        if reason in _REASONS:
            lines.append("- 結束原因：" + _REASONS[reason])
        lines += _diagnostic_lines(result)
        if partial:
            lines.append("- 結論限制：僅保留已完成的檢查與證據；未完成項目仍需後續驗證，不代表通過。")
        lines += ["", _text(summary), ""]
        for finding in result.get("findings", []):
            lines += [
                f"- **{_text(finding.get('severity'))} — {_text(finding.get('title'))}**",
                "  " + _text(finding.get("description") or finding.get("observation")),
                "  證據：" + ", ".join(map(_text, finding.get("evidence_flow_ids", []))),
            ]
            for field, label in (
                ("evidence", "驗證內容"),
                ("counterevidence", "反證檢查"),
                ("impact", "影響"),
                ("remediation", "修正建議"),
            ):
                if finding.get(field):
                    lines += ["", "  " + label + "：" + _text(finding[field])]
            lines.append("")
        for coverage in result.get("coverage", []):
            outcome = {
                "reported": "已記錄發現",
                "no_issue_found": "本次未發現問題",
                "not_applicable": "不適用",
                "needs_follow_up": "需要後續驗證",
            }.get(coverage.get("outcome"), coverage.get("outcome", "未完成"))
            lines += [
                f"- **{_text(coverage.get('risk_area'))}**：{_text(outcome)}",
                f"  請求：`{_text(coverage.get('flow_id'))}`。{_text(coverage.get('evidence'))}",
                "",
            ]
    selected = {flow_id for job in jobs for flow_id in job["flow_ids"]}
    lines += [
        "",
        "## 覆蓋與限制",
        "",
        f"已保存 {task['counts']['flows']} 筆流量（含重送）；選取 {len(selected)} 筆原始請求進入測試。",
        "端點分組是路徑推論，樣本數不代表測試次數；未選取的請求不視為已測試。",
        "部分完成、時間預算用盡、未完成、阻塞與失敗的測試均不代表通過。"
        "HTTP 模式不提供 DOM 執行或原始碼分析。",
        "本文預設隱藏憑證；原始證據保存在本機任務資料中。",
    ]
    for session in task.get("sessions", []):
        if session.get("ingest_error") or session.get("error"):
            lines.append("捕獲限制：" + _text(session.get("ingest_error") or session.get("error")))
    return {
        "id": new_id("report"),
        "task_id": task["id"],
        "created_at": cutoff,
        "source_sha256": digest,
        "sources": sources,
        "markdown": "\n".join(lines) + "\n",
    }
