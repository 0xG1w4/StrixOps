"""Optional asynchronous web search that never makes provider failure a scan failure."""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from strixops.config.web_search import from_env
from strixops.engine.scanconfig import EngineContext
from strixops.tools.web_search_runtime import SearchState, search


@function_tool(strict_mode=False)
async def web_search(ctx: RunContextWrapper[EngineContext], query: str) -> str:
    """Search current security documentation, CVEs, and mitigation references.

    Search is optional: on skipped/error results continue the assessment using
    existing evidence and other tools. Do not repeat an unavailable search.

    Args:
        query: A specific public security-reference query, without credentials
            or private target data.
    """
    context = ctx.context
    services = getattr(context, "services", None)
    state = getattr(services, "web_search_state", None)
    if state is None:
        run_state = getattr(context, "run_state", None) or getattr(services, "run_state", None)
        state = SearchState(
            run_dir=getattr(run_state, "run_dir", None),
            events=getattr(services, "events", None) or getattr(run_state, "events", None),
        )
        if services is not None:
            services.web_search_state = state
    result = await search(query, from_env(), state, agent_id=getattr(context, "agent_id", ""))
    return json.dumps(result, ensure_ascii=False)
