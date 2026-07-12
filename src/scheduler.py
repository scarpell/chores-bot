from typing import List, Text

import calendar
import datetime
import discord
import json
import logging
import types

import util


def _make_user(entry: dict, is_active: bool, member=None):
    """Return a user-like SimpleNamespace for rotation resolution.

    Args:
      entry:     dict with 'id' and 'name' keys from rotation_users.
      is_active: False when the user no longer holds the chore role.
      member:    Live discord.Member (or MockMember in tests) when available,
                 used to surface the .nick field for active users.
    """
    return types.SimpleNamespace(
        id=entry['id'],
        name=entry['name'],
        nick=member.nick if member is not None else None,
        is_active=is_active,
    )


class Scheduler:
    def __init__(self, logger_name='discord'):
        """Set up scheduler infrastructure.

        Note: Does NOT initialize rotation state. Call load_state(users) before
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
        """Initialize or refresh all rotation state from disk.

        Two independent structures are maintained:

        member_list
            Ordered list of members who currently hold the chore role.
            Updated on every call: removed members are dropped in-place;
            newly-added members are appended to the end.

        rotation
            A rolling, day-by-day list of user assignments starting from
            rotation_start_date (always today after cleanup).

            The rotation is never re-written mid-stream; it only grows:
            - Days before today are trimmed; rotation_start_date advances.
            - The rotation always carries at least len(member_list) *active*
              entries (N/A slots do not count toward that minimum).
            - A member who loses the role stays in their assigned days
              (displayed as "Name (N/A)") but is excluded from extensions.
            - A member who gains the role is queued via next_extension_id and
              appears the next time their position comes up in the round-robin.

        next_extension_id tracks which member_list user to append next.
        It is stored as an ID (not an index) so that adding or removing
        members does not corrupt the round-robin position.

        If no state file exists both structures are initialised from the
        supplied user list. Legacy files (rotation or base_queue formats) are
        migrated automatically.
        """
        if not self._state_file.exists():
            self._init_fresh(users)
            self.save_state()
            return

        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            user_map = {u.id: u for u in users}

            # --- member_list -------------------------------------------
            saved_ml_entries = data.get('member_list', [])
            saved_ml_ids = [int(e['id']) for e in saved_ml_entries]
            # Preserve saved order; drop members who no longer have the role.
            self.member_list = [user_map[uid] for uid in saved_ml_ids if uid in user_map]
            # Append newly-granted (or re-granted) members to the end of the list.
            known_ids = set(saved_ml_ids)
            self.member_list += [u for u in users if u.id not in known_ids]

            # --- rotation block (with legacy fallback) ------------------
            rot = data.get('rotation') or {}
            self.rotation_start_date = rot.get(
                'start_date', datetime.date.today().isoformat())
            raw_users = rot.get('users', [])

            if raw_users:
                self.rotation_users = [
                    {'id': int(e['id']), 'name': e.get('name', str(e['id']))}
                    for e in raw_users
                ]
            else:
                # Legacy base_queue at top level, or completely empty state.
                legacy = data.get('base_queue', data.get('queue', []))
                if legacy:
                    self._logger.info('Migrating legacy base_queue format.')
                    self.rotation_users = [
                        {
                            'id': int(e['id'] if isinstance(e, dict) else e),
                            'name': (
                                e.get('name', str(e['id']))
                                if isinstance(e, dict)
                                else str(e)
                            ),
                        }
                        for e in legacy
                    ]
                else:
                    self.rotation_users = [
                        {'id': u.id, 'name': u.name} for u in self.member_list
                    ]
                self.rotation_start_date = datetime.date.today().isoformat()

            # Derive where to continue extending from the last active entry.
            self.extension_index = self._compute_extension_index()

            # Trim past days then ensure the rotation has enough active entries.
            self._cleanup_past_days()
            self._ensure_active_coverage()
            self.save_state()
            self._logger.info('Rotation state loaded from disk.')

        except Exception as e:
            self._logger.error(
                'Failed to load rotation state ({}). Starting fresh.'.format(e))
            self._init_fresh(users)
            self.save_state()

    def _compute_extension_index(self) -> int:
        """Derive extension_index from the last active entry in rotation_users.

        Walks rotation_users in reverse to find the most recently scheduled
        active user (one who is still in member_list), then returns the index
        of the *next* member in the round-robin.  Falls back to 0 when the
        rotation is empty or every entry is N/A.
        """
        if not self.member_list:
            return 0
        active_ids = {u.id for u in self.member_list}
        current_ids = [u.id for u in self.member_list]
        for entry in reversed(self.rotation_users):
            if entry['id'] in active_ids:
                return (current_ids.index(entry['id']) + 1) % len(self.member_list)
        return 0

    def _init_fresh(self, users: List[discord.Member]):
        """Set all in-memory state to clean defaults from the supplied user list."""
        self.member_list = list(users)
        self.rotation_users = [{'id': u.id, 'name': u.name} for u in users]
        self.rotation_start_date = datetime.date.today().isoformat()
        # After seeding one full cycle the round-robin restarts from index 0.
        self.extension_index = 0

    def save_state(self):
        """Persist member_list and rotation to disk."""
        try:
            data = {
                'member_list': [{'id': u.id, 'name': u.name} for u in self.member_list],
                'rotation': {
                    'start_date': self.rotation_start_date,
                    'users': self.rotation_users,
                },
            }
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._logger.error('Failed to save rotation state: {}'.format(e))

    # ------------------------------------------------------------------
    # Rotation maintenance
    # ------------------------------------------------------------------

    def _cleanup_past_days(self):
        """Remove days before today from rotation_users, advancing start_date."""
        today = datetime.date.today()
        start = datetime.date.fromisoformat(self.rotation_start_date)
        days_to_drop = (today - start).days

        if days_to_drop <= 0:
            return

        if days_to_drop >= len(self.rotation_users):
            self.rotation_users = []
        else:
            self.rotation_users = self.rotation_users[days_to_drop:]

        self.rotation_start_date = today.isoformat()
        self._logger.info(
            'Trimmed {} past day(s). Rotation start: {}.'.format(
                days_to_drop, self.rotation_start_date))

    def _ensure_active_coverage(self, min_active: int = None):
        """Extend rotation_users until it contains at least min_active entries
        from the current member_list (i.e. active, non-N/A entries).

        N/A entries (users who have since lost the role) occupy a slot in the
        rotation but do not count toward this minimum.  Extensions always draw
        from member_list in round-robin order starting at extension_index, so
        all newly-appended entries are active by definition.

        Default minimum is len(member_list) — one full active cycle ahead.
        """
        if not self.member_list:
            return
        if min_active is None:
            min_active = len(self.member_list)

        active_ids = {u.id for u in self.member_list}
        active_count = sum(1 for e in self.rotation_users if e['id'] in active_ids)

        while active_count < min_active:
            m = self.member_list[self.extension_index]
            self.rotation_users.append({'id': m.id, 'name': m.name})
            active_count += 1  # extensions always add an active user
            self.extension_index = (self.extension_index + 1) % len(self.member_list)

    # ------------------------------------------------------------------
    # Rotation computation
    # ------------------------------------------------------------------

    def get_user_for_day(self, offset: int):
        """Return the user-like object responsible for today + offset days.

        Extends the rotation on demand (by total count) if offset exceeds the
        current length, then saves state so the extension is persisted.

        Args:
          offset: Days from today (0 = today, 1 = tomorrow, …).

        Returns:
          SimpleNamespace with .id, .name, .nick, .is_active
        """
        if not self.rotation_users and not self.member_list:
            raise ValueError('No members in the rotation.')

        target_date = datetime.date.today() + datetime.timedelta(days=offset)
        start = datetime.date.fromisoformat(self.rotation_start_date)
        days_elapsed = (target_date - start).days

        if days_elapsed < 0:
            raise ValueError('Target date is before the rotation start date.')

        if days_elapsed >= len(self.rotation_users):
            # Extend by total count until the requested index is covered.
            while len(self.rotation_users) <= days_elapsed:
                m = self.member_list[self.extension_index]
                self.rotation_users.append({'id': m.id, 'name': m.name})
                self.extension_index = (self.extension_index + 1) % len(self.member_list)
            self.save_state()

        entry = self.rotation_users[days_elapsed]
        active_member_map = {u.id: u for u in self.member_list}
        is_active = entry['id'] in active_member_map
        member = active_member_map.get(entry['id'])
        return _make_user(entry, is_active, member)

    @property
    def on_call(self):
        """The user on call for today.

        Returns:
          SimpleNamespace with .id, .name, .nick, .is_active
        """
        return self.get_user_for_day(0)

    def generate_schedule(self) -> Text:
        """Generate a markdown table of who is on call for the next 7 days.

        Ensures the rotation carries at least 7 active entries before
        rendering, so the display window always has a full active pipeline.
        Users who no longer hold the chore role are shown as 'Name (N/A)'.

        Returns:
          Text: 7-day ASCII table.
        """
        if not self.rotation_users and not self.member_list:
            return 'No members in the rotation.'

        # Ensure at least 7 active entries exist in the rotation before
        # rendering.  This may extend beyond index 6 if there are N/A entries
        # in the first 7 slots.
        self._ensure_active_coverage(7)
        self.save_state()

        today = datetime.date.today()
        cols = []
        for i in range(7):
            target_date = today + datetime.timedelta(days=i)
            d_str = target_date.strftime('%B ') + str(target_date.day)
            day_str = calendar.day_abbr[target_date.weekday()] + (' (today)' if i == 0 else '')
            user = self.get_user_for_day(i)
            display = util.discord_name(user)
            if not user.is_active:
                display += ' (N/A)'
            w = max(len(d_str), len(day_str), len(display)) + 2
            cols.append((d_str.center(w), day_str.center(w), display.center(w), '-' * w))

        table_str = 'Dishes Schedule\n' + '=' * 15 + '\n\n'
        table_str += '|' + '|'.join(c[0] for c in cols) + '|\n'
        table_str += '|' + '|'.join(c[1] for c in cols) + '|\n'
        table_str += '+' + '+'.join(c[3] for c in cols) + '+\n'
        table_str += '|' + '|'.join(c[2] for c in cols) + '|\n'

        return table_str