# CLAUDE.md — COBOL Analyzer

## プロジェクト概要

- **名称**: COBOL Analyzer
- **目的**: レガシー COBOL ソースの静的解析を行い、仕様書化・コード可視化のための HTML レポートを自動生成する
- **位置づけ**: 動くプロト + 将来的なサービス化を見据えた基盤

## 抽出対象（4種類）

1. プログラム/段落ごとの仕様・処理ロジック説明
2. CALL / COPY の呼び出し関係（呼出グラフ）
3. データ/ファイル/DB 利用状況（CRUD マトリクス）
4. 規模・複雑度メトリクス＋デッドコード検出

## ファイル構成

```
cobol-analyzer/
├── CLAUDE.md              # このファイル
├── ROADMAP.md             # フェーズ別タスク
├── README.md              # 開発者向け Getting Started
├── cobol_analyzer.py      # 静的解析エンジン + HTML 生成（標準ライブラリのみ）
├── ai_spec.py             # AI 仕様書生成（Claude API + ディスクキャッシュ）
├── analyze_ai.py          # AI 版エントリポイント
└── sample_cobol/          # テストフィクスチャ（口座利息計算バッチ）
```

## 実行コマンド

```bash
# 静的解析のみ
python cobol_analyzer.py sample_cobol -o analysis_report.html -j analysis.json

# AI 仕様書生成あり
export ANTHROPIC_API_KEY=sk-...
export COBOL_AI_MODEL=claude-sonnet-4-6
python analyze_ai.py sample_cobol -o analysis_report_ai.html
```

対応拡張子: `.cob` `.cbl` `.cobol` `.pco` `.cpy` `.cpb`

## 設計原則

1. **コアは標準ライブラリのみ**: `cobol_analyzer.py` に外部依存を持ち込まない（素の Python だけで動く）
2. **AI レイヤーは差し替え可能に分離**: `describe_program` / `describe_paragraph` を上書きする形で結合
3. **出力は日本語で統一**: HTML レポートも JSON 内のラベルも日本語
4. **文字コード**: UTF-8 / CP932 両対応（古いソースは CP932 が多い）
5. **固定形式 COBOL 前提**: 1〜6 桁＝シーケンス領域、7 桁目＝表示領域、8〜72 桁＝プログラム領域

## パーサ実装上の重要ポイント

- 段落名検出は左端 cstrip 後に正規表現を適用
- `END-READ` `GOBACK` 等の予約語は `_is_statement_word` で段落名から除外
- CRUD は `OPEN` モード（INPUT/OUTPUT/I-O/EXTEND）+ READ/WRITE/REWRITE/DELETE と `EXEC SQL` から判定
- 動的 CALL（`CALL WS-PROGRAM` 等）は `dynamic_calls` に記録し警告
- デッドコード検出は「PERFORM/GO TO されていない段落」（誤検出前提 — レビュー必須）

## AI レイヤー（ai_spec.py）

- `describe_program(program_meta) -> str`
- `describe_paragraph(paragraph_meta) -> str`
- キャッシュは `.ai_cache/` 配下に SHA256 ハッシュキーで保存
- モデルは `COBOL_AI_MODEL` 環境変数で指定（デフォルト `claude-sonnet-4-6`）

## 既知未対応項目

- [ ] COPY 実体展開（COPY 句の中身を読みに行く）
- [ ] メインフレーム方言（COBOL/370, OpenCOBOL, ACUCOBOL）の差異検証
- [ ] 大規模ソース（10万行クラス）での性能測定
- [ ] 自動テスト（現状はサンプル目視確認）

## 開発ルール

1. 変更は小分けにし、毎回 `sample_cobol` で実行確認
2. `cobol_analyzer.py` の公開関数シグネチャを変更したら `analyze_ai.py` への影響を確認
3. AI 呼び出し結果は必ずキャッシュ（`.ai_cache/`）。同じ入力で課金しない
4. PR は人間レビュー必須（特に AI 出力の品質確認）

## 関連ドキュメント

- [ROADMAP.md](./ROADMAP.md) — フェーズ別タスク
- [README.md](./README.md) — 環境構築
