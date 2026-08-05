# 『一房の葡萄』基準画像候補 v1

正式な素材カタログの17素材を、素材IDが先頭に来る安定した英数字ファイル名へ整理した候補画像セットです。

- 画像数: 17
- 形式: PNG
- 比率: 約9:16
- 状態: **人間承認前**
- 正本プロンプト: `saienjoy0/Storyboard-Generator@3070100f6ff25a3994a749b240f423f703cd294f`

このフォルダは `output/one-bunch-preproduction` の外に置きます。PR27のpreproduction packageは生成メディア混入を拒否するためです。承認時は各 `bindings.template.json` の `path` に、このフォルダ内PNGの実パスを設定してください。

## 素材一覧

| ID | 名称 | ファイル | サイズ | 状態 |
|---|---|---|---:|---|
| C01 | 僕 | `characters/C01_boku.png` | 941×1672 | pending |
| C02 | ジム | `characters/C02_jim.png` | 941×1672 | pending |
| C03 | 先生 | `characters/C03_teacher.png` | 941×1672 | pending |
| C90 | 背景の級友 | `characters/C90_background_classmates.png` | 941×1672 | pending |
| C91 | 追及する級友 | `characters/C91_reporting_classmate.png` | 941×1672 | pending |
| P01 | 舶来の木製十二色絵具箱 | `props/P01_imported_wooden_12_color_paint_box.png` | 941×1672 | pending |
| P02 | 藍色と洋紅色の固形絵具 | `props/P02_indigo_and_magenta_solid_paints.png` | 941×1672 | pending |
| P03 | 未完成の海の絵 | `props/P03_unfinished_harbor_watercolor.png` | 941×1672 | pending |
| P04 | 少年の安い絵具 | `props/P04_cheap_worn_watercolor_set.png` | 941×1672 | pending |
| P05 | 一房の葡萄 | `props/P05_one_bunch_of_grapes.png` | 941×1672 | pending |
| P06 | 銀色の小さな鋏 | `props/P06_small_silver_scissors.png` | 941×1672 | pending |
| P07 | 主人公の学生鞄 | `props/P07_brown_leather_school_satchel.png` | 941×1672 | pending |
| S01 | 教室 | `scenes/S01_classroom.png` | 941×1672 | pending |
| S02 | 教室前の廊下 | `scenes/S02_school_corridor.png` | 941×1672 | pending |
| S03 | 先生の部屋 | `scenes/S03_teacher_room.png` | 941×1672 | pending |
| S04 | 学校の校門 | `scenes/S04_school_gate.png` | 941×1672 | pending |
| S05 | 横浜港の記憶 | `scenes/S05_yokohama_harbor_memory.png` | 941×1672 | pending |

## 検証

- 返送ZIP内の17枚とGitHub上の各PNGについてGit blob SHAを照合し、全件一致を確認済み
- `manifest.json`記載のSHA-256と画像実体が全件一致
- 全画像の実寸が941×1672であることを確認済み
- `asset_path_map.json`の17パスが実在ファイルと一致

## 重要

- P05「一房の葡萄」は種類・外観の基準候補です。E02とE03では別個体として扱うルールを維持してください。
- C90は背景群衆候補、C91は追及する級友候補です。主要人物として顔を固定しすぎないでください。
- Gitへ追加しただけでは承認済みではありません。SHA固定と人間承認を経て `ApprovedAssetBundle` に登録します。
