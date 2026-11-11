"""从持久事件解释当前人工决定分支；历史记录和检查点原字节保持。"""
from __future__ import annotations

import json

from .annotation_state import _require


def decision_rows(state):
    records = state.get("decisions", {})
    return list(records.values()) if isinstance(records, dict) else list(records)


def effective_rows(state, identifiers=None):
    """只读投影可显式提供有效ID；缺恢复谱系时宁可不推断当前三轴。"""
    if identifiers is None:
        identifiers = state.get("effective_decision_ids")
    if identifiers is None:
        return [] if state.get("last_restore") else decision_rows(state)
    allowed = set(identifiers)
    return [row for row in decision_rows(state) if row["decision_id"] in allowed]


class DecisionHistory:
    """每个run按快照revision截断的事件缓存，事务内overlay仅作为返回值。"""
    OPERATIONS = ("commit_decision", "manual_final_item_review", "save_checkpoint", "restore_checkpoint")

    def __init__(self, store):
        self.store = store
        self.runs = {}

    @staticmethod
    def _empty():
        return {"revision": -1, "event_id": 0, "active": set(), "seen": set(),
                "checkpoints": {}, "last_restore": None}

    def identifiers(self, state):
        with self.store._lock:
            run_id, revision = state["run_id"], state["revision"]
            cache = self.runs.setdefault(run_id, self._empty())
            if revision < cache["revision"]:
                cache = self.runs[run_id] = self._empty()
            if revision != cache["revision"]:
                rows = self.store._connection.execute(
                    "SELECT event_id,operation,payload_json,receipt_json FROM events WHERE run_id=? "
                    "AND event_id>? AND revision_after<=? AND operation IN "
                    "('commit_decision','manual_final_item_review','save_checkpoint','restore_checkpoint') ORDER BY event_id",
                    (run_id, cache["event_id"], revision)).fetchall()
                for row in rows:
                    payload, operation = json.loads(row["payload_json"]), row["operation"]
                    if operation in {"commit_decision", "manual_final_item_review"}:
                        record = payload["record"] if operation == "commit_decision" else payload["record"]["decision"]
                        cache["active"].add(record["decision_id"])
                        cache["seen"].add(record["decision_id"])
                    elif operation == "save_checkpoint":
                        cache["checkpoints"][payload["checkpoint_id"]] = set(cache["active"])
                    else:
                        ident = payload["checkpoint_id"]
                        _require(ident in cache["checkpoints"], "DECISION_RESTORE_HISTORY", 'The checkpoint restore is missing the event needed to verify the current decision.')
                        cache["active"] = set(cache["checkpoints"][ident])
                        cache["last_restore"] = (ident, json.loads(row["receipt_json"])["catalogue_revision"])
                    cache["event_id"] = row["event_id"]
                cache["revision"] = revision
            active = set(cache["active"])
            restore = state.get("last_restore")
            if restore and (restore["checkpoint_id"], restore["catalogue_revision"]) != cache["last_restore"]:
                # 当前事务已恢复state，但restore事件要等事务成功后写入；不污染缓存。
                ident = restore["checkpoint_id"]
                _require(ident in cache["checkpoints"], "DECISION_RESTORE_HISTORY", 'The restore is missing evidence from the saved checkpoint event.')
                active = set(cache["checkpoints"][ident])
            current_ids = {row["decision_id"] for row in decision_rows(state)}
            # 复合final_item_review中commit已应用、外层事件尚未写入；仅此返回可见。
            active.update(current_ids - cache["seen"])
            return active & current_ids
