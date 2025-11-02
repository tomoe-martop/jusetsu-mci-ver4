FROM python:3.8.19
RUN pip install mysql-connector-python==8.0.33
RUN pip install pandas==1.2.5
RUN pip install requests==2.31.0
RUN pip install optuna==3.1.1
RUN pip install lightgbm==3.3.5
RUN pip install numpy==1.20.0
