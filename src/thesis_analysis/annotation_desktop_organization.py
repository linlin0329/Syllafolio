"""A3原生组织控制器：先审阅冻结预览，再由操作者明确应用。"""
from __future__ import annotations

import copy
import math
import threading

class OrganizationControllerError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise OrganizationControllerError(code, message)


def organization_summary(preview):
    """保留服务原始理由/影响；只派生展示计数，不拟合或解释为类型真值。"""
    groups = [row for row in preview.get("groups", []) if row.get("kind") != "unclustered"]
    noise = list(preview.get("algorithmic_unclustered_ids", []))
    if not noise:
        noise = [item for row in preview.get("groups", []) if row.get("kind") == "unclustered"
                 for item in row.get("item_ids", [])]
    recommendation = copy.deepcopy(preview.get("recommendation", {}))
    return {
        "requested_mode": preview.get("requested_mode"), "actual_mode": preview.get("mode"),
        "height": preview.get("height"), "input_item_count": preview.get("input_item_count", 0),
        "microcluster_count": preview.get("microcluster_count", 0), "candidate_group_count": len(groups),
        "group_sizes": [len(row.get("item_ids", [])) for row in groups],
        "largest_group_size": max((len(row.get("item_ids", [])) for row in groups), default=0),
        "groups": [{"group_id": row.get("group_id"), "label": row.get("label"), "kind": row.get("kind"),
                    "item_count": len(row.get("item_ids", [])), "item_ids": list(row.get("item_ids", [])),
                    "source_cluster_ids": list(row.get("source_cluster_ids", []))} for row in groups],
        "unclustered_count": preview.get("unclustered_count", len(noise)), "unclustered_ids": noise,
        "all_unclustered": not groups and bool(preview.get("input_item_count")),
        "recommendation": recommendation, "reason_code": recommendation.get("reason_code"),
        "reason": recommendation.get("reason", ""), "recommendation_used": bool(preview.get("recommendation_used")),
        "fallback_to_microclusters": preview.get("requested_mode") == "auto" and preview.get("mode") == "microclusters",
        "impact": copy.deepcopy(preview.get("impact", {})), "ward": copy.deepcopy(preview.get("ward", {})),
        "source_fingerprint": preview.get("source_fingerprint"),
        "new_fit": preview.get("new_fit"), "is_human_classification": preview.get("is_human_classification"),
    }


class OrganizationController:
    """UI参数与预览/确认绑定；服务保持唯一写入权威和最终幂等守卫。"""
    def __init__(self):
        self._lock = threading.RLock()
        self._parameters = {"mode": "auto", "height": None}
        self._ticket = 0
        self._cached = None
        self._confirmation = None
        self._requests = {}
        self._invalid_reason = "Preview the selected organization before applying it."

    def _invalidate(self, reason):
        self._ticket += 1
        self._cached = None
        self._confirmation = None
        self._invalid_reason = reason

    def invalidate(self, reason="The project context changed; preview again."):
        with self._lock:
            self._invalidate(reason)

    def set_parameters(self, mode="auto", height=None):
        try:
            _require(isinstance(mode, str) and mode in {"auto", "microclusters", "ward"},
                     "ORGANIZATION_MODE", "Select auto, original microclusters or manual Ward.")
            if mode == "ward":
                _require(type(height) in {int, float} and math.isfinite(height) and height >= 0,
                         "WARD_HEIGHT", "Manual Ward requires a finite nonnegative merge height.")
                height = float(height)
            else:
                _require(height is None, "WARD_HEIGHT", "Only manual Ward accepts a merge height.")
        except OrganizationControllerError:
            self.invalidate("Invalid organization parameters; correct them and preview again.")
            raise
        parameters = {"mode": mode, "height": height}
        with self._lock:
            if parameters != self._parameters:
                self._parameters = parameters
                self._invalidate("Organization parameters changed; preview and confirm again.")
            return self.snapshot()

    @staticmethod
    def _identity(project):
        return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id}

    @classmethod
    def _binding(cls, project, state, generation, parameters):
        return {**cls._identity(project), "revision": state["revision"], "generation": generation,
                "input_fingerprint": state.get("input_fingerprint"),
                "protocol_version": state.get("batch_review", {}).get("protocol_version"),
                **parameters}

    def preview(self, project, *, generation, idempotency_key):
        with self._lock:
            self._invalidate("A newer preview is being requested.")
            parameters, ticket = copy.deepcopy(self._parameters), self._ticket
        # 与服务用同一RLock读取和预览，防止“预览内容”绑定到不同的业务版本。
        with project.service._lock:
            state = project.snapshot()
            binding = self._binding(project, state, generation, parameters)
            payload = {"mode": parameters["mode"]}
            if parameters["mode"] == "ward":
                payload["height"] = parameters["height"]
            response = project.execute("batch_preview", payload, expected_revision=binding["revision"],
                                       idempotency_key=idempotency_key)
        value = {"preview": copy.deepcopy(response["preview"]), "binding": binding,
                 "summary": organization_summary(response["preview"]), "preview_request": payload,
                 "preview_request_key": idempotency_key, "applied": False}
        with self._lock:
            _require(ticket == self._ticket and parameters == self._parameters,
                     "ORGANIZATION_PREVIEW_CHANGED", "Parameters changed while the preview was being prepared.")
            self._cached = value
            self._invalid_reason = None
            return self.snapshot()

    def acknowledge(self, *, acknowledged, impact_fingerprint):
        with self._lock:
            _require(self._cached is not None, "ORGANIZATION_PREVIEW_REQUIRED", "Preview the current organization first.")
            self._confirmation = None
            _require(type(acknowledged) is bool, "ORGANIZATION_CONFIRMATION", "Impact acknowledgement must be explicit.")
            if not acknowledged:
                return self.snapshot()
            impact = self._cached["preview"].get("impact", {})
            _require(impact_fingerprint == impact.get("fingerprint"), "ORGANIZATION_CONFIRMATION",
                     "The acknowledgement does not match the displayed impact.")
            self._confirmation = {"acknowledged": True, "impact_fingerprint": impact_fingerprint}
            return self.snapshot()

    def observe_context(self, state, *, project_id, generation):
        """轮询已有快照即可失效旧预览，不新增数据库采样或隐式预览。"""
        with self._lock:
            if self._cached is None:
                return False
            binding = self._cached["binding"]
            current = {"project_id": project_id, "run_id": state.get("run_id"), "revision": state.get("revision"),
                       "generation": generation, "input_fingerprint": state.get("input_fingerprint"),
                       "protocol_version": state.get("batch_review", {}).get("protocol_version")}
            if any(binding.get(key) != value for key, value in current.items()):
                self._invalidate("The project context changed; preview and confirm again.")
                return True
            return False

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._cached) if self._cached is not None else {"preview": None, "binding": None, "summary": None}
            result.update(parameters=copy.deepcopy(self._parameters), confirmation=copy.deepcopy(self._confirmation),
                          invalid_reason=self._invalid_reason, can_apply=self._cached is not None
                          and not self._cached.get("applied") and (not self._cached["preview"].get("impact", {}).get("requires_confirmation")
                                                                  or self._confirmation is not None))
            return result

    def apply(self, project, *, generation, idempotency_key, note=""):
        _require(isinstance(note, str) and len(note) <= 4000, "ORGANIZATION_NOTE", "Optional note must be text of at most 4000 characters.")
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "An explicit apply request key is required.")
        with project.service._lock:
            with self._lock:
                prior = self._requests.get(idempotency_key)
                if prior is not None:
                    _require(prior["identity"] == self._identity(project) and prior["generation"] == generation
                             and prior["parameters"] == self._parameters and prior["note"] == note,
                             "IDEMPOTENCY_CONFLICT", "The apply key belongs to a different original request.")
                    _require(self._cached is None or self._cached["preview"]["preview_fingerprint"] == prior["payload"]["preview_fingerprint"],
                             "IDEMPOTENCY_CONFLICT", "The apply key cannot be reused for a new preview.")
                    # 精确重放原请求，不用当前revision覆写旧收条的expected_revision。
                    return project.execute("batch_apply", copy.deepcopy(prior["payload"]),
                        expected_revision=prior["revision"], idempotency_key=idempotency_key)
                _require(self._cached is not None and not self._cached.get("applied"), "ORGANIZATION_PREVIEW_REQUIRED",
                         self._invalid_reason or "Preview the current organization before applying it.")
                cached = copy.deepcopy(self._cached)
                state = project.snapshot()
                binding = self._binding(project, state, generation, self._parameters)
                if binding != cached["binding"]:
                    self._invalidate("The project context changed; preview and confirm again.")
                    raise OrganizationControllerError("ORGANIZATION_PREVIEW_CHANGED", self._invalid_reason)
                impact = cached["preview"].get("impact", {})
                _require(not impact.get("requires_confirmation") or self._confirmation is not None,
                         "ORGANIZATION_CONFIRMATION", "Read the displayed impact and explicitly acknowledge it before applying.")
                payload = {**cached["preview_request"], "preview_fingerprint": cached["preview"]["preview_fingerprint"], "note": note}
                if self._confirmation is not None:
                    payload["confirmation"] = copy.deepcopy(self._confirmation)
                request = {"identity": self._identity(project), "generation": generation,
                           "parameters": copy.deepcopy(self._parameters), "note": note,
                           "payload": copy.deepcopy(payload), "revision": binding["revision"]}
                self._requests[idempotency_key] = request
                try:
                    result = project.execute("batch_apply", payload, expected_revision=binding["revision"],
                                             idempotency_key=idempotency_key)
                except Exception:
                    self._invalidate("The shared service rejected apply; inspect the error and preview again.")
                    raise
                self._cached["applied"] = True
                self._confirmation = None
                return result
