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


def render_html(project: CobolProject) -> str:
    summary = project.summary()
    parts: List[str] = []
    parts.append("<!doctype html><html lang='ja'><head><meta charset='utf-8'>")
    parts.append("<title>COBOL 分析レポート</title>")
    parts.append("<style>")
    parts.append(_CSS)
    parts.append("</style></head><body>")
    parts.append("<header><h1>COBOL 分析レポート</h1>")
    parts.append(
        f"<p class='meta'>生成日時: {_h(datetime.now().isoformat(timespec='seconds'))} / "
        f"対象ルート: {_h(project.root)}</p>"
    )
    parts.append("</header>")

    parts.append("<section class='card'><h2>サマリ</h2><dl class='summary'>")
    parts.append(f"<dt>プログラム数</dt><dd>{summary['programs']}</dd>")
    parts.append(f"<dt>総行数</dt><dd>{summary['total_lines']}</dd>")
    parts.append(f"<dt>コード行数</dt><dd>{summary['code_lines']}</dd>")
    parts.append(f"<dt>段落数</dt><dd>{summary['paragraphs']}</dd>")
    parts.append(f"<dt>デッドコード候補</dt><dd>{summary['dead_paragraphs']}</dd>")
    parts.append("</dl></section>")

    # TOC
    parts.append("<section class='card'><h2>プログラム一覧</h2><ul class='toc'>")
    for p in project.programs:
        parts.append(
            f"<li><a href='#prog-{_h(p.program_id)}'>{_h(p.program_id)}</a> "
            f"<span class='dim'>({_h(p.source_path)})</span></li>"
        )
    parts.append("</ul></section>")

    for program in project.programs:
        parts.append(_render_program(program))

    parts.append("</body></html>")
    return "\n".join(parts)


def _render_program(p: Program) -> str:
    chunks: List[str] = []
    chunks.append(f"<section class='card program' id='prog-{_h(p.program_id)}'>")
    chunks.append(f"<h2>{_h(p.program_id)}</h2>")
    chunks.append(
        f"<p class='meta'>ファイル: <code>{_h(p.source_path)}</code> / "
        f"エンコーディング: {_h(p.encoding)}</p>"
    )

    if p.description:
        chunks.append(f"<div class='ai-desc'><h3>AI 仕様サマリ</h3><p>{_h(p.description)}</p></div>")

    chunks.append("<h3>メトリクス</h3>")
    chunks.append("<table class='kv'>")
    for label, value in (
        ("総行数", p.total_lines),
        ("コード行数", p.code_lines),
        ("コメント行数", p.comment_lines),
        ("空行", p.blank_lines),
        ("段落数", len(p.paragraphs)),
        ("複雑度（合計）", p.complexity),
        ("デッドコード候補", len(p.dead_paragraphs)),
    ):
        chunks.append(f"<tr><th>{_h(label)}</th><td>{_h(value)}</td></tr>")
    chunks.append("</table>")

    # Files
    if p.files:
        chunks.append("<h3>ファイル宣言</h3><table class='grid'>")
        chunks.append("<tr><th>FD</th><th>ASSIGN TO</th><th>ORGANIZATION</th></tr>")
        for f in p.files:
            chunks.append(
                f"<tr><td>{_h(f.fd_name)}</td><td>{_h(f.assign_to or '-')}</td>"
                f"<td>{_h(f.organization or '-')}</td></tr>"
            )
        chunks.append("</table>")

    # Paragraphs
    if p.paragraphs:
        chunks.append("<h3>段落・セクション</h3><table class='grid'>")
        chunks.append(
            "<tr><th>名称</th><th>セクション</th><th>行</th><th>文数</th>"
            "<th>複雑度</th><th>呼出元</th><th>説明</th></tr>"
        )
        for para in p.paragraphs:
            dead = para.name in p.dead_paragraphs
            row_cls = " class='dead'" if dead else ""
            referenced = ", ".join(para.referenced_by) if para.referenced_by else "<i>なし</i>"
            chunks.append(
                f"<tr{row_cls}><td>{_h(para.name)}{' <span class=tag>dead</span>' if dead else ''}</td>"
                f"<td>{_h(para.section or '-')}</td>"
                f"<td>{_h(para.start_line)}〜{_h(para.end_line)}</td>"
                f"<td>{_h(para.statements)}</td>"
                f"<td>{_h(para.complexity)}</td>"
                f"<td>{referenced}</td>"
                f"<td>{_h(para.description) or '-'}</td></tr>"
            )
        chunks.append("</table>")

    # Calls
    if p.calls:
        chunks.append("<h3>CALL 呼び出し</h3><table class='grid'>")
        chunks.append("<tr><th>行</th><th>呼出元段落</th><th>呼び先</th><th>種別</th></tr>")
        for c in p.calls:
            kind = "動的" if c.dynamic else "静的"
            chunks.append(
                f"<tr><td>{_h(c.line)}</td><td>{_h(c.caller_paragraph or '-')}</td>"
                f"<td>{_h(c.callee)}</td><td>{_h(kind)}</td></tr>"
            )
        chunks.append("</table>")

    # Copies
    if p.copies:
        chunks.append("<h3>COPY 句</h3><table class='grid'>")
        chunks.append("<tr><th>行</th><th>メンバ</th></tr>")
        for c in p.copies:
            chunks.append(f"<tr><td>{_h(c.line)}</td><td>{_h(c.member)}</td></tr>")
        chunks.append("</table>")

    # CRUD
    if p.crud:
        chunks.append("<h3>CRUD 利用状況</h3><table class='grid'>")
        chunks.append("<tr><th>行</th><th>段落</th><th>対象</th><th>種別</th><th>操作</th></tr>")
        for c in p.crud:
            chunks.append(
                f"<tr><td>{_h(c.line)}</td><td>{_h(c.paragraph or '-')}</td>"
                f"<td>{_h(c.target)}</td><td>{_h(c.target_kind)}</td>"
                f"<td>{_h(c.operation)}</td></tr>"
            )
        chunks.append("</table>")

    # Warnings
    if p.parse_warnings:
        chunks.append("<h3>警告</h3><ul class='warn'>")
        for w in p.parse_warnings:
            chunks.append(f"<li>{_h(w)}</li>")
        chunks.append("</ul>")

    chunks.append("</section>")
    return "\n".join(chunks)


_CSS = """
:root { color-scheme: light; --fg: #1a1a1a; --bg: #fafaf7; --accent: #c0392b;
        --muted: #777; --grid: #d6d3c4; --card: #fff; }
body { font-family: -apple-system, "Segoe UI", "Yu Gothic UI", sans-serif;
       background: var(--bg); color: var(--fg); margin: 0; padding: 2rem; }
header h1 { margin: 0 0 .25rem; color: var(--accent); }
.meta { color: var(--muted); font-size: .9em; }
.card { background: var(--card); border: 1px solid var(--grid); border-radius: 6px;
        padding: 1.25rem 1.5rem; margin: 1rem 0; }
.summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: .5rem 1.5rem; margin: 0; }
.summary dt { font-size: .85em; color: var(--muted); }
.summary dd { margin: 0; font-size: 1.4em; font-weight: 600; }
.toc { columns: 2; }
.toc li { break-inside: avoid; }
.dim { color: var(--muted); }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .92em; }
table th, table td { border: 1px solid var(--grid); padding: .35rem .55rem;
                     text-align: left; vertical-align: top; }
table th { background: #f3f1e8; font-weight: 600; }
table.kv th { width: 12rem; }
tr.dead td { background: #fdecea; color: #6b1a14; }
.tag { background: var(--accent); color: #fff; padding: 0 .35rem;
       border-radius: 3px; font-size: .75em; margin-left: .25rem; }
.warn { color: #6b1a14; }
.ai-desc { background: #fdf6e3; border-left: 4px solid #b58900;
           padding: .75rem 1rem; margin: 1rem 0; }
code { font-family: Consolas, Menlo, monospace; }
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
