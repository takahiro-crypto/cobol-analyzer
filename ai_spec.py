#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 仕様書生成モジュール（Claude API + ディスクキャッシュ）。

cobol_analyzer の解析結果を受け取り、プログラム/段落単位の自然言語仕様を
生成する。Claude API への呼び出し結果は `.ai_cache/` に SHA256 キーで保存し、
同じ入力では再課金しない。

設計方針:
    - cobol_analyzer.py は本モジュールに依存しない（コアは標準ライブラリのみ）
    - 本モジュールは anthropic SDK が無くてもインポート可能（呼び出し時にエラー）
    - 環境変数:
        ANTHROPIC_API_KEY  : Claude API キー
        COBOL_AI_MODEL     : 使用モデル（デフォルト claude-sonnet-4-6）
        COBOL_AI_CACHE_DIR : キャッシュディレクトリ（デフォルト .ai_cache）
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cobol_analyzer import Paragraph, Program

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_CACHE_DIR = ".ai_cache"
DEFAULT_MAX_TOKENS = 800

_PROGRAM_PROMPT = """\
あなたは COBOL レガシーシステムの保守ベテランです。以下のプログラムの
静的解析結果をもとに、5〜8 行の日本語で「このプログラムが何をしている
バッチか」を業務担当者にも分かるように説明してください。
推測する場合は『〜と推測される』と明記してください。

プログラム名: {program_id}
ソースファイル: {source_path}
行数: {total_lines} 行 / 段落数: {paragraph_count} / 複雑度合計: {complexity}

主な段落:
{paragraph_summary}

入出力ファイル:
{file_summary}

呼び出し（CALL）:
{call_summary}

CRUD:
{crud_summary}
"""

_PARAGRAPH_PROMPT = """\
以下は COBOL の 1 段落です。この段落が業務上何をしているかを 2〜4 行の
日本語で説明してください。推測は『〜と推測される』と明記してください。

段落名: {name} (セクション: {section})
呼出元: {referenced_by}
文数: {statements} / 複雑度: {complexity}

ソース抜粋:
{source}
"""


@dataclass
class AISpecConfig:
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL
    cache_dir: Path = Path(DEFAULT_CACHE_DIR)
    max_tokens: int = DEFAULT_MAX_TOKENS
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "AISpecConfig":
        return cls(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            model=os.environ.get("COBOL_AI_MODEL", DEFAULT_MODEL),
            cache_dir=Path(os.environ.get("COBOL_AI_CACHE_DIR", DEFAULT_CACHE_DIR)),
        )


class AISpecGenerator:
    """Claude API で仕様文を生成し、ディスクにキャッシュする。"""

    def __init__(self, config: Optional[AISpecConfig] = None):
        self.config = config or AISpecConfig.from_env()
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = None  # lazy

    # public ---------------------------------------------------------------

    def describe_program(self, program: Program) -> str:
        prompt = _PROGRAM_PROMPT.format(
            program_id=program.program_id,
            source_path=program.source_path,
            total_lines=program.total_lines,
            paragraph_count=len(program.paragraphs),
            complexity=program.complexity,
            paragraph_summary=self._summarize_paragraphs(program.paragraphs),
            file_summary=self._summarize_files(program),
            call_summary=self._summarize_calls(program),
            crud_summary=self._summarize_crud(program),
        )
        return self._complete(prompt, kind="program", key=program.program_id)

    def describe_paragraph(self, program: Program, paragraph: Paragraph) -> str:
        source = paragraph.raw_source or "(ソース抜粋なし)"
        prompt = _PARAGRAPH_PROMPT.format(
            name=paragraph.name,
            section=paragraph.section or "なし",
            referenced_by=", ".join(paragraph.referenced_by) or "なし",
            statements=paragraph.statements,
            complexity=paragraph.complexity,
            source=textwrap.indent(source.strip(), "    "),
        )
        return self._complete(
            prompt,
            kind="paragraph",
            key=f"{program.program_id}::{paragraph.name}",
        )

    # internal -------------------------------------------------------------

    def _complete(self, prompt: str, *, kind: str, key: str) -> str:
        cache_key = hashlib.sha256(
            (self.config.model + "\n" + prompt).encode("utf-8")
        ).hexdigest()
        cache_path = self.config.cache_dir / f"{kind}-{cache_key}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return cached.get("text", "").strip()
            except (OSError, json.JSONDecodeError):
                pass  # fall through and re-fetch

        if not self.config.enabled:
            return ""

        text = self._call_api(prompt)
        cache_path.write_text(
            json.dumps(
                {"model": self.config.model, "key": key, "text": text},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return text.strip()

    def _call_api(self, prompt: str) -> str:
        if not self.config.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY が設定されていません。AI 機能を使うには "
                "環境変数で API キーを指定してください。"
            )
        client = self._get_client()
        message = client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # SDK の content は list[ContentBlock]。テキストブロックを結合
        chunks = []
        for block in message.content:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        return "\n".join(chunks).strip()

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore
        except ImportError as err:  # pragma: no cover - 環境次第
            raise RuntimeError(
                "anthropic SDK が見つかりません。`pip install anthropic` を実行してください。"
            ) from err
        self._client = anthropic.Anthropic(api_key=self.config.api_key)
        return self._client

    # prompt helpers --------------------------------------------------------

    @staticmethod
    def _summarize_paragraphs(paragraphs) -> str:
        if not paragraphs:
            return "(なし)"
        lines = []
        for p in paragraphs[:15]:
            lines.append(
                f"- {p.name} (文数 {p.statements} / 複雑度 {p.complexity} / "
                f"呼出元 {len(p.referenced_by)})"
            )
        if len(paragraphs) > 15:
            lines.append(f"...他 {len(paragraphs) - 15} 段落")
        return "\n".join(lines)

    @staticmethod
    def _summarize_files(program: Program) -> str:
        if not program.files:
            return "(なし)"
        return "\n".join(
            f"- {f.fd_name} ASSIGN={f.assign_to or '-'} "
            f"ORG={f.organization or '-'}"
            for f in program.files
        )

    @staticmethod
    def _summarize_calls(program: Program) -> str:
        if not program.calls:
            return "(なし)"
        items = []
        for c in program.calls:
            kind = "動的" if c.dynamic else "静的"
            items.append(f"- {c.callee} ({kind}, line {c.line})")
        return "\n".join(items)

    @staticmethod
    def _summarize_crud(program: Program) -> str:
        if not program.crud:
            return "(なし)"
        items = []
        for c in program.crud:
            items.append(
                f"- {c.target_kind} {c.target} : {c.operation} "
                f"(段落 {c.paragraph or '-'}, line {c.line})"
            )
        return "\n".join(items)


def enrich_project(project, generator: Optional[AISpecGenerator] = None,
                   *, paragraphs: bool = True, verbose: bool = False) -> None:
    """CobolProject の各 Program / Paragraph に AI 説明を埋め込む。"""
    gen = generator or AISpecGenerator()
    for program in project.programs:
        if verbose:
            print(f"[ai] program: {program.program_id}", file=sys.stderr)
        try:
            program.description = gen.describe_program(program)
        except Exception as err:  # noqa: BLE001
            program.parse_warnings.append(f"AI 説明生成に失敗: {err}")
            if verbose:
                print(f"[ai]   ERROR: {err}", file=sys.stderr)
            continue
        if not paragraphs:
            continue
        for para in program.paragraphs:
            if verbose:
                print(f"[ai]   paragraph: {para.name}", file=sys.stderr)
            try:
                para.description = gen.describe_paragraph(program, para)
            except Exception as err:  # noqa: BLE001
                program.parse_warnings.append(
                    f"AI 説明生成に失敗 ({para.name}): {err}"
                )
                if verbose:
                    print(f"[ai]     ERROR: {err}", file=sys.stderr)
