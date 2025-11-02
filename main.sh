#!/bin/bash

# 二重起動チェック
# @see https://blog.denet.co.jp/cronsudo/
OLDEST=$(pgrep -fo $0)

PGID=$(echo $(ps -p $$ -o pgid | sed 's/[^0-9]//g'))
OLDPGID=$(echo $(ps -p $OLDEST -o pgid | sed 's/[^0-9]//g'))

if [ $PGID -ne $OLDPGID ] ; then
  echo "already started." >&2
  exit 9
fi

# docker container prune --force --filter "until=24h"
# docker image prune -a --force --filter "until=24h"

cd /opt/jusetsu-mci-ver3
docker-compose run --rm python 2>&1
