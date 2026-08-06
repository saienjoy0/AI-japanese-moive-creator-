# 『一房の葡萄』MiniMax H3中国版への切替とE01-G01再利用

## 決定

動画生成の第一候補をMiniMax H3中国版へ切り替える。ただし、すでに生成・検証済みの最初の10秒 `E01-G01` は再生成しない。

```text
成功済み HappyHorse E01-G01（10秒）
  -> MP4 / render report / ledger / approvalを再検証
  -> provider call 0でSegmentArtifact化
  -> E01-G01をH3対象から除外
  -> 残りのsegmentだけMiniMax H3中国版へ渡す
```

## 固定済み既存成果物

- GitHub Actions run: `31085709337`
- Provider task: `2efce664-a6c0-4c95-adb6-ad7eed80403f`
- Provider request: `43794f28-b04b-9b9a-a49a-ac71a6abcf56`
- Model: `happyhorse-1.1-r2v`
- Segment: `E01-G01`
- 720P / 9:16 / 10秒 / native audio
- References: `C01 -> S01 -> S05 -> P03 -> P04`
- Approval digest: `sha256:3ec02df5cebad83874ac04ac3b1711037af70231fa8861e3df939e744b70eb3e`
- Request fingerprint: `sha256:4cc0dbce337978c8918e4ec8e93ad6d8922519cb5d81e2a92a49b93c1d311131`

## 中国版H3設定

国際版設定は変更せず、中国版専用ファイルを追加する。

```text
examples/jp_drama/minimax_h3_cn_live_provider.json
```

- API base: `https://api.minimaxi.com`
- Model: `MiniMax-H3`
- Resolution: `768P`
- Secret: `MINIMAX_API_KEY`
- 参照画像5枚までを標準無料枠として料金ゲート計算

既存ExecutorはUSD上限で送信前に停止するため、中国版設定内のUSD値は中国元請求額そのものではなく、意図的に余裕を持たせた送信上限用スナップショットとして扱う。実際の中国版請求確認はMiniMaxコンソールを正とする。

## 再利用時の検査

`reuse_happyhorse_segment_for_h3`は以下がすべて一致しない限り成果物を作らない。

- 成功run、task ID、request ID
- approval digestとrequest fingerprint
- model、segment、参照順
- reportの実provider routeとGenerationPlanのsource route
- ledgerが最大1 callかつ唯一のvideo operationが`succeeded`
- report、ledger、実MP4のSHA-256一致
- 9:16、10秒以上、音声stream、黒画面0.25秒以下

出力は次の3点。

```text
E01-G01.happyhorse-reuse.approval.json
E01-G01.segment_artifact.json
minimax_h3_cn_continuation_handoff.json
```

## GitHub Actions

mainへマージ後、open issue `#37`へリポジトリ所有者が次を完全一致で投稿する。

```text
BUILD_ONE_BUNCH_H3_CN_HANDOFF
```

Actionは過去の2つのartifactをダウンロードする。

- sealed input: run `31077418519`
- successful MP4/evidence: run `31085709337`

その後、E01-G01を再利用成果物へ変換し、`one-bunch-h3-cn-continuation-handoff` artifactを保存する。このActionは`MINIMAX_API_KEY`も`DASHSCOPE_API_KEY`も読み込まず、動画provider callは0。

## 次の有料境界

H3で生成するのはhandoffの`remaining_segment_ids`だけ。`E01-G01`が配列に混入した場合は失敗する。中国版H3の実送信は、既存の`preflight -> approve -> render/resume`と一回POST ledgerを使い、別の明示承認で開始する。
