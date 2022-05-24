import time
import sys
import json
from pathlib import Path

import pymssql
from faker import Faker
from faker.providers import internet, date_time, company, phone_number

num_arg = len(sys.argv)
assert num_arg in (2, 5)
dev = num_arg == 2
setup = sys.argv[1]
pid = int(sys.argv[2]) if not dev else -1
epoch = sys.argv[3] if not dev else 100
batch = sys.argv[4] if not dev else 100

with open(setup, 'rt') as f:
    setup = json.loads(f.read())

print(f"Dev: {dev}")
print(f"Epoch: {epoch}")
print(f"Batch: {batch}")

ip = setup['sqlserver_public_ip'] if dev else setup['sqlserver_private_ip']
SERVER = ip['value']
# SERVER = setup['sqlserver_public_ip']['value']
USER = setup['db_user']['value']
PASSWD = setup['db_passwd']['value']
DATABASE = 'test'

print(f"{pid} Connect SQL Server at {SERVER}")
conn = pymssql.connect(SERVER, USER, PASSWD, DATABASE)
cursor = conn.cursor(as_dict=True)
print("Done")

fake = Faker()
fake.add_provider(internet)
fake.add_provider(date_time)
fake.add_provider(company)
fake.add_provider(phone_number)

st = time.time()
for j in range(epoch):
    print(f"Epoch: {j+1}")
    rows = []
    for i in range(batch):
        row = (
            pid,
            j * batch + i,
            fake.name(),
            fake.address(),
            fake.ipv4_public(),
            fake.date(),
            fake.company(),
            fake.phone_number()
        )
        rows.append(row)
    cursor.executemany("INSERT INTO person VALUES(%d, %d, %s, %s, %s, %s, %s, %s)",
        rows)
    conn.commit()

conn.close()

elapsed = time.time() - st
vel = epoch * batch / elapsed
print(f"Total {batch * epoch} rows, {int(vel)} rows per seconds with batch of {batch}.")