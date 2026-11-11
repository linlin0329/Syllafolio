"""桌面全类任务的只读投影及退出协调；不改写共同服务的状态或版本。"""
from __future__ import annotations
from .annotation_english import system_error

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import threading


ACTIVE = {"queued", "running", "cancel_requested"}
TERMINAL = {
    "import": {"ready", "succeeded", "failed", "cancelled"},
    "foreground": {"ready", "succeeded", "failed", "cancelled"},
    "local": {"succeeded", "failed", "cancelled"},
    "batch": {"ready", "failed", "cancelled"},
    "assessment": {"completed", "not_estimable", "failed", "cancelled", "interrupted"},
    "catalogue_export": {"completed", "failed", "cancelled", "interrupted"},
}
CANCEL_ACTIONS = {
    "local": ("cancel_job", "job_id"),
    "batch": ("batch_cancel", "review_id"),
    "assessment": ("assessment_cancel", "job_id"),
    "catalogue_export": ("catalogue_export_cancel", "job_id"),
}


class DesktopJobError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise DesktopJobError(code, message)


def _worker_done(worker):
    """future完成与QThread停止独立于持久终态；未知对象保守阻断。"""
    if worker is None:
        return False
    try:
        if callable(getattr(worker, "done", None)):
            return bool(worker.done())
        if callable(getattr(worker, "isRunning", None)):
            return not worker.isRunning()
    except Exception:
        return False
    return False


def _runtime_maps(service):
    shared = getattr(service, "_futures", None)
    return {
        "local": shared, "batch": shared,
        "assessment": getattr(getattr(service, "assessment", None), "futures", None),
        "catalogue_export": getattr(getattr(service, "catalogue_export", None), "futures", None),
    }


def _project_row(kind, raw, *, project_id, run_id, worker_settled):
    original_status = raw.get("status")
    status = original_status if isinstance(original_status, str) else None
    known = kind in TERMINAL and status in ACTIVE | TERMINAL.get(kind, set())
    if kind in {"import", "foreground"} and status == "creating":
        known = True
    phase = status
    if status == "creating":
        phase = "running"
    if status == "cancel_requested" or (status in {"queued", "running", "creating"}
                                         and raw.get("cancel_requested")):
        phase = "cancelling"
    if status in {"ready", "completed", "not_estimable"}:
        phase = "succeeded"
    error = copy.deepcopy(raw.get("error"))
    if status == "failed" and isinstance(error, dict) and error.get("code") == "PROCESS_INTERRUPTED":
        phase = "interrupted"
    if not known:
        phase = "unknown"
        error = error or {"code": "UNKNOWN_JOB_STATE", "message": "Unknown task state; keep the project open."}
    active = not known or status not in TERMINAL.get(kind, set())
    result_kind = None
    if phase == "succeeded":
        result_kind = {"import": "desktop_project", "local": "acoustic_result",
                       "batch": "review_materials", "assessment": "assessment_report",
                       "catalogue_export": "catalogue_package", "foreground": "operation_result"}.get(kind)
        if status == "not_estimable":
            result_kind = "support_report"
    return {
        "project_id": project_id, "run_id": run_id, "kind": kind,
        "label": raw.get("label", kind),
        "job_id": raw.get("job_id", raw.get("review_id", raw.get("operation_id"))),
        "raw_status": copy.deepcopy(original_status), "phase": phase,
        "cancel_requested": bool(raw.get("cancel_requested") or status == "cancel_requested"),
        "is_active": active, "worker_settled": bool(worker_settled),
        "progress": copy.deepcopy(raw.get("progress")), "error": error,
        "retry_policy": ("new_project" if kind == "import" else "explicit_new_job")
            if phase in {"failed", "cancelled", "interrupted"} else None,
        "result_kind": result_kind,
        "result_handles": {key: copy.deepcopy(raw[key]) for key in (
            "report_url", "bundle_url", "manifest_url", "result_ids") if key in raw},
        "closed": raw.get("closed") if kind == "batch" else None,
        "stale_for_current": bool(raw.get("stale_for_current", raw.get("stale_for_current_revision", False))
                                  or raw.get("scope_stale", False)),
        "stale_reasons": copy.deepcopy(raw.get("stale_reasons", [])),
    }


def blocking_jobs(view_or_rows):
    rows = view_or_rows.get("jobs", []) if isinstance(view_or_rows, dict) else view_or_rows
    return [row for row in rows if row.get("is_active", True) or not row.get("worker_settled", False)]


def project_job_view(snapshot, service=None, *, project_id=None, foreground=None):
    """纯投影；snapshot为同一owner已取得的快照，本函数不再次读库或递增revision。

    结果应存入独立desktop_jobs字段，不能覆盖原snapshot.jobs的local作业字典。
    服务初始化及recover成功后，无本进程future的历史终态才可判settled。
    """
    project_id = project_id or snapshot.get("desktop_project", {}).get("project_id")
    run_id = snapshot.get("run_id")
    owns_run = service is not None and getattr(service, "run_id", None) == run_id and run_id is not None
    final = snapshot.get("final_review", {})
    containers = {
        "local": snapshot.get("jobs", {}),
        "batch": snapshot.get("batch_review", {}).get("reviews", {}),
        "assessment": final.get("assessment", {}).get("jobs", []),
        "catalogue_export": final.get("publication_report", {}).get("jobs", []),
    }
    maps = _runtime_maps(service)
    rows, seen_workers = [], set()
    for kind, records in containers.items():
        records = records.values() if isinstance(records, dict) else records
        futures = maps[kind]
        for raw in records:
            ident = raw.get("job_id", raw.get("review_id"))
            future = futures.get(ident) if isinstance(futures, dict) else None
            status = raw.get("status")
            settled = _worker_done(future) if future is not None else (
                owns_run and isinstance(futures, dict) and isinstance(status, str) and status in TERMINAL[kind])
            rows.append(_project_row(kind, raw, project_id=project_id, run_id=run_id,
                                     worker_settled=settled))
            if future is not None:
                seen_workers.add((id(futures), ident))
    # 即使一个worker尚无可见持久记录，也不能因投影遗漏而退出。
    for futures in maps.values():
        if not isinstance(futures, dict):
            continue
        for ident, future in list(futures.items()):
            token = (id(futures), ident)
            if token in seen_workers:
                continue
            seen_workers.add(token)
            if not _worker_done(future):
                rows.append(_project_row("unknown", {"job_id": ident, "status": "untracked_worker"},
                    project_id=project_id, run_id=run_id, worker_settled=False))
    if foreground is not None:
        raw = foreground.snapshot() if isinstance(foreground, ForegroundOperation) else foreground
        row = _project_row(raw.get("kind", "import"), raw,
            project_id=raw.get("project_id", project_id), run_id=raw.get("run_id", run_id),
            worker_settled=raw.get("worker_settled", False))
        row["generation"] = raw.get("generation")
        rows.append(row)
    blockers = blocking_jobs(rows)
    return {"jobs": rows, "blocking_jobs": blockers, "can_close": not blockers}


def sampled_job_view(snapshot, *, foreground=None, project_id=None, run_id=None,
                     sampling_pending=False, cancellation_requests=None):
    """Merge local UI activity into a frozen packet; never read live service maps."""
    base = snapshot.get("desktop_jobs")
    rows = copy.deepcopy(base.get("jobs", [])) if isinstance(base, dict) else []
    if foreground is not None:
        rows.extend(project_job_view({}, foreground=foreground, project_id=project_id)["jobs"])
    if sampling_pending:
        rows.append({"project_id": project_id, "run_id": run_id, "kind": "sampling",
            "job_id": "owner-state-sample", "label": "Waiting for a consistent state sample",
            "raw_status": "pending", "phase": "sampling_pending", "is_active": True,
            "worker_settled": False, "cancel_requested": False, "progress": None, "error": None})
    for request in (cancellation_requests or {}).get("requests", []):
        waiting = request["state"] in {"queued", "running"} or not request["worker_settled"]
        rows.append({"project_id": request["job"].get("project_id"), "run_id": request["job"].get("run_id"),
            "kind": "cancellation_request", "job_id": request["request_key"],
            "label": "Cancellation request · " + request["job"]["kind"] + " · " + request["job"]["job_id"],
            "raw_status": request["state"], "phase": "cancel_request_pending" if waiting else "failed" if request["state"] == "rejected" else "succeeded",
            "is_active": waiting, "worker_settled": request["worker_settled"], "cancel_requested": False,
            "progress": {"request_key": request["request_key"], "submitted_revision": request["expected_revision"],
                         "target_kind": request["job"]["kind"], "target_id": request["job"]["job_id"]},
            "error": copy.deepcopy(request.get("error"))})
    blockers = blocking_jobs(rows)
    return {"jobs": rows, "blocking_jobs": blockers, "can_close": not blockers,
            "sampling_pending": bool(sampling_pending)}


def _validate_cancel_scope(owner, job, foreground=None):
    _require(isinstance(job, dict), "JOB_SCOPE", "Cancellation requires a task identity.")
    service = getattr(owner, "service", owner)
    metadata = getattr(owner, "metadata", {})
    kind, ident = job.get("kind"), job.get("job_id")
    owner_run = getattr(service, "run_id", None)
    valid_run = (job.get("run_id") in {None, owner_run} if kind in {"import", "foreground"}
                 else owner_run is not None and job.get("run_id") == owner_run)
    _require(valid_run, "JOB_SCOPE", "The task belongs to another run.")
    if metadata.get("project_id") is not None:
        _require(job.get("project_id") in {None, metadata["project_id"]}, "JOB_SCOPE", "The task belongs to another project.")
    _require(isinstance(ident, str) and bool(ident), "JOB_NOT_FOUND", "Missing task identity.")
    if kind in {"import", "foreground"}:
        _require(isinstance(foreground, ForegroundOperation) and foreground.operation_id == ident and foreground.kind == kind,
                 "JOB_SCOPE", "The foreground operation does not match this task.")
    else:
        _require(kind in CANCEL_ACTIONS, "UNKNOWN_JOB_KIND", "This task has no supported cancellation command.")
    return kind, ident


def request_cancel(owner, job, *, expected_revision, idempotency_key,
                   reason="User requested cancellation.", foreground=None):
    """按指定身份原样派发；不刷新版本、不改key、不代替worker写终态。"""
    _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
             "IDEMPOTENCY_REQUIRED", "Cancellation requires the original request key.")
    kind, ident = _validate_cancel_scope(owner, job, foreground)
    if kind in {"import", "foreground"}:
        return foreground.request_cancel(expected_revision=expected_revision,
                                         idempotency_key=idempotency_key, reason=reason)
    action, field = CANCEL_ACTIONS[kind]
    payload = {field: ident}
    if kind in {"assessment", "catalogue_export"}:
        payload["reason"] = reason
    return owner.execute(action, payload, expected_revision=expected_revision,
                         idempotency_key=idempotency_key)


class CancelRequests:
    """Frozen cancellation intents; lock contention never waits on the GUI thread.

    These are local request receipts, not service jobs or authoritative states.
    Uncontended cancellation retains the existing synchronous route; a held
    owner lock sends the same original request to one deferred worker.
    """
    def __init__(self, owner, *, generation=0):
        self.owner, self.generation = owner, generation
        self._lock = threading.RLock()
        self._requests, self._futures = {}, {}
        self._executor, self._closed = None, False

    def _finish(self, key, state, *, result=None, error=None):
        with self._lock:
            self._requests[key].update(state=state, result=copy.deepcopy(result), error=error,
                                      ended_at=datetime.now(timezone.utc).isoformat())

    def _execute(self, key, foreground):
        with self._lock:
            row = copy.deepcopy(self._requests[key]); self._requests[key]["state"] = "running"
        try:
            result = request_cancel(self.owner, row["job"], expected_revision=row["expected_revision"],
                idempotency_key=key, reason=row["reason"], foreground=foreground)
        except Exception as exc:
            self._finish(key, "rejected", error={"code": getattr(exc, "code", "CANCEL_REQUEST_FAILED"), "message": system_error(exc)})
        else: self._finish(key, "applied", result=result)

    def _response(self, key):
        with self._lock:
            row = self._requests[key]
            return {"request_key": key, "request_state": row["state"], "job": copy.deepcopy(row["job"]),
                    "expected_revision": row["expected_revision"], "result": copy.deepcopy(row.get("result")),
                    "error": copy.deepcopy(row.get("error"))}

    def request(self, job, *, expected_revision, idempotency_key, reason, foreground=None, generation=None):
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "Cancellation requires the original request key.")
        _require(generation is None or generation == self.generation, "JOB_SCOPE", "Cancellation belongs to another window generation.")
        kind, _ident = _validate_cancel_scope(self.owner, job, foreground)
        signature = hashlib.sha256(json.dumps({"project_id": job.get("project_id"), "run_id": job.get("run_id"),
            "kind": kind, "job_id": job["job_id"], "expected_revision": expected_revision, "reason": reason},
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        target = tuple(job.get(key) for key in ("project_id", "run_id", "kind", "job_id"))
        with self._lock:
            _require(not self._closed, "JOB_SCOPE", "The previous cancellation owner has been retired.")
            if idempotency_key in self._requests:
                _require(self._requests[idempotency_key]["signature"] == signature,
                         "IDEMPOTENCY_CONFLICT", "Retry the original cancellation payload and revision.")
                return self._response(idempotency_key)
            for key, row in self._requests.items():
                future = self._futures.get(key)
                # A task ID is immutable. Once its cancel intent was applied,
                # repeated clicks keep that receipt while the task winds down.
                if row["target"] == target and (row["state"] in {"queued", "running", "applied"} or future is not None and not future.done()):
                    return self._response(key)
            had_pending = bool(self.snapshot()["blocking_requests"])
            self._requests[idempotency_key] = {"request_key": idempotency_key, "signature": signature,
                "target": target, "job": copy.deepcopy(job), "expected_revision": expected_revision,
                "reason": reason, "generation": self.generation, "state": "queued", "result": None, "error": None,
                "created_at": datetime.now(timezone.utc).isoformat()}
        if kind in {"import", "foreground"}:
            self._execute(idempotency_key, foreground)
        else:
            service = getattr(self.owner, "service", self.owner)
            locked = not had_pending and service._lock.acquire(blocking=False)
            if locked:
                try: self._execute(idempotency_key, foreground)
                finally: service._lock.release()
            else:
                with self._lock:
                    if self._executor is None:
                        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-cancel")
                    try: self._futures[idempotency_key] = self._executor.submit(self._execute, idempotency_key, foreground)
                    except Exception as exc:
                        self._finish(idempotency_key, "rejected", error={"code": "CANCEL_WORKER_START_FAILED", "message": system_error(exc)})
        return self._response(idempotency_key)

    def snapshot(self):
        with self._lock:
            rows = []
            for key, original in self._requests.items():
                row = copy.deepcopy(original); row.pop("signature", None); row.pop("target", None)
                future = self._futures.get(key)
                row["worker_settled"] = future.done() if future is not None else row["state"] not in {"queued", "running"}
                rows.append(row)
            blocking = [row for row in rows if row["state"] in {"queued", "running"} or not row["worker_settled"]]
            return {"requests": rows, "blocking_requests": blocking, "settled": not blocking}

    def close(self):
        with self._lock:
            _require(self.snapshot()["settled"], "CANCEL_REQUEST_BUSY", "Wait for the original cancellation request worker before releasing its owner.")
            self._closed = True
            if self._executor is not None: self._executor.shutdown(wait=False, cancel_futures=False)


class ForegroundOperation:
    """导入/前台命令的短生命周期；不制造持久queued记录或承诺命令可撤回。"""
    def __init__(self, operation_id, *, cancel_event=None, worker=None,
                 project_id=None, run_id=None, generation=0, kind="import", label=None):
        _require(isinstance(operation_id, str) and bool(operation_id), "INVALID_ID", "Missing operation ID.")
        _require(kind in {"import", "foreground"}, "UNKNOWN_JOB_KIND", "Invalid foreground operation kind.")
        self.operation_id = operation_id
        self.kind = kind
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.worker = worker
        self._lock = threading.RLock()
        self._finished = False
        self._requests = {}
        self._data = {"operation_id": operation_id, "kind": kind, "status": "creating", "label": label or kind,
                      "project_id": project_id, "run_id": run_id, "generation": generation}

    def bind_worker(self, worker):
        with self._lock:
            self.worker = worker

    def update(self, *, status=None, progress=None, error=None, project_id=None, run_id=None):
        with self._lock:
            if status is not None:
                _require(status in {"creating", "ready", "succeeded", "failed", "cancelled"},
                         "JOB_STATE", "Invalid foreground operation state.")
                _require(self._data["status"] == "creating" or status == self._data["status"],
                         "JOB_STATE", "A finished operation cannot be restarted.")
                self._data["status"] = status
            for key, value in (("progress", progress), ("error", error),
                               ("project_id", project_id), ("run_id", run_id)):
                if value is not None:
                    self._data[key] = copy.deepcopy(value)

    def mark_finished(self, status="succeeded", **fields):
        _require(status in TERMINAL["import"], "JOB_STATE", "A finished operation needs a terminal state.")
        with self._lock:
            self.update(status=status, **fields)
            self._finished = True

    def snapshot(self):
        with self._lock:
            return {**copy.deepcopy(self._data), "cancel_requested": self.cancel_event.is_set(),
                    "worker_settled": _worker_done(self.worker) if self.worker is not None else self._finished}

    def request_cancel(self, *, expected_revision, idempotency_key, reason):
        with self._lock:
            _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                     "IDEMPOTENCY_REQUIRED", "Cancellation requires the original request key.")
            request = {"expected_revision": expected_revision, "reason": reason}
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            previous = self._requests.get(idempotency_key)
            if previous:
                _require(previous[0] == digest, "IDEMPOTENCY_CONFLICT", "The cancellation request changed.")
                return {**copy.deepcopy(previous[1]), "replayed": True}
            _require(self._data["status"] == "creating", "JOB_FINISHED", "The foreground operation has already ended.")
            self.cancel_event.set()
            receipt = {"operation_id": self.operation_id, "status": "cancel_requested",
                       "idempotency_key": idempotency_key, **request}
            self._requests[idempotency_key] = (digest, receipt)
            return copy.deepcopy(receipt)


class CloseCoordinator:
    """无Qt/IO的退出状态机；调用者负责owner排空守卫和实际资源释放。"""
    def __init__(self, generation=0):
        self.state = "open"
        self.generation = generation

    def begin(self, jobs):
        _require(self.state == "open", "CLOSE_STATE", "A close request is already pending.")
        self.state = "draining"
        return blocking_jobs(jobs)

    def ready(self, jobs):
        return self.state == "draining" and not blocking_jobs(jobs)

    def start_closing(self, jobs):
        if not self.ready(jobs):
            return False
        self.state = "closing"
        return True

    def finish(self):
        _require(self.state == "closing", "CLOSE_STATE", "Workers and draft saving must finish before close.")
        self.state = "closed"
        self.generation += 1

    def abort(self):
        _require(self.state in {"draining", "closing"}, "CLOSE_STATE", "No close request is pending.")
        self.state = "open"

    def reopen(self):
        _require(self.state == "closed", "CLOSE_STATE", "Release the previous owner before reopening.")
        self.state = "open"

    def accepts_callback(self, generation):
        return self.state == "open" and generation == self.generation
