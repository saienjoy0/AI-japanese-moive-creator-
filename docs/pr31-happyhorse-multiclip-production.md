# PR31: 『一房の葡萄』E01-G01 HappyHorse本番4クリップ化

## 目的

Seedance分鏡のE01-G01は10秒の中に、絵の大特写、港の記憶、教室での混色、少年の視線移動という4つの視覚カットを含む。

HappyHorse 1.1 I2Vは1リクエストにつき第一フレームを1枚だけ受け取るため、既存の1セグメントCanaryを無理に拡張せず、E01-G01を本番用の4クリップとして明示的に実行する。

```text
E01-G01-A  0.0-2.0秒   未完成の海の絵
E01-G01-B  2.0-4.0秒   横浜港の記憶
E01-G01-C  4.0-6.5秒   安い絵具の混色
E01-G01-D  6.5-10.0秒  落胆と隣の机への視線
```

## 生成尺

HappyHorseの最小生成尺は3秒なので、次の長さでproviderへ依頼し、ローカル結合時に分鏡どおりへ切る。

| Clip | Provider | Final |
|---|---:|---:|
| A | 3秒 | 2秒 |
| B | 3秒 | 2秒 |
| C | 3秒 | 2.5秒 |
| D | 4秒 | 3.5秒 |

最終出力は10秒、24fps、720P、9:16、約240フレーム。

## 承認とlineage

各第一フレームは次を固定する。

- repository-relative SVG preview path（内部にprovider用JPEGをbase64固定）
- 埋め込みJPEGのSHA-256 / 300×533
- OpenAI image generation operation ID
- 人間承認者と承認時刻
- 参照した17素材内のasset ID
- 17素材の正式な`human_approval.json`

preflight時にはSVGを厳密なXMLとして検証し、埋め込みJPEGを復元してSHA・寸法・MIMEを確認する。render直前に一時JPEGへmaterializeし、参照元master PNGの存在・SHAも再検証する。

## 実行

### 0円preflight

```bash
python -m src.apps.jp_drama.workflows.render_happyhorse_multiclip_production \
  --plan assets/jp_drama/one_bunch_of_grapes/production_keyframes/E01/G01/E01-G01.happyhorse_multiclip_plan.json \
  --providers examples/jp_drama/dashscope_happyhorse_live_providers.json \
  --repository-root . \
  --output-dir output/E01-G01 \
  --stage preflight \
  --max-api-calls 4 \
  --max-cost-cny 20 \
  --cost-reserve-cny-per-clip 4 \
  --report output/E01-G01/preflight.json \
  --print-report
```

### 4クリップ生成

preflightの`approval_digest`を人間が確認し、完全一致する値だけを指定する。

```bash
python -m src.apps.jp_drama.workflows.render_happyhorse_multiclip_production \
  --plan assets/jp_drama/one_bunch_of_grapes/production_keyframes/E01/G01/E01-G01.happyhorse_multiclip_plan.json \
  --providers examples/jp_drama/dashscope_happyhorse_live_providers.json \
  --repository-root . \
  --output-dir output/E01-G01 \
  --stage render \
  --execute-paid \
  --approval-digest <preflight approval_digest> \
  --max-api-calls 4 \
  --max-cost-cny 20 \
  --cost-reserve-cny-per-clip 4 \
  --report output/E01-G01/render.json \
  --print-report
```

### 10秒へ結合

```bash
python -m src.apps.jp_drama.workflows.render_happyhorse_multiclip_production \
  --plan assets/jp_drama/one_bunch_of_grapes/production_keyframes/E01/G01/E01-G01.happyhorse_multiclip_plan.json \
  --providers examples/jp_drama/dashscope_happyhorse_live_providers.json \
  --repository-root . \
  --output-dir output/E01-G01 \
  --stage assemble \
  --report output/E01-G01/assemble.json \
  --print-report
```

## 二重課金防止

- source plan digestを共有ledgerへ固定
- クリップごとにrequest fingerprint入りoperation IDを作る
- task IDをpoll前に保存
- task IDがある場合はPOSTせずpoll再開
- task ID不明の不確実な送信は再送しない
- 成功済みMP4はSHAと音声streamを再検査して再利用
- preflightのapproval digestが違えば4本とも送信しない

## 音声

A〜Dすべてにprovider audio streamを要求する。Dだけに内心独白「海は、こんな色じゃない」を指定し、少年の唇は動かさない。4本のいずれかで音声streamが欠けた場合は結合しない。

## このPRで実行しないもの

- 有料HappyHorse task
- 自動retry
- 自動fallback
- E01-G02以降への自動展開
- 人間確認なしの公開
