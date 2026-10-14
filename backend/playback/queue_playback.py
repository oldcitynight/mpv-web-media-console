"""Durable queue intent and origin-bound, at-most-once terminal action journal.

Queue locks protect selection/SQLite transactions only. Native waits occur after
the transaction and queue lock have ended. A submitted side effect is never
replayed, including after SQL failure or process restart.
"""
from __future__ import annotations

import json
from contextlib import contextmanager, closing

from backend.domain.queue import QueueConflict
from .native_episode import validate_native_episode
from .queue import _revision


class QueuePlaybackCoordinator:
    def __init__(self, queue):
        self.queue = queue
        self.migrations = queue._store.migrations

    @contextmanager
    def _connection(self):
        c = self.migrations.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            with c:
                yield c
        finally:
            c.close()

    def _latest(self, c):
        row = c.execute('SELECT max(id) FROM queue_play_intents WHERE queue_id=?',
                        (self.queue.queue_id,)).fetchone()
        return row[0]

    def _insert(self, c, item_id, path, revision, source):
        qid = self.queue.queue_id
        c.execute("UPDATE queue_play_intents SET phase='superseded' WHERE queue_id=? AND phase IN ('prepared','submitted')", (qid,))
        c.execute("UPDATE queue_terminal_actions SET phase='cancelled' WHERE queue_id=? AND phase='claimed'", (qid,))
        cur = c.execute('INSERT INTO queue_play_intents(queue_id,item_id,path,queue_revision,source,phase) VALUES(?,?,?,?,?,?)',
                        (qid, item_id, path, revision, source, 'prepared'))
        return cur.lastrowid

    def prepare(self, *, item_id=None, path=None, expected_revision=None, source='manual'):
        with self.queue._lock:
            queue = self.queue._checked_queue(expected_revision)
            if item_id is not None:
                item = next((i for i in queue.items if i.id == item_id), None)
                if item is None:
                    raise QueueConflict('queue item does not exist')
                path = item.canonical_path
            with self._connection() as c:
                identity = self._insert(c, item_id, path, _revision(queue.updated_at), source)
            return identity

    def prepare_recovery(self, path, session_id):
        # Preserve an explicit duplicate-path ID only when the prior durable
        # playback binding belongs to the session being recovered.
        item_id = None
        with self.queue._lock, closing(self.migrations.connect()) as c:
            row = c.execute('SELECT item_id,path,session_id,phase FROM queue_playback_bindings WHERE queue_id=?',
                            (self.queue.queue_id,)).fetchone()
            if row and row[1] == path and row[2] == session_id and row[3] in ('playing','pending_cause'):
                item_id = row[0]
        with self.queue._lock:
            queue = self.queue._checked_queue(None)
            with self._connection() as c:
                return self._insert(c, item_id, path, _revision(queue.updated_at), 'recovery')

    def submit(self, identity):
        """Claim once before native I/O; return immutable path, or cancel."""
        with self.queue._lock:
            queue = self.queue._store.get_queue(self.queue.queue_id)
            with self._connection() as c:
                row = c.execute('SELECT item_id,path,queue_revision,source,phase FROM queue_play_intents WHERE id=?', (identity,)).fetchone()
                valid = row and row[4] == 'prepared' and identity == self._latest(c)
                if valid and row[0] is not None:
                    item = next((i for i in queue.items if i.id == row[0]), None)
                    valid = (item is None and row[3] == 'recovery') or (item is not None and item.canonical_path == row[1])
                if valid and row[3] == 'auto':
                    valid = row[2] == _revision(queue.updated_at)
                if not valid:
                    c.execute("UPDATE queue_play_intents SET phase='cancelled' WHERE id=? AND phase='prepared'", (identity,))
                    c.execute("UPDATE queue_terminal_actions SET phase='cancelled' WHERE intent_id=? AND phase='claimed'", (identity,))
                    result = None
                else:
                    c.execute("UPDATE queue_play_intents SET phase='submitted' WHERE id=?", (identity,))
                    c.execute("UPDATE queue_terminal_actions SET phase='submitted' WHERE intent_id=? AND phase='claimed'", (identity,))
                    c.execute("UPDATE queue_playback_bindings SET phase='revoked' WHERE queue_id=?", (self.queue.queue_id,))
                    self._active(c, None)
                    result = {'path': row[1], 'source': row[3]}
            return result

    def outcome(self, identity, phase, detail=None):
        with self.queue._lock, self._connection() as c:
            # A media.started event may have confirmed before load ACK returns.
            c.execute('UPDATE queue_play_intents SET phase=?,detail=? WHERE id=? AND phase=\'submitted\'', (phase, detail, identity))
            c.execute('UPDATE queue_terminal_actions SET phase=?,detail=? WHERE intent_id=? AND phase=\'submitted\'', (phase, detail, identity))

    def snapshot(self):
        # One read transaction covers ordered items, active ID and intent state.
        # No native call/wait occurs while holding this lock or SQL snapshot.
        with self.queue._lock, closing(self.migrations.connect()) as c:
            c.execute('BEGIN')
            snapshot = self.queue.snapshot(c)
            snapshot['playback'] = self.status(c)
            return snapshot

    def status(self, connection=None):
        if connection is None:
            with closing(self.migrations.connect()) as c:
                return self.status(c)
        else:
            c = connection
            row = c.execute('SELECT id,item_id,phase,detail FROM queue_play_intents WHERE queue_id=? ORDER BY id DESC LIMIT 1',
                            (self.queue.queue_id,)).fetchone()
            return dict(zip(('intent_id','item_id','phase','detail'), row)) if row else None

    def on_event(self, event):
        if event.type not in {'media.started','native.end-file','native.terminal-pending'}:
            return
        proof = validate_native_episode(event)
        qid = self.queue.queue_id
        with self.queue._lock:
            queue = self.queue._store.get_queue(qid)
            with self._connection() as c:
                if event.type == 'media.started':
                    intent = proof['queue_intent_id']
                    if intent is None:
                        old = c.execute('SELECT item_id,player_id,load_serial,path,phase,intent_id FROM queue_playback_bindings WHERE queue_id=?', (qid,)).fetchone()
                        if old and old[1] == proof['player_id'] and old[2] >= proof['load_serial']:
                            return
                        latest = self._latest(c)
                        phase = c.execute('SELECT phase FROM queue_play_intents WHERE id=?', (latest,)).fetchone()
                        if phase and phase[0] in ('prepared','submitted','uncertain'):
                            # An unbound event cannot borrow a newer manual or
                            # uncertain native side effect's rights.
                            return
                        explicit = (old and old[1] != proof['player_id'] and old[3] == proof['path']
                                    and old[4] == 'playing' and old[5] == latest)
                        selected = old[0] if explicit else None
                        intent = self._insert(c, selected, proof['path'], _revision(queue.updated_at),
                                              'recovery' if explicit else 'direct')
                        c.execute("UPDATE queue_play_intents SET phase='submitted' WHERE id=?", (intent,))
                    row = c.execute('SELECT item_id,path,phase FROM queue_play_intents WHERE id=? AND queue_id=?', (intent, qid)).fetchone()
                    # Never attribute an unbound/stale load to whichever intent
                    # happens to be current when callbacks arrive.
                    if not row or intent != self._latest(c) or row[1] != proof['path'] or row[2] not in ('submitted','uncertain','confirmed'):
                        return
                    item_id = row[0]
                    if item_id is not None:
                        item = next((i for i in queue.items if i.id == item_id), None)
                        if item is None or item.canonical_path != proof['path']:
                            item_id = None
                    else:
                        matches = [i.id for i in queue.items if i.canonical_path == proof['path']]
                        item_id = matches[0] if len(matches) == 1 else None
                    phase = 'confirmed' if item_id is not None or row[0] is None else 'removed'
                    # Removed-after-submit is not reassociated by path, even
                    # when another row references that same file.
                    c.execute('UPDATE queue_play_intents SET phase=? WHERE id=?', (phase, intent))
                    c.execute("UPDATE queue_terminal_actions SET phase='confirmed' WHERE intent_id=? AND phase IN ('submitted','uncertain')", (intent,))
                    c.execute('INSERT INTO queue_playback_bindings VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(queue_id) DO UPDATE SET item_id=excluded.item_id,intent_id=excluded.intent_id,player_id=excluded.player_id,session_id=excluded.session_id,load_serial=excluded.load_serial,playlist_entry_id=excluded.playlist_entry_id,path=excluded.path,phase=excluded.phase',
                              (qid,row[0] or item_id,intent,proof['player_id'],proof['session_id'],proof['load_serial'],proof['playlist_entry_id'],proof['path'],'playing'))
                    self._active(c, item_id)
                    return
                binding = c.execute('SELECT item_id,intent_id,player_id,session_id,load_serial,playlist_entry_id,path,phase FROM queue_playback_bindings WHERE queue_id=?', (qid,)).fetchone()
                if event.type == 'native.end-file':
                    owned = binding and tuple(binding[2:7]) == tuple(proof[n] for n in ('player_id','session_id','load_serial','playlist_entry_id','path'))
                    intent = proof['queue_intent_id'] if proof['queue_intent_id'] is not None else binding[1] if owned else None
                    attempt = c.execute('SELECT phase,path FROM queue_play_intents WHERE id=?', (intent,)).fetchone()
                    if attempt and intent == self._latest(c) and attempt[1] == proof['path']:
                        error = event.payload.get('end_reason') == 'error'
                        pending = attempt[0] in ('submitted','uncertain')
                        if error or pending:
                            phase = 'failed' if error else 'uncertain'
                            detail = 'native_decode_error' if error else 'ended_before_start_readback'
                            c.execute('UPDATE queue_play_intents SET phase=?,detail=? WHERE id=?', (phase,detail,intent))
                            c.execute('UPDATE queue_terminal_actions SET phase=?,detail=? WHERE intent_id=?', (phase,detail,intent))
                if not binding or tuple(binding[2:7]) != tuple(proof[n] for n in ('player_id','session_id','load_serial','playlist_entry_id','path')) or binding[7] not in ('playing','pending_cause'):
                    return
                if event.type == 'native.terminal-pending':
                    c.execute("UPDATE queue_playback_bindings SET phase='pending_cause' WHERE queue_id=?", (qid,))
                    self._active(c, None)
                    return
                c.execute("UPDATE queue_playback_bindings SET phase='ended' WHERE queue_id=?", (qid,))
                # Keep the revision from the ordered snapshot used to select
                # the successor, prior to clearing its active marker.
                self._active(c, None)
                origin = json.dumps([proof[n] for n in ('player_id','session_id','load_serial','playlist_entry_id')], separators=(',',':'))
                if c.execute('SELECT 1 FROM queue_terminal_actions WHERE origin=?', (origin,)).fetchone():
                    return
                ordered = list(queue.items)
                index = next((n for n,i in enumerate(ordered) if i.id == binding[0]), None)
                successor = ordered[index+1] if index is not None and index+1 < len(ordered) else None
                allowed = event.payload.get('end_reason') == 'eof' and binding[1] == self._latest(c) and successor is not None
                if allowed:
                    # _active updates timestamp; use the resulting revision.
                    updated = c.execute('SELECT updated_at FROM queues WHERE id=?', (qid,)).fetchone()[0]
                    intent = self._insert(c, successor.id, successor.canonical_path, _revision(updated), 'auto')
                    c.execute('INSERT INTO queue_terminal_actions VALUES(?,?,?,?,?,NULL)', (origin,qid,binding[0],intent,'claimed'))
                else:
                    c.execute('INSERT INTO queue_terminal_actions VALUES(?,?,?,NULL,?,NULL)', (origin,qid,binding[0],'end'))

    def claimed(self):
        with closing(self.migrations.connect()) as c:
            return [r[0] for r in c.execute("SELECT intent_id FROM queue_terminal_actions WHERE queue_id=? AND phase='claimed'", (self.queue.queue_id,))]

    def cancel_claimed_replay(self):
        # An explicit replay supersedes only an unsubmitted EOF successor.
        # Submitted/uncertain effects remain audit facts and are never replayed.
        with self.queue._lock, self._connection() as c:
            changed = c.execute("UPDATE queue_play_intents SET phase='cancelled',detail='owner_replay' WHERE queue_id=? AND phase='prepared' AND id IN (SELECT intent_id FROM queue_terminal_actions WHERE queue_id=? AND phase='claimed')", (self.queue.queue_id,self.queue.queue_id)).rowcount
            c.execute("UPDATE queue_terminal_actions SET phase='cancelled',detail='owner_replay' WHERE queue_id=? AND phase='claimed' AND intent_id IN (SELECT id FROM queue_play_intents WHERE phase='cancelled' AND detail='owner_replay')", (self.queue.queue_id,))
            if changed:
                c.execute('UPDATE queues SET updated_at=? WHERE id=?', (self.queue._clock(),self.queue.queue_id))

    def abandon_claimed(self):
        # Every unsubmitted intent, including manual row Play, is abandoned.
        # Submitted records retain uncertainty; none is automatically replayed.
        with self.queue._lock, self._connection() as c:
            changed = c.execute("UPDATE queue_play_intents SET phase='cancelled',detail='process_restart' WHERE queue_id=? AND phase='prepared'", (self.queue.queue_id,)).rowcount
            changed += c.execute("UPDATE queue_terminal_actions SET phase='cancelled',detail='process_restart' WHERE queue_id=? AND phase='claimed'", (self.queue.queue_id,)).rowcount
            changed += c.execute("UPDATE queue_terminal_actions SET phase='uncertain',detail='process_restart' WHERE queue_id=? AND phase='submitted'", (self.queue.queue_id,)).rowcount
            changed += c.execute("UPDATE queue_play_intents SET phase='uncertain',detail='process_restart' WHERE queue_id=? AND phase='submitted'", (self.queue.queue_id,)).rowcount
            if changed:
                c.execute('UPDATE queues SET updated_at=? WHERE id=?', (self.queue._clock(),self.queue.queue_id))

    def _active(self, c, item_id):
        current = c.execute('SELECT active_item_id FROM queues WHERE id=?', (self.queue.queue_id,)).fetchone()[0]
        if current != item_id:
            c.execute('UPDATE queues SET active_item_id=?,updated_at=? WHERE id=?', (item_id,self.queue._clock(),self.queue.queue_id))
