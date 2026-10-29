"""When Portkeeper asks RomM about imports: event-driven by default, with backoff and a miss cache."""
import threading
import time
from datetime import datetime, timezone

from .safety import Invalid
from .storage import now as stamp_now

# (age in seconds below which the delay applies, delay in seconds). Age is the time since the
# event being waited for (an upload, a publish or a cover choice) or since a miss was first seen.
LADDER = ((1800, 60), (3600, 300), (7200, 900), (None, 3600))
MODES = {'1': 'Every minute', '5': 'Every 5 minutes', '15': 'Every 15 minutes', '30': 'Every 30 minutes',
         '60': 'Hourly', 'event': 'Only after an upload', 'off': 'Off (Match now only)'}
DEFAULT_MODE = 'event'
GIVE_UP = 24 * 3600  # A manual/cover event that never matches stops being polled after a day.


def retry_delay(age):
    for limit, delay in LADDER:
        if limit is None or age < limit:
            return delay


def miss_due(misses, key, at):
    """True when a prefix that earlier found nothing may be searched again."""
    entry = misses.get(key)
    return not entry or at - entry['last'] >= retry_delay(at - entry['first'])


def note_search(misses, key, found, at):
    if found:
        misses.pop(key, None)
    else:
        entry = misses.get(key) or {'first': at}
        misses[key] = {'first': entry['first'], 'last': at}


def _epoch(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


class MatchSchedule:
    def __init__(self, engine):
        self.engine = engine
        self.wake = threading.Event()

    # Settings -------------------------------------------------------------------------------
    def mode(self):
        value = self.engine.get_setting('romm_match_mode', DEFAULT_MODE)
        return value if value in MODES else DEFAULT_MODE

    def set_mode(self, value):
        if value not in MODES:
            raise Invalid('Choose how often to check RomM for new imports')
        self.engine.save_setting('romm_match_mode', value)
        self.wake.set()
        return self.status()

    def _state(self):
        return self.engine.get_setting('romm_match_state', {})

    def kick(self, reason, at=None):
        """Something was uploaded, published or changed: start checking RomM until it is matched."""
        state = self._state()
        self.engine.save_setting('romm_match_state', {**state, 'event_at': at or time.time(), 'reason': reason})
        self.wake.set()

    # Schedule -------------------------------------------------------------------------------
    def pending(self, at=None):
        """(anchor epoch, descriptions) of what is being waited for. Empty when idle."""
        at = at or time.time()
        anchors, notes = [], []
        for artifact in self.engine.store.all('artifacts'):
            romm = artifact.get('romm') or {}
            if romm.get('status') == 'awaiting_scan':
                anchors.append(_epoch(romm.get('uploaded_at')) or at)
                notes.append(f"{artifact.get('filename', artifact['id'])} uploaded, waiting for RomM to index it")
        state = self._state()
        if state.get('event_at') and at - state['event_at'] < GIVE_UP:
            anchors.append(state['event_at'])
            notes.append(state.get('reason') or 'Waiting for RomM to match a recent change')
        return (min(anchors) if anchors else None), notes

    def next_check(self, at=None):
        """(epoch or None, reason) for the next automatic check."""
        at = at or time.time()
        if not self.engine.romm.public_config().get('configured'):
            return None, 'RomM is not connected'
        mode, last = self.mode(), self._state().get('last_run')
        if mode == 'off':
            return None, 'Automatic checks are off; use Match now'
        if mode != 'event':
            return (last + int(mode) * 60 if last else at), f'Fixed interval: {MODES[mode].lower()}'
        anchor, notes = self.pending(at)
        if anchor is None:
            return None, 'Nothing is waiting for RomM, so Portkeeper is not polling'
        if not last or last < anchor:
            return at, 'Checking soon for a new upload'
        return last + retry_delay(at - anchor), 'Backing off while waiting for RomM'

    def due(self, at=None):
        at = at or time.time()
        when, _ = self.next_check(at)
        return when is not None and when <= at

    def status(self, at=None):
        at = at or time.time()
        when, why = self.next_check(at)
        state = self._state()
        _, notes = self.pending(at)
        iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds') if value else None
        return {'mode': self.mode(), 'modes': MODES, 'last_check': iso(state.get('last_run')), 'next_check': iso(when),
                'reason': why, 'pending': notes, 'known_misses': len(self.engine.get_setting('romm_match_misses', {}))}

    # Running --------------------------------------------------------------------------------
    def run(self, manual=False, at=None):
        """One check: index uploads, match imports, refresh import lists. Honours the miss cache."""
        engine, at = self.engine, at or time.time()
        misses = engine.get_setting('romm_match_misses', {})
        try:
            library = engine.romm.library_stamp()
        except Invalid:
            library = None
        state = self._state()
        if manual or (library is not None and library != state.get('library')):
            misses.clear()
        try:
            from .romm_identity import ImportIdentities
            with engine.match_lock:
                result = ImportIdentities(engine.romm, engine.data, engine.store).sync(misses=misses, at=at)
            engine.save_setting('romm_import_matching', {'checked_at': stamp_now(), **result})
            engine.publisher.poll()
            seen = set()
            for project in engine.store.all('projects'):
                prefix = (project.get('metadata') or {}).get('filename_prefix')
                if prefix and prefix not in seen:
                    seen.add(prefix)
                    engine.reviews.imports(project['id'], misses=misses, at=at)
        except Exception:
            # Do not expose credentials or remote response bodies in the status.
            engine.save_setting('romm_import_matching', {'checked_at': stamp_now(), 'error': 'Import matching failed. Check RomM connection and roms.write permission.'})
            result = None
        finally:
            engine.save_setting('romm_match_misses', misses)
            state = {**self._state(), 'last_run': at, 'library': library}
            # A recorded event is settled once nothing is awaiting a scan and every search found its import.
            if result is not None and not misses and not any((a.get('romm') or {}).get('status') == 'awaiting_scan' for a in engine.store.all('artifacts')):
                state.pop('event_at', None)
                state.pop('reason', None)
            engine.save_setting('romm_match_state', state)
        return result or {'matched': 0}

    def loop(self):
        while not self.engine.stop.is_set():
            if self.due():
                self.run()
            self.wake.wait(15)
            self.wake.clear()
