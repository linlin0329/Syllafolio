"""P1工程运行的SQLite状态、幂等写入和任务时钟；不产生真人实验结果。

一个服务进程持有一个Store，多线程共享该实例。单调时钟只累计本进程实际
观察到的区间；重启后保留已落盘时间并中断活动段，不推算断线期间的行为。
声学与三轴语义由调用服务先验证；本层再次在事务中核对版本和关键状态约束。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable
import uuid

from .annotation_file_lock import OwnerFileLock
from .annotation_project_contract import (
    CONTRACT_VERSION, ProjectContractError, contract_metadata, validate_project_contract,
)

RUNTIME_VERSION = "0.2.0"
ENGINEERING_SOURCES = {"engineering_test", "engineering_user"}
ISSUE_STATES = {
    "open", "comparison_ready", "under_human_review", "decided",
    "reviewed_unresolved", "unfinished",
}
JOB_EDGES = {
    "queued": {"running", "cancel_requested", "failed"},
    "running": {"succeeded", "failed", "cancel_requested"},
    "cancel_requested": {"cancelled", "failed", "succeeded"},
    "succeeded": set(), "failed": set(), "cancelled": set(),
}
LATE_OPERATIONS = {
    "record_job", "save_checkpoint", "restore_checkpoint", "visibility",
    "clock_stop", "lock_deadline",
}


class AnnotationStateError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise AnnotationStateError(code, message)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identifier(value, name: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), "INVALID_ID", name)


class AnnotationStateStore:
    """线程安全、单owner的工程状态存储；每个事务接受run级expected_revision。"""

    def __init__(self, db_path: str | Path, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], str] | None = None):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc).isoformat())
        self._active_monotonic: dict[str, float] = {}
        self._closed = False
        try:
            self._lock_file = OwnerFileLock(str(self.path) + ".owner.lock")
        except BlockingIOError as exc:
            raise AnnotationStateError("STORE_IN_USE", 'Another service is using this database.') from exc
        try:
            self._connection = sqlite3.connect(
                self.path, timeout=30, isolation_level=None, check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    contract_json TEXT NOT NULL,
                    contract_sha256 TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    catalogue_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    revision_before INTEGER NOT NULL,
                    revision_after INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    receipt_json TEXT,
                    UNIQUE(run_id, idempotency_key)
                );
            """)
            self._recover_interrupted()
        except Exception:
            if hasattr(self, "_connection"):
                self._connection.close()
            self._lock_file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        """不把关闭前尚未采样的区间补成人时；重开时活动段会显式中断。"""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._lock_file.close()
                self._closed = True

    def _begin(self) -> None:
        _require(not self._closed, "STORE_CLOSED", 'The store is closed.')
        self._connection.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    def _now(self) -> str:
        value = self._utc_now()
        return value.isoformat() if isinstance(value, datetime) else str(value)

    def _mono(self) -> float:
        value = float(self._monotonic())
        _require(math.isfinite(value), "CLOCK_INVALID", 'The monotonic clock must return a finite number.')
        return value

    def _load(self, run_id: str) -> dict:
        row = self._connection.execute(
            "SELECT state_json FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        _require(row is not None, "RUN_NOT_FOUND", run_id)
        return json.loads(row["state_json"])

    def _persist(self, state: dict, *, expected_db_revision: int | None = None) -> None:
        args = (_json(state), state["revision"], state["catalogue_revision"], state["run_id"])
        sql = ("UPDATE runs SET state_json=?,revision=?,catalogue_revision=? "
               "WHERE run_id=?")
        if expected_db_revision is not None:
            sql += " AND revision=?"
            args += (expected_db_revision,)
        cursor = self._connection.execute(sql, args)
        _require(cursor.rowcount == 1, "REVISION_CONFLICT", 'The expected database revision does not match.')

    def _replayed(self, run_id: str, key: str, request_hash: str) -> dict | None:
        row = self._connection.execute(
            "SELECT request_hash,receipt_json FROM events WHERE run_id=? AND idempotency_key=?",
            (run_id, key),
        ).fetchone()
        if row is None:
            return None
        _require(row["request_hash"] == request_hash, "IDEMPOTENCY_CONFLICT",
                 'An idempotency key cannot be reused with different parameters or a different expected revision.')
        result = json.loads(row["receipt_json"])
        result["replayed"] = True
        return result

    def _event(self, state: dict, operation: str, payload: dict, key: str,
               request_hash: str, revision_before: int) -> dict:
        cursor = self._connection.execute(
            "INSERT INTO events(run_id,idempotency_key,request_hash,operation,payload_json,"
            "revision_before,revision_after,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (state["run_id"], key, request_hash, operation, _json(payload),
             revision_before, state["revision"], self._now()),
        )
        receipt = {
            "schema_version": RUNTIME_VERSION, "mode": "engineering",
            "experiment_enabled": False, "source": "engineering_runtime",
            "run_id": state["run_id"], "event_id": cursor.lastrowid,
            "operation": operation, "revision_before": revision_before,
            "revision_after": state["revision"],
            "catalogue_revision": state["catalogue_revision"], "replayed": False,
            "after_deadline": state["deadline_locked"],
        }
        if state["contract"].get("schema_version") == CONTRACT_VERSION:
            receipt.update(contract_metadata(state["contract"]))
        self._connection.execute(
            "UPDATE events SET receipt_json=? WHERE event_id=?",
            (_json(receipt), cursor.lastrowid),
        )
        return receipt

    def create_run(self, run_id: str, contract: dict, *, idempotency_key: str,
                   mode: str = "engineering", experiment_enabled: bool = False) -> dict:
        _identifier(run_id, "run_id")
        _identifier(idempotency_key, "idempotency_key")
        new_contract = contract.get("schema_version") == CONTRACT_VERSION
        if new_contract:
            try:
                contract = validate_project_contract(contract, run_id=run_id)
            except ProjectContractError as exc:
                raise AnnotationStateError(exc.code, str(exc)) from exc
        _require(mode in ({"engineering", "daily_annotation"} if new_contract else {"engineering"}) and experiment_enabled is False,
                 "EXPERIMENT_DISABLED", 'This workflow creates engineering runs only.')
        _require(contract.get("schema_version") in {RUNTIME_VERSION, CONTRACT_VERSION}, "CONTRACT_VERSION",
                 'An independent 0.2.0 run contract is required. Simulation contracts cannot be executed.')
        _require(contract.get("mode", mode) == mode and
                 contract.get("experiment_enabled", False) is False,
                 "EXPERIMENT_DISABLED", 'The contract modes disagree.')
        _require(contract.get("execution_enabled") is not False, "P0_EXECUTION_DISABLED",
                 'A contract that forbids execution cannot be used to run this action.')
        budget = contract.get("budget_seconds", contract.get("budgets", {}).get("human_seconds"))
        _require(budget is None or (type(budget) in {int, float} and
                 math.isfinite(budget) and budget >= 0),
                 "INVALID_BUDGET", 'The engineering time limit must be a finite, nonnegative number of seconds or explicitly unset.')
        contract = copy.deepcopy(contract)
        payload = {"contract": contract, "mode": mode, "experiment_enabled": False}
        request_hash = _hash(payload)
        with self._lock:
            self._begin()
            try:
                prior = self._replayed(run_id, idempotency_key, request_hash)
                if prior is not None:
                    self._connection.commit()
                    return prior
                exists = self._connection.execute(
                    "SELECT contract_sha256 FROM runs WHERE run_id=?", (run_id,),
                ).fetchone()
                _require(exists is None, "IMMUTABLE_RUN", 'An existing run contract cannot be overwritten or reinitialized.')
                state = {
                    "schema_version": RUNTIME_VERSION, "run_id": run_id,
                    "mode": "engineering", "experiment_enabled": False,
                    "record_kind": "engineering_runtime", "contract": contract,
                    "contract_sha256": _hash(contract), "revision": 0,
                    "catalogue_revision": 0, "catalogue_status": "working",
                    "observations": {}, "issues": {}, "decisions": {},
                    "memberships": [], "jobs": {}, "job_runtime": {},
                    "job_recovery_required": [], "checkpoints": {},
                    "deadline_locked": False, "deadline_snapshot": None,
                    "clock": {
                        "status": "not_started", "budget_seconds": budget,
                        "elapsed_seconds": 0.0, "cost_category": "engineering_operation",
                        "human_experiment_seconds": None, "timing_complete": True,
                        "segments": [], "active_segment_id": None,
                        "visibility_hidden": False, "restart_interruptions": 0,
                    },
                }
                if new_contract:
                    state.update(contract_metadata(contract))
                    state["clock"]["cost_category"] = ("daily_work" if mode == "daily_annotation" else "engineering_operation")
                self._connection.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                    (run_id, _json(contract), _hash(contract), _json(state), 0, 0, self._now()),
                )
                receipt = self._event(state, "create_run", payload, idempotency_key,
                                      request_hash, -1)
                self._connection.commit()
                return receipt
            except Exception:
                self._rollback()
                raise

    @staticmethod
    def _capture(state: dict) -> dict:
        """保存可恢复状态；历史事件另表保留，不递归嵌套检查点和截止快照。"""
        keys = ("revision", "catalogue_revision", "catalogue_status",
                "observations", "issues", "decisions", "memberships", "jobs", "clock")
        return copy.deepcopy({key: state[key] for key in keys})

    def _deadline(self, state: dict, reason: str, *, automatic: bool = True) -> None:
        if state["deadline_locked"]:
            return
        clock = state["clock"]
        if clock["active_segment_id"] is not None:
            segment = clock["segments"][-1]
            segment.update(status="deadline_locked", ended_at=self._now(), end_reason=reason)
        clock.update(status="deadline_locked", active_segment_id=None)
        self._active_monotonic.pop(state["run_id"], None)
        before = state["revision"]
        if automatic:
            state["revision"] += 1
        state["deadline_locked"] = True
        snapshot = self._capture(state)
        if not automatic:
            # 显式锁定/零预算启动由外层请求提交一个版本，避免重叠事件版本。
            snapshot["revision"] = before + 1
        state["deadline_snapshot"] = {
            "reason": reason, "detected_at": self._now(), "snapshot": snapshot,
        }
        if automatic:
            self._event(state, "deadline_locked", {"reason": reason},
                        "system:deadline:" + str(uuid.uuid4()),
                        _hash({"reason": reason, "revision": before}), before)

    def _synchronize_clock(self, state: dict) -> None:
        clock = state["clock"]
        if clock["status"] != "running":
            return
        run_id = state["run_id"]
        _require(run_id in self._active_monotonic, "CLOCK_OWNER",
                 'The active timing interval belongs to another service. Recover the interrupted session first.')
        now = self._mono()
        previous = self._active_monotonic[run_id]
        _require(now >= previous, "CLOCK_REGRESSION", 'The monotonic clock moved backwards.')
        elapsed = now - previous
        budget = clock["budget_seconds"]
        remaining = None if budget is None else max(0.0, budget - clock["elapsed_seconds"])
        accounted = elapsed if remaining is None else min(elapsed, remaining)
        clock["elapsed_seconds"] += accounted
        segment = clock["segments"][-1]
        segment["observed_seconds"] += accounted
        segment["last_observed_at"] = self._now()
        self._active_monotonic[run_id] = now
        if remaining is not None and elapsed >= remaining:
            self._deadline(state, "budget_reached")

    def _recover_interrupted(self) -> None:
        with self._lock:
            self._begin()
            try:
                rows = self._connection.execute("SELECT state_json FROM runs").fetchall()
                for row in rows:
                    state = json.loads(row["state_json"])
                    clock = state["clock"]
                    pending = [key for key, job in state["jobs"].items()
                               if job["status"] in {"running", "cancel_requested"}]
                    if clock["status"] != "running" and not pending:
                        continue
                    before = state["revision"]
                    if clock["status"] == "running":
                        segment = clock["segments"][-1]
                        segment.update(status="interrupted", ended_at=None,
                                       interruption_detected_at=self._now(),
                                       end_reason="service_restart_unknown_gap")
                        clock.update(status="interrupted", active_segment_id=None,
                                     timing_complete=False,
                                     restart_interruptions=clock["restart_interruptions"] + 1)
                    # 不伪造worker已经停止；服务必须按持久job身份另行核对。
                    state["job_recovery_required"] = pending
                    state["revision"] += 1
                    self._persist(state, expected_db_revision=before)
                    payload = {"unknown_interval_not_counted": True,
                               "job_ids_requiring_reconciliation": pending}
                    self._event(state, "recover_interrupted", payload,
                                "system:recovery:" + str(uuid.uuid4()), _hash(payload), before)
                self._connection.commit()
            except Exception:
                self._rollback()
                raise

    def read_snapshot(self, run_id: str) -> dict:
        """Read the last committed revision for an offline preview without sampling the clock."""
        with self._lock:
            return self._load(run_id)

    def snapshot(self, run_id: str) -> dict:
        """计时采样落盘；普通时钟采样不改变业务revision，截止锁会改变。"""
        with self._lock:
            self._begin()
            active_before = dict(self._active_monotonic)
            try:
                state = self._load(run_id)
                before = state["revision"]
                self._synchronize_clock(state)
                self._persist(state, expected_db_revision=before)
                self._connection.commit()
                return copy.deepcopy(state)
            except Exception:
                self._rollback()
                self._active_monotonic = active_before
                raise

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            _require(not self._closed, "STORE_CLOSED", 'The store is closed.')
            rows = self._connection.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY event_id", (run_id,),
            ).fetchall()
            return [dict(row, payload=json.loads(row["payload_json"]),
                         receipt=json.loads(row["receipt_json"])) for row in rows]

    def lookup_event(self, run_id: str, idempotency_key: str) -> dict | None:
        """HTTP层可先核原始请求hash，避免重算时间/身份后误拒合法重试。"""
        with self._lock:
            _require(not self._closed, "STORE_CLOSED", 'The store is closed.')
            row = self._connection.execute(
                "SELECT * FROM events WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            return dict(row, payload=json.loads(row["payload_json"]),
                        receipt=json.loads(row["receipt_json"]))

    def transact(self, run_id: str, *, expected_revision: int, idempotency_key: str,
                 operation: str, payload: dict) -> dict:
        _identifier(idempotency_key, "idempotency_key")
        _require(type(expected_revision) is int and expected_revision >= 0,
                 "REVISION_REQUIRED", 'A write requires an explicit expected run revision.')
        payload = copy.deepcopy(payload)
        request_hash = _hash({"operation": operation, "payload": payload,
                              "expected_revision": expected_revision})
        with self._lock:
            self._begin()
            active_before = dict(self._active_monotonic)
            synchronized_state = None
            synchronized_active = None
            try:
                prior = self._replayed(run_id, idempotency_key, request_hash)
                if prior is not None:
                    self._connection.commit()
                    return prior
                state = self._load(run_id)
                db_revision = state["revision"]
                _require(db_revision == expected_revision, "REVISION_CONFLICT",
                         'The state has changed. Review the changes before retrying.')
                self._synchronize_clock(state)
                synchronized_state = copy.deepcopy(state)
                synchronized_active = dict(self._active_monotonic)
                if state["deadline_locked"] and operation not in LATE_OPERATIONS:
                    # 截止快照须提交，不能因为迟到写入被拒绝而回滚锁定。
                    self._persist(state, expected_db_revision=db_revision)
                    self._connection.commit()
                    raise AnnotationStateError("DEADLINE_LOCKED", 'Operator decisions cannot be backdated after the cutoff.')
                before = state["revision"]
                self._apply(state, operation, payload)
                state["revision"] += 1
                self._persist(state, expected_db_revision=db_revision)
                receipt = self._event(state, operation, payload, idempotency_key,
                                      request_hash, before)
                self._connection.commit()
                return receipt
            except Exception:
                had_transaction = self._connection.in_transaction
                if (had_transaction and synchronized_state is not None and
                        synchronized_state["deadline_locked"] and
                        synchronized_state["revision"] > db_revision):
                    # 即使允许的迟到作业/恢复请求本身非法，也不撤销刚形成的截止快照。
                    self._persist(synchronized_state, expected_db_revision=db_revision)
                    self._connection.commit()
                    self._active_monotonic = synchronized_active
                    raise
                self._rollback()
                if had_transaction:
                    self._active_monotonic = active_before
                raise

    @staticmethod
    def _engineering_record(record: dict, state=None) -> None:
        contract = state.get("contract", {}) if state else {}
        allowed_sources = ({contract["evidence_source"]} if contract.get("schema_version") == CONTRACT_VERSION else ENGINEERING_SOURCES)
        _require(record.get("source") in allowed_sources, "EVIDENCE_SOURCE",
                 'Engineering observations and decisions must be marked engineering_test or engineering_user.')
        _require(record.get("experiment_enabled", False) is False, "EXPERIMENT_DISABLED",
                 'An engineering record cannot be marked as a human experiment.')
        if contract.get("schema_version") == CONTRACT_VERSION:
            expected = contract_metadata(contract)
            for key in ("operator_id", "record_kind", "project_id", "run_id", "contract_sha256"):
                _require(key not in record or record[key] == expected[key],
                         "CONTRACT_IDENTITY", 'The record identity does not match the immutable contract: ' + key)

    @staticmethod
    def _in_scope(state: dict, item_ids: list) -> None:
        allowed = state["contract"].get("allowed_item_ids")
        if allowed is not None:
            _require(set(item_ids) <= set(allowed), "OUT_OF_SCOPE", 'The item is outside the run contract.')

    def _apply(self, state: dict, operation: str, payload: dict) -> None:
        if operation == "record_observation":
            record = copy.deepcopy(payload["record"])
            self._engineering_record(record, state)
            ident = record["observation_id"]
            _identifier(ident, "observation_id")
            _require(ident not in state["observations"], "IMMUTABLE_RECORD", ident)
            self._in_scope(state, record.get("item_ids", []))
            state["observations"][ident] = record
        elif operation == "open_issue":
            record = copy.deepcopy(payload["record"])
            ident = record["issue_id"]
            _identifier(ident, "issue_id")
            _require(ident not in state["issues"], "IMMUTABLE_RECORD", ident)
            _require(record.get("status", "open") == "open", "ISSUE_STATE", 'A new issue must start in the open state.')
            for key in ("target_ids", "fit_ids", "comparison_ids", "eligible_change_ids"):
                self._in_scope(state, record.get(key, []))
            record.update(status="open", revision=0)
            state["issues"][ident] = record
        elif operation == "update_issue":
            ident = payload["issue_id"]
            _require(ident in state["issues"], "ISSUE_NOT_FOUND", ident)
            old = state["issues"][ident]
            _require(payload["expected_issue_revision"] == old["revision"],
                     "ISSUE_REVISION_CONFLICT", 'The issue version has changed.')
            record = copy.deepcopy(payload["record"])
            _require(record["issue_id"] == ident, "ISSUE_ID", 'The issue identity cannot change.')
            _require(record["status"] in ISSUE_STATES, "ISSUE_STATE", 'Invalid issue state.')
            _require(record["status"] not in {"decided", "reviewed_unresolved"},
                     "ISSUE_STATE", 'An issue can reach a final state only after an explicit decision covers its members.')
            for key in ("target_ids", "fit_ids", "comparison_ids", "eligible_change_ids"):
                self._in_scope(state, record.get(key, []))
            record["revision"] = old["revision"] + 1
            state["issues"][ident] = record
        elif operation == "commit_decision":
            self._commit_decision(state, payload)
        elif operation == "record_job":
            self._record_job(state, payload["record"])
        elif operation == "save_checkpoint":
            ident = payload["checkpoint_id"]
            _identifier(ident, "checkpoint_id")
            _require(ident not in state["checkpoints"], "IMMUTABLE_CHECKPOINT", ident)
            _require(bool(payload.get("reason")), "REASON_REQUIRED", 'Enter a checkpoint description.')
            state["checkpoints"][ident] = {
                "checkpoint_id": ident, "reason": payload["reason"],
                "created_at": self._now(), "snapshot": self._capture(state),
            }
            if state["catalogue_status"] != "scoped_complete":
                state["catalogue_status"] = "checkpoint"
        elif operation == "restore_checkpoint":
            self._restore_checkpoint(state, payload)
        elif operation in {"clock_start", "clock_pause", "clock_resume", "clock_stop"}:
            self._clock_operation(state, operation, payload)
        elif operation == "visibility":
            _require(type(payload.get("hidden")) is bool, "VISIBILITY_VALUE",
                     'The hidden field must be true or false.')
            state["clock"]["visibility_hidden"] = payload["hidden"]
        elif operation == "lock_deadline":
            _require(bool(payload.get("reason")), "REASON_REQUIRED", 'Provide a reason for locking the run manually.')
            self._deadline(state, payload["reason"], automatic=False)
        else:
            raise AnnotationStateError("UNKNOWN_OPERATION", operation)

    def _commit_decision(self, state: dict, payload: dict) -> None:
        record = copy.deepcopy(payload["record"])
        self._engineering_record(record, state)
        ident = record["decision_id"]
        _identifier(ident, "decision_id")
        _require(ident not in state["decisions"], "IMMUTABLE_RECORD", ident)
        _require(payload["expected_catalogue_revision"] == state["catalogue_revision"],
                 "CATALOGUE_REVISION_CONFLICT", 'The catalogue version has changed.')
        issue_id = record["issue_id"]
        _require(issue_id in state["issues"], "ISSUE_NOT_FOUND", issue_id)
        issue = state["issues"][issue_id]
        _require(issue["status"] not in {"decided", "reviewed_unresolved"},
                 "ISSUE_CLOSED", 'Reopen the closed issue before adding another decision.')
        affected = record.get("affected_ids", [])
        self._in_scope(state, affected)
        eligible = issue.get("eligible_change_ids", issue.get("target_ids", []))
        _require(bool(affected) and set(affected) <= set(eligible),
                 "CHANGE_SCOPE", "The decision has no target or exceeds the issue's permitted scope.")
        processing = record["processing_state"]
        _require(processing in {"decided", "reviewed_unresolved", "unfinished"},
                 "PROCESSING_STATE", 'Invalid processing state.')
        member_states = issue.setdefault("member_processing_states", {})
        member_states.update({item: processing for item in affected})
        requested = payload.get("issue_status_after", "under_human_review")
        _require(requested in {"under_human_review", "unfinished", "decided",
                               "reviewed_unresolved"}, "ISSUE_STATE", 'Invalid post-submission issue state.')
        targets = issue.get("target_ids", [])
        if requested in {"decided", "reviewed_unresolved"}:
            _require(bool(targets) and all(member_states.get(item) in {
                "decided", "reviewed_unresolved"} for item in targets),
                "ISSUE_COVERAGE", 'Some targets are unfinished. The issue cannot be closed.')
            if requested == "decided":
                _require(all(member_states[item] == "decided" for item in targets),
                         "ISSUE_UNRESOLVED", 'Reviewed but unresolved members cannot be counted as resolved.')
        memberships = copy.deepcopy(payload["memberships_after"])
        for row in memberships:
            self._in_scope(state, row.get("member_ids", []))
        state["memberships"] = memberships
        state["catalogue_revision"] += 1
        state["catalogue_status"] = "working"
        record["catalogue_revision_after"] = state["catalogue_revision"]
        state["decisions"][ident] = record
        issue.update(status=requested, revision=issue["revision"] + 1,
                     catalogue_revision_at_request=state["catalogue_revision"])

    def _record_job(self, state: dict, record: dict) -> None:
        record = copy.deepcopy(record)
        ident = record["job_id"]
        _identifier(ident, "job_id")
        status = record["status"]
        _require(status in JOB_EDGES, "JOB_STATE", status)
        old = state["jobs"].get(ident)
        if old is None:
            _require(status == "queued", "JOB_STATE", 'A new job must start in the queue.')
            state["job_runtime"][ident] = {"execution_started": False}
        else:
            _require(status in JOB_EDGES[old["status"]], "JOB_STATE",
                     f"Cannot change from {old['status']} to {status}")
            for key in ("issue_id", "task_contract_id", "input_fingerprint", "config_id"):
                if key in old:
                    _require(record.get(key) == old[key], "IMMUTABLE_JOB_INPUT", key)
        if status == "running":
            _require(not state["deadline_locked"], "DEADLINE_LOCKED",
                     'Queued jobs cannot start after the deadline.')
            _require(not any(
                key != ident and job["status"] in {"running", "cancel_requested"} and
                state["job_runtime"].get(key, {}).get("execution_started", False)
                for key, job in state["jobs"].items()
            ), "JOB_ALREADY_RUNNING", 'Only one calculation job can run at a time in each run.')
            state["job_runtime"][ident]["execution_started"] = True
        if status == "cancelled":
            _require(record.get("worker_confirmed_stopped") is True,
                     "CANCEL_NOT_CONFIRMED", 'A cancellation request does not mean the worker has stopped.')
        if old and old["status"] == "cancel_requested" and status == "succeeded":
            _require(record.get("cancellation_race_logged") is True,
                     "CANCEL_RACE_UNRECORDED", 'Completion after a cancellation request must record the race and cannot submit automatically.')
        if status == "succeeded":
            _require(record.get("output_validation") == "passed" and
                     bool(record.get("result_ids")), "INVALID_JOB_SUCCESS",
                     'A successful job requires a verified result receipt.')
        record["after_deadline"] = state["deadline_locked"]
        state["jobs"][ident] = record
        if ident in state["job_recovery_required"]:
            state["job_recovery_required"].remove(ident)

    def _restore_checkpoint(self, state: dict, payload: dict) -> None:
        ident = payload["checkpoint_id"]
        _require(ident in state["checkpoints"], "CHECKPOINT_NOT_FOUND", ident)
        _require(bool(payload.get("reason")), "REASON_REQUIRED", 'Enter a reason for restoring this checkpoint.')
        snapshot = state["checkpoints"][ident]["snapshot"]
        restored_issues = copy.deepcopy(snapshot["issues"])
        new_catalogue_revision = state["catalogue_revision"] + 1
        for key, issue in restored_issues.items():
            issue["revision"] = max(issue.get("revision", 0),
                                    state["issues"].get(key, {}).get("revision", 0)) + 1
            issue["restored_from_checkpoint"] = ident
            issue["catalogue_revision_at_request"] = new_catalogue_revision
        # 当前作业不得被抹掉，相关议题也保留以供取消、等待或恢复核查。
        for job in state["jobs"].values():
            if job["status"] in {"queued", "running", "cancel_requested"}:
                issue_id = job.get("issue_id")
                if issue_id in state["issues"] and issue_id not in restored_issues:
                    restored_issues[issue_id] = copy.deepcopy(state["issues"][issue_id])
                    restored_issues[issue_id]["preserved_active_job_context"] = True
        state["memberships"] = copy.deepcopy(snapshot["memberships"])
        state["issues"] = restored_issues
        state["catalogue_revision"] = new_catalogue_revision
        state["catalogue_status"] = "checkpoint"
        state["last_restore"] = {
            "checkpoint_id": ident, "reason": payload["reason"],
            "catalogue_revision": state["catalogue_revision"], "restored_at": self._now(),
        }
        # observations/decisions/events/jobs以及clock/deadline全部保留当前值。

    def _clock_operation(self, state: dict, operation: str, payload: dict) -> None:
        clock = state["clock"]
        status = clock["status"]
        if operation in {"clock_start", "clock_resume"}:
            _require(not state["deadline_locked"], "DEADLINE_LOCKED", 'The timer cannot restart after the deadline.')
            allowed = {"not_started"} if operation == "clock_start" else {"paused", "interrupted"}
            _require(status in allowed, "CLOCK_STATE", f"{operation} cannot act on {status}")
            ident = "segment:" + str(uuid.uuid4())
            clock["segments"].append({
                "segment_id": ident, "status": "running", "started_at": self._now(),
                "last_observed_at": self._now(), "observed_seconds": 0.0,
                "ended_at": None, "end_reason": None,
            })
            clock.update(status="running", active_segment_id=ident)
            self._active_monotonic[state["run_id"]] = self._mono()
            if clock["budget_seconds"] is not None and (
                    clock["elapsed_seconds"] >= clock["budget_seconds"]):
                self._deadline(state, "budget_reached", automatic=False)
        elif operation == "clock_pause":
            _require(status == "running", "CLOCK_STATE", 'Only a running clock can be paused.')
            clock["segments"][-1].update(status="paused", ended_at=self._now(),
                                          end_reason=payload.get("reason", "explicit_pause"))
            clock.update(status="paused", active_segment_id=None)
            self._active_monotonic.pop(state["run_id"], None)
        elif operation == "clock_stop":
            if state["deadline_locked"]:
                return
            _require(status in {"running", "paused", "interrupted"}, "CLOCK_STATE",
                     'The clock has not started or has already stopped.')
            if clock["active_segment_id"] is not None:
                clock["segments"][-1].update(status="stopped", ended_at=self._now(),
                                              end_reason=payload.get("reason", "explicit_stop"))
            clock.update(status="stopped", active_segment_id=None)
            self._active_monotonic.pop(state["run_id"], None)

    def record_observation(self, run_id: str, record: dict, **kwargs) -> dict:
        return self.transact(run_id, operation="record_observation", payload={"record": record},
                             **kwargs)

    def open_issue(self, run_id: str, record: dict, **kwargs) -> dict:
        return self.transact(run_id, operation="open_issue", payload={"record": record}, **kwargs)

    def record_job(self, run_id: str, record: dict, **kwargs) -> dict:
        return self.transact(run_id, operation="record_job", payload={"record": record}, **kwargs)

    def commit_decision(self, run_id: str, record: dict, memberships_after: list, *,
                        expected_catalogue_revision: int, issue_status_after: str | None = None,
                        **kwargs) -> dict:
        payload = {"record": record, "memberships_after": memberships_after,
                   "expected_catalogue_revision": expected_catalogue_revision}
        if issue_status_after is not None:
            payload["issue_status_after"] = issue_status_after
        return self.transact(run_id, operation="commit_decision", payload=payload, **kwargs)

    def save_checkpoint(self, run_id: str, checkpoint_id: str, reason: str, **kwargs) -> dict:
        return self.transact(run_id, operation="save_checkpoint",
                             payload={"checkpoint_id": checkpoint_id, "reason": reason}, **kwargs)

    def restore_checkpoint(self, run_id: str, checkpoint_id: str, reason: str, **kwargs) -> dict:
        return self.transact(run_id, operation="restore_checkpoint",
                             payload={"checkpoint_id": checkpoint_id, "reason": reason}, **kwargs)

    def start_clock(self, run_id: str, **kwargs) -> dict:
        return self.transact(run_id, operation="clock_start", payload={}, **kwargs)

    def pause_clock(self, run_id: str, *, reason: str = "explicit_pause", **kwargs) -> dict:
        return self.transact(run_id, operation="clock_pause", payload={"reason": reason}, **kwargs)

    def resume_clock(self, run_id: str, **kwargs) -> dict:
        return self.transact(run_id, operation="clock_resume", payload={}, **kwargs)

    def stop_clock(self, run_id: str, *, reason: str = "explicit_stop", **kwargs) -> dict:
        return self.transact(run_id, operation="clock_stop", payload={"reason": reason}, **kwargs)

    def lock_deadline(self, run_id: str, *, reason: str, **kwargs) -> dict:
        return self.transact(run_id, operation="lock_deadline", payload={"reason": reason}, **kwargs)
