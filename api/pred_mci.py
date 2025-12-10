import os
import pandas as pd
import numpy as np
import pickle
import glob
import datetime
import lightgbm as lgb
import signal
from functools import wraps
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Union, List, Dict, Callable, Any

from myexception import InvalidInputError, PredictionError, PredictionTimeOut, UnexpectedError, TIMEOUT, timeout_handler


# Logging configuration
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = "predictor.log"

# Create a rotating file handler for logging
handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=[handler, logging.StreamHandler()])

logger = logging.getLogger(__name__)


def calc_func_time(quiet: bool = True) -> Any:
    def decorator(func: Any) -> Any:
        """
        Calculate the execution time of a function.
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            result = func(*args, **kwargs)
            end_time = time.perf_counter()
            elapsed_time = end_time - start_time
            if not quiet:
                print(f"Function '{func.__name__}' executed in {elapsed_time:.4f} seconds")
            return result
        return wrapper
    return decorator


class Predictor:
    N_DAY_ELECTRIC_DATA = 28  # 4 weeks * 7 days = 28
    N_MINUTES_PER_DAY = 1440  # 24 hours * 60 minutes = N_MINUTES_PER_DAY
    N_ROWS_ELECTRIC_DATA = N_DAY_ELECTRIC_DATA * N_MINUTES_PER_DAY # 28 days * N_MINUTES_PER_DAY minutes = 40320
    THRESHOLD_ELECTRIC_DATA = 0.95 # 95% of data must be present
    N_DAY_LIMIT_ELECTRIC_DATA = 25 # 25 days of data is the limit for electric data

    # 'age', 'edu_0', 'edu_1', 'day_cos', 'day_sin', 'AirConditioner_daytime', 'AirConditioner_midnight', 
    # 'ClothesWasher_daytime', 'ClothesWasher_midnight', 'Microwave_daytime', 'RiceCooker_midnight', 
    # 'TV_daytime', 'IH_daytime', 'day_sin * AirConditioner_daytime', 'day_sin * Microwave_daytime', 
    SANITIZER = [
        True, False, False, True, True, False, False, True, True, True, True, True, True, True, 
        False, False, True, True, False, False, False, True, False, False, False, False, False, 
        False, False, False, False, False, False, False, False, False, False, False, False, False, 
        False, True, False, False, False, True, False, False, False, False, False, False, False, 
        False, False, False, False
    ]

    def __init__(
        self, 
        lgb_models_dir_path: str,
        logi_models_dir_path: str,
        lgb_scaler_path: str,
        logi_scaler_path: str
    ):
        """
        Initialize the Predictor with model and scaler paths.
        
        :param lgb_models_dir_path: Directory path for LightGBM models.
        :param logi_models_dir_path: Directory path for Logistic Regression models.
        :param lgb_scaler_path: Path to the LightGBM scaler.
        :param logi_scaler_path: Path to the Logistic Regression scaler.
        """

        # scaler
        self.lgb_scaler = self._get_scaler(lgb_scaler_path)
        self.logi_scaler = self._get_scaler(logi_scaler_path)

        # models
        self.lgb_models = self._load_models(lgb_models_dir_path, lambda path: lgb.Booster(model_file=path), 500)
        self.logi_models = self._load_models(logi_models_dir_path, lambda path: pickle.load(open(path, 'rb')), 50)

    @staticmethod
    def _get_current_datetime() -> datetime.datetime:
        """
        Get the current date and time in Japan Standard Time (JST).
        """
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))

    @staticmethod
    def _get_models_paths(path: str, n: Union[int, None] = None) -> List[str]:
        """
        Get the list of model file paths.
        """
        models_path_lst = glob.glob(path)
        if n is not None:
            if n != len(models_path_lst):
                raise ValueError(f"Requested number of models ({n}) does not match the number of available models ({len(models_path_lst)}).")
        return models_path_lst
    
    @staticmethod
    def _get_scaler(path: str) -> str:
        """
        Load the scaler from the specified path.
        """
        try:
            with open(path, mode='rb') as f:
                scaler = pickle.load(f)
            return scaler
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Scaler file not found: {path}. Please ensure the scaler file exists.")

    def _load_models(self, dir_path: str, loader_func: Callable, expected_count: int) -> list:
        paths = self._get_models_paths(dir_path, expected_count)
        return [loader_func(path) for path in paths]

    @staticmethod
    def _datetime_encode(d: datetime.datetime) -> List[float]:
        """Encode the datetime object into a list of features.
        This includes cosine and sine transformations for the day of the week and day of the year.
        """
        day_of_year = (d.date() - datetime.date(d.year, 1, 1)).days
        cos_day_of_year = np.cos(2 * np.pi * day_of_year / 365)
        sin_day_of_year = np.sin(2 * np.pi * day_of_year / 365)
        return [cos_day_of_year, sin_day_of_year]

    def _divide_array(self, array:np.ndarray, nighttime_hour_0=5, nighttime_hour_1=2) -> tuple[np.ndarray, np.ndarray]:
        """arrayを日中帯と深夜帯のそれぞれに分割し返す関数

        Args:
            array (np.ndarray):                shapeが(n_weeks * 60m * 24h * 7d, channels)の3次元np.ndarrayであることが期待される
            n_weeks (int, default=0):          何週間分まとまったarrayかを明示する引数int
            nighttime_hour_0 (int, default=5): 深夜帯が0:00から何時間分かを定義するint。default=5(0:00〜4:59)
            nighttime_hour_1 (int, default=2): 深夜帯が23:59まで何時間分かを定義するint。default=2(22:00〜23:59)
        Returns:
            array_daytime_usage_time (np.ndarray):  日中帯のarray。shapeは(n_weeks * 60m * (24h - nighttime_hour_0 + nighttime_hour_1) * 7d, channels)
            array_midnight_usage_time (np.ndarray): 深夜帯のarray。shapeは(n_weeks * 60m * (nighttime_hour_0 + nighttime_hour_1) * 7d, channels)
        """
        channels = array.shape[1]
        array = array.reshape(self.N_DAY_ELECTRIC_DATA, self.N_MINUTES_PER_DAY, channels)

        # nighttime_0, daytime, nighttime_1に分割
        # 0:00〜4:59 (28, 300, 13)
        array_midnight_0 = array[:, :nighttime_hour_0 * 60, :]
        # 5:00〜21:59 (28, 1020, 13)
        array_daytime = array[:, nighttime_hour_0 * 60: -1 * nighttime_hour_1 * 60, :]
        # 22:00〜23:59 (28, 120, 13)
        array_midnight_1 = array[:, -1 * nighttime_hour_1 * 60:, :]

        # midnight_0とmidnight_1をあわせて整形
        array_midnight = np.concatenate([array_midnight_0, array_midnight_1], axis=1)
        array_midnight = array_midnight.reshape(self.N_DAY_ELECTRIC_DATA * (nighttime_hour_0 * 60 + nighttime_hour_1 * 60), channels)

        # daytimeを整形
        daytime_minutes = (24 - nighttime_hour_0 - nighttime_hour_1) * 60
        array_daytime = array_daytime.reshape(self.N_DAY_ELECTRIC_DATA * daytime_minutes, channels)

        array_daytime_usage_time = np.sum(array_daytime > 0, axis=0)
        array_midnight_usage_time = np.sum(array_midnight > 0, axis=0)

        return array_daytime_usage_time, array_midnight_usage_time
    
    def _load_data(self, csv_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Load data from a CSV file and preprocess it.
        """
        # Check if the CSV file exists
        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path, encoding='utf-8')
        else:
            raise FileNotFoundError(
                f"File not found: {csv_path}. Please ensure the file exists."
            )

        # Check the shape of the DataFrame
        if not df.shape == (self.N_ROWS_ELECTRIC_DATA, 10):
            raise InvalidInputError(
                201,
                f"DataFrame shape is {df.shape}, expected ({self.N_ROWS_ELECTRIC_DATA}, 10). "
                "Please ensure the CSV file has the correct format."
            )

        # Check if the required columns are present
        try:
            df = df[['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'rice_cooker', 'TV', 'cleaner', 'IH', 'Heater']]
        except KeyError as e:
            raise InvalidInputError(
                201,
                f"Missing required columns in DataFrame: {e}. ",
                f"Expected columns: ['date_time_jst', 'air_conditioner', 'clothes_washer', 'microwave', 'rice_cooker', 'TV', 'cleaner', 'IH', 'Heater']",
            )

        # Check the electric rate
        if (electric_rate := (self.N_ROWS_ELECTRIC_DATA - df.air_conditioner.isna().sum()) / self.N_ROWS_ELECTRIC_DATA) < self.THRESHOLD_ELECTRIC_DATA:
            raise InvalidInputError(
                202,
                f"Electric rate is {electric_rate:.3f}, expected >= {self.THRESHOLD_ELECTRIC_DATA}"
            )

        # Check the number of days with sufficient electric data
        threshold = int(self.N_MINUTES_PER_DAY * (1 - self.THRESHOLD_ELECTRIC_DATA)) # limit of lack rows per day
        daily_n_nan = np.sum(
            np.isnan(df.air_conditioner.values.reshape(self.N_DAY_ELECTRIC_DATA, self.N_MINUTES_PER_DAY)), 
            axis=1
        ) # daily number of lack rows
        if (n_over_threshold_per_day := np.sum(daily_n_nan > threshold)) > self.N_DAY_LIMIT_ELECTRIC_DATA:
            raise InvalidInputError(
                202,
                f"Electric rate >= 95% is only {n_over_threshold_per_day} days, expected >= 25 days"
            )

        # encode datetime features
        df.date_time_jst = df.date_time_jst.apply(
            lambda x: datetime.datetime.strptime(x, "%Y/%m/%d %H:%M:%S")
        )
        array_datetime = np.sum(np.array(list(map(self._datetime_encode, df.date_time_jst))), axis=0)

        # Divide the electric usage data into daytime and midnight usage
        array_daytime_usage_time, array_midnight_usage_time = self._divide_array(df.iloc[:, 1:].values)

        return array_datetime, array_daytime_usage_time, array_midnight_usage_time

    @staticmethod
    def _predict_soft_voting(models, X: np.ndarray, method: str) -> float:
        """
        Perform soft voting prediction using the provided models.
        """
        results = []
        for model in models:
            try:
                if method == "lightgbm":
                    results.append(model.predict(X)[0])
                elif method == "logistic":
                    results.append(model.predict_proba(X)[0][1])
            except Exception as e:
                if method == "lightgbm":
                    raise PredictionError(302, f"{method} prediction failed: {e}")
                elif method == "logistic":
                    raise PredictionError(312, f"{method} prediction failed: {e}")
        return float(np.mean(results))

    @calc_func_time()
    def predict_lightgbm(self, age: int, sex: int, edu: int, solo: int, csv_path: str) -> float:
        """
        Predict using the LightGBM model.
        """
        sex_1 = sex == 1
        sex_2 = sex == 2
        edu_0 = edu > 9
        edu_1 = edu <= 9
        solo_0 = solo == 0
        solo_1 = solo == 1

        try:
            array_datetime, array_daytime, array_midnight = self._load_data(csv_path)
        except InvalidInputError as e:
            raise e
        except FileNotFoundError as e:
            raise e

        elec_total = np.hstack([array_daytime, array_midnight])
        interactions = np.outer(array_datetime, elec_total).flatten()
        array_behavior = np.array([age, sex_1, sex_2, edu_0, edu_1, solo_0, solo_1])
        array_new = np.hstack([array_behavior, array_datetime, elec_total, interactions])

        X_scaled = self.lgb_scaler.transform(array_new.reshape(1, -1)).reshape(-1)
        array_sanitized = X_scaled[self.SANITIZER].reshape(1, -1)
        return self._predict_soft_voting(self.lgb_models, array_sanitized, "lightgbm")

    @calc_func_time()
    def predict_logistic(self, age: int, sex: int, edu: int, solo: int) -> float:
        """
        Predict using the Logistic Regression model.
        """
        edu = 1 if edu > 9 else 0
        X_scaled = self.logi_scaler.transform(np.array([age, sex, edu, solo]).reshape(1, -1))
        return self._predict_soft_voting(self.logi_models, X_scaled, "logistic")
    
    @staticmethod
    def _return_result(status_code: int, score: Union[int, None] = None) -> dict:
        return {
            "status_code": status_code,
            "score": score
        }

    def calculate_score(
            self, 
            age: int, 
            male: int, 
            edu: int, 
            solo: int, 
            csv_path: str,
            debug: bool = False
        ) -> Dict[int, Union[int, float, None]]:
        """
        Calculate the score based on the provided parameters and the CSV data.
        """
        # Validate input types
        if not isinstance(age, int):
            status_code = 211
            message = f"Invalid type for age: {type(age)}. Expected int."
            if debug:
                raise InvalidInputError(status_code, message)
            else:
                return self._return_result(status_code)
        
        if not isinstance(male, int):
            status_code = 211
            message = f"Invalid type for male: {type(male)}. Expected int."
            if debug:
                raise InvalidInputError(status_code, message)
            else:
                return self._return_result(status_code)

        if not male in [0, 1]:
            status_code = 211
            message = f"Invalid argument male: {male}. Expected 1 or 0."
            if debug:
                raise InvalidInputError(status_code, message)
            else:
                return self._return_result(status_code)
        
        if not isinstance(edu, int):
            status_code = 211
            message = f"Invalid type for edu: {type(edu)}. Expected int."
            if debug:
                raise InvalidInputError(status_code, message)
            else:
                return self._return_result(status_code)
        
        if not isinstance(solo, int):
            status_code = 211
            message = f"Invalid type for solo: {type(solo)}. Expected int."
            if debug:
                raise InvalidInputError(status_code, message)
            else:
                return self._return_result(status_code)
        
        if not solo in [0, 1]:
            status_code = 211
            message = f"Invalid argument solo: {solo}. Expected 1 or 0."
            if debug:
                raise InvalidInputError(status_code, message)
            else:   
                return self._return_result(status_code)

        # convert argument
        sex = 1 if male == 1 else 2

        # Predict using LightGBM
        try:
            # timeout signal
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(TIMEOUT)
            try:
                y_pred_proba_lgb = self.predict_lightgbm(age, sex, edu, solo, csv_path)
            except PredictionTimeOut as e:
                # Handle timeout error
                raise e
            finally:
                # Cancel the alarm
                signal.alarm(0)
        except InvalidInputError as e:
            if debug:
                raise e
            else:
                return self._return_result(e.status_code)
        except FileNotFoundError as e:
            if debug:
                raise e
            else:
                return self._return_result(200)
        except PredictionTimeOut as e:
            # Handle timeout error
            if debug:
                raise e
            else:
                return self._return_result(400)
        except Exception as e:
            if debug:
                raise PredictionError(302, f"LightGBM prediction failed: {e}")
            else:
                return self._return_result(302)

        # Predict using Logistic Regression
        try:
            # timeout signal
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(TIMEOUT)
            try:
                y_pred_proba_logi = self.predict_logistic(age, sex, edu, solo)
            except PredictionTimeOut as e:
                # Handle timeout error
                raise e
            finally:
                # Cancel the alarm
                signal.alarm(0)
        except PredictionTimeOut as e:
            # Handle timeout error
            if debug:
                raise e
            else:
                return self._return_result(400)
        except Exception as e:
            if debug:
                raise PredictionError(312, f"Logistic Regression prediction failed: {e}")
            else:
                return self._return_result(312)

        # soft voting
        y_pred_proba = np.mean([y_pred_proba_lgb, y_pred_proba_logi])
        
        if debug:
            return self._return_result(
                100,
                {
                    "lightgbm": y_pred_proba_lgb,
                    "logistic": y_pred_proba_logi,
                    "soft_voting": y_pred_proba
                }
            )
        else:
            # y_pred_probaがthreshold=0.467上の場合の対応
            if 0.47 > y_pred_proba > 0.467:
                y_pred_proba = 0.470
            elif 0.467 >= y_pred_proba >= 0.46:
                y_pred_proba = 0.460
            else:
                pass
            return self._return_result(100, int(y_pred_proba * 100))


class PredictorWithLogging(Predictor):
    def __init__(
        self,
        lgb_models_dir_path: str,
        logi_models_dir_path: str,
        lgb_scaler_path: str,
        logi_scaler_path: str
    ):
        logger.info("Initializing Predictor...")
        self.lgb_scaler = self._get_scaler(lgb_scaler_path)
        self.logi_scaler = self._get_scaler(logi_scaler_path)

        self.lgb_models = self._load_models(lgb_models_dir_path, lambda path: lgb.Booster(model_file=path), 500)
        self.logi_models = self._load_models(logi_models_dir_path, lambda path: pickle.load(open(path, 'rb')), 50)
        logger.info("Predictor initialized successfully.")

    def _load_data(self, csv_path: str):
        logger.info(f"Loading data from {csv_path}")
        try:
            data = super()._load_data(csv_path)  # 元の処理
            logger.info("Data loaded successfully.")
            return data
        except Exception as e:
            logger.error(f"Failed to load data: {e}")
            raise

    def predict_lightgbm(self, age: int, sex: int, edu: int, solo: int, csv_path: str):
        logger.info(f"Predicting LightGBM: age={age}, sex={sex}, edu={edu}, solo={solo}")
        result = super().predict_lightgbm(age, sex, edu, solo, csv_path)
        logger.info(f"LightGBM prediction result: {result:.4f}")
        return result

    def predict_logistic(self, age: int, sex: int, edu: int, solo: int):
        logger.info(f"Predicting Logistic Regression: age={age}, sex={sex}, edu={edu}, solo={solo}")
        result = super().predict_logistic(age, sex, edu, solo)
        logger.info(f"Logistic Regression prediction result: {result:.4f}")
        return result

    def calculate_score(self, age, male, edu, solo, csv_path, debug=False):
        logger.info("Starting score calculation...")
        try:
            result = super().calculate_score(age, male, edu, solo, csv_path, debug)
            logger.info(f"Score calculation completed. Result: {result}")
            return result
        except Exception as e:
            logger.exception("Error occurred during score calculation")
            raise


if __name__ == "__main__":
    p = PredictorWithLogging(
        lgb_models_dir_path="models/lgb/*.txt",
        logi_models_dir_path="models/logistic/*.pkl",
        lgb_scaler_path="scaler/lgb_scaler.pickle",
        logi_scaler_path="scaler/logi_scaler.pickle"
    )
    # arguments
    age = 100
    male = 0
    edu = 6
    solo = 1
    csv_path = "data/test_data.csv"
    print(p.calculate_score(age, male, edu, solo, csv_path, debug=False))