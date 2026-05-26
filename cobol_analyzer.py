#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COBOL 静的解析エンジン + HTML レポート生成器（標準ライブラリのみ）。"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

COBOL_EXTENSIONS = {".cob", ".cbl", ".cobol", ".pco", ".cpy", ".cpb"}

_STATEMENT_WORDS = {
    "END-READ", "END-WRITE", "END-DELETE", "END-IF", "END-PERFORM",
    "END-EVALUATE", "END-CALL", "END-SEARCH", "END-START", "END-STRING",
    "END-UNSTRING", "END-EXEC", "END-COMPUTE", "END-ADD", "END-SUBTRACT",
    "END-MULTIPLY", "END-DIVIDE", "END-RETURN", "END-REWRITE",
    "GOBACK", "EXIT", "STOP", "CONTINUE",
    "ELSE", "WHEN", "THEN", "NEXT", "SENTENCE", "OTHERWISE",
    "DECLARATIVES", "END",
}

_OPEN_MODES = {
    "INPUT": "READ",
    "OUTPUT": "CREATE",
    "I-O": "READ/UPDATE",
    "EXTEND": "APPEND",
}

_COMPLEXITY_KEYWORDS = {"IF", "EVALUATE", "WHEN", "PERFORM", "UNTIL", "WHILE"}

_DIVISIONS = ("IDENTIFICATION", "ENVIRONMENT", "DATA", "PROCEDURE")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Paragraph:
    name: str
    program: str
    section: Optional[str] = None
    start_line: int = 0
    end_line: int = 0
    statements: int = 0
    complexity: int = 1
    description: str = ""
    referenced_by: List[str] = field(default_factory=list)
    raw_source: str = ""  # AI 用に短い抜粋を保持


@dataclass
class CallEdge:
    caller_program: str
    caller_paragraph: Optional[str]
    callee: str
    dynamic: bool = False
    line: int = 0


@dataclass
class CopyRef:
    program: str
    member: str
    line: int = 0


@dataclass
class FileDecl:
    program: str
    fd_name: str
    assign_to: Optional[str] = None
    organization: Optional[str] = None


@dataclass
class CrudUsage:
    program: str
    paragraph: Optional[str]
    target: str
    target_kind: str  # "FILE" / "TABLE"
    operation: str    # READ / CREATE / UPDATE / DELETE / READ/UPDATE / APPEND
    line: int = 0


@dataclass
class Program:
    program_id: str
    source_path: str
    encoding: str
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    complexity: int = 1
    description: str = ""
    paragraphs: List[Paragraph] = field(default_factory=list)
    files: List[FileDecl] = field(default_factory=list)
    calls: List[CallEdge] = field(default_factory=list)
    copies: List[CopyRef] = field(default_factory=list)
    crud: List[CrudUsage] = field(default_factory=list)
    dead_paragraphs: List[str] = field(default_factory=list)
    parse_warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Encoding & line preprocessing
# ---------------------------------------------------------------------------


def _read_source(path: Path, forced_encoding: Optional[str] = None) -> Tuple[List[str], str]:
    """ソースを行リストとして読み込み、使用エンコーディングを返す。"""
    raw = path.read_bytes()
    candidates = [forced_encoding] if forced_encoding else ["utf-8", "cp932", "shift_jis", "latin-1"]
    last_err: Optional[Exception] = None
    for enc in candidates:
        if enc is None:
            continue
        try:
            text = raw.decode(enc)
            return text.splitlines(), enc
        except UnicodeDecodeError as err:
            last_err = err
            continue
    raise last_err if last_err else RuntimeError(f"cannot decode {path}")


def _strip_fixed_format(line: str) -> Tuple[str, str]:
    """固定形式 COBOL 行から (indicator, program area) を返す。"""
    if len(line) <= 6:
        return " ", ""
    indicator = line[6:7]
    program_area = line[7:72] if len(line) > 7 else ""
    return indicator, program_area


def _logical_lines(raw_lines: List[str]) -> List[Tuple[int, str, str]]:
    """(物理行番号, 種別, テキスト) のリストを返す。

    種別は "code" / "comment" / "blank"。
    継続行 (col7 = '-') は前の code 行に連結する。
    """
    out: List[Tuple[int, str, str]] = []
    for idx, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            out.append((idx, "blank", ""))
            continue
        indicator, body = _strip_fixed_format(raw)
        if indicator in ("*", "/"):
            out.append((idx, "comment", body.rstrip()))
            continue
        if indicator == "-" and out:
            # continuation — merge into last code line
            prev_idx, prev_kind, prev_text = out[-1]
            if prev_kind == "code":
                out[-1] = (prev_idx, prev_kind, prev_text.rstrip() + " " + body.strip())
                continue
        # free-format fallback: if the line has no fixed-format margin and is
        # comment-prefixed (`*>` or starts with `*`), treat as comment.
        candidate = body if indicator == " " else (indicator + body)
        stripped = candidate.strip() if not body else body
        if not stripped:
            # short line — treat as blank
            out.append((idx, "blank", ""))
            continue
        out.append((idx, "code", stripped))
    return out


# ---------------------------------------------------------------------------
# Tokenizing
# ---------------------------------------------------------------------------


_TOKEN_PATTERN = re.compile(r"\"[^\"]*\"|'[^']*'|[A-Za-z0-9_\-]+|\S")


def _tokenize(line: str) -> List[str]:
    return [m.group(0) for m in _TOKEN_PATTERN.finditer(line)]


def _is_identifier(tok: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_\-]*$", tok))


def _is_statement_word(tok: str) -> bool:
    return tok.upper() in _STATEMENT_WORDS


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class CobolParser:
    """1 ファイル = 1 プログラム前提のシンプルなパーサ。"""

    def __init__(self, path: Path, forced_encoding: Optional[str] = None):
        self.path = path
        self.raw_lines, self.encoding = _read_source(path, forced_encoding)

    # public ----------------------------------------------------------------

    def parse(self) -> Program:
        program = Program(
            program_id=self.path.stem.upper(),
            source_path=str(self.path),
            encoding=self.encoding,
        )

        lines = _logical_lines(self.raw_lines)
        program.total_lines = len(self.raw_lines)
        program.code_lines = sum(1 for _, k, _ in lines if k == "code")
        program.comment_lines = sum(1 for _, k, _ in lines if k == "comment")
        program.blank_lines = sum(1 for _, k, _ in lines if k == "blank")

        # Pass 1: program id, file declarations, divisions
        self._parse_header(lines, program)

        # Pass 2: procedure division — paragraphs, calls, crud
        self._parse_procedure(lines, program)

        # Pass 3: dead-code detection (mark paragraphs not referenced)
        self._mark_dead_code(program)

        # Aggregate complexity
        program.complexity = sum(p.complexity for p in program.paragraphs) or 1

        return program

    # internal --------------------------------------------------------------

    def _parse_header(self, lines: List[Tuple[int, str, str]], program: Program) -> None:
        select_buf: List[str] = []
        in_select = False
        in_fd = False
        current_fd: Optional[str] = None

        for lineno, kind, text in lines:
            if kind != "code":
                continue
            upper = text.upper()
            tokens = _tokenize(upper)
            if not tokens:
                continue

            # PROGRAM-ID. NAME.
            if tokens[0] == "PROGRAM-ID" or (len(tokens) >= 2 and tokens[0] == "PROGRAM-ID."):
                # Token may include trailing period
                merged = " ".join(tokens)
                m = re.search(r"PROGRAM-ID\.?\s+([A-Za-z][A-Za-z0-9_\-]*)", merged)
                if m:
                    program.program_id = m.group(1).upper()
                continue

            # SELECT statement may span multiple lines until period
            if tokens[0] == "SELECT" or in_select:
                in_select = True
                select_buf.append(text)
                if text.rstrip().endswith("."):
                    self._consume_select(" ".join(select_buf), program)
                    select_buf.clear()
                    in_select = False
                continue

            # FD <name>.
            if tokens[0] == "FD" and len(tokens) >= 2:
                current_fd = tokens[1].rstrip(".")
                # Ensure declaration exists (SELECT may not be present for COPY-only FDs)
                if not any(f.fd_name == current_fd for f in program.files):
                    program.files.append(FileDecl(program=program.program_id, fd_name=current_fd))
                in_fd = True
                continue

            # COPY can appear in DATA DIVISION too (WORKING-STORAGE, FILE SECTION, etc.)
            for i, tok in enumerate(tokens):
                if tok == "COPY" and i + 1 < len(tokens):
                    member = tokens[i + 1].strip("\"'").rstrip(".")
                    if not any(c.member == member and c.line == lineno for c in program.copies):
                        program.copies.append(CopyRef(
                            program=program.program_id, member=member, line=lineno,
                        ))

            if upper.startswith("PROCEDURE DIVISION"):
                # header parsing ends here
                return

    def _consume_select(self, buf: str, program: Program) -> None:
        upper = buf.upper()
        m_sel = re.search(r"SELECT\s+([A-Z][A-Z0-9_\-]*)", upper)
        if not m_sel:
            return
        name = m_sel.group(1)
        assign_to = None
        m_asg = re.search(r"ASSIGN\s+TO\s+(\"[^\"]+\"|'[^']+'|[A-Z0-9_\-\.]+)", upper)
        if m_asg:
            assign_to = m_asg.group(1).strip("\"'")
        organization = None
        m_org = re.search(r"ORGANIZATION\s+(?:IS\s+)?([A-Z\-]+)", upper)
        if m_org:
            organization = m_org.group(1)
        program.files.append(FileDecl(
            program=program.program_id,
            fd_name=name,
            assign_to=assign_to,
            organization=organization,
        ))

    def _parse_procedure(self, lines: List[Tuple[int, str, str]], program: Program) -> None:
        in_procedure = False
        current_section: Optional[str] = None
        current_para: Optional[Paragraph] = None
        in_exec_sql: List[str] = []

        # Map fd_name -> for FILE CRUD
        file_names = {f.fd_name.upper() for f in program.files}

        for lineno, kind, text in lines:
            if kind != "code":
                continue
            upper = text.upper()
            tokens = _tokenize(upper)
            if not tokens:
                continue

            if not in_procedure:
                if upper.startswith("PROCEDURE DIVISION"):
                    in_procedure = True
                continue

            # EXEC SQL ... END-EXEC handling (collect until END-EXEC)
            if in_exec_sql:
                in_exec_sql.append(upper)
                if "END-EXEC" in upper:
                    self._consume_exec_sql(" ".join(in_exec_sql), program, current_para, lineno)
                    in_exec_sql = []
                continue
            if tokens[0] == "EXEC" and len(tokens) >= 2 and tokens[1] == "SQL":
                in_exec_sql = [upper]
                if "END-EXEC" in upper:
                    self._consume_exec_sql(upper, program, current_para, lineno)
                    in_exec_sql = []
                continue

            # Section / paragraph header detection
            header = self._match_section_or_paragraph(tokens, text)
            if header is not None:
                kind_h, name_h = header
                if kind_h == "section":
                    current_section = name_h
                    # A section also acts as a paragraph anchor for PERFORM
                    para = Paragraph(
                        name=name_h,
                        program=program.program_id,
                        section=current_section,
                        start_line=lineno,
                    )
                    if current_para:
                        current_para.end_line = lineno - 1
                    current_para = para
                    program.paragraphs.append(para)
                else:  # paragraph
                    para = Paragraph(
                        name=name_h,
                        program=program.program_id,
                        section=current_section,
                        start_line=lineno,
                    )
                    if current_para:
                        current_para.end_line = lineno - 1
                    current_para = para
                    program.paragraphs.append(para)
                continue

            # Inside a paragraph — count statements + look for verbs of interest
            if current_para:
                current_para.statements += 1
                current_para.raw_source += text + "\n" if len(current_para.raw_source) < 800 else ""
                for tok in tokens:
                    if tok in _COMPLEXITY_KEYWORDS:
                        current_para.complexity += 1
                        break

            self._scan_verbs(tokens, upper, program, current_para, lineno, file_names)

        if current_para and current_para.end_line == 0:
            current_para.end_line = lines[-1][0] if lines else current_para.start_line

    # paragraph/section header detection ------------------------------------

    def _match_section_or_paragraph(
        self, tokens: List[str], text: str
    ) -> Optional[Tuple[str, str]]:
        """先頭トークンが段落/セクション名であれば (kind, name) を返す。"""
        if not tokens:
            return None
        first = tokens[0].rstrip(".")
        if not _is_identifier(first):
            return None
        if _is_statement_word(first):
            return None
        # Section: "<NAME> SECTION."
        if len(tokens) >= 2 and tokens[1].rstrip(".") == "SECTION":
            return ("section", first)
        # Paragraph: single identifier followed by period (rest of line empty)
        # We accept tokens like ['MAIN-RTN.'] or ['MAIN-RTN', '.']
        joined = " ".join(t.rstrip(".") for t in tokens).strip()
        if (tokens[0].endswith(".") and len(tokens) == 1) or (
            len(tokens) == 2 and tokens[1] == "."
        ):
            return ("paragraph", first)
        # Heuristic: only-identifier line ending with period
        compact = text.strip().rstrip(".").strip()
        if compact == first and text.strip().endswith("."):
            return ("paragraph", first)
        return None

    # Verb scanning ---------------------------------------------------------

    def _scan_verbs(
        self,
        tokens: List[str],
        upper: str,
        program: Program,
        current_para: Optional[Paragraph],
        lineno: int,
        file_names: set,
    ) -> None:
        # CALL
        for i, tok in enumerate(tokens):
            if tok == "CALL" and i + 1 < len(tokens):
                target = tokens[i + 1]
                dynamic = not (target.startswith("\"") or target.startswith("'"))
                callee = target.strip("\"'").rstrip(".")
                program.calls.append(CallEdge(
                    caller_program=program.program_id,
                    caller_paragraph=current_para.name if current_para else None,
                    callee=callee,
                    dynamic=dynamic,
                    line=lineno,
                ))
                if dynamic:
                    program.parse_warnings.append(
                        f"line {lineno}: 動的 CALL を検出 ({callee})。実行時バインドのためレビュー必須"
                    )

            if tok == "COPY" and i + 1 < len(tokens):
                member = tokens[i + 1].strip("\"'").rstrip(".")
                program.copies.append(CopyRef(
                    program=program.program_id, member=member, line=lineno,
                ))

            # PERFORM / GO TO — track paragraph references
            if tok == "PERFORM" and i + 1 < len(tokens):
                ref = tokens[i + 1].rstrip(".")
                if _is_identifier(ref) and not _is_statement_word(ref):
                    self._record_reference(program, ref, current_para)
            if tok == "GO" and i + 2 < len(tokens) and tokens[i + 1] == "TO":
                ref = tokens[i + 2].rstrip(".")
                if _is_identifier(ref) and not _is_statement_word(ref):
                    self._record_reference(program, ref, current_para)

            # OPEN <mode> <file>
            if tok == "OPEN" and i + 1 < len(tokens):
                mode = tokens[i + 1]
                if mode in _OPEN_MODES:
                    op = _OPEN_MODES[mode]
                    for j in range(i + 2, len(tokens)):
                        candidate = tokens[j].rstrip(".")
                        if candidate in _OPEN_MODES:
                            mode = candidate
                            op = _OPEN_MODES[mode]
                            continue
                        if _is_identifier(candidate) and candidate.upper() in file_names:
                            program.crud.append(CrudUsage(
                                program=program.program_id,
                                paragraph=current_para.name if current_para else None,
                                target=candidate,
                                target_kind="FILE",
                                operation=op,
                                line=lineno,
                            ))

            # READ / WRITE / REWRITE / DELETE <file>
            if tok in {"READ", "WRITE", "REWRITE", "DELETE"} and i + 1 < len(tokens):
                target = tokens[i + 1].rstrip(".")
                op = {"READ": "READ", "WRITE": "CREATE", "REWRITE": "UPDATE", "DELETE": "DELETE"}[tok]
                if _is_identifier(target):
                    target_kind = "FILE" if target.upper() in file_names else "RECORD"
                    program.crud.append(CrudUsage(
                        program=program.program_id,
                        paragraph=current_para.name if current_para else None,
                        target=target,
                        target_kind=target_kind,
                        operation=op,
                        line=lineno,
                    ))

    def _record_reference(self, program: Program, target: str, current_para: Optional[Paragraph]) -> None:
        for p in program.paragraphs:
            if p.name == target:
                caller = current_para.name if current_para else "<top>"
                if caller not in p.referenced_by:
                    p.referenced_by.append(caller)
                return
        # forward reference — will resolve in second pass
        # We rely on full-program scan, so just stash in a temp list on Program
        if not hasattr(program, "_pending_refs"):
            program._pending_refs = []  # type: ignore[attr-defined]
        program._pending_refs.append((target, current_para.name if current_para else "<top>"))  # type: ignore[attr-defined]

    def _consume_exec_sql(
        self,
        block: str,
        program: Program,
        current_para: Optional[Paragraph],
        lineno: int,
    ) -> None:
        upper = block.upper()
        operation = None
        if re.search(r"\bSELECT\b", upper):
            operation = "READ"
        elif re.search(r"\bINSERT\b", upper):
            operation = "CREATE"
        elif re.search(r"\bUPDATE\b", upper):
            operation = "UPDATE"
        elif re.search(r"\bDELETE\b", upper):
            operation = "DELETE"
        if not operation:
            return
        # crude table extraction
        m = re.search(r"(?:FROM|INTO|UPDATE)\s+([A-Z][A-Z0-9_]*)", upper)
        target = m.group(1) if m else "<UNKNOWN>"
        program.crud.append(CrudUsage(
            program=program.program_id,
            paragraph=current_para.name if current_para else None,
            target=target,
            target_kind="TABLE",
            operation=operation,
            line=lineno,
        ))

    # dead code -------------------------------------------------------------

    def _mark_dead_code(self, program: Program) -> None:
        # Resolve pending references first
        pending = getattr(program, "_pending_refs", [])
        for target, caller in pending:
            for p in program.paragraphs:
                if p.name == target and caller not in p.referenced_by:
                    p.referenced_by.append(caller)
        if pending:
            try:
                delattr(program, "_pending_refs")
            except AttributeError:
                pass

        # First paragraph is entry — always alive
        if not program.paragraphs:
            return
        entry = program.paragraphs[0].name
        for p in program.paragraphs:
            if p.name == entry:
                continue
            if not p.referenced_by:
                program.dead_paragraphs.append(p.name)


# ---------------------------------------------------------------------------
# Project-level analyzer
# ---------------------------------------------------------------------------


class CobolProject:
    """ディレクトリ単位で複数ファイルを解析する。"""

    def __init__(self, root: Path, forced_encoding: Optional[str] = None):
        self.root = root
        self.forced_encoding = forced_encoding
        self.programs: List[Program] = []

    def discover(self) -> List[Path]:
        if self.root.is_file():
            return [self.root]
        files: List[Path] = []
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix.lower() in COBOL_EXTENSIONS:
                files.append(path)
        return sorted(files)

    def analyze(self) -> None:
        for path in self.discover():
            try:
                parser = CobolParser(path, self.forced_encoding)
                self.programs.append(parser.parse())
            except Exception as err:  # noqa: BLE001
                placeholder = Program(
                    program_id=path.stem.upper(),
                    source_path=str(path),
                    encoding="?",
                )
                placeholder.parse_warnings.append(f"パース失敗: {err}")
                self.programs.append(placeholder)

    # serialization ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "root": str(self.root),
            "programs": [self._program_dict(p) for p in self.programs],
            "summary": self.summary(),
        }

    def _program_dict(self, program: Program) -> dict:
        return {
            "program_id": program.program_id,
            "source_path": program.source_path,
            "encoding": program.encoding,
            "metrics": {
                "total_lines": program.total_lines,
                "code_lines": program.code_lines,
                "comment_lines": program.comment_lines,
                "blank_lines": program.blank_lines,
                "complexity": program.complexity,
                "paragraphs": len(program.paragraphs),
            },
            "description": program.description,
            "paragraphs": [
                {
                    "name": p.name,
                    "section": p.section,
                    "start_line": p.start_line,
                    "end_line": p.end_line,
                    "statements": p.statements,
                    "complexity": p.complexity,
                    "referenced_by": p.referenced_by,
                    "description": p.description,
                }
                for p in program.paragraphs
            ],
            "files": [asdict(f) for f in program.files],
            "calls": [asdict(c) for c in program.calls],
            "copies": [asdict(c) for c in program.copies],
            "crud": [asdict(c) for c in program.crud],
            "dead_paragraphs": program.dead_paragraphs,
            "parse_warnings": program.parse_warnings,
        }

    def summary(self) -> dict:
        total_loc = sum(p.total_lines for p in self.programs)
        total_code = sum(p.code_lines for p in self.programs)
        total_paragraphs = sum(len(p.paragraphs) for p in self.programs)
        total_dead = sum(len(p.dead_paragraphs) for p in self.programs)
        return {
            "programs": len(self.programs),
            "total_lines": total_loc,
            "code_lines": total_code,
            "paragraphs": total_paragraphs,
            "dead_paragraphs": total_dead,
        }


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _h(text: object) -> str:
    return html.escape("" if text is None else str(text))


_OP_CLASS = {
    "READ": "op-read",
    "CREATE": "op-create",
    "UPDATE": "op-update",
    "DELETE": "op-delete",
    "APPEND": "op-append",
    "READ/UPDATE": "op-rw",
}


def _complexity_class(value: int) -> str:
    if value >= 8:
        return "lv-hi"
    if value >= 4:
        return "lv-md"
    return "lv-lo"


def _display_root(root: Path) -> str:
    """ヒーローに表示する解析対象名。絶対パスは出さず、入力したディレクトリ名のみ。"""
    if root.is_file():
        return root.name
    name = root.name
    return name if name else str(root.resolve().name) or "."


def _display_source(source_path: str, root: Path) -> str:
    """個別プログラムのソースパス表示。root 配下なら相対、外なら basename のみ。"""
    src = Path(source_path)
    root_full = root.resolve()
    try:
        rel = src.resolve().relative_to(root_full if root_full.is_dir() else root_full.parent)
        return rel.as_posix()
    except ValueError:
        return src.name


def render_html(project: CobolProject) -> str:
    summary = project.summary()
    generated_at = datetime.now().isoformat(timespec="seconds")
    display_root = _display_root(project.root)
    parts: List[str] = []
    parts.append("<!doctype html><html lang='ja'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append("<title>COBOL Analyzer Report</title>")
    parts.append("<style>")
    parts.append(_CSS)
    parts.append("</style></head><body>")

    # Hero
    parts.append("<header class='hero'>")
    parts.append("<div class='hero-inner'>")
    parts.append("<div class='brand'>")
    parts.append("<span class='brand-mark'></span>")
    parts.append("<span class='brand-name'>COBOL Analyzer</span>")
    parts.append("</div>")
    parts.append("<h1 class='hero-title'>静的解析レポート</h1>")
    parts.append(
        f"<p class='hero-meta'>"
        f"<span>解析対象: <code>{_h(display_root)}</code></span>"
        f"<span class='dot'>·</span>"
        f"<span>生成: {_h(generated_at)}</span>"
        f"</p>"
    )
    parts.append("</div></header>")

    # KPI strip
    kpis = [
        ("プログラム", summary["programs"], ""),
        ("総行数", f"{summary['total_lines']:,}", ""),
        ("コード行数", f"{summary['code_lines']:,}", ""),
        ("段落", summary["paragraphs"], ""),
        ("デッドコード", summary["dead_paragraphs"], "kpi-warn" if summary["dead_paragraphs"] else ""),
    ]
    parts.append("<div class='kpi-row'>")
    for label, value, extra in kpis:
        parts.append(
            f"<div class='kpi {extra}'>"
            f"<div class='kpi-value'>{_h(value)}</div>"
            f"<div class='kpi-label'>{_h(label)}</div>"
            f"</div>"
        )
    parts.append("</div>")

    # Two-column layout: sidebar + main
    parts.append("<div class='layout'>")
    # Sidebar
    parts.append("<aside class='sidebar'>")
    parts.append("<div class='side-title'>プログラム</div>")
    parts.append("<nav class='side-nav'>")
    for p in project.programs:
        dead_dot = "<span class='nav-dot'></span>" if p.dead_paragraphs else ""
        parts.append(
            f"<a href='#prog-{_h(p.program_id)}' class='nav-link'>"
            f"<span class='nav-name'>{_h(p.program_id)}</span>"
            f"<span class='nav-meta'>{len(p.paragraphs)} 段落 · {p.code_lines:,} 行</span>"
            f"{dead_dot}"
            f"</a>"
        )
    parts.append("</nav></aside>")

    # Main
    parts.append("<main class='main'>")
    for program in project.programs:
        parts.append(_render_program(program, project.root))
    parts.append("</main>")
    parts.append("</div>")

    parts.append("<footer class='site-footer'>")
    parts.append("Generated by <a href='https://github.com/takahiro-crypto/cobol-analyzer'>COBOL Analyzer</a>")
    parts.append("</footer>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_program(p: Program, root: Path) -> str:
    chunks: List[str] = []
    chunks.append(f"<article class='program' id='prog-{_h(p.program_id)}'>")

    # Program header
    chunks.append("<header class='prog-head'>")
    chunks.append(f"<h2 class='prog-title'>{_h(p.program_id)}</h2>")
    chunks.append("<div class='prog-meta'>")
    chunks.append(f"<span class='chip chip-mono'>{_h(_display_source(p.source_path, root))}</span>")
    chunks.append(f"<span class='chip chip-enc'>{_h(p.encoding)}</span>")
    if p.dead_paragraphs:
        chunks.append(f"<span class='chip chip-warn'>dead × {len(p.dead_paragraphs)}</span>")
    chunks.append("</div></header>")

    # AI summary callout
    if p.description:
        chunks.append("<section class='callout'>")
        chunks.append("<div class='callout-label'>AI 仕様サマリ</div>")
        chunks.append(f"<p class='callout-body'>{_h(p.description)}</p>")
        chunks.append("</section>")

    # Metrics chips
    chunks.append("<section class='metrics'>")
    for label, value in (
        ("総行数", f"{p.total_lines:,}"),
        ("コード", f"{p.code_lines:,}"),
        ("コメント", f"{p.comment_lines:,}"),
        ("空行", f"{p.blank_lines:,}"),
        ("段落", len(p.paragraphs)),
        ("複雑度", p.complexity),
    ):
        chunks.append(
            f"<div class='metric'>"
            f"<div class='metric-value'>{_h(value)}</div>"
            f"<div class='metric-label'>{_h(label)}</div>"
            f"</div>"
        )
    chunks.append("</section>")

    # Files as cards
    if p.files:
        chunks.append("<section class='block'>")
        chunks.append("<h3 class='block-title'>ファイル宣言</h3>")
        chunks.append("<div class='file-grid'>")
        for f in p.files:
            chunks.append("<div class='file-card'>")
            chunks.append(f"<div class='file-name'>{_h(f.fd_name)}</div>")
            chunks.append("<dl class='file-meta'>")
            chunks.append(f"<dt>ASSIGN</dt><dd>{_h(f.assign_to or '—')}</dd>")
            chunks.append(f"<dt>ORG</dt><dd>{_h(f.organization or '—')}</dd>")
            chunks.append("</dl></div>")
        chunks.append("</div></section>")

    # Paragraphs as cards
    if p.paragraphs:
        chunks.append("<section class='block'>")
        chunks.append(
            f"<h3 class='block-title'>段落・セクション "
            f"<span class='block-count'>{len(p.paragraphs)}</span></h3>"
        )
        chunks.append("<div class='para-grid'>")
        for para in p.paragraphs:
            dead = para.name in p.dead_paragraphs
            klass = "para-card" + (" para-dead" if dead else "")
            chunks.append(f"<div class='{klass}'>")
            chunks.append("<div class='para-head'>")
            chunks.append(f"<div class='para-name'>{_h(para.name)}</div>")
            chunks.append("<div class='para-badges'>")
            if dead:
                chunks.append("<span class='badge badge-dead'>dead</span>")
            chunks.append(
                f"<span class='badge badge-cplx {_complexity_class(para.complexity)}'>"
                f"cplx {_h(para.complexity)}</span>"
            )
            chunks.append("</div></div>")
            chunks.append("<div class='para-meta'>")
            chunks.append(f"<span>L{_h(para.start_line)}–{_h(para.end_line)}</span>")
            chunks.append(f"<span>文 {_h(para.statements)}</span>")
            if para.section:
                chunks.append(f"<span>§ {_h(para.section)}</span>")
            chunks.append("</div>")
            if para.referenced_by:
                chunks.append("<div class='para-refs'>")
                chunks.append("<span class='para-refs-label'>呼出元</span>")
                for ref in para.referenced_by:
                    chunks.append(f"<span class='ref-chip'>{_h(ref)}</span>")
                chunks.append("</div>")
            if para.description:
                chunks.append(f"<p class='para-desc'>{_h(para.description)}</p>")
            chunks.append("</div>")
        chunks.append("</div></section>")

    # Calls as flow list
    if p.calls:
        chunks.append("<section class='block'>")
        chunks.append(
            f"<h3 class='block-title'>CALL <span class='block-count'>{len(p.calls)}</span></h3>"
        )
        chunks.append("<ul class='flow-list'>")
        for c in p.calls:
            kind_class = "call-dynamic" if c.dynamic else "call-static"
            kind_label = "動的" if c.dynamic else "静的"
            from_ = c.caller_paragraph or "<top>"
            chunks.append(
                f"<li class='flow-item'>"
                f"<span class='flow-line'>L{_h(c.line)}</span>"
                f"<span class='flow-node'>{_h(from_)}</span>"
                f"<span class='flow-arrow'>→</span>"
                f"<span class='flow-node flow-target'>{_h(c.callee)}</span>"
                f"<span class='badge {kind_class}'>{_h(kind_label)}</span>"
                f"</li>"
            )
        chunks.append("</ul></section>")

    # Copies as chips
    if p.copies:
        chunks.append("<section class='block'>")
        chunks.append(
            f"<h3 class='block-title'>COPY <span class='block-count'>{len(p.copies)}</span></h3>"
        )
        chunks.append("<div class='chip-list'>")
        for c in p.copies:
            chunks.append(
                f"<span class='chip chip-copy'>{_h(c.member)} "
                f"<span class='chip-sub'>L{_h(c.line)}</span></span>"
            )
        chunks.append("</div></section>")

    # CRUD grouped by target
    if p.crud:
        grouped: Dict[str, List[CrudUsage]] = {}
        for c in p.crud:
            key = f"{c.target_kind}::{c.target}"
            grouped.setdefault(key, []).append(c)
        chunks.append("<section class='block'>")
        chunks.append(
            f"<h3 class='block-title'>CRUD <span class='block-count'>{len(p.crud)}</span></h3>"
        )
        chunks.append("<div class='crud-grid'>")
        for key, items in grouped.items():
            kind, target = key.split("::", 1)
            kind_icon = "🗄" if kind == "TABLE" else ("📄" if kind == "FILE" else "•")
            chunks.append("<div class='crud-card'>")
            chunks.append(
                f"<div class='crud-head'>"
                f"<span class='crud-kind'>{kind_icon} {_h(kind)}</span>"
                f"<span class='crud-target'>{_h(target)}</span>"
                f"</div>"
            )
            # Unique ops
            ops = sorted({i.operation for i in items})
            chunks.append("<div class='crud-ops'>")
            for op in ops:
                cls = _OP_CLASS.get(op, "op-other")
                chunks.append(f"<span class='badge {cls}'>{_h(op)}</span>")
            chunks.append("</div>")
            chunks.append("<ul class='crud-occurrences'>")
            for i in items:
                chunks.append(
                    f"<li><span class='occ-line'>L{_h(i.line)}</span> "
                    f"<span class='occ-para'>{_h(i.paragraph or '—')}</span> "
                    f"<span class='occ-op'>{_h(i.operation)}</span></li>"
                )
            chunks.append("</ul>")
            chunks.append("</div>")
        chunks.append("</div></section>")

    # Warnings
    if p.parse_warnings:
        chunks.append("<section class='block alert'>")
        chunks.append(
            f"<h3 class='block-title'>警告 <span class='block-count'>{len(p.parse_warnings)}</span></h3>"
        )
        chunks.append("<ul class='warn-list'>")
        for w in p.parse_warnings:
            chunks.append(f"<li>{_h(w)}</li>")
        chunks.append("</ul></section>")

    chunks.append("</article>")
    return "\n".join(chunks)


_CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  color-scheme: light;
  --bg: #f6f8fb;
  --fg: #0f172a;
  --muted: #64748b;
  --border: #e2e8f0;
  --card: #ffffff;
  --primary: #2563eb;
  --primary-soft: #dbeafe;
  --accent: #0ea5e9;
  --hero-from: #0f172a;
  --hero-to: #1e3a8a;
  --warn: #ef4444;
  --warn-soft: #fee2e2;
  --ok: #10b981;
  --shadow: 0 1px 2px rgba(15,23,42,.04), 0 4px 12px rgba(15,23,42,.06);
  --radius: 12px;
}
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Yu Gothic UI",
               "Hiragino Sans", "Noto Sans JP", sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
h2, h3 { margin: 0; font-weight: 600; letter-spacing: -0.01em; }

/* Hero */
.hero {
  background: linear-gradient(135deg, var(--hero-from), var(--hero-to));
  color: #fff;
  padding: 3.5rem 1.5rem 4.5rem;
}
.hero-inner { max-width: 1180px; margin: 0 auto; }
.brand { display: flex; align-items: center; gap: .6rem; opacity: .85;
         font-size: .85rem; letter-spacing: .1em; text-transform: uppercase; }
.brand-mark {
  width: 10px; height: 10px; border-radius: 999px;
  background: linear-gradient(135deg, #38bdf8, #818cf8);
  box-shadow: 0 0 12px rgba(56,189,248,.7);
}
.brand-name { font-weight: 600; }
.hero-title {
  font-size: clamp(1.8rem, 3.2vw, 2.6rem);
  margin: .8rem 0 .6rem; letter-spacing: -0.02em;
}
.hero-meta { display: flex; flex-wrap: wrap; gap: .6rem .9rem;
             color: rgba(255,255,255,.78); margin: 0; font-size: .92rem; }
.hero-meta .dot { opacity: .5; }
.hero-meta code { background: rgba(255,255,255,.12); padding: .1em .45em;
                  border-radius: 5px; color: #e0e7ff; }

/* KPI row */
.kpi-row {
  max-width: 1180px; margin: -2.5rem auto 0; padding: 0 1.5rem;
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 1rem;
  position: relative; z-index: 1;
}
.kpi {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1.1rem 1.2rem;
  box-shadow: var(--shadow);
}
.kpi-value { font-size: 1.8rem; font-weight: 600; letter-spacing: -0.02em; }
.kpi-label { color: var(--muted); font-size: .82rem; margin-top: .2rem;
             letter-spacing: .05em; }
.kpi.kpi-warn .kpi-value { color: var(--warn); }

/* Layout */
.layout {
  max-width: 1180px; margin: 2rem auto 0; padding: 0 1.5rem 3rem;
  display: grid; grid-template-columns: 240px 1fr; gap: 2rem;
}
@media (max-width: 900px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .layout { grid-template-columns: 1fr; }
  .sidebar { position: static !important; }
}

/* Sidebar */
.sidebar { position: sticky; top: 1.5rem; align-self: start;
           max-height: calc(100vh - 3rem); overflow-y: auto; }
.side-title { font-size: .75rem; letter-spacing: .12em; text-transform: uppercase;
              color: var(--muted); margin: 0 .6rem .5rem; font-weight: 600; }
.side-nav { display: flex; flex-direction: column; gap: 2px; }
.nav-link {
  display: flex; flex-direction: column; gap: 2px;
  padding: .6rem .8rem; border-radius: 8px;
  color: var(--fg); position: relative;
  transition: background .15s;
}
.nav-link:hover { background: var(--primary-soft); text-decoration: none; }
.nav-name { font-weight: 600; font-size: .95rem; }
.nav-meta { color: var(--muted); font-size: .78rem; }
.nav-dot {
  position: absolute; top: .9rem; right: .8rem;
  width: 7px; height: 7px; border-radius: 999px; background: var(--warn);
}

/* Main / program */
.main { display: flex; flex-direction: column; gap: 2rem; }
.program {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1.8rem;
  box-shadow: var(--shadow); scroll-margin-top: 1.5rem;
}
.prog-head { display: flex; flex-wrap: wrap; align-items: center;
             justify-content: space-between; gap: .8rem; margin-bottom: 1.2rem; }
.prog-title { font-size: 1.5rem; letter-spacing: -0.02em; }
.prog-meta { display: flex; flex-wrap: wrap; gap: .4rem; }

/* Chips & badges */
.chip {
  display: inline-flex; align-items: center;
  background: #f1f5f9; color: #334155;
  border-radius: 999px; padding: .2rem .7rem;
  font-size: .8rem; font-weight: 500;
}
.chip-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
             font-size: .76rem; background: #eef2f7; }
.chip-enc { background: #ecfeff; color: #155e75; }
.chip-warn { background: var(--warn-soft); color: #991b1b; }
.chip-copy { background: #f3e8ff; color: #6b21a8; font-family: ui-monospace, monospace; }
.chip-sub { color: var(--muted); margin-left: .3rem; font-size: .72rem; }
.chip-list { display: flex; flex-wrap: wrap; gap: .4rem; }

.badge {
  display: inline-flex; align-items: center;
  padding: .15rem .55rem; border-radius: 6px;
  font-size: .72rem; font-weight: 600; letter-spacing: .02em;
  background: #f1f5f9; color: #334155;
}
.badge-dead { background: var(--warn-soft); color: #991b1b; }
.badge-cplx.lv-lo { background: #d1fae5; color: #065f46; }
.badge-cplx.lv-md { background: #fef3c7; color: #92400e; }
.badge-cplx.lv-hi { background: #fee2e2; color: #991b1b; }
.call-static { background: #dbeafe; color: #1e40af; }
.call-dynamic { background: #fef3c7; color: #92400e; }

.op-read   { background: #dbeafe; color: #1e40af; }
.op-create { background: #d1fae5; color: #065f46; }
.op-update { background: #fef3c7; color: #92400e; }
.op-delete { background: #fee2e2; color: #991b1b; }
.op-append { background: #ede9fe; color: #5b21b6; }
.op-rw     { background: #cffafe; color: #155e75; }
.op-other  { background: #e2e8f0; color: #475569; }

/* AI callout */
.callout {
  background: linear-gradient(180deg, #eef2ff, #fff);
  border: 1px solid #c7d2fe; border-left: 4px solid #6366f1;
  border-radius: 8px; padding: 1rem 1.2rem; margin-bottom: 1.4rem;
}
.callout-label {
  font-size: .72rem; letter-spacing: .1em; text-transform: uppercase;
  color: #4338ca; font-weight: 700; margin-bottom: .3rem;
}
.callout-body { margin: 0; white-space: pre-wrap; }

/* Metrics chips */
.metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: .8rem; margin-bottom: 1.6rem;
}
.metric {
  background: #f8fafc; border: 1px solid var(--border);
  border-radius: 10px; padding: .7rem .9rem;
}
.metric-value { font-size: 1.35rem; font-weight: 600; letter-spacing: -0.01em; }
.metric-label { color: var(--muted); font-size: .76rem; margin-top: .15rem; }

/* Blocks */
.block { margin-top: 1.6rem; }
.block-title {
  font-size: 1rem; margin-bottom: .8rem;
  display: flex; align-items: center; gap: .5rem;
}
.block-count {
  background: #f1f5f9; color: var(--muted);
  border-radius: 999px; padding: .05rem .55rem;
  font-size: .75rem; font-weight: 500;
}
.block.alert .block-title { color: #991b1b; }

/* File cards */
.file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: .8rem; }
.file-card {
  background: #f8fafc; border: 1px solid var(--border);
  border-radius: 10px; padding: .9rem 1rem;
}
.file-name { font-weight: 600; font-family: ui-monospace, monospace; }
.file-meta { display: grid; grid-template-columns: auto 1fr;
             column-gap: .8rem; row-gap: .2rem;
             margin: .5rem 0 0; font-size: .8rem; }
.file-meta dt { color: var(--muted); }
.file-meta dd { margin: 0; font-family: ui-monospace, monospace; }

/* Paragraph cards */
.para-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: .8rem;
}
.para-card {
  background: #f8fafc; border: 1px solid var(--border);
  border-radius: 10px; padding: .9rem 1rem;
  display: flex; flex-direction: column; gap: .5rem;
}
.para-card.para-dead {
  background: linear-gradient(180deg, #fff1f2, #fff);
  border-color: #fecaca;
}
.para-head { display: flex; justify-content: space-between;
             align-items: flex-start; gap: .5rem; }
.para-name { font-weight: 600; font-family: ui-monospace, monospace; word-break: break-all; }
.para-badges { display: flex; flex-wrap: wrap; gap: .3rem; flex-shrink: 0; }
.para-meta { display: flex; flex-wrap: wrap; gap: .4rem .8rem;
             color: var(--muted); font-size: .78rem; }
.para-refs { display: flex; flex-wrap: wrap; gap: .3rem; align-items: center; }
.para-refs-label { color: var(--muted); font-size: .75rem; }
.ref-chip {
  background: #fff; border: 1px solid var(--border);
  border-radius: 6px; padding: .1rem .45rem;
  font-size: .73rem; font-family: ui-monospace, monospace;
}
.para-desc { margin: .2rem 0 0; font-size: .9rem; color: #334155; white-space: pre-wrap; }

/* Flow list (CALL) */
.flow-list { list-style: none; padding: 0; margin: 0;
             display: flex; flex-direction: column; gap: .4rem; }
.flow-item {
  display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  padding: .55rem .85rem; background: #f8fafc;
  border: 1px solid var(--border); border-radius: 8px;
  font-size: .88rem;
}
.flow-line { color: var(--muted); font-family: ui-monospace, monospace; font-size: .78rem; }
.flow-node { font-family: ui-monospace, monospace; font-weight: 500; }
.flow-target { color: var(--primary); }
.flow-arrow { color: var(--muted); }

/* CRUD */
.crud-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: .8rem;
}
.crud-card {
  background: #f8fafc; border: 1px solid var(--border);
  border-radius: 10px; padding: .9rem 1rem;
  display: flex; flex-direction: column; gap: .55rem;
}
.crud-head { display: flex; flex-direction: column; gap: .15rem; }
.crud-kind { color: var(--muted); font-size: .75rem; letter-spacing: .04em; }
.crud-target { font-weight: 600; font-family: ui-monospace, monospace; word-break: break-all; }
.crud-ops { display: flex; flex-wrap: wrap; gap: .3rem; }
.crud-occurrences { list-style: none; padding: 0; margin: 0;
                    display: flex; flex-direction: column; gap: .2rem; }
.crud-occurrences li { display: flex; gap: .6rem; font-size: .78rem; }
.occ-line { color: var(--muted); font-family: ui-monospace, monospace; min-width: 3rem; }
.occ-para { font-family: ui-monospace, monospace; flex: 1; }
.occ-op { color: var(--muted); }

/* Warnings */
.warn-list { list-style: none; padding: 0; margin: 0;
             display: flex; flex-direction: column; gap: .35rem; }
.warn-list li {
  background: var(--warn-soft); color: #7f1d1d;
  border-radius: 6px; padding: .5rem .8rem; font-size: .85rem;
}

/* Footer */
.site-footer { text-align: center; color: var(--muted); font-size: .82rem;
               padding: 2rem 1rem 3rem; }
.site-footer a { color: var(--muted); border-bottom: 1px dotted var(--muted); }
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="COBOL 静的解析 + HTML レポート生成")
    p.add_argument("source", help="解析対象のディレクトリまたはファイル")
    p.add_argument("-o", "--output", default="analysis_report.html",
                   help="HTML レポートの出力パス")
    p.add_argument("-j", "--json", default=None,
                   help="JSON 解析結果の出力パス（省略時は出力しない）")
    p.add_argument("--encoding", default=None,
                   help="強制エンコーディング（utf-8 / cp932 など）")
    return p


def run(args: argparse.Namespace) -> int:
    root = Path(args.source)
    if not root.exists():
        print(f"パスが見つかりません: {root}", file=sys.stderr)
        return 2

    project = CobolProject(root, forced_encoding=args.encoding)
    project.analyze()

    html_text = render_html(project)
    Path(args.output).write_text(html_text, encoding="utf-8")
    print(f"HTML レポート: {args.output}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 出力 : {args.json}")

    summary = project.summary()
    print(
        f"プログラム数 {summary['programs']} / "
        f"段落数 {summary['paragraphs']} / "
        f"デッドコード候補 {summary['dead_paragraphs']}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
