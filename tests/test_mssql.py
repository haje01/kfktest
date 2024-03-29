import time
from multiprocessing import Process
import json
from datetime import datetime, timedelta

import pymssql
import pytest
from confluent_kafka import Consumer

from kfktest.table import reset_table
from kfktest.util import (count_topic_message, get_kafka_ssh,
    start_kafka_broker, kill_proc_by_port, vm_stop, vm_start,
    restart_kafka_and_connect, stop_kafka_and_connect, count_table_row,
    local_select_proc, local_insert_proc, linfo, NUM_INS_PROCS, NUM_SEL_PROCS,
    remote_insert_proc, remote_select_proc, DB_ROWS, load_setup, insert_fake,
    db_concur, ssh_exec, s3_count_sinkmsg, KFKTEST_S3_BUCKET,
    KFKTEST_S3_DIR, rot_table_proc, rot_insert_proc, new_consumer, consume_iter,
    # 픽스쳐들
    xsetup, xcp_setup, xjdbc, xtable, xkafka, xzookeeper, xkvmstart,
    xconn, xkfssh, xdbzm, xrmcons, xcdc, xhash, xtopic, xs3rmdir, xs3sink
    )


@pytest.fixture(scope="session")
def xprofile():
    return 'mssql'

def test_ct_local_basic(xsetup, xjdbc, xprofile, xkfssh):
    """로컬 insert / select 로 기본적인 Change Tracking 테스트."""
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # insert 프로세스
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

    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_ct_broker_stop(xsetup, xjdbc, xprofile, xkfssh, xhash):
    """카프카 브로커 정상 정지시 Change Tracking 테스트.

    - 기본적으로 Insert / Select 시작 후 브로커를 멈추고 (Stop Gracefully) 다시
        기동해도 메시지 수가 일치해야 한다.
    - 그러나, 커넥터가 메시지 생성 ~ 오프셋 커밋 사이에 죽으면, 재기동시
        커밋하지 않은 오프셋부터 다시 처리하게 되어 메시지 중복이 발생할 수 있다.
        => Log Compaction 사용으로 해결?
    - Graceful 한 정지시 메시지 생성을 정지하고 처리된 오프셋까지만 커밋하면 해결
        가능할 듯 한데..
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
        p = Process(target=local_select_proc, args=(xprofile, pid))
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
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_ct_broker_kill(xsetup, xjdbc, xprofile, xkfssh):
    """카프카 브로커 다운시 Change Tracking 테스트.

    Insert / Select 시작 후 브로커 프로세스를 강제로 죽인 후, 잠시 후 다시 재개해도
        메시지 수가 일치.

    """
    # Selector 프로세스들 시작
    sel_pros = []
    for pid in range(1, NUM_SEL_PROCS + 1):
        # insert 프로세스
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
    time.sleep(7)
    kill_proc_by_port(xkfssh, 9092)
    # 잠시 후 카프카 브로커 start
    time.sleep(10)
    start_kafka_broker(xkfssh)

    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=20)
    # 브로커만 강제 Kill 된 경우, 커넥터가 offset 을 flush 하지 못해 다시 시도
    # -> 중복 메시지 발생 가능!
    assert DB_ROWS <= cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_ct_broker_vmstop(xsetup, xjdbc, xprofile, xkfssh):
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
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=20)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


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
    assert DB_ROWS  == cnt


def test_ct_remote_basic(xcp_setup, xjdbc, xprofile, xkfssh):
    """원격 insert / select 로 기본적인 Change Tracking 테스트.

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

    time.sleep(10)

    # 카프카 토픽 확인 (timeout 되기전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'db1.dbo.person', timeout=30)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")


def test_cdc_remote_basic(xcp_setup, xdbzm, xprofile, xkfssh, xtable):
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
    cnt = count_topic_message(xprofile, f'db1.dbo.person', timeout=10)
    assert DB_ROWS == cnt

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    for p in sel_pros:
        p.join()
    linfo("All select processes are done.")

    linfo(f"CDC Test Elapsed: {time.time() - xtable:.2f}")


def test_ct_modify(xcp_setup, xjdbc, xtopic, xtable, xprofile, xkfssh):
    """CT 방식에서 기존 행이 변하는 경우 동작 확인.

    - CT 방식은 기존행이 변경 (update) 된 것은 전송하지 않는다.
        - 단 Incrementing+Timestamp 모드에서 타임스탬프가 갱신되면 가능
    - 새 행이 추가된 것은 잘 보냄
    - 테이블 초기화 후 새로 insert 하면 ID 가 같은 것은 기존 데이터를 유지하고
      새 ID 의 데이터는 가져옴

    """
    num_msg = 10
    # Insert 프로세스들 시작
    ins_pros = []
    # insert 프로세스
    p = Process(target=local_insert_proc, args=(xprofile, 1, 1, num_msg))
    ins_pros.append(p)
    p.start()

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    time.sleep(2)
    setup = load_setup('mssql')
    broker_addr = setup['kafka_public_ip']['value']
    broker_port = 19092

    cnt = 0
    org_name = None
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        cnt += 1
        if msg['id'] == 1:
            org_name = msg['name']

    assert cnt == num_msg

    # 일부 행을 변경
    db_host = setup['mssql_public_ip']['value']
    db_user = setup['db_user']['value']
    db_passwd = setup['db_passwd']['value']['result']
    db_name = 'test'

    conn = pymssql.connect(host=db_host, user=db_user, password=db_passwd, database=db_name)
    cursor = conn.cursor()
    linfo("Connect done.")
    sql = "UPDATE person SET name='MODIFIED' WHERE id=1"
    cursor.execute(sql)
    conn.commit()

    cnt = mcnt = 0
    fname = None
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        print(msg)
        cnt += 1
        if msg['id'] == 1:
            fname = msg['name']
            # Timestamp 가 업데이트되지 않아 갱신 없음
            assert fname == org_name
            mcnt += 1

    assert mcnt == 1
    # 새 행을 추가
    insert_fake(conn, cursor, 1, 1, 1, 'mssql')
    conn.close()

    cnt = mcnt = 0
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        cnt += 1
        if msg['id'] == 1:
            mcnt += 1

    assert cnt == 11
    assert mcnt == 1

    # 테이블을 리셋 후 행 추가 (rotation 흉내)
    conn, cursor = reset_table(xprofile, 'dbo.person')
    insert_fake(conn, cursor, 1, 15, 1, 'mssql')

    cnt = 0
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        cnt += 1
        if msg['id'] == 1:
            # 같은 ID 에 대해서는 기존 값이 그대로 옴
            assert fname == msg['name']

    # 새로 추가된 행은 들어옴
    assert cnt == 15


@pytest.mark.parametrize('xjdbc', [{'inc_col': 'id', 'ts_col': 'regdt'}], indirect=True)
def test_ct_modify2(xcp_setup, xjdbc, xtable, xtopic, xprofile, xkfssh):
    """CT 방식에서 Current Timestamp 를 쓸 때 기존 행이 변하는 경우 동작 확인.

    - Incremental 과 Timestamp 컬럼을 함께 쓰는 경우
    - update 를 하면서 Timestamp 컬럼을 갱신하면 커넥터는 그 행을 다시 가져온다.
    - 토픽에는 기존에 가져온 행 + 다시 가져온 행의 메시지가 있게 됨

    """
    num_msg = 10
    # Insert 프로세스들 시작
    ins_pros = []
    # insert 프로세스
    p = Process(target=local_insert_proc, args=(xprofile, 1, 1, num_msg))
    ins_pros.append(p)
    p.start()

    for p in ins_pros:
        p.join()
    linfo("All insert processes are done.")

    setup = load_setup('mssql')

    cnt = 0
    org_name = None
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        cnt += 1
        if msg['id'] == 1:
            org_name = msg['name']
    assert cnt == num_msg

    # 일부 행을 변경
    db_host = setup['mssql_public_ip']['value']
    db_user = setup['db_user']['value']
    db_passwd = setup['db_passwd']['value']['result']
    db_name = 'test'

    conn = pymssql.connect(host=db_host, user=db_user, password=db_passwd, database=db_name)
    cursor = conn.cursor()
    linfo("Connect done.")
    sql = "UPDATE person SET regdt=CURRENT_TIMESTAMP, name='MODIFIED' WHERE id=1"
    cursor.execute(sql)
    conn.commit()

    time.sleep(10)

    cnt = mcnt = 0
    fnames = []
    for msg in consume_iter(new_consumer(xprofile), [xtopic]):
        cnt += 1
        if msg['id'] == 1:
            fname = msg['name']
            fnames.append(fname)
    assert len(fnames) == 2
    assert 'MODIFIED' in fnames
    assert cnt == 11


CTR_ROTATION = 1  # 로테이션 수
CTR_INSERTS = 65  # 로테이션 수 이상 메시지 인서트
CTR_BATCH = 100

# 테스트 시작 후 분이 바뀔 때까지의 시간을 구해 매크로의 지연 시간으로 사용
ctr_query = """
SELECT * FROM (
    -- 1분 단위 테이블 로테이션
    SELECT * FROM person_{{ MinAddFmt -1 ddHHmm }}
    UNION ALL
    SELECT * FROM person
) AS T
-----
SELECT * FROM person
"""
@pytest.mark.parametrize('xjdbc', [{
        'query': ctr_query,
        'query_topic': 'mssql_person',
        'inc_col': 'pid',  # 로테이션이 되어도 유니크한 컬럼으로
    }], indirect=True)
def test_ct_rtbl_inc(xcp_setup, xjdbc, xtable, xprofile, xtopic, xkfssh):
    """로테이션되는 테이블을 Dynamic SQL 쿼리로 가져오기.

    Incremental 컬럼만 이용하는 경우

    동기:
    - MSSQL 에서 로테이션되는 테이블을 CT 방식으로 가져오는 경우
    - 현재 테이블과 전 테이블을 가져오는 방식은 각각의 토픽이 되기에 문제
      - 전 테이블의 모든 메시지를 다시 가져옴
    - 쿼리 방식은 쿼리가 바뀌어도 하나의 토픽에 저장가능

    테스트 구현:
    - 원래는 MSSQL 의 Dynamic SQL 을 이용해 이번 테이블과 전 테이블을 가져오는 쿼리를 작성하려 했으나
    - JDBC 커넥터에서 쿼리 끝에 자체적인 WHERE 조건을 붙여주기에 Dynamic SQL 을 사용할 수 없음
    - 쿼리에 매크로를 지원하는 수정된 JDBC 커넥터 (github.com/haje01/kafka-connect-jdbc) 를 이용
    - 로그 생성기는 별도 프로세스로 지속
    - 또 다른 프로세스에서 샘플 로그 테이블 1분 간격으로 로테이션
        예) 현재 11 일 0시 0분 1초인 경우
        person (현재), person_102359 (전날 23시 59분까지 로테이션)

    """
    # insert 프로세스 시작
    pi = Process(target=rot_insert_proc, args=('mssql', CTR_INSERTS, CTR_BATCH))
    pi.start()

    # rotation 프로세스 시작
    pr = Process(target=rot_table_proc, args=('mssql', CTR_ROTATION))
    pr.start()

    pi.join()
    pr.join()

    time.sleep(7)

    # 토픽 메시지 수와 DB 행 수는 같아야 한다
    count = count_topic_message('mssql', 'mssql_person')
    assert CTR_INSERTS * CTR_BATCH == count
    linfo(f"Orignal Messages: {CTR_INSERTS * CTR_BATCH}, Topic Messages: {count}")

    # 빠진 ID 가 없는지 확인
    cmd = "kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic mssql_person --from-beginning --timeout-ms 3000"
    ssh = get_kafka_ssh('mssql')
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
    missed = set(range(CTR_INSERTS)) - pids
    linfo(f"Check missing messages: {missed}")
    assert len(missed) == 0


@pytest.mark.parametrize('xjdbc', [{
        'query': ctr_query,
        'query_topic': 'mssql_person',
        'inc_col': 'id',
        'ts_col': 'regdt',
    }], indirect=True)
@pytest.mark.parametrize('xs3sink', [{'flush_size': CTR_BATCH * 5}], indirect=True)
def test_ct_rtbl_incts(xcp_setup, xjdbc, xs3sink, xtable, xprofile, xtopic, xkfssh):
    """로테이션되는 테이블을 Dynamic SQL 쿼리로 가져오기.

    Incremental + Timestamp 컬럼 이용하는 경우

    동기:
    - MSSQL 에서 로테이션되는 테이블을 CT 방식으로 가져오는 경우
    - 현재 테이블과 전 테이블을 가져오는 방식은 각각의 토픽이 되기에 문제
      - 전 테이블의 모든 메시지를 다시 가져옴
    - 쿼리 방식은 쿼리가 바뀌어도 하나의 토픽에 저장가능

    테스트 구현:
    - 원래는 MSSQL 의 Dynamic SQL 을 이용해 이번 테이블과 전 테이블을 가져오는 쿼리를 작성하려 했으나
    - JDBC 커넥터에서 쿼리 끝에 자체적인 WHERE 조건을 붙여주기에 Dynamic SQL 을 사용할 수 없음
    - 쿼리에 매크로를 지원하는 수정된 JDBC 커넥터 (github.com/haje01/kafka-connect-jdbc) 를 이용
    - 로그 생성기는 별도 프로세스로 지속
    - 또 다른 프로세스에서 샘플 로그 테이블 1분 간격으로 로테이션
        예) 현재 11 일 0시 0분 1초인 경우
        person (현재), person_102359 (전날 23시 59분까지 로테이션)

    """
    # insert 프로세스 시작
    pi = Process(target=rot_insert_proc, args=('mssql', CTR_INSERTS, CTR_BATCH))
    pi.start()

    # rotation 프로세스 시작
    pr = Process(target=rot_table_proc, args=(xprofile, CTR_ROTATION))
    pr.start()

    pi.join()
    pr.join()

    time.sleep(7)

    # 토픽 메시지 수와 DB 행 수는 같아야 한다
    count = count_topic_message('mssql', 'mssql_person')
    # assert CTR_INSERTS * CTR_BATCH == count
    linfo(f"Orignal Messages: {CTR_INSERTS * CTR_BATCH}, Topic Messages: {count}")

    # 빠진 ID 가 없는지 확인
    cmd = "kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic mssql_person --from-beginning --timeout-ms 3000"
    ssh = get_kafka_ssh('mssql')
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
    assert CTR_INSERTS * CTR_BATCH == s3cnt


@pytest.mark.parametrize('xjdbc', [{
        'inc_col': None,
        'ts_col': 'regdt',
        'poll_interval': 1000,
        'ts_delay': 1000
    }],
    indirect=True)
@pytest.mark.parametrize('xtable', [{'datetime1': False}], indirect=True)
def test_ct_tsloss(xcp_setup, xjdbc, xtable, xprofile, xtopic, xkfssh):
    """CT 방식에서 DATETIME2 타입 컬럼에 대한 Timestamp 모드의 유실 대응

    - 같은 시간에 메시지가 많은 경우 일부만 Ingest 한체 오프셋 커밋 가능
        -> 유실 발생
    - poll 인터벌이 작을 수록 (빈번하게 poll 할수록) 손실이 잦음
    - 이런 경우 timestamp.delay.interval.ms 를 이용해 타임스탬프 기준 트랜잭션이
      완성되기를 기다리면 유실 방지 가능

    주: DATETIME 컬럼에 대한 Timestamp 모드 문제
        DATETIME 을 사용하는 레가시 DB 에서 메시지가 더 이상 들어오지 않을 때
        poll 할 때마다 마지막 타임스탬프의 메시지들이 계속 중복해서 토픽에
        들어오는 문제가 있음.

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

    # 카프카 토픽 확인 (timeout 되기 전에 다 받아야 함)
    cnt = count_topic_message(xprofile, f'{xprofile}_person', timeout=10)
    assert DB_ROWS == cnt
