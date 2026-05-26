# ROADMAP

## Phase 1 — 動くプロト

優先度高い順:

- [x] `cobol_analyzer.py`: 静的解析コア（段落、CALL、COPY、CRUD、メトリクス、デッドコード）
- [x] HTML レポート生成
- [x] JSON 出力（解析結果の機械可読フォーマット）
- [x] `sample_cobol/` 口座利息計算バッチ
- [x] `ai_spec.py`: Claude API 疎通 + 段落・プログラム単位の自然言語仕様生成
- [x] `.ai_cache/` ディスクキャッシュ
- [x] `analyze_ai.py`: AI 統合エントリポイント
- [x] 実 API キーで疎通確認

## Phase 2 — 実案件投入準備

- [ ] COPY 句の実体展開
- [ ] CALL 呼出グラフを SVG / Mermaid で可視化
- [ ] EXEC SQL 句のテーブル/カラム抽出強化（埋め込み SQL の完全パース）
- [ ] EBCDIC 含むメインフレーム方言対応
- [ ] パイプライン化（大規模ソースを並列処理）
- [ ] 自動テスト（pytest）

## Phase 3 — サービス化

- [ ] Web UI（アップロード → レポート閲覧）
- [ ] 解析結果の DB 保存と差分比較
- [ ] プロジェクト別ナレッジ蓄積（過去の仕様書を学習データに）
- [ ] 認証 / 利用ログ
