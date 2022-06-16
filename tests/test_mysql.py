import os
import time
from multiprocessing import Process

import pytest
from mysql.connector import connect

from kfktest.util import (SSH, register_socon, unregister_socon, list_socons,
    unregister_all_socons, count_topic_message, ssh_exec, local_exec,
    xsetup, xtable, xtopic, xsocon, xcp_setup
)
from kfktest.selector import select
from kfktest.inserter import insert

NUM_INSEL_PROCS = 5


@pytest.fixture(scope="session")
def xprofile():
    return 'mysql'


def _local_select_proc(pid):
    """로컬에서 가짜 데이터 셀렉트."""
    print(f"Select process {pid} start")
    select(db_type='mysql', pid=pid, dev=True)
    print(f"Select process {pid} done")


def _local_insert_proc(pid, epoch, batch):
    """로컬에서 가짜 데이터 인서트."""
    print(f"Insert process start: {pid}")
    insert(db_type='mysql', epoch=epoch, batch=batch, pid=pid, dev=True)
    print(f"Insert process done: {pid}")


def test_ct_local_basic(xsocon, xsetup):
    """로컬 insert / select 로 기본적인 Change Tracking 테스트.

    - 테스트 시작전 이전 토픽을 참고하는 것이 없어야 함. (delete_topic 에러 발생)

    """
    cons_ip = xsetup['consumer_public_ip']['value']
    kafka_ip = xsetup['kafka_private_ip']['value']
    ssh = SSH(cons_ip)

    # Selector 프로세스들 시작
    sel_pros = []
    sqs = []
    for pid in range(1, NUM_INSEL_PROCS + 1):
        # insert 프로세스
        p = Process(target=_local_select_proc, args=(pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INSEL_PROCS + 1):
        # insert 프로세스
        p = Process(target=_local_insert_proc, args=(pid, 100, 100))
        ins_pros.append(p)
        p.start()

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(ssh, kafka_ip, 'my-topic-person', timeout=25)
    assert 10000 * NUM_INSEL_PROCS == cnt

    for p in ins_pros:
        p.join()
    print("All insert processes are done.")

    for p in sel_pros:
        p.join()
    print("All select processes are done.")


def _remote_select_proc(xsetup, pid):
    """원격 셀렉트 노드에서 가짜 데이터 셀렉트 (원격 노드에 setup.json 있어야 함)."""
    print(f"Select process start: {pid}")
    sel_ip = xsetup['selector_public_ip']['value']
    ssh = SSH(sel_ip)
    cmd = f"cd kfktest/deploy/mysql && python3 -m kfktest.selector mysql -p {pid}"
    ret = ssh_exec(ssh, cmd, False)
    print(ret)
    print(f"Select process done: {pid}")
    return ret


def _remote_insert_proc(xsetup, pid, epoch, batch):
    """원격 인서트 노드에서 가짜 데이터 인서트 (원격 노드에 setup.json 있어야 함)."""
    print(f"Insert process start: {pid}")
    ins_ip = xsetup['inserter_public_ip']['value']
    ssh = SSH(ins_ip)
    cmd = f"cd kfktest/deploy/mysql && python3 -m kfktest.inserter mysql -p {pid} -e {epoch} -b {batch}"
    ret = ssh_exec(ssh, cmd, False)
    print(ret)
    print(f"Insert process done: {pid}")
    return ret


def test_ct_remote_basic(xcp_setup, xsocon):
    """원격 insert / select 로 기본적인 Change Tracking 테스트."""
    cons_ip = xcp_setup['consumer_public_ip']['value']
    kafka_ip = xcp_setup['kafka_private_ip']['value']
    ssh = SSH(cons_ip)

    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_INSEL_PROCS + 1):
        # insert 프로세스
        p = Process(target=_remote_select_proc, args=(xcp_setup, pid))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INSEL_PROCS + 1):
        # insert 프로세스
        p = Process(target=_remote_insert_proc, args=(xcp_setup, pid, 100, 100))
        ins_pros.append(p)
        p.start()

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(ssh, kafka_ip, 'my-topic-person', timeout=25)
    assert 10000 * NUM_INSEL_PROCS == cnt

    for p in ins_pros:
        p.join()
    print("All insert processes are done.")

    for p in sel_pros:
        p.join()
    print("All select processes are done.")
