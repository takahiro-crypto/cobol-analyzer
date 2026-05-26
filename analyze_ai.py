#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 版エントリポイント。

cobol_analyzer で静的解析を行い、ai_spec で各 Program/Paragraph に自然言語
仕様を付与してから HTML / JSON を出力する。

使い方:
    export ANTHROPIC_API_KEY=sk-...
    export COBOL_AI_MODEL=claude-sonnet-4-6
    python analyze_ai.py sample_cobol -o report.html

オフラインで雛形だけ確認したい場合は `--dry-run` を使う（API 呼び出しを
スキップし、キャッシュにヒットしないものは空文字のままになる）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from cobol_analyzer import CobolProject, render_html
from ai_spec import AISpecConfig, AISpecGenerator, enrich_project


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="COBOL 静的解析 + AI 仕様書生成 + HTML レポート"
    )
    p.add_argument("source", help="解析対象のディレクトリまたはファイル")
    p.add_argument("-o", "--output", default="analysis_report_ai.html",
                   help="HTML レポートの出力パス")
    p.add_argument("-j", "--json", default=None,
                   help="JSON 解析結果の出力パス（省略時は出力しない）")
    p.add_argument("--encoding", default=None,
                   help="強制エンコーディング（utf-8 / cp932 など）")
    p.add_argument("--no-paragraph", action="store_true",
                   help="段落ごとの AI 説明をスキップ（プログラム単位のみ）")
    p.add_argument("--dry-run", action="store_true",
                   help="API 呼び出しを行わない（キャッシュ済みの分のみ反映）")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="進捗を stderr に出力")
    return p


def run(args: argparse.Namespace) -> int:
    root = Path(args.source)
    if not root.exists():
        print(f"パスが見つかりません: {root}", file=sys.stderr)
        return 2

    project = CobolProject(root, forced_encoding=args.encoding)
    project.analyze()

    config = AISpecConfig.from_env()
    if args.dry_run:
        config.enabled = False
    generator = AISpecGenerator(config)
    enrich_project(
        project,
        generator,
        paragraphs=not args.no_paragraph,
        verbose=args.verbose,
    )

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
