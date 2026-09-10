"""Immutable request-test report snapshots with explicit evidence provenance."""

from __future__ import annotations

import hashlib
import json

from strixops.traffic.store import new_id, now


def _text(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")


def build_report(task: dict, endpoints: list[dict], jobs: list[dict]) -> dict:
    cutoff = now()
    sources = [
        {
            "id": job["id"],
            "status": job["status"],
            "flow_ids": job["flow_ids"],
            "result": job.get("result"),
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
        lines += [
            f"### `{job['id']}`",
            "",
            f"- 狀態：{_text(job['status'])}",
            "- 原始請求：" + ", ".join(f"`{flow_id}`" for flow_id in job["flow_ids"]),
            "",
            _text(result.get("summary") or job.get("error") or "測試尚未完成"),
            "",
        ]
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
        "未完成、阻塞與失敗的測試均不代表通過。HTTP 模式不提供 DOM 執行或原始碼分析。",
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
