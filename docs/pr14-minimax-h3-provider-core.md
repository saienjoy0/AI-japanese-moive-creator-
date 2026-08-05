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
- USD料金見積もり
- task_idからの再開
- `submission_unknown`時の二重送信防止
- 原子的JSON ledger
- Fake transportによるゼロ課金テスト

## 計画生成

```bash
python -m src.apps.jp_drama.workflows.prepare_generation \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output-dir output/jp_drama/generation-h3 \
  --profile examples/jp_drama/generation/minimax_h3_profile.json \
  --live-provider-config examples/jp_drama/dashscope_live_providers.json \
  --minimax-h3-config examples/jp_drama/minimax_h3_live_provider.json
```

この段階では外部APIを呼ばない。実API Canary、Qwen3-TTS音声ファイル入力、音声ミックス、全話逐次生成は次PRで接続する。
