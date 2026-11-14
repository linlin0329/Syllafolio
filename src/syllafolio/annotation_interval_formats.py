"""Parse recording-relative annotation text without rounding times or writing files.

Format references:
https://www.fon.hum.uva.nl/praat/manual/TextGrid_file_formats.html
https://www.fon.hum.uva.nl/praat/manual/Unicode.html
https://manual.audacityteam.org/man/importing_and_exporting_labels.html
https://www.ravensoundsoftware.com/knowledge-base/error-opening-selection-table/

Only interval tiers are returned. Point annotations are accepted and counted,
but never become zero-length syllables. TextGrid blank intervals remain present
for the caller to include or omit explicitly. Annotation labels are plain text,
not human-family assignments or evidence of listening.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import posixpath
import re


class AnnotationFormatError(ValueError):
    """A readable annotation-file error, independent of audio or project state."""


@dataclass(frozen=True)
class Interval:
    start: Decimal
    end: Decimal
    label: str
    source_id: str
    notes: tuple[str, ...] = ()


@dataclass
class AnnotationTier:
    name: str
    intervals: list[Interval]
    info: dict[str, object] = field(default_factory=dict)


_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_MAX_RECORDS = 200_000
_KINDS = {"raven", "textgrid", "audacity"}


def _fail(message):
    raise AnnotationFormatError(message)


def _decode(data: bytes) -> str:
    if not isinstance(data, bytes):
        _fail("Annotation input must be bytes.")
    binary = "ooBinaryFile"
    if any(binary.encode(codec) in data[:128] for codec in ("ascii", "utf-16-le", "utf-16-be")):
        _fail("Binary TextGrid is not supported. Open it in Praat and use Save as text file before importing.")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        _fail("Use UTF-8 or UTF-16 annotation text, not UTF-32.")
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        elif b"\x00" in data[:128] and len(data) % 2 == 0:
            # The formats have ASCII syntax at the beginning, so endian order
            # is unambiguous even for common UTF-16 exports without a BOM.
            head = data[:128]
            even, odd = head[::2].count(0), head[1::2].count(0)
            if even == odd:
                _fail("The annotation encoding is ambiguous. Save it as UTF-8 or UTF-16 with a BOM.")
            text = data.decode("utf-16-le" if odd > even else "utf-16-be")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise AnnotationFormatError("The annotations are not valid UTF-8 or UTF-16 text.") from exc
    if "\x00" in text:
        _fail("Annotation text contains an unexpected NUL character.")
    if not text.strip():
        _fail("The annotation file is empty.")
    return text


def _decimal(value: str, where: str) -> Decimal:
    value = value.strip()
    if not _NUMBER.fullmatch(value):
        _fail(f"{where}: expected a finite decimal number in seconds.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise AnnotationFormatError(f"{where}: invalid decimal number.") from exc
    if not number.is_finite():
        _fail(f"{where}: the number must be finite.")
    return number


def _span(start: Decimal, end: Decimal, where: str):
    if start < 0 or end < start:
        _fail(f"{where}: times must be non-negative and end must not precede start.")


def _header(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _is_raven(text: str) -> bool:
    first = next((line for line in text.splitlines() if line.strip()), "")
    names = {_header(value) for value in first.split("\t")}
    return {"begintime(s)", "endtime(s)"} <= names


def detect_format(data: bytes, annotation_name: str) -> str:
    """Recognize content; a .TextGrid hint never turns invalid text into labels."""
    text = _decode(data)
    head = text.lstrip()
    if "ooTextFile" in head[:128] and '"TextGrid"' in head[:512]:
        return "textgrid"
    if _is_raven(text):
        return "raven"
    if isinstance(annotation_name, str) and annotation_name.casefold().endswith(".textgrid"):
        _fail("The .TextGrid file does not have a supported text TextGrid header.")
    first = next((line for line in text.splitlines() if line.strip()), "")
    columns = first.split("\t", 2)
    if len(columns) >= 2 and _NUMBER.fullmatch(columns[0].strip()) and _NUMBER.fullmatch(columns[1].strip()):
        return "audacity"
    _fail("The annotation format was not recognized. Choose a Raven table, text TextGrid or Audacity label file.")


def parse_annotations(data: bytes, kind: str, recording_name: str, *, recording_channels: int = 1) -> list[AnnotationTier]:
    if kind == "auto":
        kind = detect_format(data, "")
    if kind not in _KINDS:
        _fail("Choose raven, textgrid or audacity annotations.")
    text = _decode(data)
    if kind == "textgrid":
        return _textgrid(text)
    if kind == "raven":
        return _raven(text, recording_name, recording_channels)
    return _audacity(text)


def _textgrid_tokens(text: str):
    """Praat data tokens, ignoring descriptive labels and bracketed indices."""
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "!":
            while index < length and text[index] not in "\r\n":
                index += 1
        elif char == '"':
            index += 1
            value = []
            while index < length:
                if text[index] == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        value.append('"')
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    value.append(text[index])
                    index += 1
            else:
                _fail("TextGrid contains an unterminated quoted string.")
            if index < length and not text[index].isspace() and text[index] != "!":
                _fail("TextGrid data strings must be separated by whitespace.")
            yield "text", "".join(value)
        else:
            end = index
            while end < length and not text[end].isspace() and text[end] != "!":
                end += 1
            value = text[index:end]
            if _NUMBER.fullmatch(value):
                yield "number", value
            elif value in {"<exists>", "<absent>"}:
                yield "flag", value
            index = end


class _GridReader:
    def __init__(self, text):
        self.tokens = iter(_textgrid_tokens(text))

    def take(self, kind, where):
        token = next(self.tokens, None)
        if token is None or token[0] != kind:
            _fail(f"TextGrid {where}: expected {kind} data.")
        return token[1]

    def number(self, where):
        return _decimal(self.take("number", where), "TextGrid " + where)

    def count(self, where):
        number = self.number(where)
        if number < 0 or number > _MAX_RECORDS or number != number.to_integral_value():
            _fail(f"TextGrid {where}: invalid or excessive record count.")
        return int(number)

    def done(self):
        if next(self.tokens, None) is not None:
            _fail("TextGrid has extra data after its declared tiers.")


def _textgrid(text):
    reader = _GridReader(text)
    if reader.take("text", "file type") not in {"ooTextFile", "ooTextFile short"} or reader.take("text", "object class") != "TextGrid":
        _fail("Only long or short text TextGrid files are supported.")
    grid_start, grid_end = reader.number("start"), reader.number("end")
    _span(grid_start, grid_end, "TextGrid domain")
    flag = reader.take("flag", "tier existence")
    if flag == "<absent>":
        reader.done()
        return []
    result = []
    skipped_points = skipped_point_tiers = 0
    for tier_index in range(1, reader.count("tier count") + 1):
        kind = reader.take("text", "tier class")
        name = reader.take("text", "tier name")
        start, end = reader.number("tier start"), reader.number("tier end")
        _span(start, end, "TextGrid tier " + name)
        if start < grid_start or end > grid_end:
            _fail("TextGrid tier lies outside the grid time domain.")
        count = reader.count("interval/point count")
        if kind not in {"IntervalTier", "TextTier"}:
            _fail("Unsupported TextGrid tier class: " + kind)
        intervals = []
        previous = start
        for item_index in range(1, count + 1):
            begin = reader.number("item start")
            finish = reader.number("item end") if kind == "IntervalTier" else begin
            label = reader.take("text", "item text")
            _span(begin, finish, "TextGrid item")
            if begin < start or finish > end or begin < previous:
                _fail("TextGrid intervals or points are out of order, overlap, or exceed their tier domain.")
            previous = finish
            if kind == "TextTier" or begin == finish:
                skipped_points += 1
            else:
                intervals.append(Interval(begin, finish, label, f"textgrid:{tier_index}:{item_index}"))
        if kind == "IntervalTier":
            result.append(AnnotationTier(name, intervals, {"format":"textgrid", "tier_index":tier_index,
                "blank_count":sum(not item.label.strip() for item in intervals), "skipped_point_count":0,
                "skipped_point_tier_count":0}))
        else:
            skipped_point_tiers += 1
    reader.done()
    if result:
        result[0].info.update(skipped_point_count=skipped_points, skipped_point_tier_count=skipped_point_tiers)
    return result


def _basename(value):
    return value.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _absolute_path_identity(value):
    """Compare paths within the table, not against the relocated audio folder."""
    value = value.strip().strip('"').replace("\\", "/")
    windows = bool(re.match(r"^[A-Za-z]:/", value)) or value.startswith("//")
    if not windows and not value.startswith("/"):
        return None
    normalized = posixpath.normpath(value)
    return normalized.casefold() if windows else normalized


def _same_displayed_time(begin: Decimal, offset: Decimal) -> bool:
    """Allow export rounding, never silently shift a sequence onto one file.

    Compare at the coarser displayed decimal precision. A five-millisecond
    absolute ceiling also prevents coarse integer/scientific displays from
    disguising an otherwise obvious sequence offset. Times are not changed.
    """
    if begin == offset:
        return True
    with localcontext() as context:
        context.prec = max(len(begin.as_tuple().digits), len(offset.as_tuple().digits)) + 10
        if abs(begin - offset) > Decimal("0.005"):
            return False
        quantum = Decimal(1).scaleb(max(begin.as_tuple().exponent, offset.as_tuple().exponent))
        try:
            return begin.quantize(quantum, rounding=ROUND_HALF_UP) == offset.quantize(quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return False


def _raven(text, recording_name, recording_channels):
    lines = [(index, line) for index, line in enumerate(text.splitlines(), 1) if line.strip()]
    headers = lines[0][1].split("\t")
    normalized = [_header(value) for value in headers]
    if len(normalized) != len(set(normalized)):
        _fail("Raven contains duplicate column names.")
    columns = {value:index for index,value in enumerate(normalized)}
    if not {"begintime(s)", "endtime(s)"} <= columns.keys():
        _fail("Raven requires Begin Time (s) and End Time (s) columns.")
    label_column = next((key for key in ("annotation", "label", "text") if key in columns), None)
    selection_column = "selection" in columns
    selected = _basename(recording_name) if isinstance(recording_name, str) else ""
    intervals, seen = [], {}
    skipped_points = duplicate_views = 0
    precision_matches = 0
    named = False
    absolute_paths = set()
    for ordinal, (line_number, line) in enumerate(lines[1:], 1):
        if ordinal > _MAX_RECORDS:
            _fail("Raven contains too many selections.")
        cells = line.split("\t")
        if len(cells) != len(headers):
            _fail(f"Raven line {line_number}: column count differs from the header.")
        def value(key):
            return cells[columns[key]] if key in columns else ""
        begin = _decimal(value("begintime(s)"), f"Raven line {line_number} start")
        end = _decimal(value("endtime(s)"), f"Raven line {line_number} end")
        _span(begin, end, f"Raven line {line_number}")
        for key in ("beginfile", "endfile", "beginpath", "endpath"):
            filename = value(key).strip()
            if filename:
                named = True
                if not selected or _basename(filename) != selected:
                    _fail(f"Raven line {line_number}: the file name does not match the selected recording; sound sequences are not supported.")
                absolute = _absolute_path_identity(filename)
                if absolute is not None:
                    absolute_paths.add(absolute)
                    if len(absolute_paths) > 1:
                        _fail("Raven contains multiple absolute recording paths, even though their file names may match. Export a single-recording table.")
        for key in ("fileoffset(s)", "fileoffset"):
            raw_offset = value(key).strip()
            if not raw_offset:
                continue
            offset = _decimal(raw_offset, f"Raven line {line_number} file offset")
            if offset < 0 or not _same_displayed_time(begin, offset):
                _fail(f"Raven line {line_number}: sequence time differs from file offset. Export a single-recording table instead.")
            if begin != offset:
                precision_matches += 1
        channel = value("channel").strip()
        channel_number = _decimal(channel, f"Raven line {line_number} channel") if channel else None
        if channel_number is not None and channel_number not in range(1, recording_channels + 1):
            _fail(f"Raven line {line_number}: channel {channel} is unavailable in the selected recording ({recording_channels} channel(s)).")
        ident = value("selection").strip() if selection_column else f"row-{ordinal}"
        if not ident:
            _fail(f"Raven line {line_number}: Selection must not be empty.")
        label = value(label_column) if label_column else ""
        view = value("view").strip()
        # Raven can show the same selection on different views or stereo channels.
        # That selection contributes one time interval to the mixed audition.
        view_key = (view, channel_number)
        if ident in seen:
            old, views, output_index = seen[ident]
            if old.start != begin or old.end != end or (old.label and label and old.label != label):
                _fail(f"Raven Selection {ident}: view rows disagree about time or annotation.")
            if any((not view or view == previous_view) and
                   (channel_number is None or previous_channel is None or channel_number == previous_channel)
                   for previous_view, previous_channel in views):
                _fail(f"Raven Selection {ident}: duplicate ID without a distinct view or channel.")
            views.add(view_key)
            duplicate_views += 1
            if not old.label and label:
                old = replace(old, label=label)
                seen[ident] = old, views, output_index
                if output_index is not None:
                    intervals[output_index] = old
            continue
        item = Interval(begin, end, label, ident)
        output_index = len(intervals) if begin != end else None
        seen[ident] = item, {view_key}, output_index
        if begin == end:
            skipped_points += 1
        else:
            intervals.append(item)
    return [AnnotationTier("Raven selections", intervals, {"format":"raven", "blank_count":sum(not row.label.strip() for row in intervals),
        "skipped_point_count":skipped_points, "deduplicated_view_rows":duplicate_views,
        "label_column":headers[columns[label_column]] if label_column else "",
        "offset_precision_matches": precision_matches,
        "recording_binding":"file_name" if named else "selected_recording"})]


def _audacity(text):
    intervals = []
    previous_index = None
    previous_label = False
    previous_frequency = False
    points = frequency_rows = records = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        cells = line.split("\t", 2)
        if cells[0].strip() == "\\":
            if not previous_label or previous_frequency or len(cells) != 3:
                _fail(f"Audacity line {line_number}: frequency data must follow exactly one label row.")
            low, high = (_decimal(v, f"Audacity line {line_number} frequency") for v in cells[1:])
            if low < -1 or high < -1 or (low != -1 and low < 0) or (high != -1 and high < 0) or (low != -1 and high != -1 and low > high):
                _fail(f"Audacity line {line_number}: invalid frequency limits; -1 means unspecified.")
            if previous_index is not None:
                item = intervals[previous_index]
                intervals[previous_index] = replace(item, notes=(f"Spectral label: low={low} Hz, high={high} Hz (-1 unspecified).",))
            previous_frequency = True
            frequency_rows += 1
            continue
        if len(cells) < 2:
            _fail(f"Audacity line {line_number}: expected tab-separated start and end times.")
        begin, end = (_decimal(v, f"Audacity line {line_number} time") for v in cells[:2])
        _span(begin, end, f"Audacity line {line_number}")
        records += 1
        if records > _MAX_RECORDS:
            _fail("Audacity contains too many labels.")
        previous_label = True
        previous_frequency = False
        previous_index = None
        if begin == end:
            points += 1
        else:
            previous_index = len(intervals)
            intervals.append(Interval(begin, end, cells[2] if len(cells) == 3 else "", f"audacity:{line_number}"))
    return [AnnotationTier("Audacity labels", intervals, {"format":"audacity", "blank_count":sum(not row.label.strip() for row in intervals),
        "skipped_point_count":points, "frequency_line_count":frequency_rows})]
