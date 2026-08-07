# 設定 (config.toml / ~/.config/expecto) 設計ドキュメント

- ステータス: 実装済み
- 親ドキュメント: `DESIGN.md` (§7)
- 本ドキュメントは `~/.config/expecto/` 配下のファイルと設定の読み込み・優先順位の詳細設計。

---

## 1. 方針

- **`.env` はシークレット、`config.toml` は挙動設定**という役割分離。expecto が環境変数から読むのは `OPENAI_API_KEY` ただ 1 つで、それ以外の設定はすべて `config.toml`(と CLI フラグ)で行う。
- OpenAI API / SDK 側にデフォルトが存在するもの(`base_url`、`reasoning_effort`)は、未指定なら**パラメータ自体を渡さず SDK / API のデフォルトに委ねる**。`model` は API 側にデフォルトが無いため expecto が既定値(`gpt-5.4`)を持つ。
- expecto がこのディレクトリに書き込むことはない(読み取り専用。ファイルの自動生成もしない)。

## 2. ディレクトリ構成

```
~/.config/expecto/            # $XDG_CONFIG_HOME/expecto があればそちら
├── .env                      # OPENAI_API_KEY のみ (シークレット)
├── config.toml               # 挙動設定 (本ドキュメント)
└── dangerous_patterns.txt    # 危険コマンド正規表現の追加 (DESIGN.md §5.8)
```

- パス解決は `config.config_dir()` に一元化: `$XDG_CONFIG_HOME/expecto` → 未設定なら `~/.config/expecto`。
- `.env` の探索順(実装済み・変更なし): カレントディレクトリから親方向 → 見つからなければ `<config_dir>/.env`。設定済み環境変数はファイル値より常に優先。
- `OPENAI_API_KEY` が未設定(環境変数にも `.env` にも無い)の場合は起動時にエラーメッセージを出して終了する。

## 3. config.toml スキーマ

すべて任意。書かれたキーだけが既定値を上書きする。文字列キーの空文字 `""` は「未指定」と同義。パス系(`log_file` / `raw_io_log_file` / `dangerous_patterns_file`)は `~` を展開する。

```toml
shell = "/usr/bin/fish"           # 既定: $SHELL (無ければ /bin/bash)
hotkey = "ctrl-/"                 # "ctrl-" + 1文字 (§5)
confirm_level = "destructive"     # destructive | mutating | none
history_lines = 500
scrollback_lines = 5000
settle_ms = 500
threshold_ms = 30000
max_steps = 15
log_file = ""                     # JSONL ログ。既定: 無効
raw_io_log_file = ""              # デバッグ用 raw I/O ログ。既定: 無効
dangerous_patterns_file = ""      # 既定: <config_dir>/dangerous_patterns.txt

model = "gpt-5.4"                 # expecto 側の既定
base_url = ""                     # 未指定なら SDK 既定 (api.openai.com)
reasoning_effort = ""             # none|minimal|low|medium|high|xhigh|max
                                  # 未指定ならリクエストに reasoning を含めない

compact_thresh = 100000           # 会話履歴の compaction 閾値 (prompt_tokens)
new_output_max_chars = 20000      # query 間の新規出力差分の最大文字数
```

- テーブル(セクション)は使わずフラットなキーのみ。
- **API キーは受け付けない**: `api_key` が書かれていた場合は「環境変数 `OPENAI_API_KEY` を使え」と警告して無視する。ローカル LLM 等はキーを検証しないため base_url の変更だけで足りる。

## 4. 優先順位

```
組み込み既定値 < config.toml < CLI フラグ
```

- 実装は `ExpectoConfig.load(config_file) -> (config, warnings)`: 既定値で `ExpectoConfig` を作り、TOML を適用し、`OPENAI_API_KEY` を環境から解決する。CLI フラグの上書きは従来どおり `cli.main()` が行う。
- `--config PATH` で設定ファイルの場所を変更できる(既定: `<config_dir>/config.toml`)。

## 5. hotkey の表記

- `"ctrl-" + 1文字`(大文字小文字・前後空白は無視): `ctrl-a`〜`ctrl-z` と `ctrl-/`(0x1F、既定)、`ctrl-]`、`ctrl-\`、`ctrl-^`、`ctrl-@`、`ctrl-_`。
- `ctrl-c`(実行中断)/ `ctrl-m`(Enter)/ Esc と衝突するものは拒否し、警告して既定値に戻す。
- パースは `config.parse_hotkey()`(不正時 ValueError)。

## 6. エラーハンドリング (warn-and-continue)

`dangerous_patterns.txt` の既存方針(不正な行は警告してスキップ、起動は続行)に合わせる。警告は起動時に stderr へ `expecto: warning: <path>: <key>: ...` の形式で出す。

| 状況 | 挙動 |
|------|------|
| 既定パスの config.toml が存在しない | 正常(全て既定値) |
| `--config` で明示指定したファイルが存在しない | **エラー終了**(指定ミスに気づけるように) |
| TOML 構文エラー | ファイル全体を無視して警告 |
| 未知のキー | 警告(typo 検出)、無視 |
| 型・値の不正(負の整数、enum 外の値等) | 該当キーのみ警告して既定値を維持(`confirm_level` の既定は最も安全側の `destructive` なので、フォールバックは安全方向に働く) |
| `api_key` キー | 警告して無視(§3) |
| `OPENAI_API_KEY` 未設定 | **エラー終了**(設定先を案内) |

## 7. dangerous_patterns.txt (変更なし)

フォーマット(1 行 1 正規表現・`#` コメント・大文字小文字無視)、「組み込みパターンへの追加、destructive への昇格専用」というセマンティクスは DESIGN.md §5.8 のまま。本設計での変更は既定パスの解決が `config_dir()` 経由(XDG 対応)になったことと、パスを config.toml の `dangerous_patterns_file` でも指定できるようになったことのみ。

## 8. 設計判断の記録

1. **環境変数層の廃止**: 旧実装は `OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_REASONING_EFFORT` / `SUMMARY_OPENAI_*` を環境変数から読んでいた。設定の置き場所が env / CLI に分散して見通しが悪いため、シークレット(`OPENAI_API_KEY`)以外を config.toml に一本化した。優先順位も 3 段(既定 < ファイル < CLI)に単純化。旧環境変数への移行警告は設けない。
2. **blacklist を config.toml に統合しない**: 正規表現を TOML のエスケープ越しに書くのはミスを誘うため、`dangerous_patterns.txt` の 1 行 1 正規表現を維持。機構を二重に持たない。
3. **warn-and-continue**: expecto はターミナルラッパーであり、設定の typo で起動不能になるより、警告して安全側の既定値で動く方がよい。フォールバック先(既定値)が常に安全側であることが前提。
4. **tomllib (stdlib)**: Python 3.11+ が前提のため依存追加なしで TOML を読める。書き込みはしないので tomli-w 等も不要。
