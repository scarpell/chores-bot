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
    """Return the set of user IDs that have a skip active on or after the given date."""
    return {
      e['user_id'] for e in self.skips
      if datetime.date.fromisoformat(e['date']) >= date
    }

  def _prune_expired_skips(self) -> bool:
    """Remove skip entries whose date has already passed.

    A skip is active on the specific date it is assigned. It expires at midnight
    when that date is no longer today.

    Returns:
      bool: True if any entries were removed, False otherwise.
    """
    today = datetime.date.today()
    before = len(self.skips)
    self.skips = [
      e for e in self.skips
      if datetime.date.fromisoformat(e['date']) >= today
    ]
    pruned = len(self.skips) < before
    if pruned:
      self._logger.info(
        'Pruned {} expired skip(s).'.format(before - len(self.skips)))
    return pruned

  def _prune_expired_swaps(self) -> bool:
    """Remove swap entries whose date has already passed.

    A swap is active on the day it is assigned. It expires at midnight when
    that date is no longer today.

    Returns:
      bool: True if any entries were removed, False otherwise.
    """
    today = datetime.date.today()
    before = len(self.swaps)
    self.swaps = [
      e for e in self.swaps
      if datetime.date.fromisoformat(e['date']) >= today
    ]
    pruned = len(self.swaps) < before
    if pruned:
      self._logger.info(
        'Pruned {} expired swap(s).'.format(before - len(self.swaps)))
    return pruned

  def _get_effective_user_for_date(self, date: datetime.date) -> discord.Member:
    """Return the user for a date using the skip-aware rotation, ignoring swaps.

    Accounts for active skips (so the available pool is the same as it would be
    during schedule computation) but does not consult the swaps list.  Used to
    determine which dates a member 'owns' when checking for redundant or
    member-linked swap entries.
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
    user list. If a file exists, all state (queue, start_date, swaps, skips)
    is restored from it, the queue is reconciled with the current user list,
    expired entries are pruned, and any changes are persisted.

    When a file from an older version is detected (missing start_date), only
    the queue order is preserved. Swaps and skips are discarded and the
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
        # discard swaps and skips rather than attempting conversion.
        self._logger.info(
          'Legacy state file detected. Preserving queue order; '
          'resetting swaps, skips, and start_date to today.')
        raw_queue = data.get('base_queue', data.get('queue', []))
        saved_ids = [int(item['id']) if isinstance(item, dict) else int(item) for item in raw_queue]
        user_map = {u.id: u for u in users}
        saved_ids_set = set(saved_ids)
        self.base_queue = (
          [user_map[uid] for uid in saved_ids if uid in user_map]
          + [u for u in users if u.id not in saved_ids_set]
        )
        self.start_date = datetime.date.today().isoformat()
        self.swaps = []
        self.skips = []
        self.save_state()
        return

      # -- Current format ----------------------------------------------
      self.start_date = data['start_date']
      
      self.swaps = []
      for e in data.get('swaps', []):
        if isinstance(e, dict) and 'user_id' in e:
          self.swaps.append({
            'date': e['date'],
            'user_id': int(e['user_id']),
            'name': e.get('name', '')
          })

      state_changed = False
      raw_skips = data.get('skips', [])
      self.skips = []
      for entry in raw_skips:
        if isinstance(entry, dict) and 'date' in entry and 'user_id' in entry:
          self.skips.append({
            'user_id': int(entry['user_id']),
            'date': entry['date'],
            'name': entry.get('name', '')
          })
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
        self.swaps = [e for e in self.swaps if e['user_id'] not in removed_ids_set]
        self.skips = [e for e in self.skips if e['user_id'] not in removed_ids_set]

      # Prune expired entries now that the queue is fully rebuilt
      if self._prune_expired_swaps():
        state_changed = True
      if self._prune_expired_skips():
        state_changed = True

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
    self.swaps = []
    self.skips = []

  def save_state(self):
    """Save the schedule state to disk."""
    try:
      # Helper to find a name by user_id
      def get_name(user_id):
        user = next((u for u in self.base_queue if u.id == user_id), None)
        return user.name if user else "Unknown"

      formatted_swaps = []
      for entry in self.swaps:
        formatted_swaps.append({
          'date': entry['date'],
          'user_id': entry['user_id'],
          'name': get_name(entry['user_id'])
        })

      formatted_skips = []
      for entry in self.skips:
        formatted_skips.append({
          'user_id': entry['user_id'],
          'name': get_name(entry['user_id']),
          'date': entry['date']
        })

      data = {
        'start_date': self.start_date,
        'swaps': formatted_swaps,
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
    target_date_str = target_date.isoformat()

    # Check for a swap on this specific date
    for entry in self.swaps:
      if entry['date'] == target_date_str:
        user = next((u for u in self.base_queue if u.id == entry['user_id']), None)
        if user:
          return user

    # No swap — compute from rotation, excluding currently-skipped users
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
    people_line2 = []

    next_7_dates = {(today + datetime.timedelta(days=j)).isoformat() for j in range(7)}
    any_swaps = any(e['date'] in next_7_dates for e in self.swaps)

    for i in range(7):
      target_date = today + datetime.timedelta(days=i)
      date_str = '{} {}'.format(target_date.strftime('%B'), target_date.day)

      day_str = calendar.day_abbr[target_date.weekday()]
      if i == 0:
        day_str += ' (today)'

      user = self.get_user_for_day(i)
      user_str = util.discord_name(user)

      is_swapped = any(e['date'] == target_date.isoformat() for e in self.swaps)
      swap_str = '(swapped)' if is_swapped else ''

      col_width = max(len(date_str), len(day_str), len(user_str), len(swap_str)) + 2

      headers_line1.append(' {} '.format(date_str).center(col_width))
      headers_line2.append(' {} '.format(day_str).center(col_width))
      people_line1.append(' {} '.format(user_str).center(col_width))
      if any_swaps:
        people_line2.append(' {} '.format(swap_str).center(col_width))

    header_sep = '+' + '+'.join('-' * len(h) for h in headers_line1) + '+'

    table_str = 'Dishes Schedule\n'
    table_str += '=' * 15 + '\n\n'
    table_str += '|' + '|'.join(headers_line1) + '|\n'
    table_str += '|' + '|'.join(headers_line2) + '|\n'
    table_str += header_sep + '\n'
    table_str += '|' + '|'.join(people_line1) + '|\n'
    if any_swaps:
      table_str += '|' + '|'.join(people_line2) + '|\n'

    if self.skips:
      table_str += '\nSkipped members:\n'
      for entry in self.skips:
        uid = entry['user_id']
        date_str = entry['date']
        user_obj = next((u for u in self.base_queue if u.id == uid), None)
        name = util.discord_name(user_obj) if user_obj else str(uid)
        table_str += '- {} (on {})\n'.format(name, date_str)
      table_str += '\nRun `!skip reset` to reset all skipped entries.\n'

    # Footer matches the table: only when swaps fall within the 7-day window
    if any_swaps:
      table_str += '\nRun `!swap reset` to reset all swapped entries.\n'

    return table_str

  def get_next_appearance_date(self, member: discord.Member) -> str:
    """Return the date string (YYYY-MM-DD) of the member's next on-call day."""
    for i in range(len(self.base_queue) * 2 + 1):
      if self.get_user_for_day(i).id == member.id:
        return (datetime.date.today() + datetime.timedelta(days=i)).isoformat()
    raise ValueError(
      '{} does not appear in the upcoming schedule.'.format(
        util.discord_name(member)))

  # ------------------------------------------------------------------
  # Mutations
  # ------------------------------------------------------------------

  def swap(self, mem1: discord.Member, mem2: discord.Member):
    """Swap two users' next on-call dates."""
    if mem1.id == mem2.id:
      raise ValueError('Members need to be different.')

    # Check if either member is currently skipped
    for mem in (mem1, mem2):
      if any(e['user_id'] == mem.id for e in self.skips):
        raise ValueError('{} is currently skipped and cannot be swapped.'.format(
          util.discord_name(mem)))

    # Check if either member is already involved in a swap
    # (Allowing the case where they are swapped with each other, which cancels the swap)
    swaps_mem1 = [e for e in self.swaps if e['user_id'] == mem1.id]
    swaps_mem2 = [e for e in self.swaps if e['user_id'] == mem2.id]
    
    is_swap_back = False
    if len(swaps_mem1) == 1 and len(swaps_mem2) == 1:
      entry1 = swaps_mem1[0]
      entry2 = swaps_mem2[0]
      owner1 = self._get_effective_user_for_date(datetime.date.fromisoformat(entry1['date']))
      owner2 = self._get_effective_user_for_date(datetime.date.fromisoformat(entry2['date']))
      if owner1.id == mem2.id and owner2.id == mem1.id:
        is_swap_back = True

    if not is_swap_back:
      for mem in (mem1, mem2):
        if any(e['user_id'] == mem.id for e in self.swaps):
          raise ValueError('{} is already swapped and cannot be swapped again.'.format(
            util.discord_name(mem)))

    date1_str = self.get_next_appearance_date(mem1)
    date2_str = self.get_next_appearance_date(mem2)

    # Remove any existing swap entries for these two dates before re-adding
    self.swaps = [e for e in self.swaps if e['date'] not in (date1_str, date2_str)]

    self.swaps.append({'date': date1_str, 'user_id': mem2.id})
    self.swaps.append({'date': date2_str, 'user_id': mem1.id})

    # If either new entry maps back to the skip-aware effective owner of that
    # date, the swap is a no-op (e.g. swapping back after a previous swap).
    for date_str in (date1_str, date2_str):
      date = datetime.date.fromisoformat(date_str)
      effective_user = self._get_effective_user_for_date(date)
      self.swaps = [
        e for e in self.swaps
        if not (e['date'] == date_str and e['user_id'] == effective_user.id)
      ]

    self._logger.info('Swapped {} ({}) and {} ({}).'.format(
      util.discord_name(mem1), date1_str, util.discord_name(mem2), date2_str))
    self.save_state()

  def skip(self, member: discord.Member) -> bool:
    """Skip the member for their next appearance, or remove their skip if one is already active.

    The skip is stored as {"user_id": <id>, "date": "<YYYY-MM-DD>"} where the date represents
    the specific day the member's turn is skipped.

    Returns:
      bool: True if newly skipped, False if the existing skip was removed.
    """
    # load_state() already pruned expired entries before this command ran,
    # so any entry found here is genuinely active.
    existing = next((e for e in self.skips if e['user_id'] == member.id), None)

    if existing is not None:
      self.skips.remove(existing)
      self._logger.info('Removed skip for {} (was on {}).'.format(
        util.discord_name(member), existing['date']))
      self.save_state()
      return False

    # Check if the member is currently swapped
    if any(e['user_id'] == member.id for e in self.swaps):
      raise ValueError('{} is currently swapped and cannot be skipped.'.format(
        util.discord_name(member)))

    next_appearance_date = self.get_next_appearance_date(member)

    self.skips.append({'user_id': member.id, 'date': next_appearance_date})
    self._logger.info('Skipped {} on {}.'.format(
      util.discord_name(member), next_appearance_date))
    self.save_state()
    return True

  def reset_skips(self):
    """Remove all skipped entries."""
    self.skips = []
    self._logger.info('Reset all skipped entries.')
    self.save_state()

  def reset_swaps(self):
    """Remove all swapped entries."""
    self.swaps = []
    self._logger.info('Reset all swapped entries.')
    self.save_state()