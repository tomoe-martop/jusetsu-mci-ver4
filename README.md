# jusetsu-mci-ver4

## 単発での実行方法
ローカルで。
```
$ docker-compose run --rm python
```
サーバーで。
```
$ sudo docker-compose run --rm python
```
## 実行を継続する場合は、cronを利用
```
$ chmod 755 main.sh
$ sudo cp /etc/crontab /etc/cron.d/crontab
$ sudo vi /etc/cron.d/crontab
```
### 設定例(debianの場合)
```
* * * * * ユーザー名 /bin/bash /opt/jusetsu-mci-ver2/main.sh >> /opt/jusetsu-mci-ver2/log/cron.log 2>&1
```
### 念の為cron再起動
```
$ sudo service cron restart
```
