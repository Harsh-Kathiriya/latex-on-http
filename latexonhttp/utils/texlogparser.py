# -*- coding: utf-8 -*-
"""
latexonhttp.utils.texlogparser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LaTeX log file parser, vendored from texoutparse 1.0.0 (MIT license)
and extended with additional patterns for Class warnings, LaTeX3,
LaTeX Font, LuaTeX module messages, and missing references.

Original: https://github.com/inakleinbottle/texoutparse
"""
import re
from collections import deque


class LogFileMessage:
    """
    Stores a single parsed log message with structured info and raw context lines.
    """

    def __init__(self):
        self.info = {}
        self.context_lines = []

    def __str__(self):
        return "\n".join(self.context_lines)

    def __getitem__(self, item):
        return self.info[item]

    def __setitem__(self, key, value):
        self.info[key] = value

    def to_dict(self):
        return {
            "info": self.info,
            "context": "\n".join(self.context_lines),
        }


class _LineIterWrapper:
    """
    Wrapper that allows peeking ahead for context lines without consuming
    the underlying iterator.
    """

    def __init__(self, iterable, ctx_lines):
        self.iterable = iter(iterable)
        self.cache = deque()
        self.ctx_lines = ctx_lines
        self.current = None

    def __next__(self):
        if self.cache:
            self.current = current = self.cache.popleft()
        else:
            self.current = current = next(self.iterable)
        return current

    def __iter__(self):
        return self

    def get_context(self):
        rv = [self.current] if self.current else []
        for _ in range(self.ctx_lines + 1 - len(rv)):
            try:
                next_val = next(self.iterable)
                self.cache.append(next_val)
                rv.append(next_val)
            except StopIteration:
                break
        return rv


class LatexLogParser:
    """
    Parser for LaTeX log files.

    Extracts errors, warnings, bad boxes, and missing references into
    structured lists. Each item is a LogFileMessage with .info dict and
    .context_lines for the surrounding raw log text.

    Extended beyond the original texoutparse to also catch:
    - LaTeX Font Warning
    - LaTeX3 Warning
    - Module (LuaTeX) warnings/errors
    - pdfTeX / PDF backend warnings
    - Missing character warnings
    """

    # Errors: "! <type> Error: msg" or bare "! msg"
    error = re.compile(
        r"^(?:! ((?:La|pdf)TeX|Package|Class|Module)(?: (\w+))? [eE]rror(?: \(([\\]?\w+)\))?: (.*)|! (.*))"
    )

    # Warnings: extended to catch LaTeX Font, LaTeX3, Module (LuaTeX), pdfTeX
    warning = re.compile(
        r"^((?:La|pdf)TeX|LaTeX3|LaTeX Font|Package|Class|Module)(?: (\w+))? [wW]arning(?: \(([\\]?\w+)\))?: (.*)"
    )

    # Info messages (not stored, but available for future use)
    info = re.compile(
        r"^((?:La|pdf)TeX|LaTeX3|Package|Class|Module)(?: (\w+))? [iI]nfo(?: \(([\\]?\w+)\))?: (.*)"
    )

    badbox = re.compile(
        r"^(Over|Under)full "
        r"\\([hv])box "
        r"\((?:badness (\d+)|(\d+(?:\.\d+)?pt) too \w+)\) (?:"
        r"(?:(?:in paragraph|in alignment|detected) "
        r"(?:at lines (\d+)--(\d+)|at line (\d+)))"
        r"|(?:has occurred while [\\]output is active [\[](\d+)?[\]]))"
    )

    missing_ref = re.compile(
        r"^LaTeX Warning: (Citation|Reference) `([^']+)' on page (\d+) undefined on input line (\d+)\."
    )

    # pdfTeX / LuaTeX PDF backend warnings
    pdf_warning = re.compile(
        r"^(?:pdfTeX warning|warning \(pdf backend\)): (.*)"
    )

    # Missing character: "Missing character: ..."
    missing_char = re.compile(r"^Missing character: (.*)")

    def __init__(self, context_lines=2):
        self.warnings = []
        self.errors = []
        self.badboxes = []
        self.missing_refs = []
        self.context_lines = context_lines

    def __str__(self):
        return (
            f"Errors: {len(self.errors)}, "
            f"Warnings: {len(self.warnings)}, "
            f"Badboxes: {len(self.badboxes)}"
        )

    def process(self, lines):
        """
        Process lines from a log file (iterable of strings).
        """
        lines_iterable = _LineIterWrapper(lines, self.context_lines)
        process_line = self.process_line

        for i, line in enumerate(lines_iterable):
            if not line:
                continue
            msg = process_line(line)
            if msg is not None:
                msg.context_lines = lines_iterable.get_context()

    def process_line(self, line):
        match = self.missing_ref.match(line)
        if match is not None:
            return self._process_missing_ref(match)

        match = self.badbox.match(line)
        if match is not None:
            return self._process_badbox(match)

        match = self.warning.match(line)
        if match is not None:
            return self._process_warning(match)

        match = self.error.match(line)
        if match is not None:
            return self._process_error(match)

        match = self.pdf_warning.match(line)
        if match is not None:
            return self._process_pdf_warning(match)

        match = self.missing_char.match(line)
        if match is not None:
            return self._process_missing_char(match)

        return None

    def _process_badbox(self, match):
        message = LogFileMessage()
        message["type"] = match.group(1)
        message["direction"] = match.group(2)
        message["by"] = match.group(3) or match.group(4)

        if match.group(7) is not None:
            message["lines"] = (match.group(7), match.group(7))
        elif match.group(8) is not None:
            message["lines"] = (match.group(8), match.group(8))
        else:
            message["lines"] = (match.group(5), match.group(6))

        self.badboxes.append(message)
        return message

    def _process_warning(self, match):
        message = LogFileMessage()
        message["type"] = type_ = match.group(1)

        if type_ == "Package":
            message["package"] = match.group(2)
        elif type_ == "Class":
            message["class"] = match.group(2)
        elif match.group(2) is not None:
            message["component"] = match.group(2)

        if match.group(3) is not None:
            message["extra"] = match.group(3)

        message["message"] = match.group(4)
        self.warnings.append(message)
        return message

    def _process_error(self, match):
        message = LogFileMessage()
        if match.group(1) is not None:
            message["type"] = type_ = match.group(1)

            if type_ == "Package":
                message["package"] = match.group(2)
            elif type_ == "Class":
                message["class"] = match.group(2)
            elif match.group(2) is not None:
                message["component"] = match.group(2)

            if match.group(3) is not None:
                message["extra"] = match.group(3)

            message["message"] = match.group(4)
        else:
            message["message"] = match.group(5)

        self.errors.append(message)
        return message

    def _process_missing_ref(self, match):
        message = LogFileMessage()
        message["type"] = f"Missing {match.group(1)}"
        message["key"] = match.group(2)
        message["page"] = match.group(3)
        message["line"] = match.group(4)
        self.missing_refs.append(message)
        return message

    def _process_pdf_warning(self, match):
        message = LogFileMessage()
        message["type"] = "PDF"
        message["message"] = match.group(1)
        self.warnings.append(message)
        return message

    def _process_missing_char(self, match):
        message = LogFileMessage()
        message["type"] = "Missing Character"
        message["message"] = match.group(1)
        self.warnings.append(message)
        return message


def parse_latex_log(log_text):
    """
    Parse a LaTeX log string and return a structured summary dict.

    Returns:
        {
            "errors": [{"info": {...}, "context": "..."}, ...],
            "warnings": [...],
            "badboxes": [...],
            "missing_refs": [...],
            "errors_count": int,
            "warnings_count": int,
            "badboxes_count": int,
            "has_errors": bool,
            "has_warnings": bool,
        }
    """
    parser = LatexLogParser()
    parser.process(log_text.splitlines())
    return {
        "errors": [m.to_dict() for m in parser.errors],
        "warnings": [m.to_dict() for m in parser.warnings],
        "badboxes": [m.to_dict() for m in parser.badboxes],
        "missing_refs": [m.to_dict() for m in parser.missing_refs],
        "errors_count": len(parser.errors),
        "warnings_count": len(parser.warnings),
        "badboxes_count": len(parser.badboxes),
        "has_errors": len(parser.errors) > 0,
        "has_warnings": len(parser.warnings) > 0,
    }
