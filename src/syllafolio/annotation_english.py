"""Read system-generated messages from earlier projects in English.

Apply these functions at a presentation boundary. Operator notes, names,
raw event history and signed report bytes retain their original contents.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil


@lru_cache(maxsize=1)
def _catalogue():
    path = Path(__file__).resolve().parents[2] / "configs/annotation_manual/legacy_system_messages.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    patterns = []
    for index, (parts, output) in enumerate(data["templates"]):
        expr = "".join(re.escape(p) if isinstance(p, str) else f"(?P<v{p}>[^\n]*?)" for p in parts)
        patterns.append((re.compile(expr), output, data.get("system_slots", {}).get(str(index), [])))
    return data["literals"], patterns


def system_text(value):
    """Translate a known machine message without translating arbitrary text."""
    if not isinstance(value, str) or value.isascii() or not re.search(r"[\u3400-\u9fff]", value):
        return value
    literals, patterns = _catalogue()
    if value in literals:
        return literals[value]
    win = re.fullmatch(r"\[WinError (\d+)\]\s*([^:\r\n]+)(: .*)?", value, re.DOTALL)
    if win:
        return _windows_message(int(win[1])) + (win[3] or "")
    if len(value) <= 4096:
        for pattern, output, system_slots in patterns:
            match = pattern.fullmatch(value)
            if match:
                fields = match.groupdict()
                for slot in system_slots:
                    key = f"v{slot}"
                    fields[key] = system_text(fields[key])
                return "".join(word if isinstance(word, str) else fields[f"v{word}"] for word in output)
    prefix, sep, body = value.partition(": ")
    if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,79}", prefix):
        return prefix + sep + system_text(body)
    return value


def system_fields(value):
    """Make a display copy; only documented machine-generated fields are translated."""
    if isinstance(value, list):
        return [system_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"message", "scope_stale_reason", "read_only_reason", "reason_code_label",
                   "invalid_reason", "detected_stale_reason", "exception_message", "capacity_note",
                   "system_summary"} and isinstance(item, str):
            result[key] = system_text(item)
        elif key == "reason" and value.get("reason_source") == "system_operation_summary":
            result[key] = system_text(item)
        elif key in {"label", "detail"} and "kind" in value and value["kind"] in (
                "synthetic", "real_sparrow", "mixed", "unverified") and "detail" in value:
            result[key] = system_text(item)
        elif key == "provenance" and isinstance(item, dict) and "global_reference" in item and "local_reference" in item:
            result[key] = system_fields(item)
            if "note" in item:
                result[key]["note"] = system_text(item["note"])
        elif key in {"note", "reason", "title", "name", "evidence", "operator_note", "question",
                     "observation_text", "diagnostic_body", "user_input", "operator_input"}:
            result[key] = item
        else:
            result[key] = system_fields(item)
    return result


def _windows_message(code):
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(2048)
        if ctypes.windll.kernel32.FormatMessageW(0x1200, None, code, 0x0409, buf, len(buf), None):
            return f"[WinError {code}] {buf.value.strip()}"
    return f"Windows reported error {code}."


def system_error(exc):
    """Show English Windows diagnostics while preserving the actual filenames."""
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        rows = exc.args[0]
        if rows and all(isinstance(row, (list, tuple)) and len(row) == 3 for row in rows):
            return "\n".join(f"Could not copy {src!r} to {dst!r}: {system_text(str(message))}"
                             for src, dst, message in rows)
    if isinstance(exc, OSError) and getattr(exc, "winerror", None):
        detail = _windows_message(exc.winerror)
        for filename in (exc.filename, exc.filename2):
            if filename is not None:
                detail += f" {filename!r}"
        return detail
    return system_text(str(exc))
