# PR14 MiniMax H3 Provider Core

MiniMax H3を動画生成の第一候補にし、Wan I2Vをフォールバックとして残すための基盤実装。

## 実装範囲

- `minimax/h3-reference-av`
- `minimax/h3-first-frame`
- `minimax/h3-text`
- 4〜15秒の整数尺
- 768P / 2K
- 参照画像・動画・音声
- V2 submit/query/download client
- 出力動画・参照動画・追加画像を含むUSD料金見積もり
- 永続的な`max_cost_usd`送信前ゲート
- task_idからの再開
- `submitting`で停止した場合の`submission_unknown`昇格
- `submission_unknown`時の二重送信防止
- 原子的JSON ledger
- `pending://`、SHA-256、形式、サイズ、尺、FPS、寸法の送信前preflight
- QueryとDownloadの上限付き再試行
- Fake transportによるゼロ課金テスト

## 重要な安全境界

MiniMax H3のPOSTは`MiniMaxH3Executor`以外から実行できない。Adapterの`submit()`は、永続ledger、料金上限、素材preflightを迂回するため拒否する。

POST直前にledgerへ`submitting`を書き込む。プロセスがtask_id保存前に停止した場合、再起動時は`submission_unknown`へ移行し、自動再POSTしない。実行者がMiniMax側の履歴を確認するまで課金が重複する操作を禁止する。

参照素材のURLだけでは同一性を判断せず、素材SHA-256をrequest fingerprintとledgerへ保存する。素材メタデータが欠ける場合や`pending://`が残る場合はAPIへ送信しない。

## 計画生成

```bash
python -m src.apps.jp_drama.workflows.prepare_generation \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output-dir output/jp_drama/generation-h3 \
  --profile examples/jp_drama/generation/minimax_h3_profile.json \
  --live-provider-config examples/jp_drama/dashscope_live_providers.json \
  --minimax-h3-config examples/jp_drama/minimax_h3_live_provider.json
```

この段階では外部APIを呼ばない。通常台本の計画CIでは、H3ルートエラーがゼロ、料金不明項目がゼロ、外部API呼び出しがゼロであることを検査する。台本自体の境界不足によるplanning errorはroute errorと区別する。

実API Canary、Qwen3-TTS音声ファイル入力、音声ミックス、全話逐次生成は次PRで接続する。
