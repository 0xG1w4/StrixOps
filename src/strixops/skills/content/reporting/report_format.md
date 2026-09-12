---
name: report_format
description: Presentation-only Markdown formatting for findings, summaries and final reports; preserve every required detail
---

# Report Formatting

This skill changes presentation only. Keep the existing reporting requirements,
section coverage, language, severity rules and evidence rules. Do not shorten,
omit, redact or invent facts to make a report look cleaner. Preserve
every required finding, credential value, sensitive-data detail, limitation,
identifier and technical explanation. Never replace a detailed explanation with
a summary table or diagram; they supplement the original required content.

## Typography

TYPOGRAPHY — the viewer renders markdown headings, lists, GFM tables, fenced code
blocks and basic mermaid flowcharts, so use them when they fit the content:

- Keep paragraphs short: at most 5 lines each in the Markdown source, usually
  2–4 complete sentences about one idea. Separate paragraphs with a blank line.
  Anything a section enumerates becomes bullets or a table, not run-on prose.
- Keep the required `##` sections. Break them into `###` subsections with
  meaningful titles, one per theme, stage or host. A subsection may use `####`
  when another level is actually needed. Do not skip heading levels.
- Use `-` bullets for parallel facts and numbered lists for ordered steps.
  Indent nested items consistently. Keep explanations with their own item;
  do not turn a multi-step procedure into one long bullet.
- Use GFM tables with a header row for genuinely tabular information: host and
  service inventories, credentials, affected parameters and per-finding summaries.
  Escape literal pipes in cells. Keep long explanations, multiline values and
  full request/response material outside cells in labeled paragraphs or code
  blocks; do not truncate them to fit a table.
- Bold the facts a reader must not miss: severity, endpoints and finding ids.
  Use inline code for literal identifiers and short values. Formatting markers
  are not part of the original value.
- PoC steps, commands and response excerpts go in fenced code blocks with
  their language tag. Preserve their original bytes and line breaks. Use a
  fence longer than any fence contained in the data. Never insert ellipses,
  masking, line numbers or wrapping characters into literal data.
- When a flow shows more than prose — an attack chain, an access path or an
  environment layout — add ONE fenced mermaid flowchart per relevant section
  (```mermaid, flowchart TD, ASCII node ids, plain labels in the report language,
  roughly 12 nodes at most). Use basic nodes and `-->` arrows; avoid subgraphs,
  styling directives, click actions and embedded HTML. Diagrams support the
  written evidence; they never replace it. Do not invent nodes or relationships.
- Use blank lines around headings, paragraphs, lists, tables and fenced blocks.
  Do not use HTML such as `<br>` to force layout. Never insert a table or diagram
  simply to satisfy a quota.

Before handing off, check heading order, paragraph breaks, list indentation,
table headers and code fences. Correct presentation in the content already
being written; do not spend additional testing rounds on formatting or delay
task completion because a diagram cannot be rendered.
