import os
import io
import time
import sys
import argparse
import json

import pymssql
from mysql.connector import connect

from kfktest.util import insert_fake, load_setup, DB_BATCH, DB_EPOCH, linfo

# CLI 용 파서
parser = argparse.ArgumentParser(description="MySQL DB 에 가짜 데이터 인서트.",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
)
parser.add_argument('db_type', type=str, choices=['mysql', 'mssql'], help="DBMS 종류.")
parser.add_argument('--db-name', type=str, default='test', help="이용할 데이터베이스 이름.")
parser.add_argument('-p', '--pid', type=int, default=0, help="인서트 프로세스 ID.")
parser.add_argument('-e', '--epoch', type=int, default=DB_EPOCH, help="에포크 수.")
parser.add_argument('-b', '--batch', type=int, default=DB_BATCH, help="에포크당 행수.")
parser.add_argument('-d', '--dev', action='store_true', default=False,
    help="개발 PC 에서 실행.")
parser.add_argument('-n', '--no-result', action='store_true', default=False,
    help="출력 감추기.")


def insert(db_type, db_name=parser.get_default('db_name'),
        epoch=parser.get_default('epoch'),
        batch=parser.get_default('batch'),
        pid=parser.get_default('pid'),
        dev=parser.get_default('devs'),
        no_result=parser.get_default('no_result')
        ):
    """가짜 데이터 인서트.

    `db_name` DB 에 `person` 테이블이 미리 만들어져 있어야 함.

    Args:
        db_type (str): DBMS 종류. mysql / mssql
        db_name (str): DB 이름
        epoch (int): 에포크 수
        batch (int): 에포크당 배치 수
        pid (int): 멀티 프로세스 인서트시 구분용 ID
        dev (bool): 개발 PC 에서 실행 여부
        no_result (bool): 결과 감추기 여부. 기본값 True

    """
    setup = load_setup(db_type)
    if db_type == 'mysql':
        db_ip_key = 'mysql_public_ip' if dev else 'mysql_private_ip'
    else:
        db_ip_key = 'mssql_public_ip' if dev else 'mssql_private_ip'
    db_host = setup[db_ip_key]['value']
    db_user = setup['db_user']['value']
    db_passwd = setup['db_passwd']['value']['result']

    linfo(f"Inserter {pid} connect DB at {db_host}")
    if db_type == 'mysql':
        conn = connect(host=db_host, user=db_user, password=db_passwd, db=db_name)
    else:
        conn = pymssql.connect(host=db_host, user=db_user, password=db_passwd, database=db_name)
    cursor = conn.cursor()
    linfo("Connect done.")

    st = time.time()
    insert_fake(conn, cursor, epoch, batch, pid, db_type)
    conn.close()

    elapsed = time.time() - st
    vel = epoch * batch / elapsed
    if not no_result:
        linfo(f"Inserter {pid} inserted {batch * epoch} rows. {int(vel)} rows per seconds with batch of {batch}.")


if __name__ == '__main__':
    args = parser.parse_args()
    insert(args.db_type, args.db_name, args.epoch, args.batch,
        args.pid, args.dev, args.no_result)