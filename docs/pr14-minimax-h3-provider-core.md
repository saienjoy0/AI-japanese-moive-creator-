# PR14 MiniMax H3 Provider Core and Paid Canary Safety

MiniMax H3を動画生成の第一候補にし、Wan I2Vをフォールバックとして残すための基盤と、1セグメントだけを安全に実行するCanaryを実装する。

## 実装範囲

- `minimax/h3-reference-av`
- `minimax/h3-first-frame`
- `minimax/h3-text`
- 4〜15秒の整数尺
- 768P / 2K
- 参照画像・動画・音声
- V2 submit/query/download client
- H3優先GenerationPlan
- H3専用単一セグメントCanary
- `preflight / approve / render / resume / reconcile`
- 出力動画・参照動画・追加画像を含むUSD料金再計算
- 永続的な`max_cost_usd`送信前ゲート
- task IDからの再開
- `submission_unknown`時の二重送信防止と手動reconcile
- 原子的JSON ledger
- 公開HTTPS URL、SHA-256、形式、サイズ、尺、FPS、寸法、codecの送信前preflight
- QueryとDownloadの上限付き再試行
- ffprobeによるMP4・映像stream・codec・縦型・尺・解像度検査
- Fake transportによるゼロ課金テスト

## 安全境界

MiniMax H3のPOSTは`MiniMaxH3Executor`以外から実行できない。Adapterの`submit()`は、永続ledger、料金上限、素材preflightを迂回するため拒否する。

料金は呼び出し側から申告させない。Executorが検証済みの`H3VideoGenerationRequest`と`H3ReferenceBundle`から、出力秒数、参照動画秒数、画像数、解像度を使って再計算する。再計算額が`max_cost_usd`を超える場合はPOST前に停止する。

POST直前に`submission_attempts=1`と`submitting`を原子的に保存する。POST後に接続切断、408、429、5xx、不正JSON、task ID欠落などが起きた場合は`submission_unknown`へ移行し、自動再POSTしない。MiniMaxコンソール等で確認したtask IDを`reconcile`で明示的に結び付けた後、`resume`で再開する。

task IDはトップレベル`task_id`、または明示的な`task.task_id` / `task.id`からのみ取得する。レスポンス全体の任意の`id`はtask IDとして採用しない。Queryレスポンスのtask IDがledgerと異なる場合も停止する。

## ダウンロード復旧

動画は一時ファイルへ保存し、MP4ヘッダーとffprobe検査に成功してからSHA-256を計算し、ledgerを`downloaded`、続いて`validated`へ更新する。壊れたHTML、空ファイル、映像streamなし、codec不正、尺や解像度不一致は正式成果物として保存しない。

検査に失敗した場合はファイルを削除し、taskの`result_url`を保持したまま状態を`succeeded`へ戻す。設定回数までは同じtaskから再ダウンロードするため、再生成や二重POSTは発生しない。

## H3 Canary

```bash
python -m src.apps.jp_drama.workflows.render_minimax_h3_segment_canary \
  --prepared-input output/jp_drama/prepared/prepared_episode.json \
  --generation-plan output/jp_drama/generation/generation_plan_episode.json \
  --segment-id SEGMENT_ID \
  --assets assets.json \
  --config examples/jp_drama/minimax_h3_live_provider.json \
  --output output/jp_drama/h3/segment.mp4 \
  --stage preflight \
  --max-cost-usd 1.00
```

### preflight

外部APIを呼ばず、PreparedEpisodeとGenerationPlanのdigest、route、素材、request fingerprint、料金、環境変数を検査する。

### approve

request fingerprint、素材SHA、model、resolution、duration、再計算料金、上限、料金snapshotを承認Manifestへ固定する。いずれかが変わるとrenderを拒否する。

### render

承認Manifestが一致し、素材preflightと料金上限を通過し、過去のPOST試行がない場合だけ1回POSTする。

### resume

新規POSTは禁止し、保存済みtask IDのquery、result URLの再ダウンロード、既存検証済み動画の再利用だけを許可する。

### reconcile

`submission_unknown`に対して、MiniMax側で確認したtask IDと確認根拠を明示的に保存する。自動推測はしない。

## CI

通常のpush / pull_request CIは`MINIMAX_API_KEY`を空にし、外部API呼び出しゼロで次を検査する。

- 通常の日本語MDからH3優先planを生成
- H3 route errorがゼロ
- H3 video料金がUSD・exact
- H3 Canary preflightとapproveが成功
- 承認までledgerとMP4が作られない
- 料金超過、素材不正、曖昧POST、task ID不一致を拒否
- 壊れたダウンロードから同じtaskで復旧
- 二重POSTが発生しない

実課金は`Japanese Drama MiniMax H3 Paid Canary`の手動`workflow_dispatch`のみ。preflight成果物を作成した後、GitHub Environment承認を通してrenderする。renderは`minimax-h3-paid`ラベルを持つ単一のself-hosted runnerで実行し、runner workspace外の永続ディレクトリへledgerを保存する。これによりworkflow再実行時も既存ledgerから`resume`し、POST 2回目を防ぐ。確認文字列、最大1 USD、Environment、Secretを必要とし、1セグメント・POST 1回だけを実行する。

## 後続PR

- 全話逐次Runner
- Qwen3-TTS実生成
- 音声ミックスと字幕
- セグメント連結
- H3失敗時のWan自動フォールバック
- ローカル参照素材の自動公開
