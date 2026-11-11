"""P0离线合同、角色投影与协议检索；不执行作业、不连接模型、不写人工判断。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

DEFAULT_BUNDLE = Path(__file__).resolve().parents[2] / "configs/annotation_assistant/v0_1"
VERSIONS = ("material_version", "boundary_version", "feature_version",
            "global_representation_version")
SCIENTIFIC_ROLES = {"operator"}


class ContractViolation(ValueError):
    """稳定错误码供离线用例和后续服务适配；本身不是认证服务。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ContractViolation(code, message)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_KEY", key)
            result[key] = value
        return result

    def constant(value):
        raise ContractViolation("NONFINITE_NUMBER", value)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant)


class ContractBundle:
    def __init__(self, directory: Path = DEFAULT_BUNDLE):
        self.directory = Path(directory)
        self.schemas = {}
        resources = []
        for path in sorted((self.directory / "schemas").glob("*.schema.json")):
            schema = read_json(path)
            Draft202012Validator.check_schema(schema)
            self.schemas[path.name.removesuffix(".schema.json")] = schema
            resources.append((schema["$id"], Resource.from_contents(schema)))
        require(bool(resources), "MISSING_SCHEMA", str(self.directory))
        # 未登记的引用直接报错；不使用网络解析JSON Schema。
        self.registry = Registry().with_resources(resources)
        self.catalog = read_json(self.directory / "tool_catalog.json")
        self.permissions = read_json(self.directory / "permissions.json")
        self.transitions = read_json(self.directory / "state_transitions.json")

    def shape(self, name: str, record: dict) -> None:
        require(name in self.schemas, "UNKNOWN_SCHEMA", name)
        validator = Draft202012Validator(self.schemas[name], registry=self.registry,
                                        format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(record), key=lambda e: str(e.path))
        require(not errors, "SCHEMA", str(errors[0]) if errors else "")
        canonical_hash(record)  # JSON Schema的number本身不保证Python值有限。

    def _actor(self, record: dict, context: dict, roles=SCIENTIFIC_ROLES) -> None:
        require(context.get("role") in roles, "ROLE_DENIED", 'This session cannot submit that human decision.')
        require(record["actor"] == {"actor_id": context.get("actor_id"),
                                    "role": context.get("role")},
                "ACTOR_MISMATCH", 'The recorded operator does not match the authenticated session.')

    def _base(self, record: dict, task: dict) -> None:
        require(record["task_contract_id"] == task["task_contract_id"],
                "CONTRACT_MISMATCH", 'The task contracts do not match.')
        for key in ("record_kind", "execution_enabled"):
            if key in record:
                require(record[key] == task[key], "EVIDENCE_KIND", key)
        if "input_versions" in record:
            require(record["input_versions"] == {k: task[k] for k in VERSIONS},
                    "STALE_INPUT", 'The material, boundary or representation version does not match.')

    @staticmethod
    def _scope(ids: list, task: dict) -> None:
        require(set(ids) <= set(task["allowed_item_ids"]), "OUT_OF_SCOPE",
                'The item is outside the permitted task scope.')

    def task_contract(self, task: dict, previous: dict | None = None) -> None:
        self.shape("task_contract", task)
        require(task["protocol_version"] == "0.2", "PROTOCOL_VERSION",
                'This contract supports annotation protocol v0.2 only.')
        if task["record_kind"] == "engineering_simulation":
            require(not task["execution_enabled"], "SIMULATION_LIVE",
                    'A simulation contract cannot enable live operations.')
        if task["execution_enabled"]:
            require(all(v is not None for v in task["budgets"].values()),
                    "MISSING_BUDGET", 'Set a budget for the live operation.')
            require(bool(task["participants"]["operator_ids"]) and
                    task["participants"]["coordinator_id"] is not None,
                    "MISSING_PARTICIPANT", 'Select an operator for the live operation.')
        if previous:
            self.shape("task_contract", previous)
            if task["task_contract_id"] == previous["task_contract_id"]:
                require(task == previous, "IMMUTABLE_CONTRACT", 'A contract with the same ID cannot be changed.')
            else:
                require(task["parent_contract_id"] == previous["task_contract_id"],
                        "PARENT_CONTRACT", 'The new contract must reference its parent version.')

    def human_observation(self, record: dict, task: dict, context: dict) -> None:
        self.shape("human_observation", record)
        self._base(record, task)
        self._actor(record, context)
        self._scope(record["item_ids"], task)

    def _observation_reference(self, ident: str, task: dict, context: dict) -> dict:
        observations = context.get("observations", {})
        require(ident in observations, "MISSING_OBSERVATION", ident)
        observation = observations[ident]
        require(observation["observation_id"] == ident, "REFERENCE_ID",
                'The observation ID does not match the index.')
        self.human_observation(observation, task, observation["actor"])
        require(observation["actor"]["actor_id"] == context["operator_id"],
                "OTHER_OPERATOR", 'The referenced observation belongs to a different operator.')
        return observation

    def review_issue(self, record: dict, task: dict, context: dict, *,
                     require_current: bool = True) -> None:
        self.shape("review_issue", record)
        self._base(record, task)
        for key in ("target_ids", "fit_ids", "comparison_ids", "eligible_change_ids"):
            self._scope(record[key], task)
        require(set(record["eligible_change_ids"]) <= set(record["target_ids"]),
                "CHANGE_SCOPE", 'List every item that may change among the issue targets.')
        if require_current:
            require(record["catalogue_revision_at_request"] == context["catalogue_revision"],
                    "STALE_REVISION", 'Use the issue revision for the current catalogue before calculating or submitting a decision.')
        else:
            require(record["catalogue_revision_at_request"] <= context["catalogue_revision"],
                    "FUTURE_REVISION", 'A historical issue cannot refer to a later catalogue version.')
        for ident in record["origin_observation_ids"]:
            self._observation_reference(ident, task, context)
        local = record["action"].startswith("local_")
        if local:
            require(record["action"] in task["condition"]["capabilities"],
                    "CAPABILITY_DENIED", record["action"])
            require(bool(record["fit_ids"]), "EMPTY_FIT", 'The local fitting set is empty.')
            require(bool(record["origin_observation_ids"]), "MISSING_OBSERVATION",
                    'A local calculation requires a recorded reason.')
        else:
            require(not record["fit_ids"], "UNEXPECTED_FIT",
                    'Direct comparison and fixed global ordering do not define a new fitting set.')

    def _memberships(self, rows: list, task: dict) -> dict:
        by_id = {r["local_type_id"]: r for r in rows}
        require(len(by_id) == len(rows), "DUPLICATE_TYPE", 'Duplicate type ID.')
        family_members, leaf_members = set(), set()
        for row in rows:
            self._scope(row["member_ids"], task)
            members = set(row["member_ids"])
            parent = row["parent_type_id"]
            if parent is None:
                require(not members & family_members, "DUPLICATE_MEMBERSHIP",
                        'A member is assigned to more than one family.')
                family_members |= members
            else:
                require(parent in by_id and by_id[parent]["parent_type_id"] is None,
                        "PARENT_TYPE", 'The parent of a leaf type must be a family in this catalogue version.')
                require(members <= set(by_id[parent]["member_ids"]), "PARENT_TYPE",
                        'The leaf member is outside its parent family.')
                require(not members & leaf_members, "DUPLICATE_MEMBERSHIP",
                        'A member is assigned to more than one leaf type.')
                leaf_members |= members
        return by_id

    def human_decision(self, record: dict, task: dict, context: dict) -> None:
        self.shape("human_decision", record)
        self._base(record, task)
        self._actor(record, context)
        require(record["expected_catalogue_revision"] == context["catalogue_revision"],
                "STALE_REVISION", 'This decision was based on an earlier catalogue version.')
        require(record["catalogue_revision_after"] == context["catalogue_revision"] + 1,
                "REVISION_SEQUENCE", 'The catalogue version must advance by one.')
        require(not context.get("deadline_locked", False), "DEADLINE_LOCKED",
                'Decisions made after the cutoff cannot be backdated.')
        issues = context.get("issues", {})
        require(record["issue_id"] in issues, "MISSING_ISSUE", record["issue_id"])
        issue = issues[record["issue_id"]]
        require(issue["issue_id"] == record["issue_id"], "REFERENCE_ID",
                'The issue ID does not match the index.')
        self.review_issue(issue, task, context)
        if record["proposal_id"] is not None:
            proposals = context.get("proposals", {})
            require(record["proposal_id"] in proposals, "MISSING_PROPOSAL",
                    'The proposal referenced by the decision does not exist. A manual decision must explicitly use null.')
            proposal = proposals[record["proposal_id"]]
            require(proposal["proposal_id"] == record["proposal_id"] and
                    proposal["issue_id"] == record["issue_id"],
                    "PROPOSAL_ISSUE", 'The decision refers to a proposal for a different issue.')
            require(proposal["status"] in {"prepared", "displayed"},
                    "PROPOSAL_STATUS", 'A withdrawn or superseded proposal cannot be submitted.')
            self.proposal(proposal, task, context)
        self._scope(record["affected_ids"], task)
        require(set(record["affected_ids"]) <= set(issue["eligible_change_ids"]),
                "CHANGE_SCOPE", 'Reference-only items cannot be reassigned automatically.')
        require(bool(record["observation_ids"]), "MISSING_OBSERVATION",
                'Link the decision to a recorded observation or result.')
        for ident in record["observation_ids"]:
            observation = self._observation_reference(ident, task, context)
            self._actor(observation, context)
        require(record["memberships_before"] == context.get("memberships", []),
                "MEMBERSHIP_SNAPSHOT", 'The pre-submission catalogue snapshot does not match.')
        before = self._memberships(record["memberships_before"], task)
        after = self._memberships(record["memberships_after"], task)
        # 比较每个成员的类型及父关系，防止“affected_ids”漏列附带改动。
        def relations(rows, item):
            return {(key, row["parent_type_id"]) for key, row in rows.items()
                    if item in row["member_ids"]}
        changed = {item for item in task["allowed_item_ids"]
                   if relations(before, item) != relations(after, item)}
        require(changed <= set(record["affected_ids"]), "UNDECLARED_CHANGE",
                'Undeclared membership or parent-child changes were found.')
        if record["operation"] == "retain":
            require(not changed, "OPERATION_CONFLICT", 'Keeping an item cannot change its membership.')
        if "item_axes" in record:
            require(record["axes"] is None, "AXES_MODE",
                    'Choose either individual decisions or one shared decision.')
            rows = record["item_axes"]
            by_item = {row["item_id"]: row["axes"] for row in rows}
            require(len(by_item) == len(rows) and
                    set(by_item) == set(record["affected_ids"]),
                    "AXES_COVERAGE", 'Provide exactly one individual decision for every affected syllable.')
        else:
            require(record["axes"] is not None, "AXES_MODE",
                    'Provide a shared decision if no individual decisions are supplied.')
            by_item = {item: record["axes"] for item in record["affected_ids"]}
        if record["processing_state"] == "unfinished":
            require(not changed, "PROCESSING_STATE", 'An incomplete save cannot change the catalogue.')
        for item, axes in by_item.items():
            self._decision_axes(axes, item, before, after, record)

    def _decision_axes(self, axes: dict, item: str, before: dict, after: dict,
                       record: dict) -> None:
        """共享判断逐成员适用；拆分等异质批次使用item_axes，不替人判断声音。"""
        assigned = axes["family_status"] == "assigned"
        require(assigned == (axes["family_id"] is not None), "AXES_CONFLICT",
                'The family status and ID disagree.')
        require(assigned or axes["leaf_id"] is None, "AXES_CONFLICT",
                'A confirmed leaf type cannot be assigned while the family is unresolved.')
        if axes["component_status"] in {"present", "not_observed_when_assessable"}:
            require(axes["diagnostic_region"] == "assessable", "AXES_CONFLICT",
                    'Confirming or ruling out a component requires an assessable region.')
        if axes["form_status"] in {"main", "variant"}:
            require(assigned and axes["leaf_id"] is not None, "AXES_CONFLICT",
                    'A main form or variant requires evidence at the family and leaf levels.')
            expected = "present" if axes["form_status"] == "variant" else "not_observed_when_assessable"
            require(axes["component_status"] == expected, "AXES_CONFLICT",
                    'The main form or variant conflicts with the diagnostic component status.')
        if record["processing_state"] == "reviewed_unresolved":
            require(record["operation"] == "defer_unknown" and
                    axes["form_status"] in {"unresolved", "candidate_form", "not_applicable"},
                    "PROCESSING_STATE", 'A reviewed but unresolved item cannot be marked as a confirmed main form or variant.')
        if axes["boundary_status"] == "needs_review":
            require(axes["form_status"] not in {"main", "variant"}, "AXES_CONFLICT",
                    'Resolve the boundary issue before confirming a leaf form.')
        if axes["family_id"] is not None:
            require(axes["family_id"] in after and
                    after[axes["family_id"]]["parent_type_id"] is None,
                    "AXES_MEMBERSHIP", 'The family in this decision is missing from the catalogue.')
            require(item in after[axes["family_id"]]["member_ids"],
                    "AXES_MEMBERSHIP", 'The affected syllable does not belong to the specified family.')
        if axes["leaf_id"] is not None:
            require(axes["leaf_id"] in after and
                    after[axes["leaf_id"]]["parent_type_id"] == axes["family_id"],
                    "AXES_MEMBERSHIP", 'The leaf type conflicts with its catalogue parent.')
            require(item in after[axes["leaf_id"]]["member_ids"],
                    "AXES_MEMBERSHIP", 'The affected syllable does not belong to the specified leaf type.')
        old_relations = {(key, row["parent_type_id"]) for key, row in before.items()
                         if item in row["member_ids"]}
        new_relations = {(key, row["parent_type_id"]) for key, row in after.items()
                         if item in row["member_ids"]} - old_relations
        require(record["operation"] != "reject" or not new_relations,
                "OPERATION_CONFLICT", 'Excluding an item cannot add a new membership.')
        # 未定可沿用旧归属用于追溯，但沿用值不是本轮新确认；新增关系须有本轮依据。
        for type_id, parent_id in new_relations:
            if parent_id is None:
                require(assigned and axes["family_id"] == type_id,
                        "AXES_MEMBERSHIP", 'The new family membership requires a matching decision from this round.')
            else:
                require(record["operation"] != "defer_unknown" and
                        axes["leaf_id"] == type_id and axes["family_id"] == parent_id and
                        axes["form_status"] in {"main", "variant", "candidate_form"},
                        "AXES_MEMBERSHIP", 'The new leaf membership lacks supporting evidence or is still marked unresolved.')

    def tool_request(self, record: dict, task: dict, context: dict) -> None:
        self.shape("tool_request", record)
        self._base(record, task)
        tool = self.catalog["tools"][record["tool_name"]]
        role = context.get("role")
        require(role in tool["allowed_roles"], "ROLE_DENIED", record["tool_name"])
        if role == "assistant":
            require(task["condition"]["assistant_enabled"] and
                    "assistant_orchestration" in task["condition"]["capabilities"],
                    "ASSISTANT_DISABLED", 'The assistant is disabled for this condition.')
        cap = tool["required_capability"]
        require(cap is None or cap in task["condition"]["capabilities"],
                "CAPABILITY_DENIED", str(cap))
        if tool["requires_idempotency_key"]:
            require(record["idempotency_key"] is not None, "MISSING_IDEMPOTENCY",
                    'A write request requires an idempotency key.')
        if tool["requires_expected_revision"]:
            require(record["expected_revision"] == context["state_revision"],
                    "STALE_REVISION", 'The task state version does not match the expected version.')
        if tool["mutates_state"]:
            require(not context.get("deadline_locked", False) or record["tool_name"] in {
                "save_checkpoint", "cancel_job", "export_task_record"},
                "DEADLINE_LOCKED", 'The budget is locked.')
        args = record["arguments"]
        for key in ("item_ids", "scope_ids", "target_ids", "fit_ids", "comparison_ids",
                    "eligible_change_ids", "affected_ids", "review_ids"):
            if key in args:
                self._scope(args[key], task)
        if "input_versions" in args:
            require(args["input_versions"] == {k: task[k] for k in VERSIONS},
                    "STALE_INPUT", 'The request refers to a different input version.')
        for key in ("preset_id", "config_id", "view_preset"):
            if key in args:
                require(args[key] in task["preset_ids"], "UNKNOWN_PRESET", args[key])
        for key, records_key in (("issue_id", "issues"), ("job_id", "jobs")):
            if key in args:
                require(args[key] in context.get(records_key, {}), "MISSING_REFERENCE",
                        f"{key} was not found in the current task.")
                self._base(context[records_key][args[key]], task)
        if record["tool_name"] == "start_local_job":
            issue = context["issues"][args["issue_id"]]
            self.review_issue(issue, task, context)
            require(issue["action"].startswith("local_"), "WRONG_ACTION",
                    'Direct comparison does not start local fitting.')
        if record["tool_name"] == "open_review_issue":
            issue = dict(args, schema_version="0.1.0", record_kind=task["record_kind"],
                         execution_enabled=task["execution_enabled"],
                         issue_id="proposed:" + record["request_id"],
                         task_contract_id=task["task_contract_id"], revision=0,
                         parent_issue_id=None, status="open",
                         catalogue_revision_at_request=context["catalogue_revision"])
            self.review_issue(issue, task, context)
        if record["tool_name"] == "export_task_record":
            require(args["export_scope"] in self.permissions["roles"][role]["export_scopes"],
                    "EXPORT_SCOPE", "You cannot export another role's data.")
            require(args["locked_revision"] == context.get("locked_revision"),
                    "UNLOCKED_EXPORT", 'Only locked versions can be exported.')
        if context.get("live_execution"):
            require(task["execution_enabled"], "EXECUTION_DISABLED",
                    'This contract is not ready to start live operations.')
            require(all(v is not None for v in task["budgets"].values()),
                    "MISSING_BUDGET", 'Set the run allowance first.')

    def job_receipt(self, record: dict, task: dict, context: dict) -> None:
        self.shape("job_receipt", record)
        self._base(record, task)
        require(record["issue_id"] in context.get("issues", {}), "MISSING_ISSUE",
                'The calculation must be linked to an issue.')
        issue = context["issues"][record["issue_id"]]
        require(issue["issue_id"] == record["issue_id"], "REFERENCE_ID",
                'The issue index does not match the calculation.')
        self.review_issue(issue, task, context, require_current=False)
        require(record["config_id"] in task["preset_ids"], "UNKNOWN_PRESET",
                record["config_id"])
        for key in ("fit_ids", "comparison_ids", "eligible_change_ids"):
            self._scope(record[key], task)
            require(record[key] == issue[key], "SCOPE_SNAPSHOT", key)
        require(record["action"] == issue["action"] and
                record["issue_revision"] == issue["revision"] and
                record["catalogue_revision_at_request"] ==
                issue["catalogue_revision_at_request"],
                "ISSUE_SNAPSHOT", 'The issue revision for this calculation does not match.')
        stale = record["catalogue_revision_at_request"] != context["catalogue_revision"]
        require(record["stale_for_current_revision"] == stale, "STALE_FLAG",
                'The earlier result has not been marked as historical.')
        require(record["fit_manifest_sha256"] == canonical_hash(record["fit_ids"]) and
                record["comparison_manifest_sha256"] == canonical_hash(record["comparison_ids"]),
                "MEMBER_HASH", 'The declared members do not match their fingerprint.')
        for step in record["transform_steps"]:
            if step["mode"] == "refitted" or (
                    step["name"] == "clustering" and step["mode"] == "computed"):
                require(step["fit_ids"] == record["fit_ids"], "FIT_CONTAMINATION",
                        'The fitting set contains reference items or omits declared members.')
        require(record["input_fingerprint"] == job_fingerprint(record),
                "INPUT_FINGERPRINT", 'The calculation input fingerprint does not match.')
        if record["status"] == "succeeded":
            require(record["output_validation"] == "passed" and bool(record["result_ids"]),
                    "INVALID_SUCCESS", 'A successful receipt requires verified output.')
        else:
            require(not record["result_ids"], "RESULT_NOT_READY",
                    'An unsuccessful job cannot publish a usable result.')
        terminal = record["status"] in {"succeeded", "failed", "cancelled"}
        require((record["finished_at"] is not None) == terminal,
                "JOB_TERMINAL", 'The completion time does not match the final job state.')
        if record["status"] == "cancelled":
            require(record["cancel_requested_at"] is not None, "CANCEL_RECEIPT",
                    'A cancelled job must have a recorded cancellation request.')
        if record["cache_hit"]:
            cached = context.get("cached_jobs", {}).get(record["input_fingerprint"])
            require(cached is not None and cached["result_ids"] == record["result_ids"] and
                    cached["status"] == "succeeded" and cached["output_validation"] == "passed" and
                    cached["input_fingerprint"] == record["input_fingerprint"] and
                    job_fingerprint(cached) == record["input_fingerprint"],
                    "CACHE_MISMATCH", 'No successful result with the same fingerprint is available in the cache.')

    def proposal(self, record: dict, task: dict, context: dict) -> None:
        self.shape("proposal", record)
        self._base(record, task)
        require(record["issue_id"] in context.get("issues", {}), "MISSING_ISSUE",
                'The proposal is missing its issue.')
        issue = context["issues"][record["issue_id"]]
        require(issue["issue_id"] == record["issue_id"], "REFERENCE_ID",
                'The issue index does not match the proposal.')
        self.review_issue(issue, task, context)
        require(record["issue_revision"] == issue["revision"] and
                record["catalogue_revision_at_creation"] == context["catalogue_revision"],
                "STALE_REVISION", 'The proposal version is out of date.')
        require(record["eligible_change_ids"] == issue["eligible_change_ids"],
                "CHANGE_SCOPE", 'The proposal exceeds its reassignment permissions.')
        self._scope(record["review_ids"], task)
        declared_review_scope = set(issue["target_ids"] + issue["fit_ids"] +
                                    issue["comparison_ids"])
        require(set(record["review_ids"]) <= declared_review_scope, "PROPOSAL_SCOPE",
                "The proposal exceeds the issue's fitting, reference or target scope.")
        clauses = {r["id"] for r in read_json(self.directory / "protocol_pack.json")["clauses"]}
        require(set(record["protocol_refs"]) <= clauses, "UNKNOWN_CLAUSE", 'The referenced record does not exist.')
        for ident in record["observation_ids"]:
            self._observation_reference(ident, task, context)
        # 仅裸ID不构成来源证据；直接比较收条也须关联同任务、议题和目录版本。
        usable = set()
        for ident, result in context.get("comparison_results", {}).items():
            if ident not in record["result_ids"]:
                continue
            self._base(result, task)
            require(result["issue_id"] == record["issue_id"] and
                    result["issue_revision"] == record["issue_revision"] and
                    result["catalogue_revision"] == context["catalogue_revision"],
                    "RESULT_ISSUE", 'The direct comparison result belongs to another issue or an earlier revision.')
            require(result["operator_id"] == context["operator_id"],
                    "OTHER_OPERATOR", 'The direct comparison result belongs to another operator.')
            usable.add(ident)
        for job in context.get("jobs", {}).values():
            if not set(job["result_ids"]) & set(record["result_ids"]):
                continue
            require(job["issue_id"] == record["issue_id"] and
                    job["issue_revision"] == record["issue_revision"],
                    "RESULT_ISSUE", 'The proposal refers to a local result for a different issue or revision.')
            self.job_receipt(job, task, context)
            if (job["status"] == "succeeded" and not job["stale_for_current_revision"]
                    and job["cancel_requested_at"] is None):
                usable.update(job["result_ids"])
        require(set(record["result_ids"]) <= usable, "RESULT_NOT_READY",
                'The referenced result is stale, being cancelled or missing.')
        sources = set(record["result_ids"] + record["observation_ids"] + record["protocol_refs"])
        for suggestion in record["suggestions"]:
            self._scope(suggestion["item_ids"], task)
            require(set(suggestion["item_ids"]) <= set(record["review_ids"]),
                    "PROPOSAL_SCOPE", "The suggested item is outside this proposal's review scope.")
            require(bool(suggestion["source_ids"]) and
                    set(suggestion["source_ids"]) <= sources,
                    "UNSUPPORTED_SUGGESTION", 'The suggestion is missing a traceable source.')

    def state_transition(self, record: dict, task: dict, context: dict) -> None:
        self.shape("state_transition", record)
        self._base(record, task)
        machine = self.transitions["machines"][record["object_kind"]]
        edges = [e for e in machine["transitions"] if
                 (e["from"], e["to"], e["event"]) ==
                 (record["from_state"], record["to_state"], record["event"])]
        require(len(edges) == 1, "INVALID_TRANSITION", 'Invalid state transition.')
        edge = edges[0]
        self._actor(record, context, set(edge["allowed_roles"]))
        current = context.get("object_states", {}).get(record["object_id"])
        require(current is not None, "MISSING_STATE", 'The authoritative current state is missing.')
        require(current["state"] == record["from_state"] and
                current["revision"] == record["expected_revision"],
                "STALE_REVISION", 'The state or version has changed.')
        require(record["revision_after"] == record["expected_revision"] + 1,
                "REVISION_SEQUENCE", 'State versions must be consecutive.')
        self._scope(record["affected_ids"], task)
        if record["event"] in {"complete_scope", "close_decided", "close_unresolved"}:
            require(not context.get("deadline_locked", False), "DEADLINE_LOCKED",
                    'Completion cannot be backdated after the cutoff.')
        guards = context.get("guard_evidence", {})
        for guard in edge["guards"]:
            require(guards.get(guard) is True, "MISSING_GUARD", guard)
            if guard in record["checks"]:
                require(record["checks"][guard] is True, "GUARD_CONFLICT", guard)
        if record["event"] == "reopen":
            require(record["parent_object_id"] is not None and bool(record["affected_ids"]),
                    "REOPEN_PROVENANCE", 'Reopening requires a parent version and an affected scope.')

    def tool_result(self, record: dict, task: dict, context: dict) -> None:
        self.shape("tool_result", record)
        self._base(record, task)
        require(record["source_receipt"]["input_versions"] == record["input_versions"],
                "STALE_INPUT", 'The result refers to a different source version.')
        failed = record["status"] in {"failed", "rejected"}
        require((record["error"] is not None) == failed, "RESULT_STATUS",
                'The error field conflicts with the success or failure status.')
        if failed:
            require(not record["result_ids"] and not record["numeric_summaries"],
                    "RESULT_STATUS", 'A failed result cannot include an earlier successful result.')
        req = context.get("requests", {}).get(record["request_id"])
        require(req is not None and req["tool_name"] == record["tool_name"] and
                req["task_contract_id"] == record["task_contract_id"],
                "MISSING_REQUEST", 'The result does not reference its original request.')
        for summary in record["numeric_summaries"]:
            require(summary["source_result_id"] in record["result_ids"],
                    "RESULT_SOURCE", 'The numerical summary is not linked to a result.')

    def validate_record(self, name: str, record: dict, task: dict | None = None,
                        context: dict | None = None) -> None:
        """校验持久记录；context由调用方从可信存储提供，绝不能来自模型参数。"""
        self.shape(name, record)
        if name == "task_contract":
            self.task_contract(record, (context or {}).get("previous_contract"))
            return
        require(task is not None, "MISSING_CONTEXT", 'The task contract is missing.')
        self.task_contract(task)
        self._base(record, task)
        context = context or {}
        handler = getattr(self, name, None)
        if handler is not None:
            handler(record, task, context)

    def project_operator_view(self, task: dict, state: dict) -> dict:
        """白名单投影。仅接收服务端已隔离的当轮状态；不读取协调者元数据文件。"""
        self.task_contract(task)
        view = {
            "schema_version": "0.1.0", "record_kind": task["record_kind"],
            "execution_enabled": task["execution_enabled"],
            "view_id": "view:" + task["task_contract_id"],
            "task_contract_id": task["task_contract_id"],
            "input_versions": {k: task[k] for k in VERSIONS},
            "allowed_item_ids": copy.deepcopy(task["allowed_item_ids"]),
            "protocol_version": task["protocol_version"],
            "preset_ids": copy.deepcopy(task["preset_ids"]),
            "condition": copy.deepcopy(task["condition"]),
            "catalogue_revision": state["catalogue_revision"],
            "catalogue_memberships": copy.deepcopy(state.get("memberships", [])),
            "candidate_assignments": copy.deepcopy(state.get("candidate_assignments", [])),
            "observations": copy.deepcopy(list(state.get("observations", {}).values())),
            "issues": copy.deepcopy(list(state.get("issues", {}).values())),
            "remaining_budget": {k: state.get("remaining_budget", {}).get(k)
                                 for k in ("human_seconds", "compute_seconds", "model_calls")},
            "unfinished_issue_ids": [k for k, r in state.get("issues", {}).items()
                                     if r["status"] in {"open", "comparison_ready",
                                                        "under_human_review", "unfinished"}],
        }
        self.operator_view(view, task, state)
        return view

    def operator_view(self, record: dict, task: dict, context: dict) -> None:
        self.shape("operator_view", record)
        self._base(record, task)
        require(record["allowed_item_ids"] == task["allowed_item_ids"],
                "OUT_OF_SCOPE", "The role view's allowed items have changed.")
        require(record["condition"] == task["condition"], "CONDITION_MISMATCH",
                'The role view permissions differ from the contract.')
        require(record["catalogue_revision"] == context["catalogue_revision"],
                "STALE_REVISION", 'The role view is for an earlier catalogue version.')
        require(record["remaining_budget"] == {
            k: context.get("remaining_budget", {}).get(k)
            for k in ("human_seconds", "compute_seconds", "model_calls")
        }, "BUDGET_SNAPSHOT", 'The remaining allowance must come from the recorded timer. Use null if unknown.')
        self._memberships(record["catalogue_memberships"], task)
        for row in record["candidate_assignments"]:
            self._scope([row["item_id"]], task)
        for row in record["observations"]:
            self.human_observation(row, task, row["actor"])
            require(row["actor"]["actor_id"] == context["operator_id"],
                    "OTHER_OPERATOR", 'The role view contains data for a different operator.')
        for row in record["issues"]:
            self.review_issue(row, task, dict(context, observations={
                r["observation_id"]: r for r in record["observations"]},
                catalogue_revision=record["catalogue_revision"]), require_current=False)
        expected_unfinished = {r["issue_id"] for r in record["issues"] if
                               r["status"] in {"open", "comparison_ready",
                                               "under_human_review", "unfinished"}}
        require(set(record["unfinished_issue_ids"]) == expected_unfinished,
                "UNFINISHED_SCOPE", 'The list of open issues is incomplete or includes closed issues.')

    def search_protocol(self, query: str, limit: int = 5) -> dict:
        """确定性关键词检索：返回可引用条款；不生成科学解释或向量模型调用。"""
        require(isinstance(query, str) and bool(query.strip()), "EMPTY_QUERY", 'Enter a question.')
        require(type(limit) is int and 1 <= limit <= 20, "QUERY_LIMIT", 'The limit must be between 1 and 20.')
        pack = read_json(self.directory / "protocol_pack.json")
        terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", query.lower()))
        rows = []
        for clause in pack["clauses"]:
            haystack = " ".join([clause["title"], clause["text"], *clause["keywords"]]).lower()
            score = sum(3 for k in clause["keywords"] if k.lower() in query.lower())
            score += sum(1 for term in terms if term in haystack)
            if score:
                rows.append((score, clause["id"], clause))
        rows.sort(key=lambda row: (-row[0], row[1]))
        return {"pack_id": pack["pack_id"], "version": pack["version"],
                "matches": [copy.deepcopy(row[2]) for row in rows[:limit]],
                "status": "matched" if rows else "needs_clarification",
                "generated_judgment": False}


def validate_assistant_pair(first: dict, second: dict) -> None:
    """仅助手开关不同；每个同材料任务块配对使用，跨块不要求同一材料。"""
    require(first["evaluation_design"] == second["evaluation_design"] == "assistant_increment",
            "DESIGN_MISMATCH", 'An assistant comparison cannot mix in a local module comparison.')
    keys = (*VERSIONS, "material_manifest_sha256", "feature_manifest_sha256",
            "global_fit_member_ids", "allowed_item_ids", "protocol_version",
            "preset_ids", "budgets", "timing_rules")
    require(all(first[k] == second[k] for k in keys), "UNEQUAL_BASELINE",
            'The two assistant conditions must use the same materials, tools and budget.')
    require(first["condition"]["assistant_enabled"] != second["condition"]["assistant_enabled"],
            "CONDITION_MISMATCH", 'The assistant must be enabled in only one condition.')
    cap = lambda r: set(r["condition"]["capabilities"]) - {"assistant_orchestration"}
    require(cap(first) == cap(second), "UNEQUAL_BASELINE", 'The acoustic tools differ.')


def check_idempotency(entries: list[dict]) -> None:
    """离线验证收条的一致性；不承担运行时事务或扣费。"""
    seen = {}
    for entry in entries:
        key = (entry["task_contract_id"], entry["actor_id"], entry["idempotency_key"])
        value = (entry["request_hash"], entry["result_id"], entry["charge_event_id"])
        require(key not in seen or seen[key] == value, "IDEMPOTENCY_CONFLICT",
                'The same scoped idempotency key has different parameters, receipts or charges.')
        seen[key] = value


def job_fingerprint(record: dict) -> str:
    """指纹限定同任务、议题及计算语义，不能把重聚类缓存冒充局部重排。

    config_id在同一不可变任务合同内须指向不可变预设；P1还需登记实际参数及
    预设血统。P0合成ID和步骤声明不证明真实计算已按该配置执行。
    """
    keys = ("task_contract_id", "issue_id", "action", "input_versions",
            "fit_ids", "comparison_ids", "eligible_change_ids", "transform_steps",
            "config_id", "code_version", "seed", "issue_revision")
    return canonical_hash({k: record[k] for k in keys})
