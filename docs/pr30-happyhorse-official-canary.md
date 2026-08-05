# PR30: 公式HappyHorse 1.1 I2V・1セグメントCanary

## 目的

『一房の葡萄』の承認済み基準画像と承認済み第一フレームを使い、Alibaba Cloud公式の`happyhorse-1.1-i2v`非同期APIへ1セグメントだけ送信する。

このPRでは、HappyHorseが返したMP4に音声streamが含まれるかを実測する。固定voice IDやQwen TTSを先に作らず、公式モデルの出力だけで日本語会話を成立させられるかを最小課金で判定する。

## 公式仕様を採用する範囲

公式APIリファレンス:

- https://www.alibabacloud.com/help/en/model-studio/happyhorse-image-to-video-api-reference
- https://www.alibabacloud.com/help/en/model-studio/model-pricing

実装する公式契約:

- model: `happyhorse-1.1-i2v`
- endpoint: `/api/v1/services/aigc/video-generation/video-synthesis`
- `X-DashScope-Async: enable`
- `input.media`へ`type=first_frame`をちょうど1件
- 720Pまたは1080P
- 3〜15秒
- task IDを保存して`/api/v1/tasks/{task_id}`をpoll
- 成功URLを直ちにMP4へ保存
- I2Vには`ratio`、`voice_id`、`audio_url`を送らない

## 既存実装から再利用するもの

- Singapore workspace endpoint解決
- `DASHSCOPE_API_KEY` / `DASHSCOPE_WORKSPACE_ID`
- DashScope一時OSSアップロード
- provider task IDの即時保存
- 再起動後のtask poll再開
- 永続API回数・費用上限ledger
- ApprovedAssetBundleのmaster画像、第一フレーム、SHA、lineage検査

## 変更しないもの

- 既存Wan 2.7経路
- Seedance手動経路
- 通常のApprovedAssetBundle voice ID必須ルール
- 9個の既存bundle承認コマンド
- 全話生成

HappyHorse専用Canaryだけが`voice_profile_not_ready`をwarningへ変換する。他のasset error、第一フレーム未承認、SHA不一致、lineage不足は引き続きprovider送信前に停止する。

## 実行経路

```text
17枚の承認済みmaster画像
  ↓
既存Wan第一フレーム生成・人間承認
  ↓
ApprovedAssetBundleへ第一フレーム登録
  ↓
HappyHorse preflight（API呼び出し0）
  ↓
公式HappyHorse 1.1 I2Vへ1回だけ送信
  ↓
task IDをledgerへ即保存
  ↓
poll・MP4保存
  ↓
ffprobeで音声streamを検査
  ├─ 音声あり → 人間が日本語・口パク・顔・動きを評価
  └─ 音声なし → 外部TTS設計へ戻る
```

## preflight

```bash
python -m src.apps.jp_drama.workflows.render_happyhorse_segment_canary \
  --prepared-input <prepared_episode.json> \
  --generation-plan <generation_plan_episode.json> \
  --segment-id <segment_id> \
  --asset-bundle <approved_asset_bundle.json> \
  --providers examples/jp_drama/dashscope_happyhorse_live_providers.json \
  --output output/happyhorse_canary.mp4 \
  --stage preflight \
  --report output/happyhorse_preflight.json \
  --print-report
```

preflightは次を出力し、provider呼び出しは行わない。

- 使用する第一フレームのpath / SHA-256 / 承認manifest
- 公式payloadへ入るprompt
- duration / resolution / seed
- request fingerprint
- asset readiness
- 費用予約値

## render

```bash
python -m src.apps.jp_drama.workflows.render_happyhorse_segment_canary \
  --prepared-input <prepared_episode.json> \
  --generation-plan <generation_plan_episode.json> \
  --segment-id <segment_id> \
  --asset-bundle <approved_asset_bundle.json> \
  --providers examples/jp_drama/dashscope_happyhorse_live_providers.json \
  --output output/happyhorse_canary.mp4 \
  --stage render \
  --max-api-calls 1 \
  --max-cost-cny 10 \
  --cost-reserve-cny 6 \
  --ledger-file output/happyhorse_ledger.json \
  --report output/happyhorse_render.json \
  --print-report
```

`cost-reserve-cny`は課金上限ledger用の保守的予約値であり、USD標準価格を固定為替で換算した値ではない。公式のSingapore標準価格は720Pが$0.14/秒、1080Pが$0.18/秒である。最新の割引はModel Studioコンソールを正とする。

## 二重課金防止

- provider送信前にledgerへoperationを作成
- task作成直後、長いpollより先にtask IDをatomic保存
- task IDがある再実行は新規POSTせずpollを再開
- task IDが保存されていない不確実な送信は、重複POSTせず停止
- 成功済みMP4はSHAと音声streamを再検査して再利用

## 成功条件

1. 公式payloadにfirst frameが1件だけ入る
2. `ratio`、`voice_id`、外部音声を送らない
3. voice IDが未設定でもHappyHorse Canaryだけは通る
4. master画像・第一フレーム・lineageの不足は通さない
5. preflightはAPI呼び出し0
6. renderの新規有料taskは最大1件
7. 再実行でtaskを重複作成しない
8. Qwen TTS呼び出し0
9. 返却MP4を音声削除せず保存
10. MP4にaudio streamが存在する
11. 成功後も全話へ自動展開せず、人間確認待ちにする

## 失敗時の方針

公式MP4に音声streamがなければ、Canaryは`happyhorse_native_audio_missing`で失敗する。無断でQwen TTSへfallbackしない。

その場合は既存設計どおり、4人物のvoice IDを承認し、外部TTSを生成して映像へmuxする。これにより「公式だけで最短化できたか」と「固定音声が必要か」を1回のCanaryで明確に判定できる。
