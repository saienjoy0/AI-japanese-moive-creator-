# PR29: 『一房の葡萄』基準画像の人間承認とbinding反映

## 目的

プロジェクト所有者が目視確認した17枚の基準画像を、SHA-256と寸法へ結び付けた正式な承認記録として保存し、PR27が生成する3話×3経路の`bindings.template.json`へ安全に入力する。

## 承認範囲

- 対象: C01/C02/C03/C90/C91、S01〜S05、P01〜P07の17枚
- 承認者: `saienjoy0`
- 承認日時: `2026-08-05T23:20:00+09:00`
- 範囲: `master_reference_images_only`
- 対象外: 4人物のvoice identity、Wan第一フレーム、生成動画

## 実装

- `human_approval.json`に17枚のパス・SHA-256・寸法・系譜を固定
- `manifest.json`とREADMEを人間承認済みへ更新
- `apply_master_reference_approval`を追加
  - 17枚の存在、SHA、PNG寸法、リポジトリ内パスを再検査
  - 9個のroute別binding templateへ画像パスと系譜を入力
  - voice IDは空欄のまま維持
  - `bundle_approval_commands.json`を`blocked_until_voices_are_filled`へ更新
  - 外部API呼び出し0
- SHA改変時にtemplateを一切変更せず停止する回帰テスト
- 実際の一房の葡萄preproduction packageを再構築して検証する専用CI

## 安全境界

画像承認だけを完全な`ApprovedAssetBundle`承認へ昇格させない。4人物の固有voice IDが設定されるまで、9個の承認コマンドは停止する。
