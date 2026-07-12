from typing import List, Text

import calendar
import datetime
import discord
import json
import logging

import util


class Scheduler:
  def __init__(self, logger_name='discord'):
    """Set up scheduler infrastructure.

    Note: Does NOT initialize schedule state. Call load_state(users) before
    using any other method.

    Args:
      logger_name (str, optional): Logger's name. Defaults to 'discord'
    """
    self._logger = logging.getLogger(logger_name)
    self._state_file = util.get_data_folder() / 'schedule.json'

  # ------------------------------------------------------------------
  # Persistence
  # ------------------------------------------------------------------

  def load_state(self, users: List[discord.Member]):
    """Initialize or refresh all schedule state from disk.

    This is the canonical state initializer. It must be called before any
    other method is used. When called before every command and scheduled
    message it ensures the scheduler always runs against the latest on-disk
    state, so manual edits to the file are picked up automatically.

    If no state file exists, defaults are written to disk using the supplied
    user list. If a file exists, all state (queue, start_date)
    is restored from it, the queue is reconciled with the current user list,
    and any changes are persisted.

    When a file from an older version is detected (missing start_date), only
    the queue order is preserved and the rotation restarts from today.
    """
    if not self._state_file.exists():
      self._init_fresh(users)
      self.save_state()
      return

    try:
      with open(self._state_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

      self.start_date = data.get('start_date', datetime.date.today().isoformat())
      raw_queue = data.get('base_queue', data.get('queue', []))
      saved_ids = [int(item['id']) if isinstance(item, dict) else int(item) for item in raw_queue]
      user_map = {u.id: u for u in users}

      self.base_queue = [user_map[uid] for uid in saved_ids if uid in user_map]
      self.base_queue += [u for u in users if u.id not in set(saved_ids)]

      if 'start_date' not in data or set(saved_ids) != {u.id for u in users}:
        self.save_state()
      else:
        self._logger.info('Schedule state loaded from disk.')

    except Exception as e:
      self._logger.error(
        'Failed to load schedule state ({}). Starting fresh.'.format(e))
      self._init_fresh(users)
      self.save_state()

  def _init_fresh(self, users: List[discord.Member]):
    """Set all in-memory state to clean defaults from the supplied user list."""
    self.base_queue = users
    self.start_date = datetime.date.today().isoformat()

  def save_state(self):
    """Save the schedule state to disk."""
    try:
      data = {
        'start_date': self.start_date,
        'base_queue': [{'id': u.id, 'name': u.name} for u in self.base_queue]
      }
      with open(self._state_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    except Exception as e:
      self._logger.error('Failed to save schedule state: {}'.format(e))

  # ------------------------------------------------------------------
  # Schedule computation
  # ------------------------------------------------------------------

  def get_user_for_day(self, offset: int) -> discord.Member:
    if not self.base_queue:
      raise ValueError('No members in the schedule queue.')
    target_date = datetime.date.today() + datetime.timedelta(days=offset)
    start = datetime.date.fromisoformat(self.start_date)
    days_elapsed = (target_date - start).days
    return self.base_queue[days_elapsed % len(self.base_queue)]

  @property
  def on_call(self) -> discord.Member:
    """Returns:
      discord.Member: Which discord user is on call for the chores today.
    """
    return self.get_user_for_day(0)

  def generate_schedule(self) -> Text:
    """Generate a markdown representation of who is on call for the next 7 days.

    Returns:
      Text: User forecast for next 7 days, in a formatted ascii table.
    """
    if not self.base_queue:
      return 'No members in the schedule queue.'

    today = datetime.date.today()
    cols = []
    for i in range(7):
      target_date = today + datetime.timedelta(days=i)
      d_str = target_date.strftime('%B ') + str(target_date.day)
      day_str = calendar.day_abbr[target_date.weekday()] + (' (today)' if i == 0 else '')
      user_str = util.discord_name(self.get_user_for_day(i))
      w = max(len(d_str), len(day_str), len(user_str)) + 2
      cols.append((d_str.center(w), day_str.center(w), user_str.center(w), '-' * w))

    table_str = 'Dishes Schedule\n' + '=' * 15 + '\n\n'
    table_str += '|' + '|'.join(c[0] for c in cols) + '|\n'
    table_str += '|' + '|'.join(c[1] for c in cols) + '|\n'
    table_str += '+' + '+'.join(c[3] for c in cols) + '+\n'
    table_str += '|' + '|'.join(c[2] for c in cols) + '|\n'

    return table_str