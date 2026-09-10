"""Shared business operations for the Console and MCP protocol adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from strixops.traffic.scope import allowed_url
from strixops.traffic.security import public_session
from strixops.traffic.store import TrafficStore, new_id, now, valid_id


class TrafficService:
    DELETE_WAIT_SECONDS = 40

    def __init__(self, store: TrafficStore | None = None, runtime: Any = None):
        self.store = store or TrafficStore()
        if runtime is None:
            from strixops.traffic.runtime import CaptureRuntime

            runtime = CaptureRuntime()
        self.runtime = runtime
        self.lock = threading.RLock()
        self._jobs: dict[str, tuple[str, threading.Event, threading.Thread | None]] = {}
        self._done: dict[str, threading.Event] = {}
        self._watchers: dict[str, threading.Thread] = {}
        self._last_status: dict[str, float] = {}
        self._deleting: set[str] = set()
        self._restart_grace: dict[str, float] = {}
        # Request jobs are process-owned, while capture containers persist across API restarts.
        for task in self.store.list_tasks():
            # An old process may have had an untracked manual replay. Its container
            # self-removes after the hard deadline, so deletion must honor that grace.
            self._restart_grace[task["id"]] = time.monotonic() + 40
            for job in self.store.records("jobs", task["id"]):
                if job["status"] in {"queued", "running"}:
                    job.update(
                        status="failed",
                        error="Console restarted before this test completed",
                        finished_at=now(),
                    )
                    self.store.put_record("jobs", task["id"], job)
            if task["status"] in {"starting", "capturing"}:
                self._watch(task["id"])
            elif task["status"] in {"stopping", "ending", "deleting"}:
                threading.Thread(
                    target=self._recover_shutdown,
                    args=(task["id"], task["status"]),
                    daemon=True,
                    name="mcp-recover-" + task["id"],
                ).start()

    def _recover_shutdown(self, task_id: str, status: str) -> None:
        try:
            if status == "deleting":
                self.delete(task_id)
            elif status == "ending":
                # A killed process cannot join its old workers. Their container-side hard
                # deadline is 30 seconds with auto-removal; allow a bounded restart grace.
                time.sleep(40)
                self.end(task_id)
            else:
                self.stop(task_id)
        except Exception as exc:
            with self.lock:
                try:
                    task = self.store.get_task(task_id)
                except KeyError:
                    return
                if task["status"] in {"deleting", "delete_failed"}:
                    return
                self.store.update_task(task_id, status="error", error=str(exc)[:1000])
                self.store.event(task_id, {"type": "capture.error", "message": str(exc)[:500]})

    def _require_available(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if task_id in self._deleting or task["status"] in {"deleting", "delete_failed"}:
            raise ValueError("This task is being deleted; retry deletion to finish cleanup")
        return task

    def task(self, task_id: str, *, refresh: bool = True) -> dict:
        if refresh:
            self.refresh(task_id)
        task = self.store.get_task(task_id)
        task.pop("scope_history", None)
        sessions = self.store.sessions(task_id)
        shared_fingerprint = self._public_ca_fingerprint(self.store.root / "ca" / "mitmproxy-ca-cert.pem")
        projected = []
        for session in sessions:
            public = public_session(session)
            fingerprint = session.get("ca_sha256") or self._public_ca_fingerprint(
                self.runtime.ca_path(Path(session["directory"]))
            )
            public["ca_shared"] = bool(shared_fingerprint and fingerprint == shared_fingerprint)
            if fingerprint:
                public["ca_sha256"] = fingerprint
            projected.append(public)
        task["session"] = projected[0] if projected else None
        task["sessions"] = projected
        task["counts"] = self.store.counts(task_id)
        return task

    def tasks(self) -> list[dict]:
        with self.lock:
            return [self.task(task["id"]) for task in self.store.list_tasks()]

    def create(self, body: dict) -> dict:
        from strixops.traffic.prompts import normalized_config

        config = normalized_config({}, body.get("agent_config") or {})
        task = self.store.create_task(
            body["name"], body.get("allow_hosts", []), body.get("exclude_hosts", []), config
        )
        return self.task(task["id"], refresh=False)

    def update(self, task_id: str, body: dict) -> dict:
        from strixops.traffic.prompts import normalized_config

        with self.lock:
            task = self._require_available(task_id)
            if task["status"] in {"starting", "stopping", "ending"}:
                raise ValueError("Wait for the current capture operation to finish")
            changes = {key: body[key] for key in ("name", "allow_hosts", "exclude_hosts") if key in body}
            if "agent_config" in body:
                changes["agent_config"] = normalized_config(task, body["agent_config"])
            task = self.store.update_task(task_id, **changes)
            sessions = self.store.sessions(task_id)
            if sessions and sessions[0].get("status") == "running":
                try:
                    self.runtime.update_scope(task, sessions[0], Path(sessions[0]["directory"]))
                except Exception:
                    # Never leave an old, broader active policy after acknowledging a scope change.
                    self.stop(task_id)
                    raise
            self.store.event(task_id, {"type": "task.updated"})
        return self.task(task_id)

    def _watch(self, task_id: str) -> None:
        with self.lock:
            if task_id in self._deleting:
                return
            if task_id in self._watchers and self._watchers[task_id].is_alive():
                return

            def monitor():
                while True:
                    time.sleep(1)
                    try:
                        self.refresh(task_id, watch=False)
                        task = self.store.get_task(task_id)
                        if task["status"] not in {"starting", "capturing", "stopping"}:
                            return
                    except KeyError:
                        return
                    except Exception as exc:
                        self.store.event(task_id, {"type": "capture.error", "message": str(exc)[:500]})
                        return

            thread = threading.Thread(target=monitor, daemon=True, name="mcp-capture-" + task_id)
            self._watchers[task_id] = thread
            thread.start()

    def start(self, task_id: str) -> dict:
        with self.lock:
            self._require_available(task_id)
            self.refresh(task_id, watch=False)
            task = self.store.get_task(task_id)
            if task["status"] in {"capturing", "starting"}:
                return self.task(task_id, refresh=False)
            if not task["allow_hosts"]:
                raise ValueError("Add at least one website before starting capture")
            if task["status"] in {"stopping", "ending"}:
                raise ValueError("Capture is still stopping")
            # A degraded proxy may still forward traffic. Remove it before opening a new listener.
            if any(s.get("status") != "stopped" for s in self.store.sessions(task_id)):
                self.stop(task_id)
            session_id = new_id("capture")
            directory = self.store.root / "tasks" / task_id / "captures" / session_id
            directory.mkdir(parents=True, mode=0o700)
            session = {
                "id": session_id,
                "task_id": task_id,
                "status": "starting",
                "directory": str(directory),
                "created_at": now(),
                "journal_offset": 0,
            }
            self.store.put_session(session)
            self.store.update_task(task_id, status="starting", error="")
            try:
                metadata = self.runtime.start(task, session_id, directory, ca_directory=self.ensure_ca())
                session.update(metadata, status="running")
                self.store.put_session(session)
                self.store.update_task(task_id, status="capturing", ended_at=None)
                self.store.event(task_id, {"type": "capture.started", "session_id": session_id})
            except Exception as exc:
                session.update(status="error", error=str(exc)[:1000])
                self.store.put_session(session)
                self.store.update_task(task_id, status="error", error=str(exc)[:1000])
                raise
            self._watch(task_id)
            return self.task(task_id, refresh=False)

    def _ingest(self, task: dict, session: dict, *, drain: bool = False) -> None:
        path = Path(session["directory"]) / "events.jsonl"
        if not path.is_file():
            return
        offset = int(session.get("journal_offset", 0))
        if path.stat().st_size < offset:
            offset = 0  # replay is safe: flow IDs are stable upsert keys
        changed = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            for _ in range(100000 if drain else 1000):
                position = handle.tell()
                line = handle.readline(16 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) >= 16 * 1024 * 1024:
                        raise ValueError("Capture journal record exceeded its size limit")
                    break
                try:
                    payload = json.loads(line)
                    flow = payload.get("flow", payload)
                    if not isinstance(flow, dict):
                        raise ValueError("Malformed capture record")
                    if not flow.get("request") and not flow.get("method"):
                        # Operational records may not contain traffic; retain only the bounded notice.
                        self.store.event(
                            task["id"],
                            {
                                "type": "capture.notice",
                                "message": str(payload.get("error") or payload.get("type") or "")[:300],
                            },
                        )
                    else:
                        revision = str(flow.get("scope_revision", task["scope_revision"]))
                        scope = task["scope_history"].get(revision)
                        url = flow.get("url") or (flow.get("request") or {}).get("url", "")
                        if scope is None or not allowed_url(
                            url, scope["allow_hosts"], scope["exclude_hosts"]
                        ):
                            raise ValueError("Capture record does not match its recorded website scope")
                        flow["session_id"] = session["id"]
                        self.store.put_flow(task["id"], flow, session["id"])
                        changed += 1
                except (ValueError, TypeError) as exc:
                    session["ingest_error"] = f"Journal entry at byte {position}: {exc}"
                    self.store.event(
                        task["id"], {"type": "capture.error", "message": session["ingest_error"]}
                    )
                    # Malformed complete records are visibly rejected; subsequent valid records survive.
                offset = handle.tell()
        if offset != session.get("journal_offset", 0):
            session["journal_offset"] = offset
            self.store.put_session(session)
        if changed:
            self.store.event(task["id"], {"type": "flows.updated", "count": changed})

    def refresh(self, task_id: str, *, watch: bool = True) -> None:
        with self.lock:
            task = self.store.get_task(task_id)
            if task_id in self._deleting or task["status"] in {"deleting", "delete_failed"}:
                return
            sessions = self.store.sessions(task_id)
            for session in sessions:
                self._ingest(task, session)
            if not sessions or task["status"] not in {"capturing", "starting", "error"}:
                return
            current = sessions[0]
            if current.get("status") == "stopped":
                return
            if time.monotonic() - self._last_status.get(task_id, 0) > 5:
                self._last_status[task_id] = time.monotonic()
                try:
                    status = self.runtime.status(task_id, current)
                    current.update(status)
                    runtime_status = status.get("runtime_status", "running")
                    if runtime_status not in {"running", "starting", "healthy"}:
                        current["status"] = "error"
                        self.store.update_task(
                            task_id,
                            status="error",
                            error=status.get("error") or "Capture container is no longer running",
                        )
                    elif runtime_status in {"running", "healthy"}:
                        current["status"] = "running"
                        capture = status.get("capture_status") or {}
                        if capture.get("state") in {"capacity_reached", "storage_error"}:
                            message = capture.get("message") or "Capture is no longer recording"
                            current["error"] = message
                            self.store.update_task(task_id, status="error", error=message)
                        elif task["status"] == "starting":
                            self.store.update_task(task_id, status="capturing", error="")
                    self.store.put_session(current)
                except Exception as exc:
                    current.update(error=str(exc)[:500])
                    self.store.put_session(current)
            if watch:
                self._watch(task_id)

    def stop(self, task_id: str) -> dict:
        with self.lock:
            task = self._require_available(task_id)
            sessions = self.store.sessions(task_id)
            for session in sessions:
                if session.get("status") == "stopped":
                    continue
                self.store.update_task(task_id, status="stopping")
                try:
                    if not session.get("container_id"):
                        session.update(self.runtime.status(task_id, session))
                    if session.get("container_id"):
                        self.runtime.stop(task_id, session)
                except Exception as exc:
                    self.store.update_task(task_id, status="error", error=str(exc)[:1000])
                    raise
                self._ingest(task, session, drain=True)
                session.update(status="stopped", stopped_at=now())
                self.store.put_session(session)
            if task["status"] != "ended":
                self.store.update_task(task_id, status="stopped")
            self.store.event(task_id, {"type": "capture.stopped"})
            return self.task(task_id, refresh=False)

    def end(self, task_id: str) -> dict:
        with self.lock:
            self._require_available(task_id)
            pending = []
            for identity, (owner, signal, _) in list(self._jobs.items()):
                if owner == task_id:
                    signal.set()
                    if identity in self._done:
                        pending.append(self._done[identity])
            self.stop(task_id)
            self.store.update_task(task_id, status="ending")
        # Worker cleanup needs this same service lock, so never wait while holding it.
        deadline = time.monotonic() + 15
        for done in pending:
            if not done.wait(max(0, deadline - time.monotonic())):
                raise RuntimeError(
                    "Task is ending; waiting for request containers to stop. Retry end to check."
                )
        with self.lock:
            self._require_available(task_id)
            self.store.update_task(task_id, status="ended", ended_at=now())
            self.store.event(task_id, {"type": "task.ended"})
        return self.task(task_id, refresh=False)

    def delete(self, task_id: str) -> dict:
        """Stop owned work before permanently removing this task's evidence and storage."""
        valid_id(task_id)
        result = {"deleted": True, "task_id": task_id}
        with self.lock:
            try:
                self.store.get_task(task_id)
            except KeyError:
                return result
            if task_id in self._deleting:
                raise RuntimeError("Task deletion is already in progress; retry to check completion")
            self.store.update_task(task_id, status="deleting", error="")
            self._deleting.add(task_id)
        try:
            with self.lock:
                pending = []
                for identity, (owner, signal, _) in list(self._jobs.items()):
                    if owner == task_id:
                        signal.set()
                        if identity in self._done:
                            pending.append(self._done[identity])
                directory = self.store.task_directory(task_id)
                sessions = self.store.sessions(task_id)
                for session in sessions:
                    if not Path(session["directory"]).resolve().is_relative_to(directory):
                        raise ValueError("Capture storage does not belong to this MCP task")
                for session in sessions:
                    if session.get("status") == "stopped":
                        continue
                    if not session.get("container_id"):
                        session.update(self.runtime.status(task_id, session))
                    if session.get("container_id"):
                        self.runtime.stop(task_id, session)
                    session.update(status="stopped", stopped_at=now())
                    self.store.put_session(session)
            # Do not hold the service lock: Agent/replay finalizers need it to finish.
            deadline = time.monotonic() + self.DELETE_WAIT_SECONDS
            for done in pending:
                if not done.wait(max(0, deadline - time.monotonic())):
                    raise RuntimeError("Task deletion is waiting for request workers; retry deletion")
            remaining_grace = self._restart_grace.get(task_id, 0) - time.monotonic()
            if remaining_grace > 0:
                threading.Event().wait(remaining_grace)
            with self.lock:
                # Migrate a legacy session CA before permanently removing its directory.
                self.ensure_ca()
                self.store.delete_task(task_id)
                self._watchers.pop(task_id, None)
                self._last_status.pop(task_id, None)
                self._restart_grace.pop(task_id, None)
            return result
        except Exception as exc:
            with self.lock:
                self.store.update_task(task_id, status="delete_failed", error=str(exc)[:1000])
                self.store.event(task_id, {"type": "task.delete_failed", "message": str(exc)[:500]})
            if isinstance(exc, (ValueError, RuntimeError)):
                raise
            raise RuntimeError("Task deletion did not finish; saved records remain. Retry deletion.") from exc
        finally:
            with self.lock:
                self._deleting.discard(task_id)

    def replay(
        self,
        task_id: str,
        flow_id: str,
        modifications: dict | None = None,
        *,
        test_id: str = "",
        cancel: threading.Event | None = None,
        key: str = "",
        original: dict | None = None,
        job_scope: dict | None = None,
    ) -> dict:
        modifications = modifications or {}
        body = {"flow_id": flow_id, "modifications": modifications}
        with self.lock:
            self._require_available(task_id)
            previous = self.store.idempotent(task_id, "replay", key, body)
            if previous:
                if previous.startswith("pending_"):
                    raise ValueError(
                        "This replay already started. Inspect its result before issuing a new replay."
                    )
                return self.store.get_flow(task_id, previous)
            task = self.store.get_task(task_id)
            if task["status"] in {"ended", "ending", "stopping"}:
                raise ValueError("Reopen this task before sending requests")
            flow = original or self.store.get_flow(task_id, flow_id)
            destination = str(modifications.get("url") or flow["url"])
            if not allowed_url(destination, task["allow_hosts"], task["exclude_hosts"]):
                raise ValueError("Request destination is outside the current task scope")
            if job_scope and not allowed_url(
                destination, job_scope["allow_hosts"], job_scope["exclude_hosts"]
            ):
                raise ValueError("Request destination is outside this test's fixed scope")
            signal = cancel or threading.Event()
            operation_id = test_id or new_id("replay")
            if not test_id:
                self._jobs[operation_id] = (task_id, signal, None)
                self._done[operation_id] = threading.Event()
            # Persist intent before network I/O: retries after a lost response must not send twice.
            self.store.idempotent(task_id, "replay", key, body, "pending_" + operation_id)
        try:
            if signal.is_set():
                raise ValueError("Request was cancelled before sending")
            result = self.runtime.replay(task, flow, modifications, test_id=test_id, cancel=signal)
            result.update(parent_flow_id=flow_id, test_id=test_id, source="agent" if test_id else "replay")
            persisted = self.store.put_flow(task_id, result)
            with self.lock:
                self.store.complete_idempotent(task_id, "replay", key, persisted["id"])
            self.store.event(task_id, {"type": "flow.replayed", "flow_id": persisted["id"]})
            return persisted
        finally:
            if not test_id:
                with self.lock:
                    self._jobs.pop(operation_id, None)
                    self._done.pop(operation_id).set()

    def start_test(self, task_id: str, body: dict, key: str = "") -> dict:
        from strixops.traffic.agent import build_prompt_snapshot

        with self.lock:
            self._require_available(task_id)
            existing = self.store.idempotent(task_id, "test", key, body)
            if existing:
                return self.store.record("jobs", task_id, existing)
            task = self.store.get_task(task_id)
            if task["status"] in {"ended", "ending", "stopping"}:
                raise ValueError("Reopen this task before starting a test")
            ids = body.get("flow_ids", [])
            if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids):
                raise ValueError("Select 1–50 distinct requests")
            flows = [self.store.get_flow(task_id, identity) for identity in ids]
            if any(
                not allowed_url(flow["url"], task["allow_hosts"], task["exclude_hosts"]) for flow in flows
            ):
                raise ValueError("One or more selected requests are outside the current task scope")
            config = {
                k: body[k]
                for k in ("skills", "profile_id", "instruction", "max_requests", "max_seconds")
                if body.get(k) is not None
            }
            snapshot = build_prompt_snapshot(task, config)
            job = {
                "id": new_id("test"),
                "task_id": task_id,
                "flow_ids": ids,
                "status": "queued",
                "created_at": now(),
                "config": snapshot["config"],
                "prompt_snapshot": snapshot,
                "scope_revision": task["scope_revision"],
                "scope": {"allow_hosts": task["allow_hosts"], "exclude_hosts": task["exclude_hosts"]},
                "source_flows": copy.deepcopy(flows),
                "events": [],
                "result": None,
                "error": "",
            }
            self.store.put_record("jobs", task_id, job)
            self.store.idempotent(task_id, "test", key, body, job["id"])
            cancelled = threading.Event()
            thread = threading.Thread(
                target=self._run_test,
                args=(task, job, flows, cancelled),
                daemon=True,
                name="mcp-test-" + job["id"],
            )
            self._jobs[job["id"]] = (task_id, cancelled, thread)
            self._done[job["id"]] = threading.Event()
            thread.start()
            return job

    def _run_test(self, task: dict, job: dict, flows: list[dict], cancelled: threading.Event) -> None:
        from strixops.traffic.agent import run_request_test

        identity, task_id = job["id"], task["id"]
        requests_used = 0
        replay_lock = threading.Lock()
        cancelled_before_cleanup: bool | None = None

        def emit(event: dict):
            with self.lock:
                latest = self.store.record("jobs", task_id, identity)
                latest["events"] = [*latest.get("events", []), {"created_at": now(), **event}][-200:]
                self.store.put_record("jobs", task_id, latest)
                self.store.event(task_id, {"type": "test.progress", "test_id": identity})

        async def replay(flow_id: str, modifications: dict):
            nonlocal requests_used
            with replay_lock:
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                if flow_id not in job["flow_ids"]:
                    raise ValueError("Only selected requests can be replayed by this test")
                if requests_used >= job["config"]["max_requests"]:
                    raise ValueError("Request budget exhausted")
                requests_used += 1
            original = next(flow for flow in flows if flow["id"] == flow_id)
            return await asyncio.to_thread(
                self.replay,
                task_id,
                flow_id,
                modifications,
                test_id=identity,
                cancel=cancelled,
                original=original,
                job_scope=job["scope"],
            )

        try:
            with self.lock:
                current = self.store.record("jobs", task_id, identity)
                current.update(status="running", started_at=now())
                self.store.put_record("jobs", task_id, current)

            async def execute():
                nonlocal cancelled_before_cleanup
                try:
                    return await run_request_test(
                        task, job, flows, replay=replay, emit=emit, cancelled=cancelled.is_set
                    )
                finally:
                    # Signal thread-backed requests before asyncio.run waits for its executor.
                    cancelled_before_cleanup = cancelled.is_set()
                    cancelled.set()

            result = asyncio.run(execute())
            result.pop("prompt_snapshot", None)  # persisted once on the job, never duplicated in output
            status = result.get("status", "failed")
            if status not in {"completed", "failed", "cancelled", "blocked"}:
                status = "failed"
            error = result.get("error", "")
        except BaseException as exc:
            user_cancelled = (
                cancelled.is_set() if cancelled_before_cleanup is None else cancelled_before_cleanup
            )
            status = "cancelled" if user_cancelled or isinstance(exc, asyncio.CancelledError) else "failed"
            error = str(exc)[:1000] or type(exc).__name__
            result = {"summary": "Test could not complete", "findings": [], "coverage": []}
        finally:
            cancelled.set()
        with self.lock:
            current = self.store.record("jobs", task_id, identity)
            current.update(
                status=status, error=error, result=result, finished_at=now(), requests_used=requests_used
            )
            self.store.put_record("jobs", task_id, current)
            self._jobs.pop(identity, None)
            self._done.pop(identity).set()
            self.store.event(task_id, {"type": "test.finished", "test_id": identity, "status": status})

    def cancel_test(self, task_id: str, test_id: str) -> dict:
        with self.lock:
            job = self.store.record("jobs", task_id, test_id)
            running = self._jobs.get(test_id)
            if running and running[0] == task_id:
                running[1].set()
                job["cancel_requested"] = True
                self.store.put_record("jobs", task_id, job)
            return job

    def public_test(self, job: dict, *, detail: bool = False) -> dict:
        result = {k: copy.deepcopy(v) for k, v in job.items() if k not in {"source_flows", "prompt_snapshot"}}
        snapshot = job.get("prompt_snapshot") or {}
        if isinstance(result.get("result"), dict):
            result["result"].pop("prompt_snapshot", None)
        result["prompt_snapshot"] = {
            k: v for k, v in snapshot.items() if k != "skills" and (detail or k != "prompt")
        }
        result["prompt_snapshot"]["skill_manifest"] = {
            key: value.get("sha256") for key, value in snapshot.get("skills", {}).items()
        }
        return result

    def report(self, task_id: str, key: str = "") -> dict:
        from strixops.traffic.reports import build_report

        with self.lock:
            self._require_available(task_id)
            previous = self.store.idempotent(task_id, "report", key, {})
            if previous:
                return self.store.record("reports", task_id, previous)
            self.refresh(task_id)
            task = self.task(task_id, refresh=False)
            report = build_report(task, self.store.endpoints(task_id), self.store.records("jobs", task_id))
            self.store.put_record("reports", task_id, report)
            self.store.idempotent(task_id, "report", key, {}, report["id"])
            return report

    @staticmethod
    def _public_ca_fingerprint(path: Path) -> str:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        try:
            if path.stat().st_size > 128 * 1024:
                return ""
            return x509.load_pem_x509_certificate(path.read_bytes()).fingerprint(hashes.SHA256()).hex()
        except (OSError, ValueError):
            return ""

    def ensure_ca(self) -> Path:
        from strixops.traffic.certificates import ensure_ca

        with self.lock:
            sessions = [
                session for task in self.store.list_tasks() for session in self.store.sessions(task["id"])
            ]
            sessions.sort(key=lambda s: (s.get("status") == "running", s.get("created_at", "")), reverse=True)
            candidates = [Path(session["directory"]) / "ca" for session in sessions]
            return ensure_ca(self.store.root, candidates)

    def ca_info(self) -> dict:
        from strixops.traffic.certificates import ca_info

        return ca_info(self.ensure_ca())

    def shared_ca(self) -> Path:
        return self.ensure_ca() / "mitmproxy-ca-cert.pem"

    def ca(self, task_id: str) -> Path:
        self.store.get_task(task_id)
        sessions = self.store.sessions(task_id)
        if not sessions:
            return self.shared_ca()
        session = sessions[0]
        if session.get("ca_directory"):
            path = self.runtime.ca_path(
                Path(session["directory"]), ca_directory=Path(session["ca_directory"])
            )
        else:
            # An already running legacy proxy still uses its original CA until restarted.
            path = self.runtime.ca_path(Path(session["directory"]))
        if not path.is_file():
            raise KeyError("Proxy CA is not ready yet")
        # Only the public certificate is ever returned, never the proxy CA key.
        if "PRIVATE KEY" in path.read_text(errors="replace"):
            raise ValueError("CA export unexpectedly contains private key material")
        return path


_SERVICES: dict[str, TrafficService] = {}
_SERVICE_LOCK = threading.Lock()


def get_service() -> TrafficService:
    from strixops.traffic.store import storage_root

    root = str(storage_root())
    with _SERVICE_LOCK:
        if root not in _SERVICES:
            _SERVICES[root] = TrafficService()
        return _SERVICES[root]
