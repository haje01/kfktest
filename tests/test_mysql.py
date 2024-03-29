import time
import json
from datetime import datetime
from multiprocessing import Process

import pytest
from mysql.connector import connect

from kfktest.table import reset_table
from kfktest.inserter import insert
from kfktest.util import (SSH, count_topic_message, ssh_exec, stop_kafka_broker,
    start_kafka_broker, kill_proc_by_port, vm_start, vm_stop, vm_hibernate,
    get_kafka_ssh, stop_kafka_and_connect, restart_kafka_and_connect, linfo,
    count_table_row, DB_PRE_ROWS, NUM_SEL_PROCS,  NUM_INS_PROCS,
    local_insert_proc, local_select_proc, remote_insert_proc,
    remote_select_proc, DB_ROWS, rot_insert_proc, rot_table_proc,
    KFKTEST_S3_BUCKET, KFKTEST_S3_DIR, s3_count_sinkmsg,
    # 픽스쳐들
    xsetup, xjdbc, xcp_setup, xtable, xkafka, xzookeeper, xkvmstart,
    xconn, xkfssh, xdbzm, xrmcons, xhash, xcdc, xtopic, xs3sink, xs3rmdir
    )


@pytest.fixture(scope="session")
def xprofile():
    return 'mysql'


def test_ct_local_basic(xjdbc, xkfssh, xsetup, xprofile):
    """로컬 insert / select 로 기본적인 Change Tracking 테스트.

    - 테스트 시작전 이전 토픽을 참고하는 것이 없어야 함. (delete_topic 에러 발생)

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=local_select_proc, args=(xprofile, pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    assert DB_ROWS == cnt


@pytest.mark.parametrize('xjdbc', [{
        'poll_interval': 5000,
        'batch_rows': 1000,
    }], indirect=True)
def test_ct_speed(xjdbc, xsetup, xprofile, xkfssh):
    """Change Tracking 으로 카프카에 가져오는 속도 측정.

    - poll.interval.ms / batch.max.rows 를 바꿔가며 테스트
    - poll.interval.ms 가 작고, batch.max.rows 가 클수록 DB 에서 데이터를 빠르게 가져온다.

    """
    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    ## batch.max.rows 바꿔가며 가져오는 속도 측정
    cnt = 0
    st = time.time()
    while cnt != DB_ROWS:
        cnt = count_topic_message(xprofile, f'{xprofile}_person')
        print(cnt)
        time.sleep(1)
    print(f"Sync in {time.time() - st:.1f} seconds")


def test_ct_broker_stop(xsetup, xjdbc, xkfssh, xprofile, xhash):
    """카프카 브로커 정상 정지시 Change Tracking 테스트.

    - 기본적으로 Insert / Select 시작 후 브로커를 멈추고 (Stop Gracefully) 다시
        기동해도 메시지 수가 일치해야 한다.
    - 그러나, 커넥터가 메시지 생성 ~ 오프셋 커밋 사이에 죽으면, 재기동시
        커밋하지 않은 오프셋부터 다시 처리하게 되어 메시지 중복이 발생할 수 있다.
    - Debezium 도 Exactly Once Semantics 가 아닌 At Least Once 를 지원
    - KIP-618 (Exactly-Once Support for Source Connectors) 에서 이것을 해결하려 함
        - 중복이 없게 하려면 Kafka Connect 를 Transactional Producer 로 구현해야
    - 참고:
        - https://stackoverflow.com/questions/59785863/exactly-once-semantics-in-kafka-source-connector
        - https://camel-context.tistory.com/54

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=local_select_proc, args=(xprofile, pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    time.sleep(5)
    # 의존성을 고려해 카프카 브로커와 커넥트 정지
    stop_kafka_and_connect(xprofile, xkfssh, xhash)
    # 의존성을 고려해 카프카 브로커와 커넥트 재개
    restart_kafka_and_connect(xprofile, xkfssh, xhash, False)

    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    # 정지 시점에 따라 중복 발생 가능
    assert DB_ROWS + DB_PRE_ROWS <= cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_ct_broker_kill(xsetup, xjdbc, xkfssh, xprofile):
    """카프카 브로커 다운시 Change Tracking 테스트.

    Insert / Select 시작 후 브로커 프로세스를 강제로 죽인 후, 잠시 후 다시 재개해도
        메시지 수가 일치.

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=local_select_proc, args=(xprofile, pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    # 잠시 후 카프카 브로커 강제 종료
    time.sleep(1)
    kill_proc_by_port(xkfssh, 9092)
    # 잠시 후 카프카 브로커 start
    time.sleep(1)
    start_kafka_broker(xkfssh)

    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    # 브로커만 강제 Kill 된 경우, 커넥터가 offset 을 flush 하지 못해 다시 시도
    # -> 중복 메시지 발생 가능!
    assert DB_ROWS + DB_PRE_ROWS <= cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_ct_broker_vmstop(xsetup, xjdbc, xkfssh, xprofile):
    """카프카 브로커 VM 정지시 Change Tracking 테스트.

    Insert / Select 시작 후 브로커가 정지 후 재개해도 메시지 수가 일치.

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=local_select_proc, args=(xprofile, pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    # 잠시 후 카프카 브로커 VM 정지 + 재시작
    time.sleep(2)
    vm_stop(xprofile, 'kafka')
    vm_start(xprofile, 'kafka')

    # Reboot 후 ssh 객체 재생성 필요!
    kfssh = get_kafka_ssh(xprofile)
    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    assert DB_ROWS + DB_PRE_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


# @pytest.mark.skip(reason="시간이 많이 걸림.")
# def test_ct_broker_hibernate(xsetup, xjdbc, xkfssh, xprofile):
#     """카프카 브로커 VM Hibernate 시 Change Tracking 테스트.

#     Insert / Select 시작 후 브로커 Hibernate 후 재개해도 메시지 수가 일치.

#     """
#     # Selector 프로세스들 시작
#     sel_pros = []
#     for pid in range(1, NUM_SEL_PROCS + 1):
#         # select 프로세스
#         p = Process(target=local_select_proc, args=(xprofile, pid,))
#         sel_pros.append(p)
#         p.start()

#     # Insert 프로세스들 시작
#     ins_pros = []
#     for pid in range(1, NUM_INS_PROCS + 1):
#         # insert 프로세스
#         p = Process(target=local_insert_proc, args=(xprofile, pid))
#         ins_pros.append(p)
#         p.start()

#     # 잠시 후 카프카 브로커 VM 정지 + 재시작
#     vm_hibernate(xprofile, 'kafka')
#     linfo("=== wait for a while ===")
#     time.sleep(5)
#     vm_start(xprofile, 'kafka')

#     # reboot 후 ssh 객체 재생성 필요!
#     kfssh = get_kafka_ssh(xprofile)
#     # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
#     cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
#     assert DB_ROWS + DB_PRE_ROWS == cnt

#     for p in ins_pros:
#         p.join()
#     linfo("All insert processes are done.")

#     for p in sel_pros:
#         p.join()
#     linfo("All select processes are done.")


def test_db(xcp_setup, xprofile, xkfssh, xtable):
    """DB 기본성능 확인을 위해 원격 insert / select 만 수행."""
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=remote_select_proc, args=(xprofile, xcp_setup, pid))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=remote_insert_proc, args=(xprofile, xcp_setup, pid))
        ins_pros.append(p)
        p.start()

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")

    # 테이블 행수 확인
    cnt = count_table_row(xprofile)
    assert DB_ROWS + DB_PRE_ROWS == cnt


def test_ct_remote_basic(xcp_setup, xprofile, xkfssh, xjdbc):
    """원격 insert / select 로 기본적인 Change Tracking 테스트.

    - Inserter / Selector 출력은 count 가 끝난 뒤 몰아서 나옴.
    - 가끔씩 1~4 개 정도 메시지 손실이 있는듯?

    """
    time.sleep(5)
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=remote_select_proc, args=(xprofile, xcp_setup, pid))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=remote_insert_proc, args=(xprofile, xcp_setup, pid))
        ins_pros.append(p)
        p.start()

    # 이것이 없으면 일부 메시지 유실 발생?!
    time.sleep(5)
    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=20)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_cdc_local_basic(xdbzm, xkfssh, xsetup, xprofile):
    """로컬 insert / select 로 기본적인 Change Data Capture 테스트.

    - 테스트 시작전 이전 토픽을 참고하는 것이 없어야 함. (delete_topic 에러 발생)

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=local_select_proc, args=(xprofile, pid,))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=local_insert_proc, args=(xprofile, pid))
        ins_pros.append(p)
        p.start()

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'db1.test.person', timeout=20)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_cdc_remote_basic(xcp_setup, xdbzm, xprofile, xkfssh, xcdc):
    """원격 insert / select 로 기본적인 Change Data Capture 테스트.

    - 테스트 시작전 이전 토픽을 참고하는 것이 없어야 함. (delete_topic 에러 발생)
    - Inserter / Selector 출력은 count 가 끝난 뒤 몰아서 나옴.

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # select 프로세스
        p = Process(target=remote_select_proc, args=(xprofile, xcp_setup, pid))
        sel_pros.append(p)
        p.start()

    # Insert 프로세스들 시작
    ins_pros = []
    for pid in range(1, NUM_INS_PROCS + 1):
        # insert 프로세스
        p = Process(target=remote_insert_proc, args=(xprofile, xcp_setup, pid))
        ins_pros.append(p)
        p.start()

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'db1.test.person', timeout=10)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


CTR_ROTATION = 1  # 로테이션 수
CTR_INSERTS = 65  # 로테이션 수 이상 메시지 인서트
CTR_BATCH = 100

@pytest.mark.parametrize('xjdbc', [{
        'query': """
            SELECT * FROM (
                -- 1분 단위 테이블 로테이션
                SELECT * FROM person_{{ MinAddFmt -1 ddHHmm }}
                UNION ALL
                SELECT * FROM person
            ) AS T
            -----
            SELECT * FROM person
        """,
        'query_topic': 'mysql_person',
        'inc_col': 'id',
        'ts_col': 'regdt',
    }], indirect=True)
@pytest.mark.parametrize('xs3sink', [{'flush_size': CTR_BATCH * 5}], indirect=True)
def test_ct_rtbl_incts(xcp_setup, xjdbc, xs3sink, xtable, xprofile, xtopic, xkfssh):
    """로테이션되는 테이블을 Dynamic SQL 쿼리로 가져오기.

    Incremental + Timestamp 컬럼 이용하는 경우

    동기:
    - DB 에서 로테이션되는 테이블을 CT 방식으로 가져오는 경우
    - 현재 테이블과 전 테이블을 가져오는 방식은 각각의 토픽이 되기에 문제
      - 전 테이블의 모든 메시지를 다시 가져옴
    - 쿼리 방식은 쿼리가 바뀌어도 하나의 토픽에 저장가능

    테스트 구현:
    - 쿼리에 매크로를 지원하는 수정된 JDBC 커넥터 (github.com/haje01/kafka-connect-jdbc) 를 이용
    - 로그 생성기는 별도 프로세스로 지속
    - 또 다른 프로세스에서 샘플 로그 테이블 1분 간격으로 로테이션
        예) 현재 11 일 0시 0분 1초인 경우
        person (현재), person_102359 (전날 23시 59분까지 로테이션)

    """
    # insert 프로세스 시작
    pi = Process(target=rot_insert_proc, args=(xprofile, CTR_INSERTS, CTR_BATCH))
    pi.start()

    # rotation 프로세스 시작
    pr = Process(target=rot_table_proc, args=(xprofile, CTR_ROTATION))
    pr.start()

    pi.join()
    pr.join()

    time.sleep(7)

    # 토픽 메시지 수와 DB 행 수는 같아야 한다
    count = count_topic_message(xprofile, f'{xprofile}_person')
    # assert CTR_INSERTS * CTR_BATCH == count
    linfo(f"Orignal Messages: {CTR_INSERTS * CTR_BATCH}, Topic Messages: {count}")

    # 빠진 ID 가 없는지 확인
    cmd = f"kafka-console-consumer --bootstrap-server localhost:9092 --topic {xprofile}_person --from-beginning --timeout-ms 3000"
    ssh = get_kafka_ssh(xprofile)
    ret = ssh_exec(ssh, cmd)
    pids = set()
    for line in ret.split('\n'):
        try:
            data = json.loads(line)
        except json.decoder.JSONDecodeError:
            print(line)
            continue
        pid = data['pid']
        pids.add(pid)
    missed = sorted(set(range(CTR_INSERTS)) - pids)
    linfo(f"Check missing messages: {missed}")
    assert len(missed) == 0

    # rotate.schedule.interval.ms 가 지나도록 대기
    time.sleep(10)
    # S3 Sink 커넥터가 올린 내용 확인
    s3cnt = s3_count_sinkmsg(KFKTEST_S3_BUCKET, KFKTEST_S3_DIR + "/")
    linfo(f"Orignal Messages: {CTR_INSERTS * CTR_BATCH}, S3 Messages: {s3cnt}")
    assert CTR_INSERTS * CTR_BATCH <= s3cnt


@pytest.mark.parametrize('xjdbc', [{
        'ts_col': 'regdt',
        'ts_delay': 8000,  # 같은 타임스탬프의 메시지가 늦게 오는 최대 간격보다 충분히 커야
        'inc_col': None,
    }], indirect=True)
def test_ct_tsdelay(xprofile, xcp_setup, xjdbc, xtable):
    """JDBC 소스 커넥터에서 Timestamp 지연 기능 테스트.

    Timestamp 기준 트랜잭션이 완성되기를 기다림. 같은 시간에 여러 메시지가 들어올 때
    뒷 부분이 잘리는 문제 방지

    """
    dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    insert('mysql', epoch=1, batch=1, dev=True, dt=dt, show=True)
    time.sleep(5)
    insert('mysql', epoch=1, batch=1, dev=True, dt=dt, show=True)

    # 카프카 토픽 확인
    cnt = count_topic_message(xprofile, f'mysql_person', timeout=10)
    assert 2 == cnt