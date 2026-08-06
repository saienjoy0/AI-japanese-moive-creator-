# PR31 最小設計: 既存HappyHorse R2V経路を1.1へ接続する

## 結論

大規模な日本語短劇専用R2V基盤は作らない。

現在のmainには、すでに次が存在する。

- Storyboard R2V画面
- `VideoTask.generation_mode = "r2v"`
- `VideoTask.reference_image_urls`
- 人物・場所・小道具の選択と保存
- `ComicGenPipeline.process_video_task()`
- `WanxModel`
- 複数参照画像の`resolve_media_inputs()`
- HappyHorse共通HTTP送信`_generate_hh_http()`
- provider task ID / request IDの即時保存
- poll、download、動画タスクの永続化
- HappyHorse 1.1 R2Vのモデルカタログ登録

不足しているのは、`WanxModel.generate()`のHappyHorse分岐が
`happyhorse-1.0-*`だけを対象にしており、モデルカタログが返す
`happyhorse-1.1-r2v`を既存処理へ通していない点である。

---

# 4人の判断

## 1. 生成経路担当

既存経路をそのまま使う。

```text
StoryboardFrame / VideoTask
  -> reference_image_urls
  -> ComicGenPipeline.process_video_task()
  -> WanxModel.generate()
  -> _generate_hh_http()
  -> DashScope task
  -> MP4
```

新しいGenerationPlan、AssetBundle、Canary workflowは追加しない。

## 2. 参照画像担当

既存UIと`VideoTask.reference_image_urls`を正とする。

- R2V画面で選択された人物・場所・小道具画像を利用する
- 既存の`resolve_media_inputs()`でローカル画像やURLをprovider入力へ変換する
- HappyHorseの上限9枚は送信前に検査する
- `[Image 1]`などの番号は配列順に対応させる

新しいReference Manifestや画像検索層は作らない。

## 3. Provider担当

`src/models/wanx.py`だけを最小変更する。

現在:

```python
elif final_model_name.startswith("happyhorse-1.0-"):
```

修正後:

```python
elif final_model_name.startswith(("happyhorse-1.0-", "happyhorse-1.1-")):
```

I2V判定:

```python
if final_model_name in {
    "happyhorse-1.0-i2v",
    "happyhorse-1.1-i2v",
}:
```

R2V判定:

```python
elif final_model_name in {
    "happyhorse-1.0-r2v",
    "happyhorse-1.1-r2v",
}:
```

R2Vでは既存処理を再利用する。

```python
resolved_refs = resolve_media_inputs(...)
media = [
    {"type": "reference_image", "url": item.value}
    for item in resolved_refs
]
video_url = self._generate_hh_http(...)
```

`_generate_hh_http()`、task作成、provider ID保存、poll、downloadは変更しない。

## 4. 回帰・保守担当

新しい本番基盤は増やさず、Focused testだけを追加する。

必須確認:

1. `happyhorse-1.1-r2v`がHappyHorse分岐へ入る
2. 参照画像が`reference_image`のmedia配列になる
3. 画像順序が維持される
4. 画像0枚を拒否する
5. 画像10枚以上を拒否する
6. `ratio="9:16"`が既存HTTP payloadへ渡る
7. task ID / request ID callbackが維持される
8. `happyhorse-1.0-r2v`を壊さない
9. `happyhorse-1.1-i2v`を壊さない

---

# 実装対象

原則2ファイルだけにする。

```text
src/models/wanx.py
tests/test_happyhorse_11_r2v_routing.py
```

縦動画の既定値をモデル選択時に保証できないことがテストで判明した場合だけ、
次の3ファイル目を変更する。

```text
config/model_catalog/families/happyhorse.yaml
```

その場合の追加はR2Vの`ratio`既定値だけである。

```yaml
ratio:
  options: ["9:16", "16:9", "1:1", "4:3", "3:4"]
  default: "9:16"
```

---

# 変更しないもの

- `src/apps/jp_drama/**`
- PreparedEpisode
- GenerationPlanEpisode
- ApprovedAssetBundle
- WanMasterReferenceManifest
- H3 publication
- provider ledger
- SegmentArtifact
- episode composer
- 音声自動キャスト
- 全15セグメントdispatcher
- 自動fallback
- 自動retry
- 追加キーフレーム生成

---

# 受入条件

## コード

- 変更は2ファイル、必要時のみ3ファイル
- 実装差分はおおむね50行以内
- テストを含めても150行前後
- 新しいクラスやManifestを追加しない
- 既存`_generate_hh_http()`を使用する

## 動作

```text
happyhorse-1.1-r2v
+ reference_image_urls 1〜9枚
+ prompt
+ ratio=9:16
+ duration 3〜15秒
  -> 既存HappyHorse HTTP payload
  -> task ID保存
  -> poll
  -> MP4保存
```

## 最初の実確認

最小修正をmainへ入れた後、既存Storyboard R2V画面からE01-G01を1回だけ生成する。

- 17枚を全部送らない
- そのショットに必要な参照だけ選択する
- 追加キーフレームなし
- HappyHorse taskは1回
- 結果が悪い場合だけ次の設計を考える

---

# 削除した旧案

旧PR31で追加した次の専用実装は不要なため削除した。

- 専用Reference Resolver
- 派生HappyHorse GenerationPlan
- 汎用R2V Prompt Bundle
- 専用Approval Manifest
- 専用R2V Canary workflow
- 専用GitHub Actions
- E01-G01専用creative override
- 大規模contract tests
- `HappyHorse11I2VModel`の共通Transport化

このPR31は、既存経路の小さな接続修正だけに作り直す。
