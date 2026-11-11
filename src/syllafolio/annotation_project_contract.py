"""日常/工程项目的不可变运行合同；不激活研究条件或模型。"""
from __future__ import annotations

import copy
import hashlib
import json
import math


CONTRACT_VERSION = "0.3.0"
LEGACY_VERSION = "0.2.0"
DEFAULT_BUDGET = object()
PURPOSES = {"engineering", "daily_annotation"}


class ProjectContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, code, message):
    if not value:
        raise ProjectContractError(code, message)


def contract_sha256(contract):
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_creation_options(*, purpose, operator_id=None, budget_seconds=DEFAULT_BUDGET,
                              evidence_source=None):
    """在创建目录或计算之前检查用途；省略预算与明确None有不同含义。"""
    _require(isinstance(purpose, str) and purpose in PURPOSES, "PROJECT_PURPOSE", 'Choose an engineering or daily annotation project. A research condition requires a separate contract.')
    if purpose == "engineering" and operator_id is None:
        operator_id = "local-engineering-operator"
    _require(isinstance(operator_id, str) and bool(operator_id.strip()),
             "OPERATOR_REQUIRED", 'Specify a nonempty operator ID.')
    if budget_seconds is DEFAULT_BUDGET:
        budget_seconds = 3600 if purpose == "engineering" else None
    _require(budget_seconds is None or (type(budget_seconds) in {int, float}
             and math.isfinite(budget_seconds) and budget_seconds >= 0),
             "INVALID_BUDGET", 'The budget must be a finite, nonnegative number of seconds or None.')
    _require(purpose != "daily_annotation" or budget_seconds is None,
             "DAILY_BUDGET", 'Daily annotation does not enforce a deadline at this stage.')
    expected_sources = {"engineering_user", "engineering_test"} if purpose == "engineering" else {"daily_user"}
    if evidence_source is None:
        evidence_source = "engineering_user" if purpose == "engineering" else "daily_user"
    _require(isinstance(evidence_source, str) and evidence_source in expected_sources, "EVIDENCE_SOURCE", 'The source does not match the project purpose.')
    return {"purpose": purpose, "mode": purpose, "operator_id": operator_id,
            "budget_seconds": budget_seconds, "evidence_source": evidence_source,
            "record_kind": "engineering_runtime" if purpose == "engineering" else "daily_annotation_runtime"}


def build_project_contract(*, project_id, run_id, purpose, operator_id=None,
                           input_fingerprint, input_versions, allowed_item_ids,
                           preset_id, preset_sha256, evidence_source=None,
                           budget_seconds=DEFAULT_BUDGET, lineage=None):
    options = validate_creation_options(purpose=purpose, operator_id=operator_id,
                                       budget_seconds=budget_seconds, evidence_source=evidence_source)
    contract = {"schema_version": CONTRACT_VERSION, "project_id": project_id, "run_id": run_id,
                **options, "experiment_enabled": False, "model_calls_allowed": False,
                "execution_enabled": True, "protocol_version": "0.2",
                "allowed_item_ids": copy.deepcopy(allowed_item_ids),
                "input_versions": copy.deepcopy(input_versions), "input_fingerprint": input_fingerprint,
                "preset_id": preset_id, "preset_sha256": preset_sha256,
                "lineage": copy.deepcopy(lineage)}
    return validate_project_contract(contract, run_id=run_id)


def validate_project_contract(contract, *, run_id=None):
    """校验新合同；旧合同须由兼容服务保留原字节/字段，不补填新身份。"""
    _require(isinstance(contract, dict) and contract.get("schema_version") == CONTRACT_VERSION,
             "CONTRACT_VERSION", 'Unsupported project run contract.')
    _require("budget_seconds" in contract, "CONTRACT_IDENTITY", 'Specify the budget explicitly. None is allowed.')
    for key in ("project_id", "run_id", "input_fingerprint", "preset_id", "preset_sha256"):
        _require(isinstance(contract.get(key), str) and bool(contract[key].strip()),
                 "CONTRACT_IDENTITY", 'Missing contract identity field: ' + key)
    _require(run_id is None or contract["run_id"] == run_id, "CONTRACT_IDENTITY", 'The run does not match the contract.')
    options = validate_creation_options(purpose=contract.get("purpose"),
        operator_id=contract.get("operator_id"), budget_seconds=contract.get("budget_seconds", DEFAULT_BUDGET),
        evidence_source=contract.get("evidence_source"))
    _require(all(contract.get(key) == value for key, value in options.items()),
             "CONTRACT_IDENTITY", 'The contract purpose, source, operator or budget does not match.')
    _require(contract.get("experiment_enabled") is False, "EXPERIMENT_DISABLED", 'Research conditions are disabled for this contract.')
    _require(contract.get("model_calls_allowed") is False and "assistant" not in contract,
             "MANUAL_ONLY", 'This contract does not support models or assistants.')
    _require(contract.get("execution_enabled") is True, "P0_EXECUTION_DISABLED", 'The run contract must explicitly allow execution.')
    ids = contract.get("allowed_item_ids")
    _require(isinstance(ids, list) and all(isinstance(item, str) and item for item in ids)
             and len(ids) == len(set(ids)), "CONTRACT_SCOPE", 'Freeze a list of unique item IDs in the contract.')
    _require(isinstance(contract.get("input_versions"), dict) and bool(contract["input_versions"]),
             "CONTRACT_IDENTITY", 'The contract is missing an input version.')
    _require(contract.get("lineage") is None or isinstance(contract["lineage"], dict),
             "CONTRACT_IDENTITY", 'The lineage must be a mapping or None.')
    contract_sha256(contract)  # 同时拒绝非有限/非JSON值。
    return copy.deepcopy(contract)


def contract_metadata(contract, *, source=None):
    """仅从已验证合同派生新记录身份；旧工程运行保留其历史命名。"""
    if contract.get("schema_version") == CONTRACT_VERSION:
        return {"schema_version": CONTRACT_VERSION, "purpose": contract["purpose"],
                "mode": contract["mode"], "record_kind": contract["record_kind"],
                "source": contract["evidence_source"], "operator_id": contract["operator_id"],
                "experiment_enabled": False, "contract_sha256": contract_sha256(contract),
                "project_id": contract["project_id"], "run_id": contract["run_id"]}
    return {"schema_version": LEGACY_VERSION, "mode": "engineering",
            "record_kind": "engineering_runtime", "source": source or "engineering_runtime",
            "experiment_enabled": False,
            **({"operator_id": contract["operator_id"]} if "operator_id" in contract else {})}
