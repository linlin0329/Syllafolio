"""尚不能正式处理的族/整池临时只读试听；不登记任务、资产或人工决定。"""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import threading
import uuid

import soundfile as sf

from .annotation_acoustics import _cancel, _sha
from .annotation_assistant_contracts import canonical_hash
from .annotation_family_acoustics import family_audition_route
from .annotation_batch_acoustics import NORMALIZATIONS, prepare_review, render_page_display, validate_batch_display
from .annotation_batch_review import (
    ACTIVE, _effective_active_reviews, active_pool, blank, current_organization, current_pool_review,
    family_definition_fingerprint, family_member_fingerprint, family_members,
    family_title, first_pass_complete, item_version, pending_family_reviews,
    pool_entries, pool_sources, review_stale_reason,
)


class FamilyAuditionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise FamilyAuditionError(code, message)


def _conflicts(state, family_id):
    """与batch_prepare_family调用的reject_review_overlap使用同一冻结范围。"""
    members = set(family_members(state, family_id))
    batch = state.get("batch_review", blank())
    active = set(_effective_active_reviews(batch).values()) | set(batch.get("active_family_reviews", {}).values())
    if batch.get("latest_pool_review_id"):
        active.add(batch["latest_pool_review_id"])
    # 正式prepare同样排除该family自己的上一轮；有效上一轮另行优先处理。
    active.discard(batch.get("active_family_reviews", {}).get(family_id))
    groups = {row["group_id"]: row for row in (batch.get("organization") or
        batch.get("organizations", {}).get(batch.get("active_organization_id"), {})).get("groups", [])}
    rows = []
    for ident in sorted(active):
        row = batch.get("reviews", {}).get(ident, {})
        # 普通族轮可以开始；当前池自己对相交的音节实施只读保护。
        if row.get("closed") or current_pool_review(row) or row.get("status") not in ACTIVE | {"ready"}:
            continue
        overlap = sorted(members & set(row.get("item_ids", [])))
        if not overlap:
            continue
        if row.get("stage") == "first":
            group = groups.get(row.get("group_id"), {})
            name = "Candidate group " + (group.get("label") or row.get("group_id") or ident)
        elif row.get("stage") == "pool":
            name = "Global pool"
        elif row.get("stage") == "family":
            name = "Human family " + family_title(state, row.get("family_id"))
        else:
            name = "Review " + ident
        rows.append({"review_id": ident, "name": name, "stage": row.get("stage"),
                     "item_ids": sorted(row.get("item_ids", [])), "overlap_ids": overlap})
    return rows


def family_review_conflicts(state, family_id):
    """纯snapshot派生的可读阻挡名称；不打开项目、不执行命令。"""
    try:
        return [row["name"] for row in _conflicts(state, family_id)]
    except (KeyError, TypeError, ValueError):
        return []


def _formal_family_available(state, family_id):
    batch = state.get("batch_review", {})
    row = batch.get("reviews", {}).get(batch.get("active_family_reviews", {}).get(family_id), {})
    return bool(row and row.get("status") in ACTIVE | {"ready"} and not any(row.get(k) for k in
        ("closed", "read_only", "archived", "scope_stale")) and not review_stale_reason(state, row))


def _binding(state, family_id):
    members = family_members(state, family_id)
    conflicts = _conflicts(state, family_id)
    _require(members and conflicts and not _formal_family_available(state, family_id),
             "FAMILY_AUDITION_UNAVAILABLE", "Continue an existing family review, or prepare one normally when no review overlaps.")
    return {"project_id": (state.get("desktop_project") or {}).get("project_id"),
            "run_id": state.get("run_id"), "input_fingerprint": state.get("input_fingerprint"),
            "family_id": family_id, "member_ids": members,
            "member_fingerprint": family_member_fingerprint(state, family_id),
            "definition_fingerprint": family_definition_fingerprint(state, family_id),
            "conflicts": [{k: row[k] for k in ("review_id", "stage", "item_ids", "overlap_ids")} for row in conflicts]}


class FamilyAuditionPreview:
    """同步worker API；matches不读库，close不等待在途绘图锁。"""

    _temporary_prefix = "sfa-"
    _error_prefix = "FAMILY"
    _typed_normalization = False
    _identity_message = "The selected family belongs to another project or frozen input."
    _coverage_message = "The temporary pages do not cover the complete family exactly once."

    def __init__(self, project, family_id, binding, temporary):
        self._project = project
        self.family_id = family_id
        self.scope_key = "family:" + family_id if family_id is not None else "pool"
        self.review_id = "family-audition-" + uuid.uuid4().hex
        self._binding = binding
        self._temporary = temporary
        self._root = Path(temporary.name).resolve()
        self._media = self._root / "r"
        self._lock = threading.RLock()
        self._users = 0
        self._closed = False
        self._cleaned = False
        self._cache = {}
        self._pages = {}
        self.review = {}
        self.profile = {}

    @classmethod
    def prepare(cls, project, family_id, state, *, gap_seconds, normalization, settings, check_cancelled=None, page_layout=None):
        binding = _binding(state, family_id)
        route = family_audition_route(len(binding["member_ids"]))
        return cls._prepare_media(project, family_id, binding, state, route=route, gap_seconds=gap_seconds,
                                  normalization=normalization, settings=settings, check_cancelled=check_cancelled, page_layout=page_layout)

    @classmethod
    def _new_instance(cls, project, family_id, binding, temporary):
        return cls(project, family_id, binding, temporary)

    @staticmethod
    def _bound_item_ids(binding):
        return binding["member_ids"]

    @classmethod
    def _prepare_media(cls, project, family_id, binding, state, *, route, gap_seconds, normalization, settings, check_cancelled, page_layout=None):
        """两类共用身份、真实媒体生成、完整覆盖、缓存发布及失败清理。"""
        _require(binding["project_id"] == project.metadata["project_id"]
                 and binding["run_id"] == project.service.run_id
                 and binding["input_fingerprint"] == project.dataset.input_fingerprint,
                 cls._error_prefix + "_AUDITION_STALE", cls._identity_message)
        params = validate_batch_display(settings)
        if cls._typed_normalization:
            valid_normalization = isinstance(normalization, str) and normalization in NORMALIZATIONS
        else:
            valid_normalization = normalization in NORMALIZATIONS
        _require(valid_normalization, "DISPLAY_NORMALIZATION", "Choose per-item or whole-page normalization.")
        _cancel(check_cancelled)
        instance = cls._new_instance(project, family_id, binding, tempfile.TemporaryDirectory(prefix=cls._temporary_prefix))
        instance._begin()
        try:
            ids = cls._bound_item_ids(binding)
            result = prepare_review(project.dataset, ids, instance._media,
                                    route, gap_seconds, normalization, display=params,
                                    check_cancelled=instance._checker(check_cancelled), page_layout=page_layout)
            instance._current(check_cancelled)
            ordered = [ident for page in result["pages"] for ident in page["item_ids"]]
            _require(len(ordered) == len(set(ordered)) == len(ids) and sorted(ordered) == ids,
                     cls._error_prefix + "_AUDITION_COVERAGE", cls._coverage_message)
            instance.profile = copy.deepcopy(result)  # 仅本地临时媒体收条，不进入service状态。
            instance._pages = {page["page_id"]: copy.deepcopy(page) for page in result["pages"]}
            instance.review = instance._review_metadata(state, result, ordered, params, gap_seconds, normalization)
            for page in result["pages"]:
                key = instance._display_key(page["page_id"], params, normalization)
                instance._cache[key] = {"display": copy.deepcopy(page["display"]),
                    "spectrogram": instance._artifact(result, page["spectrogram_path"]),
                    "waveform": instance._artifact(result, page["waveform_path"])}
            return instance
        except Exception:
            instance.close()
            raise
        finally:
            instance._end()

    def _review_metadata(self, state, result, ordered, params, gap_seconds, normalization):
        binding = self._binding
        return {"review_id": self.review_id, "stage": "family", "status": "ready",
                "closed": False, "read_only": True, "audition_only": True, "compact_submit_allowed": False,
                "family_id": self.family_id, "family_title": family_title(state, self.family_id),
                "item_ids": list(binding["member_ids"]), "ordered_item_ids": ordered,
                "pages": copy.deepcopy(result["pages"]), "route": result["route"],
                "gap_seconds": float(gap_seconds), "normalization": normalization,
                "display_settings": params, "review_blockers": family_review_conflicts(state, self.family_id),
                "conflict_review_ids": [row["review_id"] for row in binding["conflicts"]],
                "family_member_fingerprint": binding["member_fingerprint"],
                "family_definition_fingerprint": binding["definition_fingerprint"],
                "input_fingerprint": binding["input_fingerprint"],
                "read_only_reason": "Audition only while overlapping reviews remain open; no decisions are saved here.",
                "result": {k: copy.deepcopy(result[k]) for k in ("parameters", "coverage", "family_ordering", "family_clustering") if k in result}}

    def matches(self, state):
        """只比较已采样身份/冻结成员/定义/仍存在的同一组冲突；无服务调用。"""
        if self._closed or getattr(self._project.service, "_closed", False):
            return False
        try:
            return _binding(state, self.family_id) == self._binding
        except (KeyError, TypeError, ValueError):
            return False

    def _checker(self, check_cancelled):
        return lambda: self._closed or bool(check_cancelled and check_cancelled())

    def _current(self, check_cancelled):
        _cancel(self._checker(check_cancelled))
        _require(self.matches(self._project.snapshot()), "FAMILY_AUDITION_STALE",
                 "Family members, definition or overlapping review changed. Open the current scope again.")
        _cancel(self._checker(check_cancelled))

    def _checked_file(self, path, checksum):
        path = Path(path).absolute()
        _require(path.is_relative_to(self._root), "FAMILY_AUDITION_MEDIA", "Temporary media leaves this preview.")
        cursor = path
        while True:
            _require(not cursor.is_symlink() and not (hasattr(cursor, "is_junction") and cursor.is_junction()),
                     "FAMILY_AUDITION_MEDIA", "Temporary media cannot follow a link or junction.")
            if cursor == self._root:
                break
            cursor = cursor.parent
        _require(path.resolve().is_relative_to(self._root) and path.is_file() and _sha(path) == checksum,
                 "FAMILY_AUDITION_MEDIA", "The frozen temporary media is missing or changed.")
        return path

    def _artifact(self, result, relative):
        _require(isinstance(relative, str) and "\\" not in relative and not Path(relative).is_absolute()
                 and ".." not in Path(relative).parts, "FAMILY_AUDITION_MEDIA", "Invalid temporary asset path.")
        checksum = result["artifacts"][relative]["sha256"]
        return {"path": self._checked_file(self._media / relative, checksum), "sha256": checksum}

    @staticmethod
    def _display_key(page_id, settings, normalization):
        return canonical_hash({"page_id": page_id, "settings": settings, "normalization": normalization})

    def load_page(self, page_id, display_snapshot, check_cancelled=None):
        self._begin()
        try:
            self._current(check_cancelled)
            _require(page_id in self._pages, "FAMILY_AUDITION_PAGE", "Choose a page from this temporary family preview.")
            _require(isinstance(display_snapshot, dict), "DISPLAY_SETTINGS", "Use the current shared display snapshot.")
            params = validate_batch_display(display_snapshot.get("settings", self.review["display_settings"]))
            normalization = display_snapshot.get("normalization", self.review["normalization"])
            _require(normalization in NORMALIZATIONS, "DISPLAY_NORMALIZATION", "Choose per-item or whole-page normalization.")
            page = copy.deepcopy(self._pages[page_id])
            audio = self._checked_file(self._media / page["audio_path"], page["audio_sha256"])
            samples, rate = sf.read(audio, dtype="float32")
            _require(samples.ndim == 1 and len(samples) == page["frame_count"] and rate == page["sample_rate_hz"],
                     "FAMILY_AUDITION_MEDIA", "Temporary PCM does not match its frozen page.")
            key = self._display_key(page_id, params, normalization)
            with self._lock:
                shown = self._cache.get(key)
            if shown is None:
                folder = self._root / ("d-" + uuid.uuid4().hex[:10])
                rendered = render_page_display(audio, page["slots"], folder, params, normalization,
                                               check_cancelled=self._checker(check_cancelled), page_layout=page.get("page_layout"))
                shown = {"display": rendered["display"]}
                for name in ("spectrogram", "waveform"):
                    relative = rendered[name + "_path"]
                    checksum = rendered["artifacts"][relative]["sha256"]
                    shown[name] = {"path": self._checked_file(folder / relative, checksum), "sha256": checksum}
            spectrum = self._checked_file(shown["spectrogram"]["path"], shown["spectrogram"]["sha256"])
            waveform = self._checked_file(shown["waveform"]["path"], shown["waveform"]["sha256"])
            self._current(check_cancelled)
            _require(_sha(audio) == page["audio_sha256"], "FAMILY_AUDITION_MEDIA", "Temporary PCM changed during display.")
            with self._lock:
                _cancel(self._checker(check_cancelled))
                self._cache[key] = shown
                self.review["display_settings"] = params
                self.review["normalization"] = normalization
            page.update(display=copy.deepcopy(shown["display"]), preview_spectrogram_path=str(spectrum),
                        preview_waveform_path=str(waveform), preview_audio_path=str(audio),
                        read_only=True, audition_only=True, review_id=self.review_id)
            return {"page": page, "samples": samples, "rate": int(rate)}
        finally:
            self._end()

    def _begin(self):
        with self._lock:
            _require(not self._closed, "FAMILY_AUDITION_CLOSED", "The temporary family preview is closed.")
            self._users += 1

    def _end(self):
        cleanup = False
        with self._lock:
            self._users -= 1
            if self._closed and not self._users and not self._cleaned:
                self._cleaned = True
                cleanup = True
        if cleanup:
            self._temporary.cleanup()

    def close(self):
        cleanup = False
        with self._lock:
            self._closed = True
            if not self._users and not self._cleaned:
                self._cleaned = True
                cleanup = True
        if cleanup:
            self._temporary.cleanup()


def pool_review_blockers(state):
    """仅返回可由整池只读预听承接的首轮/族轮前置阻挡；其它情形不替代正式流程。"""
    try:
        batch = state.get("batch_review", blank())
        organization = current_organization(batch)
        if not organization or not active_pool(batch):
            return []
        current = batch.get("reviews", {}).get(batch.get("latest_pool_review_id"), {})
        # 已有正式轮应继续；过期、失败或中断的未结束轮也应由正式界面核对/归档。
        if current and not current.get("closed"):
            return []
        reasons = []
        if not first_pass_complete(batch):
            for group in organization.get("groups", []):
                review = batch.get("reviews", {}).get(batch.get("active_reviews", {}).get(group["group_id"]), {})
                if not review.get("closed"):
                    reasons.append("Candidate group " + (group.get("label") or group["group_id"]))
            if not reasons:
                reasons.append("Changed candidate assignments require confirmation")
        for review in sorted(pending_family_reviews(batch), key=lambda row: (row.get("family_id", ""), row["review_id"])):
            reasons.append("Human family " + family_title(state, review.get("family_id")))
        return list(dict.fromkeys(reasons))
    except (KeyError, TypeError, ValueError):
        return []


def _pool_binding(state):
    blockers = pool_review_blockers(state)
    _require(blockers, "POOL_AUDITION_UNAVAILABLE",
             "Apply an organisation and use the current nonempty pool. Continue or archive an existing formal pool review first.")
    batch = state.get("batch_review", blank())
    organization = current_organization(batch)
    ids = active_pool(batch)
    projected = batch.get("pool")
    _require(not projected or projected.get("active_ids") == ids,
             "POOL_AUDITION_STALE", "The sampled pool does not match its current complete source state.")
    sources = pool_sources(batch)
    entries = pool_entries(batch, state)
    active = set(_effective_active_reviews(batch).values()) | set(batch.get("active_family_reviews", {}).values())
    unfinished = []
    for ident in sorted(active):
        row = batch.get("reviews", {}).get(ident, {})
        if not row or row.get("closed"):
            continue
        item = {"review_id": ident, "stage": row.get("stage"), "status": row.get("status"),
                "item_ids": sorted(row.get("item_ids", [])), "group_id": row.get("group_id"),
                "family_id": row.get("family_id")}
        if row.get("stage") == "family":
            item.update(member_fingerprint=family_member_fingerprint(state, row["family_id"]),
                        definition_fingerprint=family_definition_fingerprint(state, row["family_id"]))
        unfinished.append(item)
    # 不使用总revision/进度/显示偏好；真实来源、处置版本和活动冻结范围改变才失效。
    return {"project_id": (state.get("desktop_project") or {}).get("project_id"),
            "run_id": state.get("run_id"), "input_fingerprint": state.get("input_fingerprint"),
            "input_versions": copy.deepcopy(state.get("input_versions") or state.get("contract", {}).get("input_versions")),
            "protocol_version": batch.get("protocol_version", "0.4"),
            "organization_id": batch.get("active_organization_id"),
            "organization_fingerprint": canonical_hash(organization), "active_ids": ids,
            "entries": {ident: copy.deepcopy(entries[ident]) for ident in ids},
            "sources": {ident: copy.deepcopy(sources.get(ident, [])) for ident in ids},
            "assignment_versions": {ident: item_version(state, ident) for ident in ids},
            "current_assignments": {ident: copy.deepcopy(batch.get("assignments", {}).get(ident)) for ident in ids},
            "algorithmic_prior_assignments": {ident: copy.deepcopy(batch.get("algorithmic_prior_assignments", {}).get(ident)) for ident in ids},
            "current_protocol_restore_id": batch.get("current_protocol_restore_id"),
            "active_candidate_maps": copy.deepcopy(_effective_active_reviews(batch)),
            "active_family_maps": copy.deepcopy(batch.get("active_family_reviews", {})),
            "unfinished_reviews": unfinished, "first_pass_complete": first_pass_complete(batch)}


class PoolAuditionPreview(FamilyAuditionPreview):
    """整个当前池的临时预听；正式pool_cluster仍由原服务守卫独立管理。"""

    _temporary_prefix = "spa-"
    _error_prefix = "POOL"
    _typed_normalization = True
    _identity_message = "The current pool belongs to another project or frozen input."
    _coverage_message = "The temporary pages do not cover the complete current pool exactly once."

    def __init__(self, project, binding, temporary):
        super().__init__(project, None, binding, temporary)
        self.review_id = "pool-audition-" + uuid.uuid4().hex

    @classmethod
    def prepare(cls, project, state, *, gap_seconds, normalization, settings, check_cancelled=None, page_layout=None):
        binding = _pool_binding(state)
        return cls._prepare_media(project, None, binding, state, route="pool_cluster", gap_seconds=gap_seconds,
                                  normalization=normalization, settings=settings, check_cancelled=check_cancelled, page_layout=page_layout)

    @classmethod
    def _new_instance(cls, project, family_id, binding, temporary):
        return cls(project, binding, temporary)

    @staticmethod
    def _bound_item_ids(binding):
        return binding["active_ids"]

    def _review_metadata(self, state, result, ordered, params, gap_seconds, normalization):
        binding = self._binding
        return {"review_id": self.review_id, "stage": "pool", "status": "ready",
                "closed": False, "read_only": True, "audition_only": True, "compact_submit_allowed": False,
                "item_ids": list(binding["active_ids"]), "ordered_item_ids": ordered,
                "pages": copy.deepcopy(result["pages"]), "route": "pool_cluster",
                "gap_seconds": float(gap_seconds), "normalization": normalization, "display_settings": params,
                "review_blockers": pool_review_blockers(state),
                "pool_entries": copy.deepcopy(binding["entries"]), "pool_sources": copy.deepcopy(binding["sources"]),
                "pool_state_fingerprint": canonical_hash(binding), "input_fingerprint": binding["input_fingerprint"],
                "read_only_reason": "Audition only while first-pass or family reviews remain unfinished; no pool decisions are saved here.",
                "result": {k: copy.deepcopy(result[k]) for k in ("parameters", "coverage", "pool_clustering") if k in result}}

    def matches(self, state):
        if self._closed or getattr(self._project.service, "_closed", False):
            return False
        try:
            return _pool_binding(state) == self._binding
        except (KeyError, TypeError, ValueError):
            return False

    def _current(self, check_cancelled):
        _cancel(self._checker(check_cancelled))
        _require(self.matches(self._project.snapshot()), "POOL_AUDITION_STALE",
                 "Pool members, source versions or active reviews changed. Open the current pool again.")
        _cancel(self._checker(check_cancelled))
