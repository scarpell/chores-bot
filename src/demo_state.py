import sys, json, datetime
sys.path.insert(0, '.')
import util

# Patch date so output is deterministic
import scheduler as sched_mod
_real_date = datetime.date
class FixedDate(datetime.date):
    @classmethod
    def today(cls): return _real_date(2026, 7, 12)
sched_mod.datetime.date = FixedDate

import scheduler

# Mock members
class M:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.nick = None

users = [M(1, 'A'), M(2, 'B'), M(3, 'C'), M(4, 'D'), M(5, 'E')]

# Clean state
state_file = util.get_data_folder() / 'schedule.json'
if state_file.exists():
    state_file.unlink()

# Boot the scheduler
sch = scheduler.Scheduler(logger_name='test')
sch.load_state(users)

print('=== After load_state ===')
print('member_list :', [u.id for u in sch.member_list])
print('rotation    :', [e['id'] for e in sch.rotation_users])
print('start_date  :', sch.rotation_start_date)
print('ext_index   :', sch.extension_index)
print()

print('=== Disk (schedule.json) after load_state ===')
with open(state_file) as f:
    data = json.load(f)
print(json.dumps(data, indent=2))
print()

print('=== generate_schedule() ===')
print(sch.generate_schedule())

print()
print('=== Disk (schedule.json) after generate_schedule ===')
with open(state_file) as f:
    data = json.load(f)
print(json.dumps(data, indent=2))
