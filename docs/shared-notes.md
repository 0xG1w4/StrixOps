# Shared task notes

Web and Internal tasks provide a shared notebook for their root and child
Agents. Open a task's **Notes** tab to inspect its saved observations,
inventories, methodology, questions and plans. MCP request tests have their own
execution path and do not use this notebook in this release.

Notes are reference material. A note in the `findings` category is not a verified
vulnerability or a formal report entry. Agents still use the existing coverage,
threat-model and reporting tools for those records.

## Agent workflow

Both root and child Agents receive these tools:

| Tool | Behavior |
|---|---|
| `create_note` | Save a new note with its title, content, category and tags; attribute it to the calling Agent. |
| `list_notes` | Search/filter a page of note metadata and previews, without loading every full body into model context. |
| `get_note` | Read a selected note's full body and current revision; optionally request retained history. |
| `update_note` | Replace selected fields using `expected_revision`, or atomically add text with `append_content`. |
| `delete_note` | Soft-delete a note using `expected_revision`, preserving its record and retained history. |

New run prompts guide Agents to search relevant notes before reconnaissance or
independent validation, reuse discoveries, and write facts useful to other
Agents. They do not require a notebook read on every model turn. Notes are
returned by tools when requested, rather than continually appended in full to
every Agent's prompt. The model decides when a note would help; the prompt does
not guarantee that every observation is recorded.

Before replacing note fields or deleting a note, read its current revision and
pass that number as `expected_revision`. If another Agent has changed it, the
operation returns a conflict and the current revision. Read the latest note and
reconcile the change instead of retrying an old replacement. An append-only
update can omit the expected revision; it adds to the current content while
holding the task's notebook lock. If an expected revision is supplied, it is
still checked. Append mode cannot be combined with replacement fields; it adds
the exact supplied text, so include a newline when one is needed. Deleted
records are retained; there is no restore tool in this phase.

Use evidence files for large command outputs and reference them from notes.
Notes may contain target data and appear in tool conversations and the Console,
so the existing task-data access boundary applies.

## Persistence and failure behavior

Each task owns `<run directory>/.state/notes.json`. Its root and children share
one store and lock in the engine process. Different task directories do not
share notebooks. This is not a cross-task knowledge base or a store for multiple
independent engine processes writing the same run directory.

Successful mutations atomically replace the notebook file before updating the
in-memory state. A failed save returns an error and does not claim that the
change was saved. Corrupt, unreadable or oversized files are preserved and
reported as unavailable, rather than silently replaced with an empty notebook.
Expected note errors return a structured result with `continue_task: true`;
Agents are instructed to continue with messages, evidence and the remaining
tools. Cancellation waits for an in-flight note operation to settle before it
propagates, so a note writer does not outlive Agent cleanup. Notes cannot recover an
unrelated model-provider outage or guarantee a model follows the continuation
guidance.

Older runs without a notebook display an empty state. Reading that state creates
no file. Existing prompt snapshots are not rewritten during upgrade; start a
new run to receive the new note guidance.

## Limits and history

| Item | Limit |
|---|---|
| Categories | `general`, `findings`, `methodology`, `questions`, `plan`, `wiki` |
| Title | 200 characters |
| Content | 32,768 characters per note revision |
| Tags | 20 tags, up to 64 characters each |
| Notes | 500 records per task, including deleted records |
| History | Up to 20 previous revisions per note, plus its current revision |
| Persisted notebook | 16 MiB |
| List response | 20 notes by default; at most 100 per page |
| Preview | Up to 280 characters |

Each revision retains authorship and timestamps. Once the history limit is
reached, the oldest prior revisions roll off and the detail view indicates that
history was truncated. Deletion is soft, so it does not free a record slot.
The file-size limit may be reached before the record limit when bodies and
revision histories are large. An operation exceeding a limit returns an error;
it does not silently truncate the new content.

## Console and API

The Notes tab is read-only. It provides search, category/tag/author filters,
pagination, a deleted-note toggle, full content and revision history. The list
uses metadata and previews; full content and history are fetched on demand.
While the tab is visible, it refreshes periodically so Agent updates appear.
Markdown is rendered without executing raw HTML or automatically loading
embedded remote images.

The existing Console task access boundary also covers these read-only routes:

- `GET /api/runs/{name}/notes`: `search`, `category`, repeated `tags`, `author`,
  `include_deleted`, `limit` and `offset`.
- `GET /api/runs/{name}/notes/{note_id}`: `include_history`, default `false`.

Tag filters require all selected tags. Author filtering matches creator or
latest editor identity. Missing old data is distinguishable from unreadable
data; unreadable data is not reported as a healthy empty notebook. Responses
use `Cache-Control: no-store`. These routes do not offer manual note mutations.

## 中文重點

- Web／Internal 的主 Agent、子 Agent 共用同一任務的筆記，不同任務互相隔離。
- 新任務會收到使用筆記的提示；不要求每回合讀取，也不會自動塞入全部內容。
- 覆寫與刪除需要版本號；純追加在鎖定下完成，避免互相覆蓋。
- 筆記持久保存，保留近期修訂與刪除紀錄；並非無上限的歷史備份。
- 筆記儲存異常會明確回報，提示 Agent 繼續任務，不會假裝寫入成功。
- 任務頁的筆記頁籤提供唯讀搜尋與歷史檢視；MCP 本次不接入。
- 升級後啟動新任務即可使用，不需要新的容器或外部服務。
