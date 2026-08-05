
# jo-u (`jou`) 設計ドキュメント

- ステータス: Draft v0.1 (プロトタイプ実装用)
- 対象実装言語: Python 3.11+ (プロトタイプ)
- パッケージマネージャ: uv
- 対象OS: macOS, Linux

---

## 1. 目的 / コンセプト

`jou` は、ターミナル(tty)とユーザーの入出力の間に透過的に挟まる CLI ツールである。

- 普段は通常のシェル(またはユーザーが起動した任意のプログラム)への入出力をそのまま中継する「透明なラッパー」として振る舞う。
- ユーザーが特定のキーバインド(ホットキー)を押すと **query mode** に入り、自然文でのクエリを受け付ける。
- クエリ確定(Enter)時に、直近のターミナル入出力の履歴とクエリを LLM に渡し、LLM はそれを達成するためのコマンドを **tty へ入力する形で** 実行する。
- 重要な特性: `jou` は個々のシェルではなく **tty そのもの** をラップするため、ユーザーが `ssh` で別ホストに入っても、あるいはネットワーク機器(ルーター/スイッチ)のベンダー独自 CLI に入っても、そのまま LLM による支援・代理操作が届く。シェルの `PS1` や readline に依存しない。

### 1.1 非目的

- Windows対応
- GUIやTUIダッシュボード
- 複数ペイン/マルチプレクサ的機能 (tmux的機能の再実装)
- 高度な認証・シークレット管理 (パスワード入力のGUIダイアログ化など) — v0.1ではユーザーがそのまま素通りで入力する想定
- 厳密な監査ログ・コンプライアンス機能

---

## 2. 用語

| 用語                       | 意味                                                                                               |
|----------------------------|----------------------------------------------------------------------------------------------------|
| outer tty                  | `jou` を起動した実際のターミナル(ユーザーが目視している画面)                                       |
| pty (master/slave)         | `jou` が内部で確保する擬似端末。slave 側に子プロセス(shell/ssh等)をアタッチする                    |
| child process              | pty の slave にアタッチされるプロセス。デフォルトは `$SHELL`、あるいは起動時に指定した任意コマンド |
| passthrough mode           | 通常状態。outer tty ⇄ pty master のバイトを素通しする                                              |
| query mode                 | ホットキーで入る、クエリ入力用のオーバーレイモード                                                 |
| completion mode            | LLM がコマンド候補を1つ生成し、Enterを押さずに tty のバッファに置いて返すモード                    |
| auto mode                  | LLM がゴールを達成するまで、コマンド実行→出力確認→次のコマンド、を繰り返す agentic モード          |
| screen buffer / scrollback | LLM に渡すために保持している「直近の入出力の履歴」(生バイト列とクリーン化テキストの両方)           |

---

## 3. ハイレベルな動作フロー

```
起動: jou [-- command...]
  └─ 指定がなければ $SHELL を起動

[passthrough mode]
  ユーザーの入力 → そのまま child process へ
  child process の出力 → そのまま outer tty へ
  (この間も履歴バッファには常時記録している)

  ユーザーがホットキーを押す
  ※ ホットキーは2つあり、どちらを押したかで実行モードが決まる:
    - completion hotkey (例: Ctrl-/) → completion mode
    - auto hotkey (例: Ctrl-]) → auto mode
        │
        ▼
[query mode]
  - outer tty に最小限のオーバーレイ("miniprompt")を描画
  - どのモードで起動されたかを表示 (例: `jou(comp)> ` / `jou(auto)> `)
  - ユーザーの入力は child process には送らず、query バッファに蓄積
  - Enter で確定 / Esc で passthrough に戻る(キャンセル)
        │ Enter
        ▼
  LLM へ「直近の入出力履歴 + query」を送信
        │
        ├─ completion mode (completion hotkey で起動) ───┐
        │   LLMが1コマンドを生成                          │
        │   → pty master にコマンド文字列だけ書き込み       │
        │     (Enterは送らない。ユーザーが確認して実行)     │
        │   → passthrough mode に戻る                     │
        │                                               │
        └─ auto mode (auto hotkey で起動) ───────────────┘
            ループ:
              1. LLMがコマンドまたは制御文字(Ctrl-C等)を生成
              2. pty master に コマンド + Enter (または制御文字) を書き込み
              3. 出力が安定(settle)するまで待つ
                 settle せず閾値時間を超えた場合は、その時点の出力を
                 LLMに送り、LLMが判断する(Ctrl-Cで中断するか待つか等)
              4. 新規出力を履歴に追加してLLMに返す
              5. LLMが「完了/失敗/継続」を判断
            ゴール達成 or 上限回数 or ユーザーのCtrl-C割込みで終了
            → passthrough mode に戻る
```

---

## 4. アーキテクチャ

```
┌───────────────────────────────────────────────┐
│ outer tty (ユーザーの実ターミナル)                │
└───────────────────────────────────────────────┘
        ▲ stdout                 │ stdin (raw mode)
        │                        ▼
┌───────────────────────────────────────────────┐
│                    jou (親プロセス)               │
│                                                 │
│  TTYController      : outer ttyのraw化/復元        │
│  InputRouter         : 1バイトずつ見て              │
│                        hotkey検出 / モード分岐       │
│  IORelay(select loop): outer <-> pty master 中継   │
│  ScreenBuffer        : pty出力の履歴保持(pyte併用)  │
│  QueryModeUI         : ミニプロンプトの描画/入力回収  │
│  LLMClient           : OpenAI API呼び出し           │
│  ModeExecutor        : completion / auto の実行制御 │
│  SettleDetector      : 出力安定検知(auto mode用)    │
└───────────────────────────────────────────────┘
        │ pty master fd
        ▼
   pty slave ── child process ($SHELL, ssh, など)
```

---

## 5. コンポーネント設計

### 5.1 TTYController

- 責務: `jou` 起動時に outer tty の `termios` 設定を保存し、raw モードにする。終了時(正常終了・例外・シグナル)に必ず元の設定へ復元する。
- Python標準: `termios`, `tty` モジュール。`tty.setraw(fd)` を使うが、復元用に `termios.tcgetattr()` の結果を保存しておくこと。
- `atexit` とシグナルハンドラ(`SIGTERM`, `SIGINT` を自プロセスが受けた場合)の両方で復元処理を確実に呼ぶ。
- `SIGWINCH` を受け取ったら outer tty の現在サイズ (`fcntl.ioctl(fd, termios.TIOCGWINSZ, ...)`) を取得し、pty 側にも `TIOCSWINSZ` で反映する。

### 5.2 PTYManager

- 責務: pty master/slave の確保、child process の fork/exec、終了検知。
- Python標準の `pty` モジュール(`pty.fork()`)を第一候補とする。より柔軟な制御が必要なら `ptyprocess` ライブラリの利用も検討(プロセス終了検知やリサイズAPIが扱いやすい)。
- child process のデフォルトは `os.environ.get("SHELL", "/bin/bash")`。`jou -- ssh myrouter` のように任意コマンドの指定も可能にする。
- child の終了(exit)を検知したら `jou` 自体も終了する。

### 5.3 IORelay (メインループ)

- 責務: `select.select()`(または `selectors` モジュール)で outer tty の stdin fd と pty master fd を監視し、双方向にバイトを中継する。
- ループの概形:
  ```
  while running:
      readable = select([outer_stdin_fd, pty_master_fd], [], [])
      if outer_stdin_fd in readable:
          data = os.read(outer_stdin_fd, 4096)
          InputRouter.handle(data)   # hotkey検出 & 分岐
      if pty_master_fd in readable:
          data = os.read(pty_master_fd, 4096)
          ScreenBuffer.feed(data)
          os.write(outer_stdout_fd, data)   # passthrough中のみ描画
  ```
- query mode 中は pty master への書き込みを止める(ユーザー入力を child に渡さない)が、pty master からの出力読み取り自体は継続してバッファに溜め続けても良い(表示は保留)。

### 5.4 InputRouter / ホットキー検出

- 1バイトずつ状態機械として処理する。UTF-8マルチバイト文字の分割に注意(バッファリングして完全な1文字になるまで待つ処理が必要)。
- ホットキーは2つ設ける。どちらを押したかで、query mode 確定後の実行モード(completion / auto)が決まる:
  - completion hotkey: `Ctrl-/` (0x1f) を第一候補とする。
  - auto hotkey: `Ctrl-]` (0x1d) を第一候補とする。
- いずれも設定可能にし、衝突が判明したら容易に変更できるようにする。
- query mode 中の入力ハンドリング:
  - 通常文字 → クエリバッファに追記 + ミニプロンプトに反映
  - Backspace → 1文字削除
  - Enter → 確定してLLM呼び出しへ
  - Esc または Ctrl-C → キャンセルして passthrough に戻る

### 5.5 ScreenBuffer (履歴管理)

- 二重に履歴を保持する:
  1. **raw ログ**: pty からの生バイト列(直近 N KB、リングバッファ)。デバッグ用。
  2. **クリーンテキスト**: [`pyte`](https://pypi.org/project/pyte/) の `pyte.Screen` + `pyte.Stream` を使い、ANSIエスケープシーケンスを解釈した「現在の画面内容」および直近のスクロールバック行を保持する。LLMに渡すのは主にこちら。
- LLMへの入力としては、直近の「表示されているテキスト」を中心に、必要であれば直前に実行したコマンド文字列も別途保持して付与する(コマンド入力自体はScreenBuffer上にも現れるが、明示的にログしておくとプロンプト構築が楽)。
- トークン量を抑えるため、既定では直近 Nスクリーン分 or 直近 M 行 のみを送る設計とし、`--history-lines` などで調整可能にする。

### 5.6 QueryModeUI

- responsibiliy: outer tty 上に、child process の画面を破壊せずにミニプロンプト(例: 画面最下行に `jou> ` の入力行を描画)を表示する。
- 実装簡易化のため v0.1 では「現在のカーソル位置を保存 → 最下行に移動してオーバーレイを描画 → 確定/キャンセル後にカーソル位置復元」という、単純なANSIエスケープ操作(`\x1b[s` / `\x1b[u` や `\x1b[<n>;<m>H` )で十分。
- alternate screen (vimなど) 使用中に query mode に入るケースは v0.1 では未対応(既知の課題として明記。詳細は §10）。

### 5.7 LLMClient

- OpenAI **Responses API** (`client.responses.parse()`) を利用する。モデルは設定可能(既定は現行世代の高速モデル)。
- 入力: `instructions=` でシステムプロンプト、`input=` でユーザーメッセージ、`text_format=` で Pydantic モデルを指定し Structured Outputs を強制する。
- システムプロンプトは英語で記述し、「コマンド以外の出力はユーザーのクエリの言語に合わせる」旨を指示する。
- completion mode 用スキーマ (`CompletionResponse`):
  ```json
  {
    "command": "df -h",
    "explanation": "Check disk usage"
  }
  ```
- auto mode 用スキーマ (`AutoResponse`):
  ```json
  {
    "action": {"type": "command", "value": "df -h"},
    "status": "continue",
    "explanation": "Checking disk usage"
  }
  ```
  - `status: "continue"` + `action` → コマンドまたは制御文字を実行。
  - `status: "done"` / `"fail"` → `action` なし、auto mode ループを終了。
  - `action.type: "control"` → 制御文字(例: `"ctrl-c"`)を pty master に書き込み。

#### Auto mode の会話チェーン (`previous_response_id`)

Responses API は `previous_response_id` パラメータで会話の継続をサポートする。サーバー側に過去のやり取りが保持されるため、クライアントは新しいメッセージだけを `input=` に渡せばよい。

- `store=True` で各レスポンスをサーバー側に保存する。
- 次のリクエストで `previous_response_id=response.id` を渡すと、API 側が過去の会話コンテキストを自動的に前置きする。
- `instructions=` は毎回送る必要がある（`previous_response_id` では引き継がれない）。
- 課金上の差異はない（過去の入力トークンは毎回カウントされる）。コード側の簡素化とトークン転送量の削減が利点。

Auto mode の各ステップにおけるメッセージ構築:

```
Step 1 (初回):
  input = [{ role: "user", content: "## Goal\n{query}\n\n## Current Terminal Screen\n```\n{screen}\n```" }]
  previous_response_id = なし
  store = true
  → response_id を記録

Step N (フォローアップ):
  input = [{ role: "user", content: "## Command Output\n```\n{new_screen}\n```" }]
  previous_response_id = 前回の response_id
  store = true
  → response_id を更新
```

これにより、毎ステップで全画面履歴と過去のメッセージ列を再送する必要がなくなる。

### 5.8 ModeExecutor

- どのモードで実行するかは、ユーザーがどちらのホットキーで query mode に入ったかで決まる(§5.4参照)。クエリ内容によるモード判定は行わない。
- **completion mode**:
  - LLM応答の `command` を pty master に書き込む(Enterは送らない)。
  - passthrough mode に戻り、ユーザーが自分でEnterを押すか編集するのを待つ。
- **auto mode**:
  - ループ処理は下記 SettleDetector を利用し、既定で最大ステップ数(例: 10)を設け、無限ループを防止する。
  - LLMは `action: "control"` で制御文字（Ctrl-C等）を送信できる。これにより、長時間実行中のコマンドの中断などをLLM自身が判断できる。
  - 各ステップ実行前に、危険そうなコマンド(`rm -rf`, `dd`, `mkfs` など簡易ブラックリスト)を検知したら、ユーザーに1回だけ確認を挟む安全策を入れる(v0.1でも最低限のガードとして実装する)。
  - ユーザーはいつでも `Ctrl-C` で auto mode を中断できるようにする(中断検知は InputRouter が auto mode 中の入力を優先的にハンドルする形にする)。

### 5.9 SettleDetector (出力安定検知)

- auto mode でコマンド実行後、「出力が落ち着いた」タイミングを検知するために、シンプルなアイドルタイマー方式を採用する:
  - コマンド送信後、pty master からの出力が最後に届いた時刻を記録。
  - 一定時間(既定 300ms 〜 800ms、設定可能)出力が来なければ「settle」と判断。
  - settle せずに閾値時間(既定 30秒、設定可能)を超えた場合: タイムアウトエラーとはせず、その時点までの出力を LLM に送る。LLMは出力内容を見て次のアクションを判断する（例: `action: "control"` で Ctrl-C を送信してコマンドを中断する、さらに待つ、別のアプローチに切り替える等）。30秒以上継続するコマンド（ダウンロード、ビルド等）は日常的にあるため、一律でタイムアウト扱いにはしない。
- シェル固有のプロンプトマーカー注入(PS1書き換え)には**依存しない**。これは ssh 越しやルーターCLIでは使えないため、汎用性を優先してアイドル検知のみで v0.1 を組む。将来的に「対応シェルではPS1マーカーも併用してより正確に検知する」拡張は可能(§11参照)。

### 5.10 WorkingSummary (セッションコンテキスト) — TODO

`jou` セッション内でユーザーがこれまで行ってきた操作の要約を LLM に渡すことで、文脈を踏まえたコマンド提案・自動実行を可能にする。

#### 概要

- `jou` 起動後の全入出力（ScreenBuffer が保持する内容）を元に、LLM にワンショットで「ユーザーが何をしているか」を要約させ、その結果を **working summary** として保持する。
- LLM 呼び出し時（completion mode / auto mode 共通）に、この working summary をシステムプロンプトに含めて送信する。

#### 生成・更新タイミング

1. **行数トリガー**: セッション開始（または前回の summary 生成）から一定行数（例: 100行、設定可能）の入出力が蓄積したら、バックグラウンドで LLM を呼び出して working summary を生成・更新する。
2. **LLM 呼び出し時の遅延生成**: completion/auto の LLM 呼び出し時に working summary がまだ存在しない場合は、まずこれまでの入出力を渡して working summary を生成してから、本来の LLM 呼び出しを行う。
3. **差分更新**: 更新時は「前回の working summary + 前回生成以降の入出力」を LLM に渡し、更新された summary を返させる。全履歴を毎回送る必要はない。

#### 非同期実行

- 行数トリガーによる生成・更新はユーザーのシェル操作をブロックしてはならない。バックグラウンド（別スレッド or asyncio）で LLM 呼び出しを行い、結果が返ったら working summary を差し替える。
- LLM 呼び出し時の遅延生成（上記 2.）は同期的に待つ必要がある（summary が無い状態で本来の呼び出しを行っても品質が下がるため）。

#### LLM への渡し方

- working summary はシステムプロンプトの一部として送信する。例:
  ```
  ## セッションコンテキスト
  ユーザーはこのターミナルセッションで以下の作業を行っています:
  {working_summary}
  ```
- completion mode / auto mode 両方で共通に利用する。

#### 検討事項

- working summary 生成用の LLM 呼び出しのモデル・コスト: メインの呼び出しと同じモデルを使うか、軽量モデルを使うか。
- 入出力のトークン量が大きい場合の扱い: summary 生成に渡す入出力の最大行数・トークン数の制限。
- セッションが長時間に及ぶ場合の summary の品質劣化: 差分更新を繰り返すと要約が薄くなる可能性。定期的な全体再生成の検討。
- **summary 生成プロンプトの設計**: 単なる操作履歴の要約だけでなく、「ユーザーが現在いる環境」の推測結果を含めることが重要。`jou` の目的上、SSH 先のルーターやスイッチのベンダー CLI を操作する可能性がある。ネットワーク装置の CLI コマンドはベンダーごとに異なる（Cisco IOS, Junos, Arista EOS, etc.）ため、「どの装置・どの環境にいるか」という推測をsummaryに含めることで、後続の LLM 呼び出しにおけるコマンド提案の精度が大きく向上する。

---

## 6. 状態機械 (State Machine)

ホットキーが2つあるため、query mode 進入時点で実行モードが確定する。状態機械は completion / auto で分岐する:

| 状態                | 説明                                 | 遷移条件                                                     |
|---------------------|--------------------------------------|--------------------------------------------------------------|
| `PASSTHROUGH`       | 通常中継                             | completion hotkey → `QUERY_COMPLETION` / auto hotkey → `QUERY_AUTO` |
| `QUERY_COMPLETION`  | クエリ入力受付中 (completion mode)   | Enter → `LLM_COMPLETION` / Esc,Ctrl-C → `PASSTHROUGH`       |
| `QUERY_AUTO`        | クエリ入力受付中 (auto mode)         | Enter → `LLM_AUTO` / Esc,Ctrl-C → `PASSTHROUGH`             |
| `LLM_COMPLETION`    | LLM応答待ち (completion)             | 応答受信 → `COMPLETION_INJECT`                               |
| `LLM_AUTO`          | LLM応答待ち (auto)                   | 応答受信 → `AUTO_LOOP`                                       |
| `COMPLETION_INJECT` | コマンド文字列をpty masterへ書き込み | 書き込み完了 → `PASSTHROUGH`                                 |
| `AUTO_LOOP`         | 自動実行ループ                       | ゴール達成/失敗/上限到達/Ctrl-C → `PASSTHROUGH`              |

※ 実装上は `QUERY_COMPLETION` と `QUERY_AUTO` のUI処理は共通化し、内部フラグで分岐すればよい。`LLM_COMPLETION` と `LLM_AUTO` も同様にLLMClient呼び出し自体は共通で、応答後のディスパッチのみ異なる。

---

## 7. CLI / 設定

```
jou [OPTIONS] [-- COMMAND ...]

OPTIONS:
  --shell PATH              子プロセスとして起動するコマンド (既定: $SHELL)
  --completion-hotkey KEY    completion mode に入るキー (既定: ctrl-/)
  --auto-hotkey KEY          auto mode に入るキー (既定: ctrl-])
  --model NAME               使用するLLMモデル (既定値あり)
  --max-auto-steps N         auto mode の最大ステップ数 (既定: 10)
  --settle-ms N              出力安定判定の待機時間 (既定: 500)
  --history-lines N          LLMに渡すスクリーン履歴の行数 (既定: 200)
  --log-file PATH            raw I/Oログの出力先 (デバッグ用、既定は無効)
```

設定ファイル: `~/.config/jou/config.toml` に既定値を書けるようにし、CLIオプションが上書きする。APIキーは環境変数 `OPENAI_API_KEY` を利用(設定ファイルに平置きしない)。

### TODO (将来追加予定のオプション)

- `--no-confirm` — 危険コマンドの確認をスキップ(非推奨、明示オプトインのみ)

### TODO (将来追加予定の改善)

- **危険コマンドの確認**: auto mode でコマンド実行前に、危険なコマンド（`rm -rf`, `dd`, `mkfs` 等）を簡易ブラックリストで検知し、ユーザーに確認を挟む（§5.8, §9 参照）。`--no-confirm` オプションも合わせて実装する。
- **シークレットのマスキング**: LLM に送信するスクリーン履歴から、パスワード・APIキー等の機密情報を簡易マスキングする（§9 参照）。`password`, `token`, `secret` 等を含む行の値部分を伏字にする。
- **Working Summary (セッションコンテキスト)**: LLM呼び出し時に、ユーザーが当該 `jou` セッション内でこれまで何をしてきたかの要約（working summary）をシステムプロンプトに含める。詳細設計は §5.10 を参照。
- **プロンプトエンジニアリング**: completion mode / auto mode のシステムプロンプト・ユーザーメッセージの内容を精査し、出力品質を改善する。現状は最低限の指示のみ。

---

## 8. LLMへ渡すコンテキストの構築

システムプロンプトの骨子(実装時にチューニング)。応答形式は Structured Outputs で強制するため、プロンプト内での JSON 形式指示は不要:

```
あなたはユーザーの操作しているターミナル(またはSSH先、あるいはネットワーク機器のCLI)
に対してコマンドを提案・実行するアシスタントです。
- 直近のターミナル画面の内容(以下に付与)を参照し、現在どのシステム/シェル/CLIに
  接続されているかを判断してください。
- completion mode: 1つのコマンドだけを提案してください。実行はユーザーが行います。
- auto mode: ゴールが達成されるまで、1ステップずつコマンドまたは制御文字を提案
  してください。前のコマンドの出力を評価し、次に何をすべきか判断してください。
  コマンドが長時間実行中の場合、Ctrl-Cで中断するか待つかを判断してください。
```

ユーザーメッセージには以下を含める:
- 直近のクリーンテキスト画面/スクロールバック(§5.5)
- ユーザーのクエリ文字列
- (auto modeのループ中は) これまでの全ステップの履歴をOpenAIのメッセージ列として保持し、マルチターン会話として渡す。具体的には、各ステップごとに「LLMの応答(assistant message)」と「コマンド実行結果の出力(user message)」のペアを蓄積し、LLM呼び出し時にはシステムプロンプト + 初回コンテキスト + これまでの全ステップ履歴 + 最新の出力、という形で渡す。これにより、LLMは過去に何を試して何が起きたかを踏まえて次のアクションを判断できる。

---

## 9. 安全性・注意点

- **明示的な確認**: v0.1では危険コマンドの簡易検出+確認プロンプトのみ。本格的なサンドボックスや権限制御は範囲外。
- **シークレットの扱い**: raw ログ・LLMへの送信内容にパスワードやAPIキーが混入しうる。v0.1では簡易マスキング(例: `password`, `token`, `secret` を含む行の値部分を伏字にする)を検討するが、完全性は保証しない旨をドキュメントに明記する。
- **auto modeの暴走対策**: 最大ステップ数、全体タイムアウト、ユーザーによる即時中断(Ctrl-C)を必須実装とする。
- **ホットキーの衝突**: 子プロセス側(vim, tmux, ルーターCLI等)が同じキーを使っている場合に奪い合いが発生する。設定で変更可能にしておくことが必須。

---

## 10. 既知の課題 / v0.1で扱わないもの (オープン課題)

1. **alternate screen 対応**: `vim`, `less`, `top` などが alternate screen buffer を使っている最中に query mode のオーバーレイを描画すると、復帰時に描画が乱れる可能性がある。pyteでalternate screen判定をして、query mode進入前に一時的に元の画面へ戻す/después復元する、といった対応が今後必要。
2. **パスワードプロンプトの扱い**: sshやsudoのパスワード入力中にhistoryバッファへ内容が残る可能性。マスキングロジックの精度向上が必要。
3. **完了検知の精度**: アイドル検知は「出力がただ遅いだけ」のケース(大きなダウンロード等)と「本当に終わった」ケースを区別できない。将来的にOSC 133等のシェル統合や、プロンプト正規表現の学習的検出を追加検討。
4. **マルチバイト入力**: 日本語入力(IME)がある場合、raw モードでの1バイトずつの処理とIME側のコンポジションの扱いに注意が必要(v0.1はASCIIコマンド入力を主眼とし、日本語クエリ自体は問題ないが、IME変換中のキーイベントの扱いは要検証)。
5. **ネストしたjou**: sshした先で更に`jou`を起動するようなケースは非対応(想定しない)。

---

## 11. 将来拡張の候補 (v0.1では実装しない)

- シェル統合(OSC133 / PS1マーカー)による完了検知の精度向上(自分のローカルシェルに限り)。
- pty-mcp / agent-tty のような「録画・スクリーンショットでの検証」機能。
- 複数セッションの並行管理。
- 危険コマンド判定のLLM化(ヒューリスティックでなく判断させる)。

---

## 12. プロトタイプの実装スコープ (v0.1 MVP)

実装は2段階で進める。ただし、Phase 1 の時点から auto mode を見据えた設計(状態機械、LLMClient のモード分岐、SettleDetector のインターフェース等)を組み込んでおくこと。

### Phase 1: completion mode

- passthrough mode (raw tty中継、SIGWINCH伝播)
- completion hotkey による query mode 突入・キャンセル
- completion mode (LLM 1コマンド生成 → tty注入、Enterは押さない)
- pyteによる画面のクリーンテキスト化とLLMへのコンテキスト付与
- `--shell` オプションによる任意コマンド起動(sshやルーターCLIへの接続を含む)

### Phase 2: auto mode

- auto hotkey による query mode 突入
- auto mode ループ (コマンド実行 → settle検知 → LLM判断 → 繰り返し)
- LLMからの制御文字送信 (Ctrl-C等)
- settle 閾値超過時のLLMへの中間報告
- 最大ステップ数制限、ユーザーによるCtrl-C中断
- 基本的な危険コマンド確認

### 含まない (両Phase共通):
- §10, §11 に記載の課題・拡張

## 13. 想定ディレクトリ構成

```
jou/
├── pyproject.toml
├── src/
│   └── jou/
│       ├── __init__.py
│       ├── cli.py            # エントリポイント、引数解析
│       ├── tty_controller.py # raw mode化/復元、SIGWINCH
│       ├── pty_manager.py    # pty確保、child起動
│       ├── io_relay.py       # メインループ
│       ├── input_router.py   # ホットキー検出、状態機械
│       ├── screen_buffer.py  # pyte連携、履歴管理
│       ├── query_ui.py       # ミニプロンプト描画
│       ├── llm_client.py     # OpenAI API呼び出し (Structured Outputs)
│       ├── mode_executor.py  # completion/auto実行
│       ├── settle.py         # 出力安定検知
│       └── config.py         # 設定読み込み
└── tests/
```

## 14. 依存ライブラリ (想定)

- `pyte` — 端末エミュレーション / クリーンテキスト化
- `openai` — LLM呼び出し (OpenAI公式Python SDK、Structured Outputs対応)
- `pytest` — テストフレームワーク (dev dependency)
- 標準ライブラリ: `pty`, `termios`, `tty`, `select`, `os`, `signal`, `fcntl`, `struct`

## 16. テスト・型チェック方針

- テストフレームワークには `pytest` を使用する。
- 型チェックには `ty` を使用する (`uv run ty check`)。設定は `pyproject.toml` の `[tool.ty]` に記述する。
- すべてのコードに型ヒントを付与する。公開インターフェースは明示的な型を書き、`Any` の使用は避ける。
- 各コンポーネント実装時に対応するテストを `tests/` 配下に作成する。既存コンポーネントの変更時もテストを適宜更新する。
- tty/pty 操作のテストには `pty.openpty()` で疑似端末ペアを作成して使用する。
- IORelay 等の `sys.stdin` に依存するコンポーネントは、fd を外部から注入可能にしてテスタビリティを確保する。

---

## 15. 参考にした既存ツール(先行事例調査より)

- [Butterfish Shell](https://github.com/bakks/butterfish) — シェルラッパー型。大文字始まりでLLMプロンプト、`!`でGoal Mode(自動実行)。PS1マーカーで完了検知。`jou`との違いは、シェルのみをラップし子プロセス(ssh等)には介入しない点。
- [pty-mcp](https://github.com/raychao-oao/pty-mcp) — ローカル/SSH/シリアル(ネットワーク機器)向けPTYセッションをAIエージェントに提供するMCPサーバー。settle detection・wait_forパターンマッチの実装が参考になる。
- [coder/agent-tty](https://github.com/coder/agent-tty) — PTY駆動+スクリーン記録・検証ツール。tmux/expect/asciinema/Playwrightとの比較整理が実装方針検討の参考になった。
- `expect`, `script(1)`, `asciinema`, `ttyrec` — PTY記録・自動応答の古典的ツール群。基本技術(forkpty, termios raw mode)の土台。
