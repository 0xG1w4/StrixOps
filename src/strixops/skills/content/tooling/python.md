---
name: python
description: Run Python through exec_command in the SDK sandbox. Use the image-baked caido_api module for Caido proxy automation from Python scripts.
---

# Python In The Sandbox

Use `exec_command` for Python. There is no separate Python executor.

Prefer writing reusable scripts to a `.py` file and running them with
`python3 <name>.py`. For short one-off transformations, `python3 -c` or a
small here-document is fine.

The `shell` parameter on `exec_command` is for swapping POSIX shells
(`bash`/`zsh`/`sh`), not for picking interpreters. Put the interpreter
invocation in `cmd` instead: `cmd="python3 -c '...'"`, not
`shell=python3, cmd="..."`. The `shell=<interpreter>` shortcut breaks
in subtle ways — `python3` works only with `login=False` (because the
SDK adds `-l`/`-i`), and other interpreters (`node`, `ruby`, `perl`)
take `-e` not `-c` so they fail even with `login=False`.

## Proxy Automation From Python

For interactive inspection, use the six built-in agent tools directly:

- `list_requests(httpql_filter=, first=50, after=, sort_by=, sort_order=, scope_id=)` returns request entries and cursor pagination in `page_info`.
- `view_request(request_id, part="request", search_pattern=, page=1, page_size=50)` returns raw request/response text by page, or regex matches when a search pattern is supplied.
- `repeat_request(request_id, modifications={...})` replays a captured request with changes to `url`, `params`, `headers`, `body`, or `cookies`.
- `list_sitemap(scope_id=, parent_id=, depth="DIRECT", page=1)` lists root domains or children of a selected entry.
- `view_sitemap_entry(entry_id)` returns entry details and up to 30 recent related requests.
- `scope_rules(action, allowlist=, denylist=, scope_id=, scope_name=)` supports `list`, `get`, `create`, `update`, and `delete`.

The built-in tools also retain `max`/`offset` pagination aliases,
`part="both"`, and replay changes using `method`, `path`, or `header_<name>`.

These agent tools return JSON. For reusable Python scripts, the sandbox
image also includes an installed `caido_api` module. Its async helpers
have similar names but return SDK objects or dictionaries as described
below; they are a separate API from the built-in agent tools. Import them
explicitly:

```python
from caido_api import (
    list_requests,
    list_sitemap,
    repeat_request,
    scope_rules,
    view_request,
    view_sitemap_entry,
)
```

All helpers are async. Use them inside `asyncio.run(...)` or an async
function:

```python
import asyncio

from caido_api import list_requests, view_request


async def main():
    posts = await list_requests(
        httpql_filter='req.method.eq:"POST" AND req.path.cont:"/api/"',
        first=50,
    )
    candidates = []
    for edge in posts.edges:
        request_id = edge.node.request.id
        body = await view_request(request_id, part="request")
        raw = body.request.raw.decode("utf-8", errors="replace")
        if "id=" in raw or "user=" in raw:
            candidates.append(request_id)

    print(f"{len(candidates)} candidates")
    print(candidates[:10])


asyncio.run(main())
```

Available Python helpers:

- `list_requests(httpql_filter=, first=50, after=, sort_by=, sort_order=, scope_id=)` returns a cursor-paginated Caido SDK `Connection`.
- `view_request(request_id, part="request")` returns a Caido SDK request object with raw request/response bytes.
- `repeat_request(request_id, modifications={...})` replays a captured request after modifying `url`, `params`, `headers`, `body`, or `cookies`.
- `list_sitemap(scope_id=, parent_id=, depth="DIRECT", page=1)` walks Caido's request-tree view of the discovered surface. Omit `parent_id` for root domains; pass an entry id with `depth="DIRECT"` or `"ALL"` to drill in.
- `view_sitemap_entry(entry_id)` returns one entry plus its 30 most recent related requests.
- `scope_rules(action, allowlist=, denylist=, scope_id=, scope_name=)` manages Caido scopes.

For one-off arbitrary requests (e.g. probing a fresh endpoint, hitting an
external API), use `exec_command` with `curl` / `httpx` / `requests`. The
sandbox's proxy environment routes HTTP/HTTPS requests from clients that
honor these settings through Caido when it is active. Keep environment
proxy handling enabled (`trust_env=True` in `httpx`, or
`Session.trust_env = True` in `requests`).
Localhost is excluded by `NO_PROXY`. This is not a firewall or transparent
network interception: raw sockets and ordinary nmap port scans bypass
the HTTP proxy. Captured requests appear in `list_requests` and can be
replayed with `repeat_request`.

Caido scopes select traffic for queries; they do not block outbound
connections. Pass a scope's `scope_id` to `list_requests` or
`list_sitemap` when filtering by that scope.

## Workflow

For iterative exploit work, put code in a file:

```text
1. Create or edit a task-unique script (e.g. `poc_<task-id>.py`, so it can't
   clobber a project file or another agent's script) with `apply_patch`.
2. Run it with `exec_command`: `python3 poc_<task-id>.py`.
3. Edit and rerun until the proof-of-concept is reliable.
```

## Installing extra packages

The sandbox's Python lives in `/app/.venv`, and it is the active virtualenv
(`python3` / `pip` already resolve to it). The following common libraries are
**pre-installed** — import them directly, no install step needed:
`requests`, `httpx`, `beautifulsoup4` (`bs4`), `lxml`, `pyjwt` (`jwt`),
`cryptography`.

To add a one-off dependency for an exploit script, use `uv` (already in the
image and much faster than pip):

```bash
uv pip install --python /app/.venv/bin/python <package>
```

Plain `pip install <package>` also works because the venv is active. Install
before you import, so scripts don't fail with `ModuleNotFoundError`.
