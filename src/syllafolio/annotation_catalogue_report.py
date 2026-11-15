'Static readable catalogue report and CSV. Present frozen records without recomputing metrics or certifying scientific validity.\n\nwrite_catalogue_report(output_dir, document) writes offline HTML, original-value JSON, CSV and documentation.\nrender_html(document, asset_urls=None) shares offline and local-preview content; asset_urls maps only controlled package-relative assets to same-origin local URLs. Unknown values remain unknown; stale assessments are separated from the current catalogue.\n'
from __future__ import annotations

import copy
import csv
import html
import json
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote, urlsplit

from .annotation_english import system_text

SCHEMA_VERSION = "annotation-catalogue-readable-v1"
AXIS_FIELDS = ("family_status", "family_id", "diagnostic_region", "component_status",
               "form_status", "leaf_id", "boundary_status")
TYPE_FIELDS = ("local_type_id", "parent_type_id", "title", "definition_state", "card_id", "card_revision",
               "task_member_count", "scope_member_count", "member_ids", "scope_member_ids",
               "diagnostic_body", "main_variant_difference", "representative_ids", "atypical_ids",
               "counterexample_ids", "competing_type_ids", "uncertainties", "reason")
ITEM_FIELDS = ("item_id", "in_scope", "family_id", "leaf_id", "processing_state", "disposition",
               "label_status", "category", "reason", "disposition_reason", "reason_code", "reason_code_label",
               "reason_source", "submission_contract_version", "axes_reason", "source_clip_id", "raw_recording_id", "source_frame_start",
               "source_frame_stop", "sample_rate_hz", "family_status", "diagnostic_region",
               "component_status", "form_status", "boundary_status", "axes", "notes", "family_name", "leaf_name")
SOURCE_FIELDS = ("item_id", "in_scope", "source_clip_id", "raw_recording_id", "status", "evidence", "origin", "record_id")
MEDIA_FIELDS = ("item_id", "type_id", "role", "in_scope", "wav_path", "spectrogram_path", "wav_sha256",
                "spectrogram_sha256", "source_clip_id", "raw_recording_id", "source_frame_start", "source_frame_stop",
                "sample_rate_hz", "parent_audio_sha256", "pcm_sha256", "audio_processing", "display")
ASSESSMENT_ID_FIELDS = ("assessment_job_id", "assessment_status", "stale_for_current", "assessment_snapshot_fingerprint")
SUPPORT_FIELDS = ("family_id", "n_in_scope", "n_confirmed", "n_known_source", "n_unknown_source",
                  "n_source_groups", "max_source_share", "max_source_share_denominator", "source_counts", "eligible", "reasons")

STATUSES = {"draft": 'Progress draft', "complete": 'Complete delivery for the declared scope', "completed": 'Computation completed',
            "not_estimable": 'Not estimable', "failed": 'Computation failed', "cancelled": 'Cancelled',
            "interrupted": 'Interrupted', "queued": 'Queued', "running": 'Running', "not_started": 'Not run',
            "unfinished": 'Unfinished', "decided": 'Disposition recorded', "reviewed_unresolved": 'Reviewed with unresolved items',
            "confirmed": 'Family membership confirmed', "unresolved": 'Unresolved', "excluded": 'Excluded',
            "reviewed": 'Definition reviewed', "stale": 'Definition stale', "missing": 'Definition missing',
            "representative": 'Human-selected representative', "atypical": 'Human-selected atypical example', "counterexample": 'Human-selected counterexample'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)


def _text(value):
    if value is None or value == "":
        return 'Not recorded'
    if isinstance(value, (dict, list, tuple)):
        return _json(value)
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def _e(value):
    return html.escape(_text(value), quote=True)


def _status(value):
    return STATUSES.get(value, system_text(_text(value)))


def _disposition_code(row):
    code = row.get("reason_code")
    if code is None or code == "":
        return None
    label = system_text(row.get("reason_code_label"))
    return f"{label} [{code}]" if label else f"Unrecognized category [{code}]"


def _disposition_source(row):
    if "reason_source" not in row:
        return 'Legacy record: source not recorded'
    value = row["reason_source"]
    label = {"operator_note": 'Operator note', "operator_code": 'Operator-selected category',
             "not_provided": 'No note supplied (allowed for this disposition)'}.get(value)
    return f"{label} [{value}]" if label else value


def _percent(value):
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else 'Not estimated'


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    # 表格软件可能把前置空白、换行或制表符后的符号解释为公式。只转义文本，数值保持数值。
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError('Report assets must use controlled package-relative paths')
    parsed = urlsplit(value)
    path = PurePosixPath(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or path.is_absolute() or any(p in (".", "..") for p in value.split("/")):
        raise ValueError('Report assets cannot contain external URLs, query parameters or out-of-scope paths')
    return value


def _href(value, asset_urls):
    value = _relative_path(value)
    if value in asset_urls:
        url = asset_urls[value]
        if not isinstance(url, str):
            raise ValueError('Online report asset mappings must be same-origin local URLs')
        parsed = urlsplit(url)
        absolute_local = url.startswith("/") and not url.startswith("//")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        bound_relative = (re.fullmatch(r"media/asset-[0-9a-f]{24}", parsed.path) is not None
                          and len(pairs) == 1 and pairs[0][0] == "token" and bool(pairs[0][1]))
        if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in url or not (absolute_local or bound_relative):
            raise ValueError('Online report asset mappings must be controlled local paths')
        return html.escape(url, quote=True)
    return html.escape(quote(value, safe="/._-"), quote=True)


def _link(path, label, asset_urls):
    return f'<a href="{_href(path, asset_urls)}">{_e(label)}</a>'


def _table(headers, rows, *, empty='No records', classes=""):
    rows = list(rows)
    if not rows:
        return f'<p class="muted">{_e(empty)}</p>'
    head = "".join(f"<th scope=\"col\">{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap {classes}"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _paragraphs(value):
    return "".join(f"<p>{_e(line)}</p>" for line in _text(value).splitlines())


def _reasons(values):
    if not values:
        return '<p class="muted">No additional limitations recorded.</p>'
    rows = []
    for row in values:
        if isinstance(row, dict):
            rows.append(system_text(row.get("message")) or row.get("code") or _json(row))
        else:
            rows.append(system_text(row))
    return '<ul class="reasons">' + "".join(f"<li>{_e(row)}</li>" for row in rows) + "</ul>"


def localize_system_document(document):
    """Translate known system fields in a derived export, never human text or audit history."""
    result = copy.deepcopy(document)

    def messages(rows):
        if not isinstance(rows, list):
            return rows
        for index, row in enumerate(rows):
            if isinstance(row, dict):
                if "message" in row:
                    row["message"] = system_text(row["message"])
            elif isinstance(row, str):
                rows[index] = system_text(row)
        return rows

    for row in result.get("items", []):
        if "reason_code_label" in row:
            row["reason_code_label"] = system_text(row["reason_code_label"])
    completion = (result.get("completion") or {}).get("report") or {}
    messages(completion.get("missing"))
    sources = result.get("source_registry") or {}
    for field in ("issues", "blocking_reasons", "warnings"):
        messages(sources.get(field))
    assessment = result.get("assessment") or {}
    for field in ("reasons", "stale_reasons"):
        messages(assessment.get(field))
    error = assessment.get("error")
    if isinstance(error, dict) and "message" in error:
        error["message"] = system_text(error["message"])
    elif isinstance(error, str):
        assessment["error"] = system_text(error)
    report = assessment.get("report") or {}
    for field in ("reasons", "warnings", "limitations"):
        messages(report.get(field))
    for row in report.get("support", []):
        messages(row.get("reasons"))
    preset = report.get("preset")
    if isinstance(preset, dict) and "note" in preset:
        preset["note"] = system_text(preset["note"])
    return result


def _type_rows(document):
    result = []
    for row in document.get("catalogue", []):
        card = row.get("card") or {}
        result.append({
            **{key: copy.deepcopy(row.get(key)) for key in ("local_type_id", "parent_type_id", "definition_state")},
            "title": row.get("title") or card.get("title"), "card_id": card.get("card_id"), "card_revision": card.get("revision"),
            "task_member_count": len(row.get("member_ids", [])), "scope_member_count": len(row.get("scope_member_ids", [])),
            "member_ids": row.get("member_ids", []), "scope_member_ids": row.get("scope_member_ids", []),
            **{key: copy.deepcopy(card.get(key)) for key in TYPE_FIELDS[10:]},
        })
    return result


def _item_rows(document):
    result = []
    names = {r["local_type_id"]: r.get("title") or (r.get("card") or {}).get("title")
             for r in document.get("catalogue", [])}
    for row in document.get("items", []):
        values = copy.deepcopy(row)
        values["family_name"] = names.get(row.get("family_id"))
        values["leaf_name"] = names.get(row.get("leaf_id"))
        axes = values.get("axes") or {}
        for field in AXIS_FIELDS:
            if field not in {"family_id", "leaf_id"}:
                values[field] = axes.get(field, values.get(field))
        result.append(values)
    return result


def _source_rows(document):
    items = {row["item_id"]: row for row in document.get("items", [])}
    mappings = document.get("source_registry", {}).get("mappings", [])
    if isinstance(mappings, dict):
        mappings = list(mappings.values())
    return [{**copy.deepcopy(row), "in_scope": items.get(row.get("item_id"), {}).get("in_scope", False)} for row in mappings]


def _media_rows(document):
    result = []
    items = {row["item_id"]: row for row in document.get("items", [])}
    for media in document.get("media", []):
        for key in ("wav_path", "spectrogram_path"):
            if media.get(key):
                _relative_path(media[key])
        for role in media.get("roles", []):
            item = items.get(media.get("item_id"), {})
            result.append({**{key: item.get(key) for key in ("source_clip_id", "raw_recording_id")},
                           **copy.deepcopy(media), **role, "in_scope": item.get("in_scope", False)})
    return result


def _assessment(document):
    assessment = document.get("assessment") or {}
    report = assessment.get("report")
    valid = isinstance(report, dict) and assessment.get("status") in {"completed", "not_estimable"} and report.get("status") == assessment.get("status")
    return assessment, report if valid else None


def assessment_family_names(state, full_report):
    """Resolve display labels for the actual definitions bound to this run."""
    names = {}
    for key, definition in full_report["frozen_snapshot"]["definitions"].items():
        card_id = definition.get("card_id")
        if card_id:
            card = state.get("type_cards", {}).get(key, {})
            if card.get("card_id") != card_id:
                card = state.get("type_card_history", {}).get(card_id, {})
            if card.get("local_type_id") == key and card.get("title"):
                names[key] = card["title"]
    return names


def _assessment_html(document, asset_urls, *, standalone=False):
    assessment, report = _assessment(document)
    stale = bool(assessment.get("stale_for_current"))
    status = assessment.get("status", "not_started")
    names = assessment.get("family_display_names", {})
    def family(key):
        return names.get(key) or key
    intro = f'<p class="status">Selected run: {_e(assessment.get("job_id"))} · {_e(_status(status))}</p>'
    if stale:
        intro += '<p class="notice historical">Historical assessment: it does not match this catalogue. The following content is the original assessment snapshot, not metrics for the current catalogue.</p>' + _reasons(assessment.get("stale_reasons"))
    elif report:
        intro += '<p class="notice">The assessment matches this frozen catalogue; it still measures internal label predictability, not annotation accuracy or independent external validation.</p>'
    else:
        intro += '<p class="notice">There is no complete assessment report matching the current catalogue. Not run, failed or cancelled does not mean assessment completed; metrics are not borrowed from another run.</p>'
    if assessment.get("error"):
        intro += _reasons([assessment["error"]])
    if not report:
        return '<section id="assessment"><h2>Internal acoustic assessment</h2>' + intro + '</section>'
    if assessment.get("report_path"):
        intro += '<p>' + _link(assessment["report_path"], 'Full frozen assessment report and computation audit JSON', asset_urls) + '</p>'
    coverage = report.get("coverage") or {}
    coverage_rows = [
        ('Syllables in declared scope', coverage.get("n_scope")), ('Eligible syllables', coverage.get("n_eligible")),
        ('In-scope syllable coverage', _percent(coverage.get("item_fraction"))),
        ('All declared or referenced families', coverage.get("n_families")), ('Eligible families', coverage.get("n_eligible_families")),
        ('Catalogue family coverage', _percent(coverage.get("family_fraction"))),
        ('Confirmed family members with unknown source', coverage.get("n_unknown_source")),
        ('In-scope unresolved', coverage.get("n_unresolved")), ('In-scope excluded', coverage.get("n_excluded")),
    ]
    body = intro + _table(['Assessment scope', 'Count / proportion'], coverage_rows)
    body += '<p class="muted">Syllable coverage uses all syllables in the declared scope as its denominator. Family coverage uses all declared or referenced families, including empty and out-of-scope families. Algorithmic noise is included equally after human assignment. Computational eligibility does not establish type validity.</p>'
    metrics = report.get("metrics") if report.get("status") == "completed" else None
    if metrics:
        body += '<div class="metrics">' + ''.join(
            f'<div><span>{label}</span><strong>{_e(_percent((metrics.get(key) or {}).get("mean")))}</strong><small>SD across partitions {_e(_percent((metrics.get(key) or {}).get("sd")))}</small></div>'
            for key, label in (("balanced_accuracy", "Balanced accuracy"), ("macro_f1", "Macro-F1"))) + '</div>'
        unique = (report.get("plan") or {}).get("n_unique_partitions")
        body += f'<p>Five repeats were planned; the number of distinct partitions was {_e(unique)}. SD across partitions is not a confidence interval. All five repeats use the same syllables; sample size is not multiplied by five.</p>'
        if unique == 1:
            body += '<p class="notice">Only one distinct partition occurred. An SD of zero is not evidence of stability across partitions.</p>'
    else:
        body += '<p class="notice">Not estimable: retain type support and specific reasons without classification scores; random syllable splits are not used to manufacture scores.</p>'
    body += _reasons(report.get("reasons"))
    support = report.get("support", [])
    body += '<h3>Source support and eligibility</h3>' + _table(["Family", "Stable family ID", 'In scope', 'Confirmed', 'Known / unknown source', 'Source groups', 'Maximum source share', 'Count eligibility / limitations'],
        [(family(r.get("family_id")), r.get("family_id"), r.get("n_in_scope"), r.get("n_confirmed"), f'{r.get("n_known_source", "?")} / {r.get("n_unknown_source", "?")}',
          r.get("n_source_groups"), _percent(r.get("max_source_share")), 'Count requirements met' if r.get("eligible") else "; ".join(x.get("message", x.get("code", "")) for x in r.get("reasons", []))) for r in support])
    body += '<p class="muted">Maximum source share uses confirmed syllables with known sources in that family as its denominator. Original-recording groups are declared by the operator; consistency checks do not certify recording identity or different individual birds.</p>'
    body += '<h3>Per-family recall</h3>' + _table(["family", 'Syllable n', 'Mean recall', 'SD across partitions'],
        [(family(r.get("family_id")), r.get("n"), _percent(r.get("recall_mean")), _percent(r.get("recall_sd"))) for r in report.get("per_family", [])], empty='No estimable per-family recall for this run.')
    confusion = report.get("confusion") or {}
    body += '<h3>Main confusion directions</h3>' + _table(['Human family in selected assessment', 'Predicted family', 'Mean row-normalized confusion', 'SD across partitions'],
        [(family(r.get("actual_family_id")), family(r.get("predicted_family_id")), _percent(r.get("rate_mean")), _percent(r.get("rate_sd"))) for r in confusion.get("top_pairs", [])], empty='No nonzero main confusion pairs to report, or confusion is not estimable for this run.')
    family_ids = confusion.get("family_ids", [])
    if 0 < len(family_ids) <= 20:
        body += '<h3>Confusion matrix</h3>' + _table(
            ["Human family ↓ / Predicted →", *(family(i) for i in family_ids)],
            ([family(row_id), *(_percent(x) for x in values)] for row_id, values in
             zip(family_ids, confusion["matrix_mean_row_normalized"], strict=True)))
    body += '<p>' + _link("tables/assessment_confusion.csv", 'Complete mean normalized confusion matrix CSV (including zeros)', asset_urls) + ' · ' + _link("tables/assessment_repeats.csv", 'Per-repeat metrics CSV', asset_urls) + ' · ' + _link("tables/assessment_predictions.csv", 'All out-of-fold predictions CSV', asset_urls) + '</p>'
    body += '<p class="muted">Rows are the human families in the selected assessment; columns are model predictions. The main pairs are at most 20 directions stored in the frozen report; CSV retains the complete matrix. Low scores do not automatically relabel, reopen review or merge types.</p>'
    body += '<h3>Method and limitations</h3><p>Fixed 94-dimensional non-energy acoustics and 2048-dimensional log-mel representations; scaling, PCA and classifiers are fitted only within training folds. Declared original recordings define groups for 3 folds × 5 repeats with class-balanced logistic regression. At least 10 confirmed syllables with known sources, 3 source groups and 2 eligible families are computational requirements, not thresholds of biological reliability.</p>'
    body += _reasons(report.get("warnings")) + _reasons(report.get("limitations"))
    body += '<details><summary>Frozen preset, software versions and fold plan</summary><pre>' + _e({"preset": report.get("preset"), "packages": report.get("audit", {}).get("packages"), "plan": report.get("plan")}) + '</pre></details>'
    title = 'Historical internal assessment' if stale else 'Assessment results'
    if not standalone:
        title = 'Historical internal assessment' if stale else 'Internal acoustic assessment'
    return f'<section id="assessment" class="{"historical" if stale else ""}"><h2>{title}</h2>{body}</section>'


CSS = """
:root{color-scheme:light;--ink:#23353d;--muted:#62757d;--line:#dce4e7;--accent:#345e67;--paper:#fff;--wash:#f2f6f7}*{box-sizing:border-box}body{margin:0;background:#edf1f2;color:var(--ink);font:15px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",sans-serif}main{max-width:1180px;margin:32px auto;padding:36px 44px;background:var(--paper);border:1px solid var(--line)}header{border-bottom:3px solid var(--accent);padding-bottom:24px}h1{font-size:29px;line-height:1.3;margin:8px 0 12px}h2{font-size:22px;line-height:1.4;margin:0 0 18px}h3{font-size:17px;margin:24px 0 10px}p{margin:8px 0 14px}.eyebrow{letter-spacing:.13em;font-size:11px;font-weight:700;color:var(--accent)}.meta,.muted,small{color:var(--muted)}.meta{font-size:13px;overflow-wrap:anywhere}.badge{display:inline-block;background:var(--wash);border:1px solid #b7cbd0;padding:3px 12px;border-radius:4px;font-weight:600}.notice{border-left:3px solid #79949c;background:var(--wash);padding:12px 16px}.historical .notice,.notice.historical{border-left-color:#977e58;background:#f7f3ec}nav{display:flex;gap:9px 22px;flex-wrap:wrap;margin-top:20px}a{color:var(--accent);text-underline-offset:3px}section{padding:30px 0;border-bottom:1px solid var(--line)}.table-wrap{width:100%;overflow:auto;margin:10px 0 16px}table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}th,td{padding:9px 11px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}th{background:var(--wash);font-weight:600}td{max-width:380px}code,pre{font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:14px;background:var(--wash)}.type-card{margin:20px 0;border:1px solid var(--line);padding:22px;break-inside:avoid}.type-card.leaf{margin-left:26px;border-left:3px solid #b8cdd2}.type-card h3{margin-top:0}.type-meta{font-size:13px;color:var(--muted)}.example-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.example{margin:0;border:1px solid var(--line);padding:12px;break-inside:avoid}.example img{width:100%;height:auto;display:block}.example figcaption{font-size:12px;overflow-wrap:anywhere}.example audio{width:100%;margin-top:8px}.metrics{display:flex;gap:20px;flex-wrap:wrap;margin:20px 0}.metrics>div{min-width:190px;padding:16px 20px;background:var(--wash);border:1px solid var(--line)}.metrics span,.metrics strong,.metrics small{display:block}.metrics strong{font-size:27px;font-weight:600}.metrics small{font-size:12px}summary{cursor:pointer;color:var(--accent);font-weight:600}details{margin:14px 0}.reasons{padding-left:22px}.reasons li{margin-bottom:6px}.print-note{font-size:12px;color:var(--muted)}footer{padding-top:24px;font-size:12px;color:var(--muted)}@media(max-width:720px){main{margin:0;padding:24px 16px;border:0}.example-grid{grid-template-columns:1fr}.type-card.leaf{margin-left:12px}h1{font-size:25px}}@page{size:A4;margin:16mm}@media print{body{background:white;font-size:10pt}main{max-width:none;margin:0;padding:0;border:0}header{border-bottom-width:2px}nav,.print-note,audio{display:none}h1{font-size:21pt}h2{font-size:15pt}h3{font-size:12pt}section{padding:18px 0}table{font-size:8.5pt}thead{display:table-header-group}tr,img{break-inside:avoid}.table-wrap{overflow:visible}th,td{padding:5px 6px}.type-card{padding:12px}.example-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.example{padding:8px}a{color:inherit}.metrics strong{font-size:19pt}details{display:block}details>*{display:block!important}summary{list-style:none}pre{font-size:8pt}footer{font-size:8pt}}
"""


CATALOGUE_CSS = """
.catalogue-home main {max-width:1240px;margin:24px auto;padding:28px 36px}
.catalogue-home header {padding-bottom:16px}
.cover-title,.card-heading {display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:4px 16px}
.cover-title h1 {margin:0}
.cover-title .badge {font-size:13px}
.cover-purpose {font-size:17px;margin-bottom:5px}
.cover-counts {display:flex;flex-wrap:wrap;gap:4px 20px;list-style:none;margin:12px 0 0;padding:0}
.cover-counts li {white-space:nowrap}
.catalogue-home header nav {margin-top:12px}
.family-directory {display:grid;grid-template-columns:minmax(180px,23%) minmax(0,1fr);align-items:start;gap:26px}
.family-index {position:sticky;top:12px;display:block;margin:0;padding:12px 14px;background:var(--wash);border:1px solid var(--line)}
.family-index h2 {font-size:17px;margin:0}
.family-index p {font-size:13px;margin:3px 0}
.family-index ul {margin:0;padding:0;list-style:none}
.family-index .index-list {margin-top:8px;max-height:56vh;overflow:auto}
.family-index li a {display:block;padding:5px 4px;overflow-wrap:anywhere;text-decoration:none}
.family-index li a:hover,.family-index li a:focus {text-decoration:underline;background:white}
.family-index li ul {padding-left:14px;border-left:2px solid var(--line)}
.index-id {display:block;color:var(--muted);font-size:11px}
.family-cards>.type-card:first-child {margin-top:0}
.catalogue-home .type-card {margin:0 0 22px;padding:16px 20px;border-top:3px solid var(--accent);scroll-margin:10px}
.catalogue-home .type-card:target {outline:2px solid var(--accent);outline-offset:2px}
.catalogue-home .type-card.leaf {margin-left:16px;border-top-color:var(--line)}
.card-heading h3 {font-size:21px;margin:0;overflow-wrap:anywhere}
.card-heading a {font-size:13px}
.catalogue-home .type-card h4 {margin:10px 0 3px}
.type-meta {margin:2px 0 8px}
.diagnostic-criteria p {margin:0 0 6px}
.catalogue-home .example-grid {grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:8px}
.catalogue-home .example {max-width:520px;background:#fcfdfd;padding:8px}
.catalogue-home .example img {max-height:230px;object-fit:contain;object-position:left center}
.catalogue-home .example audio {display:block;margin:3px 0}
.example summary,.example p {font-size:12px}
.example details {margin:4px 0 0}
@media(max-width:780px) {
  .catalogue-home main {margin:0;padding:18px 14px}
  .family-directory {grid-template-columns:1fr;gap:14px}
  .family-index {position:static}
  .family-index .index-list {max-height:23vh}
  .catalogue-home .type-card {padding:12px}
}
@media print {
  .catalogue-home main {margin:0;padding:0}
  .family-directory {display:block}
  .family-index,.card-heading a {display:none}
  .catalogue-home .example {max-width:none}
}
"""


def _type_anchor(type_id):
    # Produce unique fragment IDs even for Unicode and URL punctuation.
    return "type-" + type_id.encode("utf-8").hex()


def _type_link(row, include_id=False):
    key = row["local_type_id"]
    name = row.get("title") or key
    label = _e(name)
    if include_id:
        context = f'{row["scope_member_count"]}/{row["task_member_count"]} in scope'
        suffix = _e(key) + " · " if name != key else ""
        label += f' <span class="index-id">{suffix}{context}</span>'
    return f'<a href="#{_type_anchor(key)}">{label}</a>'


def _example_player(media, *, filtered, asset_urls):
    ident = media["item_id"]
    role = media["role"]
    scope = "In scope" if media["in_scope"] else "Outside scope · reference example"
    parts = [f'<figure class="example" data-syllable-id="{_e(ident)}" data-role="{_e(role)}">',
             f'<figcaption><strong>{_e(ident)}</strong> · {_e(_status(role))} · {scope}</figcaption>']
    image = media.get("spectrogram_path")
    if image:
        parts.append(f'<img src="{_href(image, asset_urls)}" alt="Spectrogram of {_e(ident)}" loading="lazy">')
    wav = media.get("wav_path")
    if wav:
        parts.append(f'<audio controls preload="none" aria-label="Listen to {_e(ident)}" src="{_href(wav, asset_urls)}"></audio>')
    parts.append('<details><summary>Source and media details</summary>')
    if wav:
        parts.append(_link(wav, "Open filtered syllable WAV" if filtered else "Open syllable WAV", asset_urls))
    parts.append(f'<p>Parent clip {_e(media.get("source_clip_id"))} · Original recording {_e(media.get("raw_recording_id"))}</p>')
    parts.append('<pre>' + _e({k: media.get(k) for k in ("audio_processing", "source_frame_start", "source_frame_stop",
        "sample_rate_hz", "display", "wav_sha256", "spectrogram_sha256")}) + '</pre></details></figure>')
    return "".join(parts)


def _type_card(row, type_map, media, *, filtered, asset_urls):
    key = row["local_type_id"]
    parent = row["parent_type_id"]
    name = row.get("title") or key
    level = "Family" if parent is None else "Fine form · " + (_type_link(type_map[parent]) if parent in type_map else _e(parent))
    parts = [f'<article id="{_type_anchor(key)}" class="type-card {"family" if parent is None else "leaf"}" data-type-id="{_e(key)}" tabindex="-1">',
             f'<div class="card-heading"><h3>{_e(name)}</h3><a href="#family-index">Family index</a></div>',
             f'<p class="type-meta">{level} · <code>{_e(key)}</code> · {_e(_status(row["definition_state"]))}<br>'
             f'{row["scope_member_count"]} in scope / {row["task_member_count"]} project members</p>',
             '<div class="diagnostic-criteria"><h4>Diagnostic criteria</h4>',
             _paragraphs(row["diagnostic_body"] or "No diagnostic criteria recorded."), '</div>']
    if row.get("main_variant_difference"):
        parts.extend(['<h4>Main form and variants</h4>', _paragraphs(row["main_variant_difference"])])
    representatives = [m for m in media if m["role"] == "representative"]
    if representatives:
        parts.append('<h4>Representative syllables</h4><div class="example-grid">')
        parts.extend(_example_player(m, filtered=filtered, asset_urls=asset_urls) for m in representatives)
        parts.append('</div>')
    else:
        parts.append('<p class="muted">No representative audio has been registered for this type.</p>')
    other = [m for m in media if m["role"] != "representative"]
    if other:
        parts.append(f'<details><summary>Additional examples ({len(other)})</summary><div class="example-grid">')
        parts.extend(_example_player(m, filtered=filtered, asset_urls=asset_urls) for m in other)
        parts.append('</div></details>')
    parts.append('<details><summary>Definition evidence and member IDs</summary>')
    for caption, field in (("Uncertainties", "uncertainties"), ("Basis for this definition", "reason")):
        if row.get(field):
            parts.extend([f'<h4>{caption}</h4>', _paragraphs(row[field])])
    competitors = row.get("competing_type_ids") or []
    if competitors:
        links = [_type_link(type_map[k]) if k in type_map else _e(k) for k in competitors]
        parts.append('<p>Competing types · ' + " · ".join(links) + '</p>')
    parts.append('<pre>' + _e({k: row.get(k) for k in ("member_ids", "scope_member_ids", "representative_ids",
        "atypical_ids", "counterexample_ids", "card_id", "card_revision")}) + '</pre></details></article>')
    return "".join(parts)


def _family_directory(types, examples, *, filtered, asset_urls):
    by_id = {row["local_type_id"]: row for row in types}
    children, media = {}, {}
    for row in types:
        children.setdefault(row["parent_type_id"], []).append(row)
    for ex in examples:
        media.setdefault(ex["type_id"], []).append(ex)
    def order(row):
        return (not row["scope_member_count"], (row["title"] or row["local_type_id"]).casefold(), row["local_type_id"])
    roots = [r for r in types if r["parent_type_id"] is None or r["parent_type_id"] not in by_id]
    nav, cards = [], []
    for family in sorted(roots, key=order):
        family_id = family["local_type_id"]
        leaves = sorted(children.get(family_id, []), key=order)
        nav.append("<li>" + _type_link(family, include_id=True))
        if leaves:
            nav.append("<ul>" + "".join("<li>" + _type_link(leaf, include_id=True) + "</li>" for leaf in leaves) + "</ul>")
        nav.append("</li>")
        cards.extend(_type_card(r, by_id, media.get(r["local_type_id"], []), filtered=filtered, asset_urls=asset_urls)
                     for r in [family, *leaves])
    if not cards:
        cards.append('<p>No human-defined families have been recorded.</p>')
    parts = ['<section id="catalogue"><div class="family-directory">',
             '<nav class="family-index" id="family-index" aria-label="Family index" tabindex="-1"><h2>Family index</h2>',
             '<p>Find a name or ID with your browser’s Find command.</p>',
             '<ul class="index-list">', *nav, '</ul></nav><div class="family-cards">',
             *cards,
             '</div></div><p>', _link("tables/types.csv", "Type definitions CSV", asset_urls), ' · ',
             _link("tables/examples.csv", "Example roles and media CSV", asset_urls), '</p></section>']
    return "".join(parts)


def _cover(document, mode, scope, items, scoped, examples):
    counts = document.get("counts", {})
    total, selected = len(items), len(scoped)
    outside_examples = {m["item_id"] for m in examples if not m["in_scope"]}
    media_ids = {m["item_id"] for m in examples}
    declared = scope.get("declared")
    level = {"family": "family level", "leaf": "fine-form level"}.get(scope.get("target_level"), "level unspecified")
    scope_label = f'Declared scope · {selected} of {total} syllables · {level}' if declared else f'Provisional scope · all {total} project syllables'
    title = "Complete catalogue" if mode == "complete" else "Draft catalogue"
    assessment = document.get("assessment") or {}
    run_status = assessment.get("status") or "not_started"
    assessment_label = {"completed": "Scored", "not_estimable": "Support report (not estimable)"}.get(run_status, _status(run_status))
    if assessment.get("stale_for_current"):
        assessment_label += " · historical"
    timestamp = str(document.get("created_at") or "")
    date = timestamp.partition("T")[0] or "Date not recorded"
    parts = ['<header><div class="cover-title"><h1>Syllable type catalogue</h1>',
             f'<span class="badge">{title}</span></div>',
             f'<p class="cover-purpose">{_e(scope_label)} · {_e(counts.get("families", 0))} in-scope families</p>']
    if scope.get("purpose"):
        parts.append(f'<p>{_e(scope["purpose"])}</p>')
    parts.append('<ul class="cover-counts" aria-label="Review outcomes in scope">')
    for field, label in (("assigned", "assigned"), ("unresolved", "reviewed unresolved"),
                         ("excluded", "excluded"), ("unreviewed", "unfinished")):
        parts.append(f'<li><strong>{_e(counts.get(field))}</strong> {label}</li>')
    parts.append('</ul>')
    parts.append(f'<p class="meta">Package includes the index and audit for all {total} project syllables'
                 f' · {len(media_ids)} example recordings ({len(outside_examples)} outside the scope)'
                 f' · <time datetime="{_e(timestamp)}">{_e(date)}</time> · Assessment: {_e(assessment_label)}</p>')
    parts.append('<nav aria-label="Report sections"><a href="#family-index">Families and examples</a>'
                 '<a href="#scope">Scope and status</a><a href="#registers">Syllable records</a>'
                 '<a href="#assessment">Internal assessment</a><a href="#provenance">Versions and files</a></nav></header>')
    return "".join(parts)




def render_html(document, asset_urls=None):
    'Produce script-free, network-independent HTML from service-frozen facts and roles only.'
    document = localize_system_document(document)
    asset_urls = asset_urls or {}
    mode = document.get("mode", "draft")
    if mode not in {"draft", "complete"}:
        raise ValueError('Unknown export mode')
    scope = document.get("scope") or {}
    items = _item_rows(document)
    scoped = [row for row in items if row.get("in_scope") is True]
    types = _type_rows(document)
    examples = _media_rows(document)
    assessment, report = _assessment(document)
    completion = document.get("completion") or {}
    if mode == "complete" and (not scope.get("declared") or not report or assessment.get("stale_for_current")
                               or not (completion.get("report") or {}).get("can_submit") or not completion.get("record")):
        raise ValueError('Complete delivery requires valid human completion, a declared scope and a matching assessment')
    body = _cover(document, mode, scope, items, scoped, examples)
    body += _family_directory(types, examples, filtered=bool(document.get("audio_preprocessing")), asset_urls=asset_urls)
    body += '<section id="scope"><h2>Declared scope and delivery status</h2>'
    if document.get("audio_preprocessing"):
        params = document["audio_preprocessing"]
        body += (f'<p>Import band-pass filter: {_e(params["low_hz"])}–{_e(params["high_hz"])} Hz, '
                 f'Butterworth order {_e(params["order"])} (forward and backward). Example audio uses this filter.</p>')
    if not scope.get("declared"):
        body += '<p class="notice">No delivery scope is declared. This draft lists current task records and is not a complete delivery of the whole catalogue or any declared subset.</p>'
    else:
        body += '<p class="notice">This report applies only to the listed frozen scope. Out-of-scope members and examples are marked and excluded from in-scope completion counts.</p>'
    counts = document.get("counts") or {}
    source_counts = document.get("source_registry", {}).get("counts") or {}
    body += _table(['Field', 'Frozen value'], [('Project ID', (document.get("project_identity") or {}).get("project_id")), ('Export ID', document.get("export_id")), ('Generated', document.get("created_at")), ('Snapshot fingerprint', document.get("snapshot_fingerprint")), ('Scope ID', scope.get("scope_id")), ('Purpose', scope.get("purpose")),
        ('All task syllables', scope.get("total_task_items", len(items))), ('Syllables in this scope', len(scoped)),
        ('Out-of-scope syllables', len(items) - len(scoped)), ('In-scope family-assigned', counts.get("assigned")),
        ('In-scope family-unresolved', counts.get("unresolved")), ('In-scope excluded', counts.get("excluded")),
        ('In-scope unreviewed / unfinished', counts.get("unreviewed")), ('Target level', scope.get("target_level", scope.get("level"))),
        ('Source version', document.get("source_registry", {}).get("version")),
        ('Known / unknown original-recording declarations in task', f'{source_counts.get("declared", 'Not recorded')} / {source_counts.get("unknown", 'Not recorded')}'),
        ('Declared original-recording groups in task', source_counts.get("raw_recording_groups")), ('Human completion record', (document.get("completion", {}).get("record") or {}).get("completion_id"))])
    completion = document.get("completion") or {}
    body += _reasons((completion.get("report") or {}).get("missing"))
    body += '<details><summary>Full scope, purpose and human completion record</summary><pre>' + _e({"scope": scope, "counts": document.get("counts"), "completion": completion}) + '</pre></details></section>'
    body += '<section id="registers"><h2>Syllable records, unresolved items and exclusions</h2><p>Membership, diagnostic-region observability, diagnostic-component evidence, form and boundary status remain separate. Unrecorded three-axis fields remain unknown; family membership does not imply them.</p>'
    body += _table(['Syllable ID', "family", "leaf", 'Disposition / processing state', 'Form', 'Disposition note (optional)', 'Operator-selected reason category', 'Disposition reason source', 'Submission contract', 'Three-axis reasoning'],
        [(r.get("item_id"), r.get("family_id"), r.get("leaf_id"), f'{_text(r.get("disposition"))} / {_status(r.get("processing_state"))}',
          r.get("form_status"), r.get("disposition_reason"), _disposition_code(r), _disposition_source(r),
          r.get("submission_contract_version"), r.get("axes_reason")) for r in scoped[:100]], empty='No syllable records in this scope.')
    if len(scoped) > 100:
        body += f'<p class="muted">The body previews only the first 100 in-scope syllables. See scope_items.csv for all {len(scoped)} items and all three-axis fields. The whole-task table also has an in_scope column.</p>'
    body += '<p>' + ' · '.join(_link(path, label, asset_urls) for path, label in (("tables/scope_items.csv", 'All members in this scope'), ("tables/items.csv", 'All task members (with in_scope)'), ("tables/unresolved.csv", 'In-scope family-unresolved list'), ("tables/form_unresolved.csv", 'Family assigned, form unresolved / unjudged'), ("tables/excluded.csv", 'In-scope exclusions'), ("tables/unfinished.csv", 'In-scope unfinished items'), ("tables/sources.csv", 'Original-recording declarations and evidence'))) + '</p>'
    body += '<p class="muted">Family dispositions follow frozen category values: assigned, unresolved, excluded and unreviewed. Unresolved or unjudged forms are listed separately in form_unresolved.csv; these items remain assigned and are not counted again as family-unresolved. Reason categories are operator-selected disposition codes, not free-text evidence of actual listening, and are not inserted into notes. Disposition notes may be empty; source and submission contract have separate columns. Missing legacy fields remain unrecorded. Disposition reasons and current three-axis evidence remain separate; categories or earlier batch notes do not substitute for current fine-review reasoning. The main JSON retains original fields and complete reasons.</p></section>'
    body += _assessment_html(document, asset_urls)
    body += '<section id="provenance"><h2>Versions, files and review evidence</h2><p>An export is a frozen snapshot and does not update after label, source or definition changes. Export a new version from the application when needed. This package does not merge types, choose examples, rerun assessment or finish human work for you.</p><p>' + _link("catalogue.json", 'Original-value frozen catalogue JSON', asset_urls) + ' · ' + _link("README.md", 'Package files and interpretation limits', asset_urls) + '</p>'
    if document.get("audit_path"):
        body += '<p>' + _link(document["audit_path"], 'Full audit record JSON', asset_urls) + '</p>'
    body += '<details><summary>Input and implementation versions</summary><pre>' + _e(document.get("versions", {})) + '</pre></details></section>'
    body += '<footer>Acoustic annotation · Data and metrics come only from frozen records. Media includes explicitly registered human examples, not the full audio corpus or a backup for restoring the entire workspace. CSV is a spreadsheet-safe copy; JSON preserves original text.</footer>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Syllable type catalogue and internal assessment</title><link rel="stylesheet" href="' + _href("report.css", asset_urls) + '"></head><body class="catalogue-home"><main>' + body + '</main></body></html>'


def _is_excluded(row):
    return row.get("category") == "excluded"


def _is_unfinished(row):
    return row.get("category") == "unreviewed"


def _is_unresolved(row):
    return row.get("category") == "unresolved"


def _is_form_unresolved(row):
    return row.get("category") == "assigned" and row.get("form_status") in {None, "unresolved", "candidate_form", "not_assessed"}


def _assessment_data(report):
    confusion = (report or {}).get("confusion") or {}
    families = confusion.get("family_ids", [])
    matrix = confusion.get("matrix_mean_row_normalized", [])
    if len(matrix) != len(families) or any(len(row) != len(families) for row in matrix):
        raise ValueError('The frozen confusion-matrix dimensions do not match the family index')
    metrics = (report or {}).get("metrics") or {}
    yield ("tables/assessment_metrics.csv", ("metric", "mean", "sd", "sd_meaning"),
              ({"metric": key, "mean": metrics[key].get("mean"), "sd": metrics[key].get("sd"),
                "sd_meaning": "Sample SD across the five grouped partitions; not a confidence interval"}
               for key in ("balanced_accuracy", "macro_f1") if key in metrics))
    yield ("tables/assessment_support.csv", SUPPORT_FIELDS, (report or {}).get("support", []))
    yield ("tables/assessment_per_family.csv", ("family_id", "n", "recall_mean", "recall_sd", "per_repeat_recall"), (report or {}).get("per_family", []))
    repeats = (report or {}).get("repeats", [])
    yield ("tables/assessment_repeats.csv", ("repeat", "seed", "n", "balanced_accuracy", "macro_f1"), repeats)
    predictions = ({"repeat": repeat.get("repeat"), "seed": repeat.get("seed"), **pred} for repeat in repeats for pred in repeat.get("predictions", []))
    yield ("tables/assessment_predictions.csv", ("repeat", "seed", "item_id", "source_group_id", "true_family_id", "predicted_family_id", "fold"), predictions)
    yield ("tables/assessment_confusion.csv", ("actual_family_id", "predicted_family_id", "mean_row_normalized_rate"),
              ({"actual_family_id": a, "predicted_family_id": b, "mean_row_normalized_rate": matrix[i][j]}
               for i, a in enumerate(families) for j, b in enumerate(families)))


def assessment_tables(assessment, report):
    """Yield the six D3 tables with the same names, values and identities for either export."""
    identity = {"assessment_job_id": assessment.get("job_id"),
                "assessment_status": assessment.get("status", "not_started"),
                "stale_for_current": bool(assessment.get("stale_for_current")),
                "assessment_snapshot_fingerprint": assessment.get("snapshot_fingerprint")}
    names = assessment.get("family_display_names", {})

    def with_names(rows, id_columns):
        for row in rows:
            labels = {field[:-2] + "name": names.get(row.get(field), "") for field in id_columns}
            yield {**identity, **row, **labels}

    for name, fields, rows in _assessment_data(report):
        ids = [field for field in fields if field.endswith("family_id")]
        yield name, (*ASSESSMENT_ID_FIELDS, *fields, *(field[:-2] + "name" for field in ids)), with_names(rows, ids)


def write_catalogue_report(output_dir, document):
    'Write controlled filenames; do not create a ZIP, read audio or modify workspace state.'
    document = localize_system_document(document)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # 先验证序列化、资源路径及矩阵索引；服务在临时目录完整成功后发布。
    document_json = _json(document) + "\n"
    readable = render_html(document)
    items = _item_rows(document)
    scoped = [r for r in items if r.get("in_scope") is True]
    assessment, report = _assessment(document)
    outputs = []
    def write_text(name, value):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(value)
        outputs.append(name)
    def write_csv(name, fields, rows):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(row.get(key)) for key in fields})
        outputs.append(name)
    write_text("catalogue.json", document_json)
    write_text("report.html", readable)
    write_text("report.css", CSS + CATALOGUE_CSS + "\n")
    write_csv("tables/types.csv", TYPE_FIELDS, _type_rows(document))
    write_csv("tables/items.csv", ITEM_FIELDS, items)
    write_csv("tables/scope_items.csv", ITEM_FIELDS, scoped)
    write_csv("tables/unresolved.csv", ITEM_FIELDS, [r for r in scoped if _is_unresolved(r)])
    write_csv("tables/form_unresolved.csv", ITEM_FIELDS, [r for r in scoped if _is_form_unresolved(r)])
    write_csv("tables/excluded.csv", ITEM_FIELDS, [r for r in scoped if _is_excluded(r)])
    write_csv("tables/unfinished.csv", ITEM_FIELDS, [r for r in scoped if _is_unfinished(r)])
    write_csv("tables/sources.csv", SOURCE_FIELDS, _source_rows(document))
    write_csv("tables/examples.csv", MEDIA_FIELDS, _media_rows(document))
    for name, fields, records in assessment_tables(assessment, report):
        write_csv(name, fields, records)
    assessment_meta = {key: copy.deepcopy(assessment.get(key)) for key in ("job_id", "status", "stale_for_current", "stale_reasons", "snapshot_fingerprint", "report_path")}
    write_text("assessment_status.json", _json(assessment_meta) + "\n")
    readme = f'''# Syllable type catalogue and internal assessment package

Export ID: {document.get("export_id", 'Not recorded')}  
Delivery status: {_status(document.get("mode", "draft"))}  
Snapshot fingerprint: {document.get("snapshot_fingerprint", 'Not recorded')}

After extraction, open report.html. Use its family index to jump to diagnostic criteria, spectrograms and playable representative syllables. No network connection is required. A browser can print it to PDF, but PDF does not embed audio. Keep the complete directory and relative paths; do not move the HTML alone.

## Files

- catalogue.json: authoritative original-value snapshot of scope, catalogue and members, sources, human completion and the selected internal assessment. No statistics are recomputed.
- report.html and report.css: readable catalogue, local styling, human examples, scope, dispositions and assessment. Missing definitions, unknown forms and not-estimable results are retained.
- tables/types.csv: all families and leaves, with task_member_count and scope_member_count in separate columns.
- tables/items.csv: all task syllables; in_scope identifies this delivery scope; family_name and leaf_name accompany the stable type IDs. tables/scope_items.csv contains only the selected scope. disposition_reason retains the original batch disposition note; reason_code is the operator-selected reason category, and reason_code_label is its display label. A category is not free-text evidence of listening and is never inserted into notes. reason_source describes only the batch disposition reason: operator_note means an operator note, operator_code an operator-selected category, and not_provided no note supplied. submission_contract_version is the saved submission contract. Legacy records without these fields remain blank; listening or new provenance is not inferred. axes_reason is current valid three-axis reasoning; reason is the compatibility main reason, preferring current axes_reason without replacing separate columns.
- tables/unresolved.csv, excluded.csv and unfinished.csv: in-scope family-unresolved, excluded and unfinished items, following category unresolved/excluded/unreviewed. tables/form_unresolved.csv separately lists assigned items with unresolved or unjudged form; they retain family membership and must not be added again to mutually exclusive disposition totals.
- tables/sources.csv: declared original recordings and evidence, retaining unknowns. Grouping does not prove individual-bird independence.
- tables/examples.csv: each type × syllable × example-role registration and media path. A syllable may have several roles while its media is stored once.
- tables/assessment_metrics.csv, assessment_support.csv, assessment_per_family.csv, assessment_repeats.csv, assessment_predictions.csv and assessment_confusion.csv: overall metrics, support, per-family recall, repeat metrics, all out-of-fold predictions and the complete mean normalized confusion matrix (rows = human families, columns = predicted families, including zeros). Without a full report, only column headers are retained.
- assessment_status.json: job, status and currentness for these assessment tables. Every assessment_*.csv row also carries assessment_job_id, assessment_status, stale_for_current and assessment_snapshot_fingerprint, so the selected run remains identifiable separately. stale_for_current=true means it was already historical at export and is not a metric for this catalogue; false applies only at the export snapshot and does not guarantee applicability after later changes.
- The export service adds full audits, the original assessment report, media and file hashes; see catalogue.json and the manifest for paths. Media contains only human-registered representatives, atypical examples and counterexamples, not a full audio backup.

## Scope and interpretation

A draft does not claim human work or internal assessment is complete. Complete delivery for a declared scope must satisfy the program checks for human work and a matching assessment; it does not certify biological type truth. A not_estimable report due to insufficient sources may accompany complete delivery with its limitations. Sources are not invented and rare families are not removed to obtain scores. Stale assessments are historical content, not current catalogue results. Scores do not automatically relabel items or select representatives.

Metrics describe internal label predictability under a fixed method. BA is not human accuracy; SD across five partitions is not a confidence interval, sample size is not multiplied by five, and repeated partitions may coincide. Family coverage uses all declared or referenced families; syllable coverage uses all syllables in the declared scope; maximum source share uses confirmed syllables with known sources. Revisions informed by metrics on the same material remain internal development evidence, not independent external validation or an efficiency comparison.

## Spreadsheet safety and original values

CSV uses UTF-8 BOM. Text that spreadsheet software could interpret as a formula (including =, +, -, @ after leading whitespace, or initial control characters) receives a leading apostrophe; numbers are unchanged. Exact IDs, text and structures remain in catalogue.json and the original audit JSON. Empty CSV cells mean unrecorded, not biological absence. JSON null remains unknown and is not changed to zero.
'''
    write_text("README.md", readme)
    return outputs
