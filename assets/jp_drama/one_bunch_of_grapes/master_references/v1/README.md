# 『一房の葡萄』承認済み基準画像 v1

正式な素材カタログの17素材を、素材IDが先頭に来る安定した英数字ファイル名へ整理した基準画像セットです。

- 画像数: 17
- 形式: PNG
- 比率: 約9:16
- 状態: **人間確認・承認済み**
- 承認者: `saienjoy0`
- 承認日時: `2026-08-05T23:20:00+09:00`
- 承認記録: `human_approval.json`
- 正本プロンプト: `saienjoy0/Storyboard-Generator@3070100f6ff25a3994a749b240f423f703cd294f`

このフォルダは `output/one-bunch-preproduction` の外に置きます。PR27のpreproduction packageは生成メディア混入を拒否するためです。`apply_master_reference_approval`を実行すると、各`bindings.template.json`へこのフォルダ内PNGの実パスを検証付きで設定します。

## 素材一覧

| ID | 名称 | ファイル | サイズ | 状態 |
|---|---|---|---:|---|
| C01 | 僕 | `characters/C01_boku.png` | 941×1672 | approved |
| C02 | ジム | `characters/C02_jim.png` | 941×1672 | approved |
| C03 | 先生 | `characters/C03_teacher.png` | 941×1672 | approved |
| C90 | 背景の級友 | `characters/C90_background_classmates.png` | 941×1672 | approved |
| C91 | 追及する級友 | `characters/C91_reporting_classmate.png` | 941×1672 | approved |
| P01 | 舶来の木製十二色絵具箱 | `props/P01_imported_wooden_12_color_paint_box.png` | 941×1672 | approved |
| P02 | 藍色と洋紅色の固形絵具 | `props/P02_indigo_and_magenta_solid_paints.png` | 941×1672 | approved |
| P03 | 未完成の海の絵 | `props/P03_unfinished_harbor_watercolor.png` | 941×1672 | approved |
| P04 | 少年の安い絵具 | `props/P04_cheap_worn_watercolor_set.png` | 941×1672 | approved |
| P05 | 一房の葡萄 | `props/P05_one_bunch_of_grapes.png` | 941×1672 | approved |
| P06 | 銀色の小さな鋏 | `props/P06_small_silver_scissors.png` | 941×1672 | approved |
| P07 | 主人公の学生鞄 | `props/P07_brown_leather_school_satchel.png` | 941×1672 | approved |
| S01 | 教室 | `scenes/S01_classroom.png` | 941×1672 | approved |
| S02 | 教室前の廊下 | `scenes/S02_school_corridor.png` | 941×1672 | approved |
| S03 | 先生の部屋 | `scenes/S03_teacher_room.png` | 941×1672 | approved |
| S04 | 学校の校門 | `scenes/S04_school_gate.png` | 941×1672 | approved |
| S05 | 横浜港の記憶 | `scenes/S05_yokohama_harbor_memory.png` | 941×1672 | approved |

## 重要

- P05「一房の葡萄」は種類・外観の基準画像です。E02とE03では別個体として扱うルールを維持してください。
- C90は背景群衆、C91は追及する級友の基準です。主要人物として顔を固定しすぎないでください。
- 17枚のマスター画像は人間承認済みです。完全な`ApprovedAssetBundle`には、別途4人物の固有voice ID設定が必要です。

## 次工程

```bash
python -m src.apps.jp_drama.workflows.apply_master_reference_approval \
  --preproduction-root output/one-bunch-preproduction \
  --approval-manifest assets/jp_drama/one_bunch_of_grapes/master_references/v1/human_approval.json \
  --repository-root . \
  --print-report
```

この処理は画像を再生成せず、17枚のSHA-256・寸法・パスを検査して9個のroute別binding templateへ入力します。voice IDが未設定のため、AssetBundle承認コマンドは音声設定まで引き続き停止します。
