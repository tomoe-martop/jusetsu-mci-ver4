ERROR_CODE_DICT = {
    100: "PredictionSuccess",
    200: "ElectricDataNotFound",
    201: "ElectricDataFormatError",
    202: "ElectricDataShortage",
    203: "ElectricDataEmpty",
    211: "BehaviorDataFormatError",
    300: "ElectricModelNotFound",
    301: "ElectricModelFormatError",
    302: "ElectricModelPredictError",
    310: "BehaviorModelNotFound",
    311: "BehaviorModelFormatError",
    312: "BehaviorModelPredictError",
    400: "PredictionTimeOut",
    900: "UnexpectedError",
}
TIMEOUT = 1

# Define a timeout handler for signal
def timeout_handler(signum, frame):
    raise PredictionTimeOut(400, f"Prediction timed out.")


# define original exception class
class MyException(Exception):
    """
    Custom exception class for handling specific errors.
    """
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = ERROR_CODE_DICT.get(self.status_code, 'UnknownError')


# Error code 200 seires
class InvalidInputError(MyException):
    """
    Exception raised for invalid input parameters.
    """
    def __str__(self):
        return f"{self.message} (status code: {self.status_code}, error code: {self.error_code})"

    def __repr__(self):
        return self.__str__()
    

# Error code 300 series
class PredictionError(MyException):
    """
    Exception raised for errors during prediction.
    """
    def __str__(self):
        return f"{self.message} (status code: {self.status_code}, error code: {self.error_code})"

    def __repr__(self):
        return self.__str__()


# Error code 400 series
class PredictionTimeOut(MyException):
    """
    Exception raised for prediction timeout errors.
    """
    def __str__(self):
        self.status_code = 400
        return f"{self.message} (status code: {self.status_code}, error code: {self.error_code})"

    def __repr__(self):
        self.status_code = 400
        return self.__str__()


# Error code 900 series
class UnexpectedError(MyException):
    """
    Exception raised for unexpected errors.
    """
    def __str__(self):
        return f"{self.message} (status code: {self.status_code}, error code: {self.error_code})"

    def __repr__(self):
        return self.__str__()