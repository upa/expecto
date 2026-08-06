
# expecto 設計ドキュメント

- ステータス: Draft v0.1 (プロトタイプ実装用)
- 対象実装言語: Python 3.11+ (プロトタイプ)
- パッケージマネージャ: uv
- 対象OS: macOS, Linux

---

## 1. 目的 / コンセプト

`expecto` は、ターミナル(tty)とユーザーの入出力の間に透過的に挟まる CLI ツールである。

- 普段は通常のシェル(またはユーザーが起動した任意のプログラム)への入出力をそのまま中継する「透明なラッパー」として振る舞う。
- ユーザーが特定のキーバインド(ホットキー)を押すと **query mode** に入り、自然文でのクエリを受け付ける。
- クエリ確定(Enter)時に、直近のターミナル入出力の履歴とクエリを LLM に渡し、LLM はそれを達成するためのコマンドを **tty へ入力する形で** 実行する。
- 重要な特性: `expecto` は個々のシェルではなく **tty そのもの** をラップするため、ユーザーが `ssh` で別ホストに入っても、あるいはネットワーク機器(ルーター/スイッチ)のベンダー独自 CLI に入っても、そのまま LLM による支援・代理操作が届く。シェルの `PS1` や readline に依存しない。

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
| outer tty                  | `expecto` を起動した実際のターミナル(ユーザーが目視している画面)                                       |
| pty (master/slave)         | `expecto` が内部で確保する擬似端末。slave 側に子プロセス(shell/ssh等)をアタッチする                    |
| child process              | pty の slave にアタッチされるプロセス。デフォルトは `$SHELL`、あるいは起動時に指定した任意コマンド |
| passthrough mode           | 通常状態。outer tty ⇄ pty master のバイトを素通しする                                              |
| query mode                 | ホットキーで入る、クエリ入力用のオーバーレイモード                                                 |
| 実行ループ (execution)     | クエリ確定後、LLM がゴールを達成するまでコマンド実行→出力確認→次のコマンド、を繰り返す agentic ループ。expecto の唯一の実行動作 |
| screen buffer / scrollback | LLM に渡すために保持している「直近の入出力の履歴」(生バイト列とクリーン化テキストの両方)           |

---

## 3. ハイレベルな動作フロー

```
起動: expecto [-- command...]
  └─ 指定がなければ $SHELL を起動

[passthrough mode]
  ユーザーの入力 → そのまま child process へ
  child process の出力 → そのまま outer tty へ
  (この間も履歴バッファには常時記録している)

  ユーザーがホットキー (例: Ctrl-/) を押す
        │
        ▼
[query mode]
  - outer tty に最小限のオーバーレイ("miniprompt" `? `)を描画
  - ユーザーの入力は child process には送らず、query バッファに蓄積
  - Enter で確定 / Esc で passthrough に戻る(キャンセル)
        │ Enter
        ▼
  LLM へ「直近の入出力履歴 + query」を送信
        │
        ▼
  実行ループ (function calling):
    1. LLMが tool (run_command / send_control) を呼び出す
    2. expecto が pty master に コマンド + Enter (または制御文字) を書き込み
    3. 出力が安定(settle)するまで待つ
       settle せず閾値時間を超えた場合は「settle しなかった」
       旨の注記を結果に付ける
    4. その時点の画面テキストを tool の実行結果
       (function_call_output) として LLM に返す
    5. LLM は次の tool を呼ぶ(継続)か、最終応答(プレーンテキスト)を返す
       (最終応答は streaming で届いた文字から画面に表示する)
  ゴール達成 or ユーザーのCtrl-C割込みで終了。
  一定ステップ数(チェックポイント、既定15)ごとにユーザーへ
  継続確認(y/n)を行い、n なら終了(会話チェーンは y の間維持)
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
│                    expecto (親プロセス)               │
│                                                 │
│  TTYController      : outer ttyのraw化/復元        │
│  InputRouter         : 1バイトずつ見て              │
│                        hotkey検出 / 状態遷移         │
│  IORelay(asyncioポンプ): outer <-> pty master 中継  │
│  ScreenBuffer        : pty出力の履歴保持(pyte併用)  │
│  QueryModeUI         : ミニプロンプトの描画/入力回収  │
│  LLMClient           : OpenAI API呼び出し(async)    │
│  Executor            : 実行ループの制御              │
│  settle.wait_settle  : 出力安定検知                 │
└───────────────────────────────────────────────┘

並行処理は asyncio 単一スレッドのイベントループで行う(スレッド・Lock は
使わない)。詳細は **`DESIGN-Async.md`** を参照。
        │ pty master fd
        ▼
   pty slave ── child process ($SHELL, ssh, など)
```

---

## 5. コンポーネント設計

### 5.1 TTYController

- 責務: `expecto` 起動時に outer tty の `termios` 設定を保存し、raw モードにする。終了時(正常終了・例外・シグナル)に必ず元の設定へ復元する。
- Python標準: `termios`, `tty` モジュール。`tty.setraw(fd)` を使うが、復元用に `termios.tcgetattr()` の結果を保存しておくこと。
- `atexit` とシグナルハンドラ(`SIGTERM`, `SIGINT` を自プロセスが受けた場合)の両方で復元処理を確実に呼ぶ。
- `SIGWINCH` のハンドラ登録は cli が asyncio の `loop.add_signal_handler()` で行う(イベントループ上で実行され安全)。ハンドラは TTYController の `get_window_size()` / `set_pty_window_size()` で outer tty の現在サイズを pty へ反映し、あわせて `ScreenBuffer.resize()` で pyte スクリーンも追従させる。

### 5.2 PTYManager

- 責務: pty master/slave の確保、child process の fork/exec、終了検知。
- Python標準の `pty` モジュール(`pty.fork()`)を第一候補とする。より柔軟な制御が必要なら `ptyprocess` ライブラリの利用も検討(プロセス終了検知やリサイズAPIが扱いやすい)。
- child process のデフォルトは `os.environ.get("SHELL", "/bin/bash")`。`expecto -- ssh myrouter` のように任意コマンドの指定も可能にする。
- child の終了(exit)を検知したら `expecto` 自体も終了する。
- child process には環境変数 `EXPECTO_ACTIVE=1` を設定する(`execvp` で子孫プロセスに伝播)。`expecto` は起動時にこの変数を検査し、既に設定されていれば「expecto セッション内でのネスト起動」としてエラーメッセージを出して終了する(§10 参照)。意図的にネストしたい場合は `env -u EXPECTO_ACTIVE expecto` で回避できる。

### 5.3 IORelay (常時ポンプ + イベントハブ)

- 責務: asyncio イベントループ上で `loop.add_reader()` により outer tty の stdin fd と pty master fd を監視し、双方向にバイトを中継する常時ポンプ。加えて、実行ループのためのイベントハブ (settle 待ち・キー待ち・pty 読み取りの一時停止・実行タスク管理) を担う (DESIGN-Async.md §4.1)。
- fd コールバックの概形 (同期処理のみ):
  ```
  stdin_fd 読み取り → InputRouter.handle(data)   # hotkey検出 & 状態別ルーティング
  pty_fd 読み取り   → ScreenBuffer.feed(data)
                    → outer stdout へ書き込み
                    → 出力イベント通知(settle検知用) / summary トリガー
  ```
- query mode 中は pty master への書き込みを止める(ユーザー入力を child に渡さない)。`[thinking...]` オーバーレイ表示中は `pause_pty()` で pty の読み取り自体を止める(カーネル背圧。オーバーレイと子プロセス出力の混線を防ぐ)。
- クエリ確定で実行 (`Executor.execute`) を asyncio タスクとして起動し、実行中のユーザー Ctrl-C はこのタスクの `cancel()` になる。タスク終了 (done callback) で InputRouter を PASSTHROUGH に戻す。

### 5.4 InputRouter / ホットキー検出

- 1バイトずつ状態機械として処理する。UTF-8マルチバイト文字の分割に注意(バッファリングして完全な1文字になるまで待つ処理が必要)。
- ホットキーは 1 つ: `Ctrl-/` (0x1f) を既定とする。設定可能にし、衝突が判明したら容易に変更できるようにする。
- **端末応答シーケンスの転送** (`response_filter.py` の `ResponseFilter`): 子プロセスは外側端末へ問い合わせを送り、応答が届くまで入力処理を保留することがある (fish 4.x は毎プロンプトで CPR `\x1b[6n`・OSC 11・Primary DA を送る。bash/readline は送らない)。QUERY / EXECUTING 中は stdin を pty に流さないため、そのままでは応答が子プロセスに届かず、注入コマンドが処理されない (§10-5)。そこで両状態では通常のバイト処理の手前に応答認識の状態機械を挟み、端末→アプリケーション応答と確定したシーケンスのみ pty へ転送する:
  - CSI 応答: 最終文字 `R n y t`、または `?`/`>` プレフィクス付きの `c`・`?` 付きの `u` (DA・kitty keyboard protocol 応答)。矢印キー (`\x1b[A`)・`~` 系・kitty キー入力 (`?` なし `u`) は応答とみなさず従来処理へ再生する。
  - OSC / DCS 応答 (BEL / ST 終端): キーボードからは発生しないため内容を問わず転送。
  - チャンク (1回の read) 末尾で ESC 1バイトだけが保留されている場合はユーザーの Esc キーとして扱う。2バイト以上の有効プレフィクスはチャンクをまたいで保留し、分割された応答にも対応する。保留上限 1024 バイト。
  - 副次効果として、OSC 応答の終端 `\x1b\` に含まれる ESC が y/n 確認プロンプト (`wait_key`) に「n」と誤答する問題も防ぐ。
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
- **新規出力の抽出 (tool 結果用)**: 実行ループの tool 結果には「そのコマンドが生んだ新規出力のみ」を渡す(全履歴を毎回再送しない)。
  - `mark()`: 現時点の状態を記録して `ScreenMark` を返す。記録内容は「スクロールバックへ押し出した累計行数(deque の maxlen 溢れや clear に影響されない単調カウンタ)」と「カーソル行」。同時に pyte の `screen.dirty`(変更行の set。pyte は追加のみ、クリアはコンシューマの責務)をクリアする。
  - `get_text_since(mark)`: 「mark 以降に履歴へ押し出された行(mark 時点でカーソルより上にあった既存行がスクロールで押し出された分は除外)」+「mark 以降に変更された可視行の範囲」を結合して返す。スクロールが起きた場合、可視部分は mark 時点のカーソル行の移動先から下を採用する(スクロールは全行を dirty にするため)。
  - 1コマンドの出力がスクロールバック容量を超えた場合は、先頭に truncation 注記を付ける。画面クリアを伴う再描画では全行が dirty になるため、可視画面全体が結果になる(妥当なフォールバック)。

### 5.6 QueryModeUI

- 責務: outer tty 上に、child process の画面を破壊せずにミニプロンプトを表示する。
- 実装: 現在のカーソル位置(開始列は ScreenBuffer から取得)からそのままグレーで `? ` とクエリ文字列をインライン描画する。文字の追記は差分エコーのみ、削除・書き換え時はプロンプト先頭へ戻って `\x1b[J` で消去し再描画する(戻り位置の計算は East Asian Width を考慮した表示幅で行い、行折り返しに対応)。キャンセル時は消去し、確定時 (`finalize()`) はクエリ行を画面に残したまま終了する(直後に送る改行のエコーが行送りを供給するため)。
- alternate screen (vimなど) 使用中に query mode に入るケースは v0.1 では未対応(既知の課題として明記。詳細は §10）。

### 5.7 LLMClient

- OpenAI **Responses API** を利用する。実行ループの各ステップは `AsyncOpenAI` の `client.responses.stream()`(streaming)、working summary の生成は `client.responses.parse()`(Structured Outputs)。モデルは設定可能(既定は現行世代の高速モデル)。
- 入力: `instructions=` でシステムプロンプト、`input=` でユーザーメッセージ。
- システムプロンプトは英語で記述し、「コマンド以外の出力はユーザーのクエリの言語に合わせる」旨を指示する。
- 実行ループの**最終応答はプレーンテキスト**(スキーマ強制なし)。ゴール達成の要約、または達成不能な場合はその理由をテキストで返すようシステムプロンプトで指示する。done/fail の status フィールドは持たない(区別に実質的な意味がなく、プレーンテキスト化により streaming 表示が単純になるため)。
  - 「継続」は tool call そのもので表現される。**tool call を含まない応答をループ終了とみなす。**
  - 最終応答のテキストは `response.output_text.delta` イベントで届いた文字から順次、グレーの `[` + 本文 + `]` として outer tty に表示する(§5.8)。

#### Function calling

「コマンドを実行し結果を受け取る」1ステップは、Responses API の function calling による **tool** として LLM に公開する:

| tool           | 引数                                   | 動作                                                                                       |
|----------------|----------------------------------------|--------------------------------------------------------------------------------------------|
| `run_command`  | `command: string`                      | コマンド + Enter を pty master に書き込み、出力が settle するまで待って画面テキストを返す |
| `send_control` | `key: "ctrl-c" \| "ctrl-d" \| "ctrl-z"` | 制御文字を pty master に書き込み、同様に settle を待って画面テキストを返す                |

- tool 定義は `strict: true` の JSON Schema で渡し、引数は Pydantic モデル (`RunCommandParams` / `SendControlParams`) で検証する。検証に失敗した場合(strict のため通常発生しない)は、該当 `call_id` にエラーテキストを output として返す。
- `parallel_tool_calls=False` を指定し、1応答1 tool call を API レベルで保証する(防御的に複数返った場合も順次実行する)。
- LLM は tool call を返す(継続)か、最終応答(プレーンテキスト)を返すかを選ぶ。**tool call を含まない応答をループ終了とみなす。** Structured Outputs を使わないため、モデルが tool call の前に前置きテキストを出すことが構造上は可能になる(プロンプトで抑止し、出た場合はそのまま streaming 表示して継続する)。
- tool の実行結果 (`function_call_output`) の形式(画面全体ではなく、そのコマンドが生んだ新規出力のみを渡す。抽出方法は §5.5 参照):

      ## Terminal Output
      ```
      {mark 以降の新規出力 (§5.5 の get_text_since)}
      ```

  settle せず閾値時間を超えた場合は末尾に注記を付け、LLM が「Ctrl-C で中断するか、待つか」を判断できるようにする:

      [Note] Output did not settle within 30s. The command may still be running.

#### 会話チェーン (`previous_response_id`)

Responses API は `previous_response_id` パラメータで会話の継続をサポートする。サーバー側に過去のやり取りが保持されるため、クライアントは新しいメッセージだけを `input=` に渡せばよい。

- `store=True` で各レスポンスをサーバー側に保存する。
- 次のリクエストで `previous_response_id=response.id` を渡すと、API 側が過去の会話コンテキストを自動的に前置きする。
- `instructions=` は毎回送る必要がある（`previous_response_id` では引き継がれない）。
- 課金上の差異はない（過去の入力トークンは毎回カウントされる）。コード側の簡素化とトークン転送量の削減が利点。

各ステップにおけるメッセージ構築:

```
Step 1 (初回):
  input = [{ role: "user", content: "## Goal\n{query}\n\n## Current Terminal Screen\n```\n{screen}\n```" }]
  previous_response_id = なし
  store = true
  → response_id を記録。LLM は tool call (function_call) または最終応答を返す

Step N (フォローアップ):
  input = [{ type: "function_call_output", call_id: {前回の tool call の call_id},
             output: "## Terminal Output\n```\n{new_screen}\n```" }]
  previous_response_id = 前回の response_id
  store = true
  → response_id を更新
```

function_call 本体はサーバー側に保存されているため、クライアントは `function_call_output` だけを送ればよい。毎ステップで全画面履歴と過去のメッセージ列を再送する必要がなくなる。

### 5.8 Executor

- クエリ確定後の実行ループ(コマンド実行→出力確認→次のアクション)を制御する。
- 開始時(クエリ確定直後)にまず改行 1 つを pty に送り、シェルの新しいプロンプトを表示・settle 待ちで画面に反映してから、最初の LLM 呼び出しに進む。クエリ行が直前のプロンプト行を消費しているため、これを行わないと最初のコマンドがプロンプトのない行に注入されたように見える。
- LLM が function calling で `run_command` / `send_control` を呼び出すループとして実装する(§5.7)。tool call の実行(pty master への書き込み + settle 待ち + 新規出力の回収(§5.5 の mark / get_text_since)と `function_call_output` の構築)は Executor が担う。
- **危険コマンドの確認ゲート(3層)**: (1) LLM が `run_command` の必須フィールド `risk`(`read_only` / `mutating` / `destructive`)+ `risk_reason` で毎コマンドのリスクを自己申告する(環境適応の主判定。ベンダー CLI の知識は LLM 側にあり、working summary の環境推測が精度を支える)。(2) 決定論的バックストップ: 組み込み + ユーザー定義(`~/.config/expecto/dangerous_patterns.txt`、1行1正規表現・`#`コメント・大文字小文字無視)の正規表現にマッチしたコマンドは LLM の申告に関係なく `destructive` に**昇格**する(昇格専用 — 確認を免除する方向には働かない)。(3) `--confirm-level`(既定 `destructive`)以上のリスクのコマンドは実行前にユーザーへ y/n 確認。**拒否は abort ではなく** `function_call_output` で「ユーザーが拒否した」を LLM に返し、代替アプローチを取らせる。非対話環境では確認できないため拒否扱い。この機構は保証ではなくプロンプト削減であり(LLM 自己申告は prompt injection・自己評価バイアスに対し堅牢でない)、最終的な安全装置は「全コマンドと出力が常にユーザーの画面に見える」構造 + ユーザー在席 + Ctrl-C + チェックポイントである。
- `run_command` のコマンドエコーは、expecto のステータス表示と同じグレーで表示する。コマンド注入直前に outer tty へ SGR(グレー)を書き、IORelay に予約 (`arm_sgr_reset_at_newline()`) しておくと、ポンプがエコーの最初の改行(Enter のエコー)の直後にリセットを挿入する。改行が来ないまま settle・中断・EOF となった場合も `flush_sgr_reset()`(冪等)で必ずリセットする。SGR は outer tty にのみ書き、ScreenBuffer には入らないため LLM へ渡すテキストには影響しない(シェル自身がエコーに色を付ける場合はそちらが優先されうる、ベストエフォート)。
- ループ処理は下記 settle 検知 (§5.9) を利用する。`max_steps`(既定 15、LLM 呼び出し回数でカウント)ステップごとに**チェックポイント**としてユーザーに継続確認(`continue? (y/n)`)を行う。y なら `previous_response_id` チェーンを維持したまま続行、n / Esc / Ctrl-C なら終了。hard-abort ではなく確認にすることで、上限が「タスク失敗」ではなく「人間の目を挟む機会」として機能する(対話型ツールとして、ユーザー在席が第一の安全装置)。確認待ちの間も pty 出力の中継は継続する(常時ポンプが担う)。stdin が失われた場合(EOF)はセッション終了として実行タスクがキャンセルされる。
- LLMは `send_control` tool で制御文字（Ctrl-C等）を送信できる。これにより、長時間実行中のコマンドの中断などをLLM自身が判断できる。
- **最終応答の streaming 表示**: 最終応答テキストは `output_text.delta` が届き始めた時点で `[thinking...]` オーバーレイを消去し、グレーの `[` に続けて届いた文字から順次表示、応答完了で `]` + SGR リセットで閉じる。オーバーレイの消去はストリーミング開始時に一度だけ行い、以降は巻き戻さない(複数行の応答で画面がスクロールすると DECSC の保存位置が信頼できないため)。ストリーミング途中で Ctrl-C 中断された場合は、表示済みの部分テキストを画面に残したまま改行して `[expecto interrupted]` を表示する。delta が一度も届かなかった場合(空応答等)は従来どおり完了後に一括表示する。streaming 中も pty の読み取りは pause されたままであり、子プロセス出力との混線は起きない。
- ユーザーはいつでも `Ctrl-C` で実行を中断できるようにする(中断検知は InputRouter が実行中の入力を優先的にハンドルする形にする)。
- LLM 呼び出し(各ステップの step、およびクエリ直前の WorkingSummary 生成)は `AsyncOpenAI` を await する。実行全体が 1 つの asyncio タスクであり、**LLM 応答待ち中を含むあらゆる await 点で Ctrl-C により即座に中断**できる(タスクキャンセル)。キャンセル時は in-flight の HTTP リクエスト自体も中断され、後始末(オーバーレイ消去・SGR リセット・末尾改行)は finally で保証される (DESIGN-Async.md §5)。

### 5.9 settle 検知 (出力安定検知)

- コマンド実行後、「出力が落ち着いた」タイミングを検知するために、シンプルなアイドルタイマー方式を採用する。実装は `settle.wait_settle()` コルーチン (IORelay の出力イベントを `asyncio.Event` 経由で待つ):
  - コマンド送信後、pty master からの出力到着イベントを監視。
  - 一定時間(既定 300ms 〜 800ms、設定可能)出力が来なければ「settle」と判断。
  - settle せずに閾値時間(既定 30秒、設定可能)を超えた場合: タイムアウトエラーとはせず、その時点までの出力を tool の実行結果として LLM に送る。このとき「出力が settle しなかった(コマンドはまだ実行中の可能性がある)」旨の注記を結果に含め、LLM が次のアクションを判断する（例: `send_control` で Ctrl-C を送信してコマンドを中断する、さらに待つ、別のアプローチに切り替える等）。30秒以上継続するコマンド（ダウンロード、ビルド等）は日常的にあるため、一律でタイムアウト扱いにはしない。
- シェル固有のプロンプトマーカー注入(PS1書き換え)には**依存しない**。これは ssh 越しやルーターCLIでは使えないため、汎用性を優先してアイドル検知のみで v0.1 を組む。将来的に「対応シェルではPS1マーカーも併用してより正確に検知する」拡張は可能(§11参照)。

### 5.10 WorkingSummary (セッションコンテキスト)

`expecto` セッション内でユーザーがこれまで行ってきた操作の要約 (working summary) を保持し、LLM 呼び出し時にシステムプロンプトへ含めることで、文脈を踏まえた自動実行を可能にする。

詳細設計は **`DESIGN-WorkingSummary.md`** を参照。要点:

- 直近の生画面 (`get_text()`) = 短期記憶、working summary = スクロールアウトした情報の長期記憶、という役割分担。
- スクロールバックへ押し出された行数（§5.5 の単調カウンタ）をトリガーとするバックグラウンド差分更新 (anchored iterative summarization) + クエリ確定時の遅延同期生成。tool 結果が新規出力のみになったため、セッション全体の文脈供給は instructions 内の working summary が担う。
- 環境推測（SSH 先・ベンダー CLI 種別）を独立フィールドとして保持する構造化スキーマ。
- `history_lines`(LLM に送る行数、既定 500)と `scrollback_lines`(pyte スクロールバック容量、既定 5000)は分離されている（分離の経緯と既定値の根拠は同設計 §7 を参照）。

---

## 6. 状態機械 (State Machine)

| 状態          | 説明                                       | 遷移条件                                              |
|---------------|--------------------------------------------|-------------------------------------------------------|
| `PASSTHROUGH` | 通常中継                                   | hotkey → `QUERY`                                      |
| `QUERY`       | クエリ入力受付中                           | Enter → `EXECUTING` / Esc,Ctrl-C → `PASSTHROUGH`      |
| `EXECUTING`   | 実行ループ (LLM 応答待ち・コマンド実行)    | ゴール達成/失敗/チェックポイントで n/Ctrl-C → `PASSTHROUGH` |

※ `EXECUTING` の中身(LLM 呼び出し・tool 実行・確認プロンプト)は asyncio の実行タスクとして表現され、タスク完了 (done callback) で `PASSTHROUGH` に戻る (DESIGN-Async.md §4.2)。

---

## 7. CLI / 設定

```
expecto [OPTIONS] [-- COMMAND ...]

OPTIONS (実装済み):
  --shell PATH               子プロセスとして起動するコマンド (既定: $SHELL)
  --history-lines N          LLMに渡すスクリーン履歴の行数 (既定: 500)
  --scrollback-lines N       pyteスクロールバック容量 (既定: 5000)
  --summary-trigger-lines N  working summary 更新トリガーの行数 (既定: 500)
  --summary-max-input-lines N  working summary 1回の差分入力上限 (既定: 2000)
  --confirm-level LEVEL      確認を求める最低リスク
                             (destructive|mutating|none、既定: destructive)
  --dangerous-patterns-file PATH  危険コマンド正規表現の追加ファイル
                             (既定: ~/.config/expecto/dangerous_patterns.txt)
  --log-file PATH            クエリ・LLM要求/応答のJSONLログ出力先 (既定は無効)
  --raw-io-log-file PATH     raw I/Oログの出力先 (デバッグ用、既定は無効)
```

LLM 接続は環境変数で指定する:
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` (既定: gpt-4o) /
`OPENAI_REASONING_EFFORT`。

`.env` ファイルの探索順: カレントディレクトリ (python-dotenv の慣例に従い親方向にも探索) → 見つからなければ `~/.config/expecto/.env`。既に設定済みの環境変数はファイルの値より常に優先される。インストール済みコマンドとしてどこから起動しても API キー等を読めるようにするための仕組み。

Working summary 生成用の LLM 接続 (URL・APIキー・モデル・reasoning effort) は環境変数 `SUMMARY_OPENAI_BASE_URL` / `SUMMARY_OPENAI_API_KEY` / `SUMMARY_OPENAI_MODEL` / `SUMMARY_OPENAI_REASONING_EFFORT` で指定する (未設定時は対応する `OPENAI_*` にフォールバック)。詳細は `DESIGN-WorkingSummary.md` §7 を参照。

未実装 (将来対応): `--hotkey` (現状は既定の ctrl-/ 固定。config 上のフィールドはある)、`--model`、`--max-steps` (既定 15 固定)、`--settle-ms` (既定 500 固定)、および設定ファイル `~/.config/expecto/config.toml` (既定値を書けるようにし、CLIオプションが上書きする。APIキーは環境変数を利用し設定ファイルに平置きしない)。

### TODO (将来追加予定の改善)

- **シークレットのマスキング**: LLM に送信するスクリーン履歴から、パスワード・APIキー等の機密情報を簡易マスキングする（§9 参照）。`password`, `token`, `secret` 等を含む行の値部分を伏字にする。
- **プロンプトエンジニアリング**: システムプロンプト・ユーザーメッセージの内容を精査し、出力品質を改善する。現状は最低限の指示のみ。

---

## 8. LLMへ渡すコンテキストの構築

システムプロンプトの骨子(実装時にチューニング)。応答形式は Structured Outputs で強制するため、プロンプト内での JSON 形式指示は不要:

```
あなたはユーザーの操作しているターミナル(またはSSH先、あるいはネットワーク機器のCLI)
を操作してゴールを達成するアシスタントです。
- 直近のターミナル画面の内容(以下に付与)を参照し、現在どのシステム/シェル/CLIに
  接続されているかを判断してください。
- ゴールが達成されるまで、1ステップずつコマンドまたは制御文字を実行してください。
  前のコマンドの出力を評価し、次に何をすべきか判断してください。
  コマンドが長時間実行中の場合、Ctrl-Cで中断するか待つかを判断してください。
```

ユーザーメッセージには以下を含める:
- 直近のクリーンテキスト画面/スクロールバック(§5.5)
- ユーザーのクエリ文字列
- (ループ中は) 各ステップの履歴は「LLMの tool call (function_call)」と「その実行結果 (function_call_output)」のペアとして会話に蓄積される。履歴の保持は `previous_response_id` によるサーバー側チェーンに委ねるため、クライアントは毎ステップ最新の `function_call_output` だけを送る(§5.7)。これにより、LLMは過去に何を試して何が起きたかを踏まえて次のアクションを判断できる。

---

## 9. 安全性・注意点

- **危険コマンドの排除**: expecto は pty ラッパーであり、実行先が ssh 先や NW 装置の場合ローカル OS のサンドボックスは届かないため、使える手段は実行前ゲートのみ。§5.8 の 3 層ゲート(LLM リスク自己申告 / 決定論的バックストップによる昇格 / ユーザー確認)で対応する。コマンド体系が UNIX に限らないため静的ブロックリストだけでは覆えず、環境適応の主判定は LLM が担い、静的パターンは「明白に破滅級」の下限保証に限定する。本格的なサンドボックスや権限制御は範囲外。
- **シークレットの扱い**: raw ログ・LLMへの送信内容にパスワードやAPIキーが混入しうる。v0.1では簡易マスキング(例: `password`, `token`, `secret` を含む行の値部分を伏字にする)を検討するが、完全性は保証しない旨をドキュメントに明記する。
- **実行ループの暴走対策**: ステップ数チェックポイントでの継続確認(既定 15 ステップごと、非対話環境では終了)と、ユーザーによる即時中断(Ctrl-C)を必須実装とする。`previous_response_id` は過去入力を毎回再課金するため、コストはステップ数に対し超線形に伸びる — 将来的にトークン/コスト予算上限(SWE-agent 型)の併設を検討する。
- **ホットキーの衝突**: 子プロセス側(vim, tmux, ルーターCLI等)が同じキーを使っている場合に奪い合いが発生する。設定で変更可能にしておくことが必須。

---

## 10. 既知の課題 / v0.1で扱わないもの (オープン課題)

1. **alternate screen 対応**: `vim`, `less`, `top` などが alternate screen buffer を使っている最中に query mode のオーバーレイを描画すると、復帰時に描画が乱れる可能性がある。pyteでalternate screen判定をして、query mode進入前に一時的に元の画面へ戻す/después復元する、といった対応が今後必要。
2. **パスワードプロンプトの扱い**: sshやsudoのパスワード入力中にhistoryバッファへ内容が残る可能性。マスキングロジックの精度向上が必要。
3. **完了検知の精度**: アイドル検知は「出力がただ遅いだけ」のケース(大きなダウンロード等)と「本当に終わった」ケースを区別できない。将来的にOSC 133等のシェル統合や、プロンプト正規表現の学習的検出を追加検討。
4. **マルチバイト入力**: 日本語入力(IME)がある場合、raw モードでの1バイトずつの処理とIME側のコンポジションの扱いに注意が必要(v0.1はASCIIコマンド入力を主眼とし、日本語クエリ自体は問題ないが、IME変換中のキーイベントの扱いは要検証)。
5. **端末応答と入力保留の干渉** (対応済み): fish 4.x は毎プロンプトで端末に問い合わせ (CPR 等) を送り、応答が届くまで入力処理を保留する。当初の実装は EXECUTING 中の stdin を全破棄していたため、外側端末の応答が子プロセスに届かず、fish 4.x 上でコマンド注入が無反応になった (macOS + wezterm + fish で発見)。§5.4 の ResponseFilter による応答転送で解決。応答と区別できない一部のキー (F3 の CPR 衝突等) が実行中に pty へ届く可能性は許容する。
6. **ネストしたexpecto**: 非対応(想定しない)。ローカルでのネスト起動は、`expecto` が子プロセスに設定する環境変数 `EXPECTO_ACTIVE` により起動時に検知してエラー終了する(§5.2)。外側の expecto がホットキーを先に捕まえてしまい、過去セッションのスクロールバックを引き継いだ LLM 応答が返るなど、気づきにくい誤動作の原因になるため。ssh 先で `expecto` を起動するケースは環境変数が伝わらないため検知できない(引き続き非対応)。

---

## 11. 将来拡張の候補 (v0.1では実装しない)

- シェル統合(OSC133 / PS1マーカー)による完了検知の精度向上(自分のローカルシェルに限り)。
- pty-mcp / agent-tty のような「録画・スクリーンショットでの検証」機能。
- 複数セッションの並行管理。

---

## 12. プロトタイプの実装スコープ (v0.1 MVP)

当初は「completion mode (LLM が 1 コマンドを提案・注入のみ行い、実行はユーザーに委ねるモード)」を Phase 1、auto mode を Phase 2 として段階実装したが、その後 completion mode は削除し、実行ループ(旧 auto mode)を expecto の唯一の振る舞いとした。現在のスコープ:

- passthrough mode (raw tty中継、SIGWINCH伝播)
- ホットキーによる query mode 突入・キャンセル
- 実行ループ (コマンド実行 → settle検知 → LLM判断 → 繰り返し)
- LLMからの制御文字送信 (Ctrl-C等)
- settle 閾値超過時のLLMへの中間報告
- ステップ数チェックポイントでの継続確認、ユーザーによるCtrl-C中断
- 危険コマンドの確認ゲート(リスク分類+バックストップ+確認)
- pyteによる画面のクリーンテキスト化とLLMへのコンテキスト付与
- `--shell` オプションによる任意コマンド起動(sshやルーターCLIへの接続を含む)

### 含まない:
- §10, §11 に記載の課題・拡張

## 13. 想定ディレクトリ構成

```
expecto/
├── pyproject.toml
├── src/
│   └── expecto/
│       ├── __init__.py
│       ├── cli.py            # エントリポイント、引数解析、asyncio.run
│       ├── tty_controller.py # raw mode化/復元、ウィンドウサイズ取得/反映
│       ├── pty_manager.py    # pty確保、child起動
│       ├── io_relay.py       # asyncio常時ポンプ、実行タスク管理
│       ├── input_router.py   # ホットキー検出、状態機械
│       ├── response_filter.py # 端末応答シーケンス認識 (stdin保留中の転送用)
│       ├── screen_buffer.py  # pyte連携、履歴管理
│       ├── query_ui.py       # ミニプロンプト描画
│       ├── llm_client.py     # OpenAI API呼び出し (AsyncOpenAI、Structured Outputs)
│       ├── executor.py       # 実行ループ制御
│       ├── settle.py         # 出力安定検知 (wait_settleコルーチン)
│       ├── dangerous.py      # 危険コマンド正規表現バックストップ
│       ├── working_summary.py # セッション要約 (長期記憶)
│       └── config.py         # 設定読み込み
└── tests/
```

## 14. 依存ライブラリ (想定)

- `pyte` — 端末エミュレーション / クリーンテキスト化
- `openai` — LLM呼び出し (OpenAI公式Python SDK、AsyncOpenAI / Structured Outputs対応)
- `pytest` / `pytest-asyncio` — テストフレームワーク (dev dependency)
- 標準ライブラリ: `asyncio`, `pty`, `termios`, `tty`, `os`, `signal`, `fcntl`, `struct`

## 16. テスト・型チェック方針

- テストフレームワークには `pytest` を使用する。
- **テストケース作成のクライテリア**: `expecto` のために実装したロジックをテストすること。標準ライブラリや既存ライブラリの動作をただ確認するだけのテストケース（例: argparse がオプションをパースできること自体の検証）は書かない。
- async コンポーネントのテストには `pytest-asyncio` を使用する (`asyncio_mode = "auto"` を `pyproject.toml` に設定済み。テストコルーチンにデコレータは不要)。
- 型チェックには `ty` を使用する (`uv run ty check`)。設定は `pyproject.toml` の `[tool.ty]` に記述する。
- すべてのコードに型ヒントを付与する。公開インターフェースは明示的な型を書き、`Any` の使用は避ける。
- 各コンポーネント実装時に対応するテストを `tests/` 配下に作成する。既存コンポーネントの変更時もテストを適宜更新する。
- tty/pty 操作のテストには `pty.openpty()` で疑似端末ペアを作成して使用する。
- IORelay 等の `sys.stdin` に依存するコンポーネントは、fd を外部から注入可能にしてテスタビリティを確保する。

---

## 15. 参考にした既存ツール(先行事例調査より)

- [Butterfish Shell](https://github.com/bakks/butterfish) — シェルラッパー型。大文字始まりでLLMプロンプト、`!`でGoal Mode(自動実行)。PS1マーカーで完了検知。`expecto`との違いは、シェルのみをラップし子プロセス(ssh等)には介入しない点。
- [pty-mcp](https://github.com/raychao-oao/pty-mcp) — ローカル/SSH/シリアル(ネットワーク機器)向けPTYセッションをAIエージェントに提供するMCPサーバー。settle detection・wait_forパターンマッチの実装が参考になる。
- [coder/agent-tty](https://github.com/coder/agent-tty) — PTY駆動+スクリーン記録・検証ツール。tmux/expect/asciinema/Playwrightとの比較整理が実装方針検討の参考になった。
- `expect`, `script(1)`, `asciinema`, `ttyrec` — PTY記録・自動応答の古典的ツール群。基本技術(forkpty, termios raw mode)の土台。
