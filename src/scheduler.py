from typing import List, Text

import calendar
import datetime
import discord
import json
import logging
import types

import util


def _make_user(entry: dict, is_active: bool, member=None):
    """Return a user-like SimpleNamespace for schedule resolution.

    Args:
      entry:     dict with 'id' and 'name' keys from rotation_users / member_list.
      is_active: False when the user no longer holds the chore role.
      member:    Live discord.Member (or MockMember in tests) when available,
                 used to surface the .nick field for display.
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

        Two independent structures are maintained:

        member_list
            Ordered list of members who currently hold the chore role.
            Updated on every call: removed members are dropped in-place;
            newly-added members are appended to the end.

        rotation
            The in-progress rotation (start_date + ordered user snapshot).
            It is NOT modified when members join or leave:
            - A member who loses the role stays in rotation.users but is
              displayed as "Name (N/A)" in the schedule.
            - A member who gains the role is appended to member_list but will
              only appear in the *next* rotation.
            When the current rotation is exhausted (today >= start_date +
            len(rotation.users) days), a new rotation is started automatically
            from the current member_list.

        If no state file exists, both structures are initialised from the
        supplied user list. Legacy files (old base_queue format) are migrated:
        the saved queue order becomes the first rotation and start_date is
        reset to today.
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
            saved_entries = data.get('member_list', [])
            saved_ids = [int(e['id']) for e in saved_entries]
            # Preserve existing order; drop members who no longer have the role.
            self.member_list = [user_map[uid] for uid in saved_ids if uid in user_map]
            # Append members who were newly granted the role.
            known_ids = set(saved_ids)
            self.member_list += [u for u in users if u.id not in known_ids]

            # --- rotation -----------------------------------------------
            rotation = data.get('rotation', {})
            raw_rot_users = rotation.get('users', [])

            if raw_rot_users:
                self.rotation_start_date = rotation.get(
                    'start_date', datetime.date.today().isoformat())
                self.rotation_users = [
                    {'id': int(e['id']), 'name': e.get('name', str(e['id']))}
                    for e in raw_rot_users
                ]
            else:
                # Legacy format: old base_queue or queue key at top level.
                legacy_queue = data.get('base_queue', data.get('queue', []))
                if legacy_queue:
                    self._logger.info('Migrating legacy schedule format.')
                    self.rotation_users = [
                        {
                            'id': int(e['id'] if isinstance(e, dict) else e),
                            'name': (
                                e.get('name', str(e['id']))
                                if isinstance(e, dict)
                                else str(e)
                            ),
                        }
                        for e in legacy_queue
                    ]
                else:
                    # No rotation data at all — seed from current member_list.
                    self.rotation_users = [
                        {'id': u.id, 'name': u.name} for u in self.member_list
                    ]
                # Always reset start_date when migrating from a legacy format.
                self.rotation_start_date = datetime.date.today().isoformat()

            self.save_state()
            self._logger.info('Schedule state loaded from disk.')

        except Exception as e:
            self._logger.error(
                'Failed to load schedule state ({}). Starting fresh.'.format(e))
            self._init_fresh(users)
            self.save_state()

    def _init_fresh(self, users: List[discord.Member]):
        """Set all in-memory state to clean defaults from the supplied user list."""
        self.member_list = list(users)
        self.rotation_users = [{'id': u.id, 'name': u.name} for u in users]
        self.rotation_start_date = datetime.date.today().isoformat()

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
            self._logger.error('Failed to save schedule state: {}'.format(e))

    # ------------------------------------------------------------------
    # Rotation advancement
    # ------------------------------------------------------------------

    def _maybe_advance_rotation(self):
        """If the current rotation has fully elapsed, start a new one.

        Uses the current member_list as the user list for the new rotation.
        Handles the case where the bot was offline across multiple full cycles.
        """
        if not self.rotation_users or not self.member_list:
            return

        today = datetime.date.today()
        start = datetime.date.fromisoformat(self.rotation_start_date)
        days_elapsed = (today - start).days
        n = len(self.rotation_users)

        if days_elapsed >= n:
            full_cycles = days_elapsed // n
            new_start = start + datetime.timedelta(days=full_cycles * n)
            self.rotation_start_date = new_start.isoformat()
            self.rotation_users = [{'id': u.id, 'name': u.name} for u in self.member_list]
            self.save_state()
            self._logger.info(
                'Rotation advanced. New rotation started {}.'.format(
                    self.rotation_start_date))

    # ------------------------------------------------------------------
    # Schedule computation
    # ------------------------------------------------------------------

    def _resolve_for_date(self, target_date: datetime.date):
        """Return a user-like object for the given target_date.

        If target_date falls within the current rotation it is resolved from
        rotation_users (is_active=False when the user has since lost the role).
        If target_date falls beyond the current rotation the next rotation is
        approximated using the current member_list.

        The returned SimpleNamespace always exposes:
          .id        – Discord user ID (int)
          .name      – username
          .nick      – nickname when available; None for N/A users
          .is_active – False when the user no longer holds the chore role
        """
        active_member_map = {u.id: u for u in self.member_list}
        start = datetime.date.fromisoformat(self.rotation_start_date)
        days_elapsed = (target_date - start).days
        n = len(self.rotation_users)

        if n == 0:
            raise ValueError('No members in the schedule.')

        if days_elapsed < n:
            # Within the current rotation.
            entry = self.rotation_users[days_elapsed]
        else:
            # Beyond the current rotation — approximate with member_list.
            member_dicts = [{'id': u.id, 'name': u.name} for u in self.member_list]
            if not member_dicts:
                raise ValueError('No members in the schedule.')
            idx = (days_elapsed - n) % len(member_dicts)
            entry = member_dicts[idx]

        is_active = entry['id'] in active_member_map
        member = active_member_map.get(entry['id'])
        return _make_user(entry, is_active, member)

    def get_user_for_day(self, offset: int):
        """Return the user-like object responsible for today + offset days.

        Args:
          offset: Number of days from today (0 = today, 1 = tomorrow, etc.).

        Returns:
          SimpleNamespace with .id, .name, .nick, .is_active
        """
        if not self.rotation_users:
            raise ValueError('No members in the schedule queue.')
        target_date = datetime.date.today() + datetime.timedelta(days=offset)
        return self._resolve_for_date(target_date)

    @property
    def on_call(self):
        """The user on call for today.

        Automatically advances the rotation to a new cycle if the current one
        has elapsed before returning today's user.

        Returns:
          SimpleNamespace with .id, .name, .nick, .is_active
        """
        self._maybe_advance_rotation()
        return self.get_user_for_day(0)

    def generate_schedule(self) -> Text:
        """Generate a markdown table of who is on call for the next 7 days.

        Users who no longer hold the chore role are shown as 'Name (N/A)'.

        Returns:
          Text: 7-day ASCII table.
        """
        if not self.rotation_users:
            return 'No members in the schedule queue.'

        today = datetime.date.today()
        cols = []
        for i in range(7):
            target_date = today + datetime.timedelta(days=i)
            d_str = target_date.strftime('%B ') + str(target_date.day)
            day_str = calendar.day_abbr[target_date.weekday()] + (' (today)' if i == 0 else '')
            user = self._resolve_for_date(target_date)
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