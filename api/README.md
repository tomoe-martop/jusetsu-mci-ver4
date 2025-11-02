# MCI Prediction

## 実装サンプル 

```python
from pred_mci import Predictor

age = 70
male = 0
edu = 12
solo = 1
csv_path = "data/test_data.csv"

# インスタンス化
p = Predictor(
    lgb_models_dir_path="models/lgb/*.txt",
    logi_models_dir_path="models/logistic/*.pkl",
    lgb_scaler_path="scaler/lgb_scaler.pickle",
    logi_scaler_path="scaler/logi_scaler.pickle"
)
res = p.calculate_score(age, male, edu, solo, csv_path) # {"status_code": 100, "score": 82}
```



## 仕様

### `pred_mci.Predictor`

```python
pred_mci.Predictor.__init__(lgb_models_dir_path: str, logi_models_dir_path: str, lgb_scaler_path: str, logi_scaler_path: str) -> None
```

#### 引数

`lgb_models_dir_path: str` : LightGBMモデルのディレクトリパス
`logi_models_dir_path: str` : Logistic回帰モデルのディレクトリパス
`lgb_scaler_path: str` : LightGBMモデル向け変数スケーラのファイルパス
`logi_scaler_path: str` : Logistic回帰モデル向け変数スケーラのファイルパス



### `pred_mci.Predictor.predict_lightgbm()`

```python
pred_mci.Predictor.predict_lightgbm(age: int, sex: int, edu: int, solo: int, csv_path: str) -> float
```

電力モデルで予測するメソッド

#### 引数

`age: int` : 年齢
`sex: int` : 性別(男性=1、女性=2)
`edu: int` : 教育年数
`solo: int` : 独居かどうか(独居=1、同居者あり=0)
`csv_path: str` : 電力データのファイルパス

#### 返り値

`float` : 電力モデルの予測確率



### `pred_mci.Predictor.predict_logistic()`

```python
pred_mci.Predictor.predict_logistic(age: int, sex: int, edu: int, solo: int) -> float
```

背景モデルで予測するメソッド

#### 引数

`age: int` : 年齢
`sex: int` : 性別(男性=1、女性=2)
`edu: int` : 教育年数
`solo: int` : 独居かどうか(独居=1、同居者あり=0)

#### 返り値

`float` : 背景モデルの予測確率



### `pred_mci.Predictor.calculate_score()`

```python
pred_mci.Predictor.calculate_score(age: int, male: int, edu: int, solo: int, csv_path: str, debug: bool = False) -> Dict[int, Union[int, None]]
```

電力モデルと背景モデルの両方で予測するメソッド

#### 引数

`age: int` : 年齢
`male: int` : 男性かどうか(男性=1、女性=0)
`edu: int` : 教育年数
`solo: int` : 独居かどうか(独居=1、同居者あり=0)
`csv_path: str` : 電力データのCSVファイルパス
`debug: bool = False` : デバックモードで起動する場合、引数に`True`を渡す。デフォルトは`False`

#### 返り値

```python
# debug = Falseのとき
{
  "status_code": int, # ステータスコード
  "score": Union[int, None] # 成功の場合は認知機能スコア、失敗の場合はNone
}

# debug = True のとき
{
  "status_code": int, # ステータスコード
  "score": {
    "lightgbm": float, # 電力モデルの予測値
    "logistic": float, # 背景モデルの予測値
    "soft_voting": float, # 2モデルの平均(soft voting)の予測値
  }
}
```

| ステータスコード | エラーコード                | 内容                               |
| ---------------- | --------------------------- | ---------------------------------- |
| `100`            | `PredictionSuccess`         | 予測成功                           |
| `200`            | `ElectricDataNotFound`      | CSVがない                          |
| `201`            | `ElectricDataFormatError`   | 電力データフォーマットエラー       |
| `202`            | `ElectricDataShortage`      | 必要な電力データ量を満たしていない |
| `203`            | `ElectricDataEmpty`         | 電力データが空                     |
| `211`            | `BehaviorDataFormatError`   | 背景データフォーマットエラー       |
| `300`            | `ElectricModelNotFound`     | 電力モデルがない                   |
| `301`            | `ElectricModelFormatError`  | 電力モデル読み込みエラー           |
| `302`            | `ElectricModelPredictError` | 電力モデル予測時のエラー           |
| `310`            | `BehaviorModelNotFound`     | 背景モデルがない                   |
| `311`            | `BehaviorModelFormatError`  | 背景モデル読み込みエラー           |
| `312`            | `BehaviorModelPredictError` | 背景モデル予測時のエラー           |
| `400`            | `PredictionTimeOut`         | 予測時のタイムアウト               |
| `900`            | `UnexpectedError`           | 予期せぬエラー                     |



### `pred_mci.calc_func_time()`

```python
calc_func_time(quiet: bool = True) -> Any:
```

`Predictor`の各メソッドの実行時間を監視するためのデコレータです。

#### 引数

`quiet: bool = True` : 引数に`False`を取ると、デコレーションした関数の実行時間を標準出力する



### `pred_mci.PredictorWithLogging`

```python
pred_mci.PredictorWithLogging.__init__(lgb_models_dir_path: str, logi_models_dir_path: str, lgb_scaler_path: str, logi_scaler_path: str) -> None
```

`pred_mci.Predictor`のラッパーで、ログ出力機構が追加されたクラスです。
`pred_mci.Predictor`と同様、インスタンス化して使用します。

#### 引数

`lgb_models_dir_path: str` : LightGBMモデルのディレクトリパス
`logi_models_dir_path: str` : Logistic回帰モデルのディレクトリパス
`lgb_scaler_path: str` : LightGBMモデル向け変数スケーラのファイルパス
`logi_scaler_path: str` : Logistic回帰モデル向け変数スケーラのファイルパス