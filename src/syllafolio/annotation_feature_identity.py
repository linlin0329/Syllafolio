"""Strict existing-run aliases for two explicitly verified source-only revisions.

The English acoustic module changes only known system text. The earlier EI
cleanup retains the exact numerical core. All PCM/matrix/preset/version fields
are still hashed unchanged and must match the complete persisted fingerprint.
New runs use their real current source identities. Historical assessment code
identities are never rewritten or represented as current by this bridge.
"""
from __future__ import annotations
import ast
import copy
import hashlib
from pathlib import Path

LEGACY_FEATURE_SOURCE_SHA256 = '27bae18fb74fd30e0048f0d76b5e27e69447d59badff7861a09c130a8e110f82'
CLEAN_FEATURE_SOURCE_SHA256 = '13aa3e2220fe1af50ace648bcb36146c85fe448022684ba8f863c654ea331663'
LEGACY_ACOUSTIC_SOURCE_SHA256 = 'cdc8b894b98cb641dae7bdf91f54646f0d811777eb2775bcd346631af4e5b842'
ENGLISH_ACOUSTIC_SOURCE_SHA256 = '03b6646aa9ef762131300c75602d17340154ea53e902dfc0c86c747b5411a6f4'
NUMERIC_CORE_AST_SHA256 = '139e588635336ebc61f26cfc27d62be795249cfcfd0e892403d4f96f010734a2'
ACOUSTIC_LOGIC_AST_SHA256 = '7130578fa3d28eb78a193292a08efec89d1895d488f4614171379931137a38ea'
_NUMERIC_NAMES = frozenset({"CONDITION_ID", "TASK_MODE", "EXCLUDED_INDICES", "FEATURE_NAMES", "PARAMETERS", "_cluster", "_neighbors", "_organize"})
_ENGLISH_ACOUSTIC_PRESENTATION = frozenset([' contains an unregistered media item', ' items; use direct comparison when it does not apply.', ' must be a list of path-free identifiers', ' must be a non-negative integer', ': no usable local variation is available.', ': the fixed preset requires at least ', 'A global fit ID has no numeric row.', 'A media subset does not identify all numeric rows; declare feature_item_ids.', 'All features must be finite real numbers.', 'Cancellation requested', 'Choose a new output location; existing jobs cannot be overwritten.', 'Choose an explicit local reordering or reclustering action.', 'Different media items cannot share a numeric row without an explicit record.', 'Duplicate JSON field: ', 'Fixed clustering requires umap-learn, hdbscan and a writable NUMBA_CACHE_DIR; the algorithm is not replaced automatically.', 'Grouped audition requires one sample rate; audio is not resampled automatically.', 'Input versions must be specified explicitly.', 'Non-finite JSON number: ', 'Only mono parent audio is supported.', 'The 105D field names or order do not match; the 94D layout cannot be inferred from dimensions alone.', 'The acoustic matrix must have shape N × 105.', 'The anonymous syllable ID, feature_id and row index do not match.', 'The audio read is incomplete or contains non-finite samples.', 'The comparison package exceeds the fixed preset size.', 'The complete numeric row identities are missing.', 'The fixed global representation contains non-finite values.', 'The integer frame interval exceeds the parent audio.', 'The local representation contains non-finite values.', 'The log-mel matrix must match the 105D matrix row for row.', 'The manifest, numeric input or preset changed during loading.', 'The media item list cannot be empty.', 'The number of complete numeric row IDs does not match.', 'The output clip does not match its parent-source interval.', 'The parent audio changed after loading; create a new input version.', 'The parent audio changed during reading.', 'The parent audio file is missing.', 'The parent audio fingerprint has changed.', 'The same anonymous parent-source ID refers to different WAV files.', 'The selected 94D columns do not match the authoritative 105D layout.', 'The syllable sample rate does not match the parent audio.', 'Unsupported acoustic preset version.', 'Unsupported media or feature manifest version.', 'k must be a positive integer.'])


def _numeric_ast_digest(path):
    nodes = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.FunctionDef) and node.name in _NUMERIC_NAMES or
                isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in _NUMERIC_NAMES for target in node.targets)):
            nodes.append(ast.dump(node, include_attributes=False))
    return hashlib.sha256("\n".join(nodes).encode("utf-8")).hexdigest()


def _acoustic_logic_digest(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _ENGLISH_ACOUSTIC_PRESENTATION:
            node.value = "<system-presentation>"
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def existing_run_feature_identity(dataset, saved_contract):
    """Return only an exact proven saved-input alias; otherwise let strict guards reject."""
    from .annotation_acoustics import _hash
    saved = saved_contract.get("input_fingerprint")
    actual = dataset.input_fingerprint
    if not isinstance(saved, str) or not saved or saved == actual:
        return None
    feature_path = Path(__file__).with_name("ei_b_assistance.py")
    acoustic_path = Path(__file__).with_name("annotation_acoustics.py")
    feature_source = hashlib.sha256(feature_path.read_bytes()).hexdigest()
    acoustic_source = hashlib.sha256(acoustic_path.read_bytes()).hexdigest()
    if feature_source != CLEAN_FEATURE_SOURCE_SHA256 or acoustic_source != ENGLISH_ACOUSTIC_SOURCE_SHA256:
        return None
    if (_numeric_ast_digest(feature_path) != NUMERIC_CORE_AST_SHA256 or
            _acoustic_logic_digest(acoustic_path) != ACOUSTIC_LOGIC_AST_SHA256 or
            dataset._provenance.get("feature_definition_code_sha256") != feature_source or
            dataset._provenance.get("source_code_sha256") != acoustic_source):
        return None
    # Both earlier distributions used the exact same acoustic source. Only the
    # older one used the pre-cleanup EI source. No arbitrary old SHA is accepted.
    for previous_feature in (CLEAN_FEATURE_SOURCE_SHA256, LEGACY_FEATURE_SOURCE_SHA256):
        candidate = copy.deepcopy(dataset._provenance)
        candidate["source_code_sha256"] = LEGACY_ACOUSTIC_SOURCE_SHA256
        candidate["feature_definition_code_sha256"] = previous_feature
        previous = _hash(candidate)
        if previous != saved:
            continue
        changed = [name for name in ("source_code_sha256", "feature_definition_code_sha256")
                   if candidate[name] != dataset._provenance[name]]
        return {"schema": "feature-source-presentation-compat-v2", "existing_saved_run_only": True,
            "actual_input_fingerprint": actual, "effective_input_fingerprint": previous,
            "actual_acoustic_source_sha256": acoustic_source,
            "legacy_acoustic_source_sha256": LEGACY_ACOUSTIC_SOURCE_SHA256,
            "actual_feature_source_sha256": feature_source,
            "legacy_feature_source_sha256": previous_feature,
            "numeric_core_ast_sha256": NUMERIC_CORE_AST_SHA256,
            "acoustic_nonpresentation_ast_sha256": ACOUSTIC_LOGIC_AST_SHA256,
            "substituted_identity_fields": changed,
            "registered_data_preset_and_package_versions_strictly_matched": True,
            "assessment_note": "Source identities changed; historical assessment applicability remains subject to the normal code-identity checks."}
    return None
