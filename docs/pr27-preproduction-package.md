# PR27 動画生成直前パッケージ

## 目的

三話の台本・分鏡・provider計画が完成した後、画像・動画・TTSを一度も生成せずに、実制作に必要な残作業を一つの可搬パッケージへまとめる。

```text
SeriesProductionManifest
+ source series YAML
+ source asset catalogue YAML
+ E01/E02/E03 PreparedEpisode
+ H3/Wan/Seedance GenerationPlan
+ pending AssetBundle
  -> zero-call preproduction package
```

## 出力

```text
preproduction/
├── preproduction_manifest.json
├── README.md
├── asset_creation_checklist.json
├── asset_creation_checklist.md
├── voice_identity_checklist.json
├── first_frame_plan.json
├── provider_route_summary.json
├── canary_recommendation.json
├── bundle_approval_commands.json
├── approval_templates/
├── approved_bundles/               # 後工程の出力先
├── wan_first_frames/                # 後工程の出力先
├── source_contract/
└── production_contract/
```

`source_contract`と`production_contract`はハッシュ固定されたスナップショットであり、編集用の第二の正本ではない。

## 素材チェックリスト

17素材をasset catalogueから直接読み、次を保持する。

- asset ID / 種別 / 名称
- prompt / negative prompt
- story function
- 使用話・使用segment
- instance rules
- voice identity要否
- 9個のroute別AssetBundleへの実際のbinding

`instance_rules`が存在する素材は、同じ基準画像を使い回せるか人間が判断する。特にE02とE03の葡萄は別個体である。

## 音声チェックリスト

台詞を持ち、`voice_identity_required=true`の人物だけを重複排除する。各AssetBundleのprofile IDと使用箇所を保持し、別人物へ同じvoice IDを誤割当てしない。

## Asset Bundle承認テンプレート

E01/E02/E03 × H3/Wan/Seedanceの9個について、現在のpending bundleから直接binding templateを作る。

テンプレートへ必要なPNGパス、承認者、voice IDを記入した後、既存の`approve_asset_bundle`を実行する。第一フレームはこの段階では承認しない。

## Wan第一フレーム計画

全15segmentを順番に列挙し、各segmentへ次を固定する。

- PreparedEpisode
- Wan GenerationPlan
- 現在の入力AssetBundle
- 必要な人物・場所・小道具asset ID
- WanMasterReferenceManifestの保存先
- keyframe preflight report
- keyframe PNG
- lineage-bound approval manifest
- 登録後AssetBundle
- prepare / preflight / paid / approve / register command

有料keyframeコマンドはテンプレートとして出すだけで自動実行しない。毎回preflightの最新approval digestが必要である。

## keyframe-only workflow

`render_wan_master_keyframe`は三段階で動く。

1. `preflight`: API 0、素材・費用・operation ID・approval digestを固定
2. `keyframe`: `--execute-paid`と完全一致するapproval digestが必要
3. `approve`: API 0、provider ledgerとPNG SHAを検査し、master lineage付き承認Manifestを作る

動画生成はこのworkflowに含めない。

## Canary選定

既存の`select_safe_canary_candidate`だけを使用し、Wan single-shot契約を満たす最初のsegmentを提示する。独自のスコアや恣意的な選定は追加しない。

## 費用情報

各GenerationPlanに保存済みのcall数、通貨別合計、未知コスト項目、価格snapshot日だけを集約する。現在価格を推測したり、新しい価格を埋めたりしない。

## fail closed

次の場合はパッケージ作成または後続preflightを停止する。

- source YAMLのSHAがSeriesProductionManifestと違う
- PreparedEpisode / GenerationPlan / AssetBundle digest不一致
- 三話・route・segmentが欠落
- catalogue assetがAssetBundleへ結び付かない
- voice-required characterにprofileがない
- Wan segmentにmaster referenceがない、重複する、9枚を超える
- package内へPNG/MP4/WAVなど生成メディアが混入
- 既存出力があり`--overwrite`なし

途中失敗時は既存のpreproduction出力を保持する。

## 実作品検証

CIではStoryboard-Generatorを`3070100f6ff25a3994a749b240f423f703cd294f`へ固定し、『一房の葡萄』で次を確認する。

- 3話
- 15segment
- 17基準素材
- 4固定音声
- 15第一フレーム枠
- 9provider route
- E02/E03の葡萄instance ruleを保持
- approval template 9個
- 推奨Wan Canaryあり
- 生成メディア0
- external API calls 0
