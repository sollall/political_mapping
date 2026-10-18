# political_mapping

国勢調査2020の市区町村別データから社会構造の指標（軸）を作り、日本地図上に可視化する。
仕様は「政治地理マップ 仕様書（国勢調査版）」に従う。政治傾向そのものは扱わず、後で選挙データを重ねるための属性レイヤーを先に整備する。

## すぐ試す（合成データのデモ）

e-Stat のアプリケーションIDやネットワークがなくても、架空の2県（デモ県・サンプル県）でパイプライン全体と地図を確認できる。

```sh
uv sync                                     # 仮想環境の作成と依存パッケージのインストール
uv run polmap demo                          # 合成データ生成 → 全段階を実行
uv run python -m http.server -d web 8000    # http://localhost:8000/ で地図を開く
```

検証レポートは `reports/demo/validation.md` に出る。

## 本番データで動かす

1. **e-Stat のアプリケーションID** を取得し、`.env.example` を `.env` にコピーして `ESTAT_APP_ID=` の後に書く（`.env` はGit管理外。環境変数 `ESTAT_APP_ID` があればそちらが優先）。
2. **境界データ**: 国土数値情報「行政区域（N03）」2020年版の GeoJSON を `data/boundary/N03-20200101.geojson` に置く（パスは `config/settings.yaml` の `paths.boundary`）。
3. **軸の統計表IDを埋める**: `config/axes.yaml` には Democracy 4 の有権者グループを参考にした16軸（直接測れる9軸＋代理指標7軸）と、追加の3軸（大卒率・転入者率・持ち家率）が入っている。持ち家率だけは統計表ID（statsDataId）とカテゴリーコードが `TBD` のままなので、e-Stat で確認して置き換える（`TBD` が残る軸は `polmap run` で除外される）。`uv run polmap check` で未記入の箇所が一覧できる。統計表IDとカテゴリーコードは次のコマンドで調べられる（`.env` のアプリケーションIDを使う）。

   ```sh
   uv run polmap search "年齢 5歳階級"      # 国勢調査2020の市区町村別の統計表を検索 → 統計表ID
   uv run polmap meta 0003xxxxxx           # その統計表のカテゴリーコード（cat01 など）を一覧
   uv run polmap dump                      # 追加の3軸用の検索結果とカテゴリーコードを explore/ に書き出す
   ```

   `polmap dump` の出力（`explore/search.md`、`explore/meta/<統計表ID>.txt`）はコミットして共有できる。検索語を変えるときは `uv run polmap dump 配偶関係 --ids 0003xxxxxx` のように渡す。

4. **合併・分割の対応表**: 2015年の値を使う場合、`config/boundary_changes.csv` を確認・追記する（同梱の行は要確認の例）。
5. 実行：

```sh
uv run polmap check                  # 設定と軸定義の検証
uv run polmap run                    # fetch → stage → align → compute → analyze → map → validate
uv run polmap run --from analyze     # 途中の段階から再実行
uv run polmap run --to compute       # 途中の段階まで
uv run pytest                        # テスト
```

既定は全国（`scope.prefectures: []`）。`["13"]` のように都道府県コードを書くとその範囲に絞る。境界データも対象範囲に合わせて N03 の全国版か都道府県版を置く。

## 構成

```
config/
  settings.yaml          パス・対象範囲・正規化・クラスタリング・品質基準の設定
  axes.yaml              軸定義（仕様書 第5・6章）
  boundary_changes.csv   2015→2020 の合併・分割対応表
  demo/                  デモ用の設定と軸定義
src/polmap/
  fetch.py      1. 取得       e-Stat API v3 → raw層（応答JSON + Parquet）
  stage.py      2. 整形       地域×カテゴリー×値の縦持ち、秘匿値（-、… など）を欠損に
  align.py      3. 境界揃え   全国・都道府県・政令市の市全体・特別区部を除外、2015年値を2020年境界へ組み替え
  compute.py    4. 指標計算   比率・比率の差・増減率 → mart層
  analyze.py    5. 統合分析   zスコア（加重/非加重）、ウィンソライズ、小人口処理、k-means + Ward
  mapdata.py    6. 地図用     N03 と結合、TopoJSON + 属性JSON を web/data/ へ
  validate.py   7. 検証       仕様書 第10章のチェック → reports/validation.md
  demo.py       合成データの生成
web/            MapLibre GL JS のビューア（単一軸・2軸・地域類型）
tests/          pytest
```

データ層（`data/` 以下、Git管理外）:

| 層 | ファイル | 内容 |
|---|---|---|
| raw | `raw/<statsDataId>/page_*.json`, `values.parquet`, `classes.parquet` | APIの応答そのもの |
| staging | `values.parquet`, `areas.parquet`, `aligned.parquet`, `aggregates.parquet` | 縦持ち・境界揃え済み |
| mart | `axes_long.parquet`, `axes_wide.parquet`, `analysis/*` | 地域×軸、分析結果 |

## 軸の定義

1軸＝1概念。すべての軸を「分子 ÷ 分母」の比率、比率の差、増減率のいずれかで書く。

```yaml
- axis_id: housing_rental            # 英小文字のスラッグ
  name: 持ち家 ↔ 借家                 # 値が大きい側を右に
  group: 住まい
  formula: ratio                     # ratio / difference / growth
  stats_data_id: "00032xxxxx"
  numerator:   {cat01: ["2"]}        # カテゴリーコードの和
  denominator: {cat01: ["1", "2"]}   # 「不詳」を除いた母集団
  unknown:     {cat01: ["9"]}        # 不詳率（品質指標）の計算用、任意
  small_area: true                   # 任意
  known_bias: ""                     # 任意
  updatable_2025: true               # 2025年簡易調査で更新できるか、任意
```

`filter` のキーには e-Stat の次元名（`tab`, `cat01`〜`cat15`, `time` など）をそのまま使う。

## 主な設定と決めた方針

- **秘匿値**: `compute.suppressed_policy: missing`（既定）は、秘匿セルを1つでも含む合計を欠損にする。`skip` で秘匿セルを除いて合計する。
- **zスコア**: 非加重と加重（各軸の分母で加重）の両方を計算・保存し、`normalize.weighting` で採用する方を選ぶ。平均と標準偏差は対象範囲（既定は全国）の市区町村から計算する。
- **小人口**: `small_population.threshold`（暫定 1000）未満にフラグ。`method: exclude` は統合分析から除外、`eb` は比率の軸を経験ベイズで平滑化する。地図上は斜線で注記し、除外した地域も元の値の位置で色を塗る。
- **クラスター**: 各軸のzスコアに直接 k-means（主成分分析はしない）（シード固定）。`cluster.k_range` の中でシルエット係数が最大の k を採用し、Ward法との一致度（ARI）を `cluster_selection.csv` に記録する。類型名は `cluster.names` で人が付ける。
- **政令市**: 2020年10月1日時点の20市の市全体コードと特別区部（13100）を除外する。

## ビューア

`web/index.html` は `web/data/` の3ファイルを読む（`?data=別のディレクトリ` で切り替え可）。

- 単一軸: 発散型（青 ↔ 赤、中央 ±0.5σ はグレー）。固定区分と分位点区分を切り替えられる
- 2軸: 各軸を3分位にした 3×3 のバイバリエイト配色
- 地域類型: カテゴリー配色。凡例をクリックするとその類型だけを強調
- ツールチップ: 市区町村名、元の値、zスコア、順位、分母
- クリックで全軸のプロフィール（平均との差の棒グラフ）、一覧表も表示できる

## 注意

- 言えるのは地域の傾向まで。個人の属性として解釈しない（生態学的誤謬）。
- 多くの軸は都市化と強く相関する。`mart/analysis/correlation.csv` と検証レポートの「軸間の相関」を確認する。
- 2020年は新型コロナ流行中の調査で、通勤などに影響があり得る。
