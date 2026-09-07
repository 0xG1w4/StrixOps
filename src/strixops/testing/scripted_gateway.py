"""Scripted OpenAI-compatible gateway for zero-token dry runs.

A threaded ``HTTPServer`` speaks just enough of the chat-completions
protocol to drive the real SDK model path: every ``POST`` returns the next
scripted completion (tool calls or plain text). The engine then uses the
platform Chat Completions model against ``http://127.0.0.1:<port>/v1``,
wrapped by :class:`NonStreamingModel` so each turn is one non-streaming
request whose result is replayed as a single terminal stream event — the
same pattern the reference implementation uses for SSE-hostile gateways.

Script format (JSON list of turns)::

    [
      {"tool_calls": [{"name": "think", "arguments": {"thought": "..."}}]},
      {"text": "message shown in the conversation"}
    ]
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from agents import Model, ModelResponse, ModelSettings, ModelTracing, Tool
from agents.items import TResponseInputItem
from agents.models.fake_id import FAKE_RESPONSES_ID
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseCompletedEvent, ResponseUsage

from strixops.config.vision_transport import PlatformChatCompletionsModel


class ScriptedGateway:
    """Serve per-model scripted chat-completion sequences over local HTTP.

    ``scripts`` maps model name → turn list; requests are routed by the
    ``model`` field of the chat-completions body so concurrent agents
    (root + children) each consume their own script. Unknown models fall
    back to the ``"default"`` script when present.
    """

    def __init__(self, scripts: dict[str, list[dict[str, Any]]]) -> None:
        self._scripts: dict[str, list[dict[str, Any]]] = {
            name: list(turns) for name, turns in scripts.items()
        }
        self._served: dict[str, int] = {}
        self._lock = threading.Lock()
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body: dict[str, Any] = {}
                if length:
                    try:
                        import json as _json

                        body = _json.loads(self.rfile.read(length) or b"{}")
                    except Exception:
                        body = {}
                model = str(body.get("model") or "")
                with gateway._lock:
                    turns = gateway._scripts.get(model) or gateway._scripts.get("default")
                    if turns is None:
                        turns = [{"text": f"no script for model {model!r}"}]
                        gateway._scripts[model] = turns
                    index = gateway._served.get(model, 0)
                    gateway._served[model] = index + 1
                turn = turns[index] if index < len(turns) else {"text": "script exhausted"}
                delay = float(turn.get("delay_seconds") or 0)
                if delay > 0:
                    # deterministic mid-run pause (hint-interruption tests)
                    import time as _time

                    _time.sleep(delay)
                payload = json.dumps(_completion(turn, index)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def served(self, model: str) -> int:
        with self._lock:
            return self._served.get(model, 0)

    def make_model(self, model_name: str) -> NonStreamingModel:
        return make_scripted_model(self, model_name)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _completion(turn: dict[str, Any], index: int) -> dict[str, Any]:
    tool_calls = turn.get("tool_calls")
    if tool_calls:
        # A turn may carry narration text alongside its tool calls — every
        # scripted turn keeps the run alive until a lifecycle tool fires.
        message: dict[str, Any] = {
            "role": "assistant",
            "content": turn.get("text") or None,
            "tool_calls": [
                {
                    "id": f"call_{index}_{i}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments", {})),
                    },
                }
                for i, call in enumerate(tool_calls)
            ],
        }
        return {
            "id": f"chatcmpl-{index}",
            "object": "chat.completion",
            "created": 0,
            "model": "strixops-dry-run",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }
    return {
        "id": f"chatcmpl-{index}",
        "object": "chat.completion",
        "created": 0,
        "model": "strixops-dry-run",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": str(turn.get("text", ""))},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


def make_scripted_model(gateway: ScriptedGateway, model_name: str = "strixops-dry-run") -> NonStreamingModel:
    inner = PlatformChatCompletionsModel(
        model=model_name,
        openai_client=AsyncOpenAI(base_url=gateway.base_url, api_key="strixops-dry-run"),
    )
    return NonStreamingModel(inner)


class NonStreamingModel(Model):
    """Serve the SDK's streamed run loop from single non-streaming requests.

    Each ``stream_response`` call performs one ``get_response`` (``stream:
    false`` on the wire) and replays the completed result as a single
    terminal stream event; the run loop executes tools and emits run items
    from it exactly as for a real stream.
    """

    def __init__(self, inner: Model) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    async def close(self) -> None:
        await self._inner.close()

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any = None,
    ) -> ModelResponse:
        return await self._inner.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any = None,
    ):
        response = await self.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        yield completed_stream_event(response, self.model)


def completed_stream_event(model_response: ModelResponse, model_name: str) -> ResponseCompletedEvent:
    """Wrap a non-streamed ``ModelResponse`` as the terminal stream event."""
    usage = model_response.usage
    response = Response(
        id=model_response.response_id or FAKE_RESPONSES_ID,
        created_at=time.time(),
        model=model_name or "",
        object="response",
        output=list(model_response.output),
        tool_choice="auto",
        tools=[],
        parallel_tool_calls=False,
        usage=ResponseUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            input_tokens_details=usage.input_tokens_details,
            output_tokens_details=usage.output_tokens_details,
        )
        if usage is not None
        else None,
    )
    return ResponseCompletedEvent(response=response, sequence_number=0, type="response.completed")


def web_script(target: str = "https://example.com") -> list[dict[str, Any]]:
    """A representative web-scan script: think → plan → finding → finish.

    Every turn carries a tool call so the SDK run loop keeps going (a
    text-only response would be treated as the final output and end the
    run); narration rides along as message content.
    """
    return [
        {
            "text": f"Starting authorized assessment of {target}. Reconnaissance first.",
            "tool_calls": [
                {
                    "name": "think",
                    "arguments": {
                        "thought": "Plan: map the attack surface, probe auth flows, verify findings with PoCs before filing."
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "create_todo",
                    "arguments": {
                        "todos": json.dumps(
                            [
                                {"title": "Map attack surface", "status": "in_progress", "priority": "high"},
                                {
                                    "title": "Test authentication flows",
                                    "status": "pending",
                                    "priority": "high",
                                },
                            ]
                        )
                    },
                }
            ]
        },
        {
            "text": "Reconnaissance complete. Delegating hands-on probing to a scanner child agent.",
            "tool_calls": [
                {
                    "name": "create_agent",
                    "arguments": {
                        "name": "scanner",
                        "task": "Probe the search and authentication flows on the authorized target; validate and file any findings with working PoCs.",
                        "inherit_context": True,
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "wait_for_agents",
                    "arguments": {"reason": "scanner probing target", "timeout_seconds": 120},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "finish_scan",
                    "arguments": {
                        "executive_summary": "本次评估在 /search 参数验证了一处反射型XSS，整体风险为中危。",
                        "methodology": "侦察、委派 scanner 子代理探测、人工PoC验证。",
                        "technical_analysis": "q 参数未经上下文编码直接渲染，导致脚本注入。",
                        "recommendations": "按上下文编码输出；部署CSP；补充反射型XSS回归测试。",
                        "overall_severity": "Medium",
                        "severity_rationale": "存在可利用的反射型XSS，但需要用户交互触发。",
                        "battle_gains": "确认搜索参数注入点，可构造钓鱼链接窃取会话。",
                        "attack_narrative": "侦察发现搜索端点 → 参数回显验证 → PoC触发alert确认。",
                        "future_leverage": "可结合钓鱼场景升级为会话劫持。",
                        "limitations": "未覆盖API层面与认证流程深度测试。",
                    },
                }
            ]
        },
    ]


def web_script_with_shell(target: str = "https://example.com") -> list[dict[str, Any]]:
    """Root script exercising the sandbox Shell capability: exec → delegate → wait → finish."""
    return [
        {
            "text": f"Starting authorized assessment of {target}; verifying sandbox tooling.",
            "tool_calls": [
                {
                    "name": "exec_command",
                    "arguments": {
                        "cmd": "echo strixops-sandbox-ok && uname -a && ls /workspace",
                        "timeout_seconds": 30,
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "create_agent",
                    "arguments": {
                        "name": "scanner",
                        "task": "Probe the authorized target and file validated findings.",
                        "inherit_context": True,
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "wait_for_agents",
                    "arguments": {"reason": "scanner probing target", "timeout_seconds": 120},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "finish_scan",
                    "arguments": {
                        "executive_summary": "Sandbox-verified scan complete.",
                        "methodology": "Sandbox shell verification, delegated probing.",
                        "technical_analysis": "Shell capability exercised inside the container.",
                        "recommendations": "Continue with deeper methodology milestones.",
                    },
                }
            ]
        },
    ]


def internal_script(target: str = "10.10.10.5", socks5: str = "") -> list[dict[str, Any]]:
    """Internal-mode script: verify tunnel env → immediate findings → finish."""
    env_check = "env | grep -E 'SOCKS5_PROXY|GSOCKET_KEY' || echo no-tunnel-env"
    return [
        {
            "text": f"Beginning internal assessment of {target}; verifying tunnel environment.",
            "tool_calls": [{"name": "exec_command", "arguments": {"cmd": env_check, "timeout_seconds": 30}}],
        },
        {
            "text": "First credentials recovered; reporting immediately per discipline.",
            "tool_calls": [
                {
                    "name": "create_internal_finding",
                    "arguments": {
                        "finding_type": "credential",
                        "title": "Local administrator NTLM hash",
                        "content": "admin:1000:aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0:::",
                        "host": target,
                        "source": "secretsdump.py via provided tunnel",
                        "severity": "critical",
                    },
                }
            ],
        },
        {
            "text": "Domain trust mapped; reporting the topology finding.",
            "tool_calls": [
                {
                    "name": "create_internal_finding",
                    "arguments": {
                        "finding_type": "architecture",
                        "title": "Domain trust path to CORP forest",
                        "content": "CHILD.CORP.LOCAL has bidirectional trust with CORP.LOCAL; "
                        "krbtgt hash recoverable via DCSync from current privileges.",
                        "host": "dc01.child.corp.local",
                        "source": "BloodHound collection analysis",
                        "severity": "high",
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "finish_scan",
                    "arguments": {
                        "executive_summary": "Internal assessment complete: domain admin path "
                        "confirmed via DCSync; two internal findings recorded.",
                        "methodology": "Tunnel verification, credential dumping, trust mapping.",
                        "technical_analysis": "DCSync rights on the domain controller enable "
                        "full domain compromise.",
                        "recommendations": "Rotate krbtgt twice; constrain DCSync rights; "
                        "segment the forest trust.",
                    },
                }
            ]
        },
    ]


def hint_scenario_scripts(token: str = "echo12345") -> dict[str, list[dict[str, Any]]]:
    """Hint-interruption scenario: root waits, an operator hint wakes it
    (cancel + mailbox), root echoes the token, then finishes after the child.

    Root script: think → spawn → wait → [interrupted] → echo+think → wait →
    finish. Child script: think (4 s delay) → agent_finish.
    """
    root = [
        {"tool_calls": [{"name": "think", "arguments": {"thought": "Delegating probing."}}]},
        {
            "tool_calls": [
                {
                    "name": "create_agent",
                    "arguments": {
                        "name": "scanner",
                        "task": "Probe the authorized target.",
                        "inherit_context": True,
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {"name": "wait_for_agents", "arguments": {"reason": "probing", "timeout_seconds": 120}}
            ]
        },
        # wake turn after the hint interrupt: echo the token, keep working
        {
            "text": f"[hint:{token}] acknowledged — operator instruction received.",
            "tool_calls": [{"name": "think", "arguments": {"thought": "Applying operator guidance."}}],
        },
        {
            "tool_calls": [
                {
                    "name": "wait_for_agents",
                    "arguments": {"reason": "final collection", "timeout_seconds": 120},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "finish_scan",
                    "arguments": {
                        "executive_summary": "Scan complete after operator steering.",
                        "methodology": "Delegated probing with operator guidance.",
                        "technical_analysis": "Operator hint incorporated mid-run.",
                        "recommendations": "Maintain operator steering channel.",
                    },
                }
            ]
        },
    ]
    child = [
        {"delay_seconds": 4, "tool_calls": [{"name": "think", "arguments": {"thought": "Probing slowly."}}]},
        {
            "tool_calls": [
                {
                    "name": "agent_finish",
                    "arguments": {"result_summary": "Probing complete.", "success": True},
                }
            ]
        },
    ]
    return {"strixops-dry-run": root, "child": child}


def child_script(target: str = "https://example.com") -> list[dict[str, Any]]:
    """Child-agent script: think → file the finding → agent_finish."""
    return [
        {
            "text": "Probing the authorized target now.",
            "tool_calls": [
                {
                    "name": "think",
                    "arguments": {"thought": "Test /search parameter reflection first, then auth flows."},
                }
            ],
        },
        {
            "text": "One issue validated with a working PoC; filing the report.",
            "tool_calls": [
                {
                    "name": "create_vulnerability_report",
                    "arguments": {
                        "title": "Reflected XSS in search parameter",
                        "description": "The q parameter of /search is echoed unescaped into the page.",
                        "impact": "Session theft via crafted link; same-origin script execution.",
                        "target": target,
                        "technical_analysis": "Missing output encoding on the q parameter render path.",
                        "poc_description": "Submit <script>alert(1)</script> as q and observe execution.",
                        "poc_script_code": "curl -G https://example.com/search --data-urlencode 'q=<script>alert(1)</script>'",
                        "remediation_steps": "Context-aware output encoding; CSP as a second line of defense.",
                        "evidence": "alert(1) executed in browser on submit.",
                        "assumptions": "No WAF normalization in front of the parameter.",
                        "counterevidence": "Tried the same payload in the id parameter — properly encoded there.",
                        "confidence": "High — reproduced three times, deterministic.",
                        "severity_change_conditions": "Would drop if session cookies are HttpOnly and CSP blocks inline script.",
                        "fix_effort": "low",
                        "cvss_breakdown": {
                            "attack_vector": "N",
                            "attack_complexity": "L",
                            "privileges_required": "N",
                            "user_interaction": "R",
                            "scope": "U",
                            "confidentiality": "L",
                            "integrity": "L",
                            "availability": "N",
                        },
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "agent_finish",
                    "arguments": {
                        "result_summary": "Probed search and auth flows. One reflected XSS validated on /search and filed as vuln report.",
                        "findings": "Reflected XSS in /search q parameter (filed).",
                        "open_items": "Auth flows looked hardened; nothing further pending.",
                        "success": True,
                    },
                }
            ],
        },
    ]
