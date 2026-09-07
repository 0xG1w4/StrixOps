"""web_search — Perplexity-backed security-focused web search.

Optional tool: gives agents real-time CVE/CWE/exploit lookup. Requires
``PERPLEXITY_API_KEY`` in the engine environment; without it the tool
returns a graceful "not configured" result (never kills the run).
"""

from __future__ import annotations

import json
import logging
import os

import requests
from agents import RunContextWrapper, function_tool

logger = logging.getLogger("strixops.web_search")

_PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_PERPLEXITY_MODEL = "sonar"
_TIMEOUT = 30

_SECURITY_SYSTEM_PROMPT = """You are assisting a cybersecurity agent specialized in vulnerability
scanning and security assessment running on Kali Linux. When responding to search queries:

1. Prioritize cybersecurity-relevant information including:
   - Vulnerability details (CVEs, CVSS scores, impact)
   - Security tools, techniques, and methodologies
   - Exploit information and proof-of-concepts
   - Security best practices and mitigations
   - Penetration testing approaches

2. Provide technical depth appropriate for security professionals
3. Include specific versions, configurations, and technical details when available
4. Focus on actionable intelligence for security assessment
5. Cite reliable security sources (NIST, OWASP, CVE databases, security vendors)
6. When providing commands, prioritize Kali Linux compatibility
7. Be detailed and specific — include concrete code examples, command-line
   instructions, or configuration snippets when applicable"""


def _api_key() -> str:
    return (os.environ.get("PERPLEXITY_API_KEY") or "").strip()


@function_tool(strict_mode=False)
def web_search(ctx: RunContextWrapper, query: str) -> str:
    """Search the web for security information (CVEs, exploits, mitigations).

    Use this when you need current vulnerability data, exploit details, or
    security documentation that your training data may not have.

    Args:
        query: Search query — be specific (e.g. "WordPress 6.4.3 stored XSS
            CVE 2024" not just "WordPress XSS").
    """
    key = _api_key()
    if not key:
        return json.dumps(
            {
                "success": False,
                "error": ("Web search is not configured (set PERPLEXITY_API_KEY). Proceed without it."),
            }
        )
    if not query.strip():
        return json.dumps({"success": False, "error": "Query cannot be empty"})

    try:
        resp = requests.post(
            _PERPLEXITY_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": _PERPLEXITY_MODEL,
                "messages": [
                    {"role": "system", "content": _SECURITY_SYSTEM_PROMPT},
                    {"role": "user", "content": query.strip()},
                ],
                "max_tokens": 2000,
                "temperature": 0.1,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])

        return json.dumps(
            {
                "success": True,
                "answer": content,
                "citations": citations or [],
            }
        )
    except requests.Timeout:
        return json.dumps({"success": False, "error": "Search timed out (30s)"})
    except requests.RequestException as exc:
        return json.dumps({"success": False, "error": f"Search failed: {exc}"})
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        return json.dumps({"success": False, "error": f"Unexpected response format: {exc}"})
