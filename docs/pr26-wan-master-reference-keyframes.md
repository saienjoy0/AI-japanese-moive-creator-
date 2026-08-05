# PR26 Wan approved-master first-frame flow

## 目的

Wanの第一フレームをテキストだけで生成せず、承認済みの人物・場所・小道具PNGを実際の`ref_image_paths`として渡す。

```text
PreparedEpisode
+ Wan GenerationPlan
+ ApprovedAssetBundle
  -> WanMasterReferenceManifest
  -> Wan image request with exact ordered ref_image_paths
  -> human review
  -> lineage-bound keyframe approval
  -> updated ApprovedAssetBundle
  -> video generation preflight
```

このPRのCIと準備コマンドはprovider APIを呼ばない。

## WanMasterReferenceManifest

Manifestは次を固定する。

- GenerationPlan digest
- segment ID
- `wan/i2v` route
- 計画に記載された参照画像の順序
- asset ID / role / subject ID
- local path / SHA-256 / width / height
- asset generation lineage
- master asset-set digest
- manifest content digest

AssetBundle全体のdigestは第一フレーム登録時に変化するため、Manifestは人物・場所・小道具の参照集合だけへ結び付ける。

## zero-call準備

```bash
python -m src.apps.jp_drama.workflows.prepare_wan_master_keyframe \
  --prepared-input prepared_episode.json \
  --generation-plan wan/generation_plan_episode.json \
  --asset-bundle wan/asset_bundle_approved.json \
  --segment-id E01-G01 \
  --manifest-output E01-G01.wan-master-references.json \
  --report E01-G01.wan-master-preflight.json
```

次の場合はprovider送信前に停止する。

- 未承認素材
- PNG欠損
- SHAまたは寸法変更
- 計画との不一致
- 重複参照
- 参照0件
- 9枚超過
- Wan以外のroute

## 第一フレーム生成

`WanMasterReferenceLiveTaskExecutor`はManifestがない場合にfail closedする。Manifestがある場合だけ、計画順の`ref_image_paths`をWan画像アダプターへ渡す。

Keyframe operation IDとledger source digestにはManifest digestを含める。素材が変わった場合、以前のprovider operationや承認を再利用しない。

## 人間承認

第一フレーム承認Manifestには次を保存する。

- keyframe SHA / dimensions
- provider / operation ID
- master-reference Manifest digest
- ordered master asset IDs
- ordered master asset hashes

## AssetBundle登録

```bash
python -m src.apps.jp_drama.workflows.register_wan_master_keyframe \
  --prepared-input prepared_episode.json \
  --generation-plan wan/generation_plan_episode.json \
  --asset-bundle wan/asset_bundle_approved.json \
  --master-reference-manifest E01-G01.wan-master-references.json \
  --approval-manifest E01-G01.keyframe.approval.json \
  --segment-id E01-G01 \
  --approved-by reviewer-name \
  --output-bundle wan/asset_bundle_with_E01-G01.json \
  --report E01-G01.registration.json
```

登録時と動画生成直前に、元素材・Manifest・第一フレーム承認・AssetBundleを再検証する。

## CI

- upstream Seedance fixtureからWan計画を作成
- fake PNG masterを承認
- Manifestの順序とround-tripを検査
- fake Wan画像モデルが正しい`ref_image_paths`を受け取ることを検査
- Manifestなしではfake provider callも0
- master PNG変更で失敗
- 異なるManifestの第一フレーム承認を拒否
- 第一フレーム登録後にAssetBundle digestが変わってもmaster lineageは有効
- prepare/register CLIの`external_api_calls == 0`
- 既存Asset Bundle、Wan Canary、Generation bridgeの回帰テスト

## このPRで実行しないもの

- 実Wan画像生成
- 実Wan動画生成
- TTS
- OSS upload
- H3生成
- Seedance生成
- 自動retry
- 自動provider fallback
