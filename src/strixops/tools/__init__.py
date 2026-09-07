"""Agent function tools.

Parameter names are part of the platform contract — the conversation parser
(``apps/api/services/strix_data.py:_infer_unified_tool_context``) classifies
tool events by argument-key shape:

* shell: ``command|cmd|chars|session_id``
* todo: ``todos|todo_ids|updates`` (or keys ⊆ {status, priority})
* finish: exactly ``executive_summary+methodology+technical_analysis+recommendations``
* vulnerability_report: ``title`` + any of
  ``description|impact|technical_analysis|endpoint|target``
* agent_finish: ``result_summary``
* proxy: ``httpql_filter|request_id|part|search_pattern|modifications|entry_id|
  scope_id|allowlist|denylist|parent_id|depth``
* thinking: ``thought``

Never add a classifier keyword to a tool that should classify differently —
``tests/contract/test_tool_shapes.py`` fails the build if you do.
"""
