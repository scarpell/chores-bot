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
  # Helpers
  # ------------------------------------------------------------------

  def _skipped_user_ids_for_date(self, date: datetime.date) -> set:
    """Return the set of user IDs that have a skip active on the given date."""
    return {
      e['user_id'] for e in self.skips
      if datetime.date.fromisoformat(e['start_date']) <= date < datetime.date.fromisoformat(e['expiry_date'])
    }

  def _get_effective_user_for_date(self, date: datetime.date) -> discord.Member:
    """Return the user for a date using the skip-aware rotation.

    Accounts for active skips (so the available pool is the same as it would be
    during schedule computation).
    """
    if not self.base_queue:
      raise ValueError('No members in the schedule queue.')

    start = datetime.date.fromisoformat(self.start_date)
    if date < start:
      return self.base_queue[(date - start).days % len(self.base_queue)]

    days_elapsed = (date - start).days
    current_idx = 0
    for day in range(days_elapsed + 1):
      date_for_day = start + datetime.timedelta(days=day)
      while True:
        user = self.base_queue[current_idx % len(self.base_queue)]
        skipped_ids = self._skipped_user_ids_for_date(date_for_day)
        # If all queue members are skipped on this day, avoid infinite loop
        # by temporarily ignoring skips.
        if len(skipped_ids) >= len(self.base_queue):
          break
        if user.id not in skipped_ids:
          break
        current_idx += 1
      
      if day < days_elapsed:
        current_idx += 1

    return self.base_queue[current_idx % len(self.base_queue)]

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
    user list. If a file exists, all state (queue, start_date, skips)
    is restored from it, the queue is reconciled with the current user list,
    expired entries are pruned, and any changes are persisted.

    When a file from an older version is detected (missing start_date), only
    the queue order is preserved. Skips are discarded and the
    rotation restarts from today.
    """
    if not self._state_file.exists():
      self._init_fresh(users)
      self.save_state()
      return

    try:
      with open(self._state_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

      # -- Version detection -------------------------------------------
      if 'start_date' not in data:
        # Old format (day_index-based). Preserve only the queue order;
        # discard skips and start_date to today.
        self._logger.info(
          'Legacy state file detected. Preserving queue order; '
          'resetting skips and start_date to today.')
        raw_queue = data.get('base_queue', data.get('queue', []))
        saved_ids = [int(item['id']) if isinstance(item, dict) else int(item) for item in raw_queue]
        user_map = {u.id: u for u in users}
        saved_ids_set = set(saved_ids)
        self.base_queue = (
          [user_map[uid] for uid in saved_ids if uid in user_map]
          + [u for u in users if u.id not in saved_ids_set]
        )
        self.start_date = datetime.date.today().isoformat()
        self.skips = []
        self.save_state()
        return

      # -- Current format ----------------------------------------------
      self.start_date = data['start_date']
      
      state_changed = False
      raw_skips = data.get('skips', [])
      self.skips = []
      for entry in raw_skips:
        if isinstance(entry, dict) and 'user_id' in entry:
          expiry_date = entry.get('expiry_date')
          if not expiry_date:
            # Migrate legacy 'date' to 'expiry_date'
            expiry_date = entry.get('date')
            if expiry_date:
              state_changed = True
          
          start_date = entry.get('start_date')
          if not start_date:
            start_date = self.start_date
            state_changed = True
          
          if expiry_date:
            self.skips.append({
              'user_id': int(entry['user_id']),
              'start_date': start_date,
              'expiry_date': expiry_date,
              'name': entry.get('name', '')
            })
          else:
            state_changed = True
        else:
          state_changed = True

      # -- Base queue --------------------------------------------------
      raw_queue = data.get('base_queue', data.get('queue', []))
      saved_ids = [int(item['id']) if isinstance(item, dict) else int(item) for item in raw_queue]
      current_user_ids = {u.id for u in users}
      user_map = {u.id: u for u in users}

      removed_ids = [uid for uid in saved_ids if uid not in current_user_ids]
      saved_ids_set = set(saved_ids)
      added_users = [u for u in users if u.id not in saved_ids_set]

      # Rebuild queue in saved order, then append any new members
      new_queue = [user_map[uid] for uid in saved_ids if uid in user_map]
      new_queue += added_users
      self.base_queue = new_queue   # always assign, even if empty

      if removed_ids or added_users:
        state_changed = True
        self._logger.info(
          'Schedule updated: added users {}, removed user IDs {}'.format(
            [u.name for u in added_users], removed_ids))
        removed_ids_set = set(removed_ids)
        self.skips = [e for e in self.skips if e['user_id'] not in removed_ids_set]

      if state_changed:
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
    self.skips = []

  def save_state(self):
    """Save the schedule state to disk."""
    try:
      # Helper to find a name by user_id
      def get_name(user_id):
        user = next((u for u in self.base_queue if u.id == user_id), None)
        return user.name if user else "Unknown"

      formatted_skips = []
      for entry in self.skips:
        formatted_skips.append({
          'user_id': entry['user_id'],
          'name': get_name(entry['user_id']),
          'start_date': entry['start_date'],
          'expiry_date': entry['expiry_date']
        })

      data = {
        'start_date': self.start_date,
        'skips': formatted_skips,
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
    target_date = datetime.date.today() + datetime.timedelta(days=offset)
    return self._get_effective_user_for_date(target_date)

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

    headers_line1 = []
    headers_line2 = []
    people_line1 = []

    for i in range(7):
      target_date = today + datetime.timedelta(days=i)
      date_str = '{} {}'.format(target_date.strftime('%B'), target_date.day)

      day_str = calendar.day_abbr[target_date.weekday()]
      if i == 0:
        day_str += ' (today)'

      user = self.get_user_for_day(i)
      user_str = util.discord_name(user)

      col_width = max(len(date_str), len(day_str), len(user_str)) + 2

      headers_line1.append(' {} '.format(date_str).center(col_width))
      headers_line2.append(' {} '.format(day_str).center(col_width))
      people_line1.append(' {} '.format(user_str).center(col_width))

    header_sep = '+' + '+'.join('-' * len(h) for h in headers_line1) + '+'

    table_str = 'Dishes Schedule\n'
    table_str += '=' * 15 + '\n\n'
    table_str += '|' + '|'.join(headers_line1) + '|\n'
    table_str += '|' + '|'.join(headers_line2) + '|\n'
    table_str += header_sep + '\n'
    table_str += '|' + '|'.join(people_line1) + '|\n'

    active_skips = [
      e for e in self.skips
      if datetime.date.fromisoformat(e['expiry_date']) > today
    ]
    if active_skips:
      table_str += '\nSkipped members:\n'
      for entry in active_skips:
        uid = entry['user_id']
        date_str = entry['expiry_date']
        user_obj = next((u for u in self.base_queue if u.id == uid), None)
        name = util.discord_name(user_obj) if user_obj else str(uid)
        table_str += '- {} (until {})\n'.format(name, date_str)
      table_str += '\nRun `!skip reset` to reset all skipped entries.\n'

    return table_str

  # ------------------------------------------------------------------
  # Mutations
  # ------------------------------------------------------------------

  def skip(self, member: discord.Member) -> bool:
    """Skip the member, or remove their skip if one is already active.

    The skip is stored as {"user_id": <id>, "start_date": "<YYYY-MM-DD>", "expiry_date": "<YYYY-MM-DD>"}
    where the start_date is when the skip was set, and the expiry_date represents the day the skip
    expires (exclusive).

    Returns:
      bool: True if newly skipped, False if the existing active skip was removed.
    """
    today_date = datetime.date.today()
    existing = next((
      e for e in self.skips
      if e['user_id'] == member.id and datetime.date.fromisoformat(e['expiry_date']) > today_date
    ), None)

    if existing is not None:
      self.skips.remove(existing)
      self._logger.info('Removed active skip for {} (was until {}).'.format(
        util.discord_name(member), existing['expiry_date']))
      self.save_state()
      return False

    # Skip for the same number of days as there are people in the list
    num_members = len(self.base_queue)
    if num_members == 0:
      raise ValueError('No members in the schedule queue.')

    start_date = today_date.isoformat()
    expiry_date = (today_date + datetime.timedelta(days=num_members)).isoformat()

    self.skips.append({
      'user_id': member.id,
      'start_date': start_date,
      'expiry_date': expiry_date
    })
    self._logger.info('Skipped {} from {} until {}.'.format(
      util.discord_name(member), start_date, expiry_date))
    self.save_state()
    return True

  def reset_skips(self):
    """Remove all skipped entries."""
    self.skips = []
    self._logger.info('Reset all skipped entries.')
    self.save_state()