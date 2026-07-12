import unittest
from unittest import mock
import json
import os
import datetime

import scheduler
import util


class MockDate(datetime.date):
  _today_val = datetime.date(2026, 7, 6)

  @classmethod
  def today(cls):
    return cls._today_val


class MockMember:
  def __init__(self, id, name, nick=None):
    self.id = id
    self.name = name
    self.nick = nick


class SchedulerTestCase(unittest.TestCase):
  def setUp(self):
    self.users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(222, 'Bob'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(444, 'David')
    ]
    self.test_data_dir = util.get_data_folder()
    self.state_file = self.test_data_dir / 'schedule.json'
    if self.state_file.exists():
      os.remove(self.state_file)

  def tearDown(self):
    if self.state_file.exists():
      os.remove(self.state_file)

  def make_scheduler(self, users=None):
    """Create a scheduler and load state — mirrors the real bot startup."""
    sch = scheduler.Scheduler()
    sch.load_state(users if users is not None else self.users)
    return sch

  def today(self):
    return datetime.date.today()

  def date_offset(self, days):
    return (self.today() + datetime.timedelta(days=days)).isoformat()

  # ------------------------------------------------------------------
  # Basic rotation
  # ------------------------------------------------------------------

  def test_on_call(self):
    sch = self.make_scheduler()
    # Queue is [Alice, Bob, Charlie, David]; start_date = today → Alice is day 0
    self.assertEqual(sch.on_call.id, 111)

  def test_get_user_for_day(self):
    sch = self.make_scheduler()
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
    self.assertEqual(sch.get_user_for_day(2).id, 333)  # Charlie
    self.assertEqual(sch.get_user_for_day(3).id, 444)  # David
    self.assertEqual(sch.get_user_for_day(4).id, 111)  # Alice wraps

  def test_start_date_drives_rotation(self):
    """Rotation is anchored to start_date, not a counter."""
    sch = self.make_scheduler()
    self.assertEqual(sch.start_date, self.today().isoformat())
    # Verify that a scheduler loaded from the same file agrees
    sch2 = self.make_scheduler()
    self.assertEqual(sch2.start_date, sch.start_date)
    self.assertEqual(sch2.get_user_for_day(0).id, 111)


  # ------------------------------------------------------------------
  # User membership changes
  # ------------------------------------------------------------------

  def test_load_state_add_and_remove_users(self):
    sch = self.make_scheduler()
    self.assertEqual([u.id for u in sch.base_queue], [111, 222, 333, 444])

    new_users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(555, 'Eve')
    ]

    sch.load_state(new_users)

    self.assertEqual([u.id for u in sch.base_queue], [111, 333, 555])
    self.assertTrue(sch._state_file.exists())

  # ------------------------------------------------------------------
  # Serialization
  # ------------------------------------------------------------------

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_human_readable_serialization(self):
    """Verify that usernames are serialized with user IDs on disk."""
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    sch.save_state()
    
    # Read raw JSON from disk
    self.assertTrue(sch._state_file.exists())
    with open(sch._state_file, 'r', encoding='utf-8') as f:
      data = json.load(f)
      
    # Check base_queue
    base_queue_data = data.get('base_queue', [])
    self.assertEqual(len(base_queue_data), len(self.users))
    for entry in base_queue_data:
      self.assertIn('id', entry)
      self.assertIn('name', entry)
      orig_user = next(u for u in self.users if u.id == entry['id'])
      self.assertEqual(entry['name'], orig_user.name)

    # Now verify we can load it back correctly
    sch2 = self.make_scheduler()
    self.assertEqual([u.id for u in sch2.base_queue], [u.id for u in self.users])


if __name__ == '__main__':
  unittest.main()