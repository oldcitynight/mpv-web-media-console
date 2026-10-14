"""Validate the fixed host-owned start/end receipt before projecting it."""
from collections.abc import Mapping


class NativeEpisodeProofError(ValueError):
    pass


def validate_native_episode(event):
    proof = event.payload.get('native_episode')
    state = event.payload.get('state')
    keys = {'load_serial', 'playlist_entry_id', 'queue_intent_id', 'path', 'player_id', 'session_id'}
    if not isinstance(proof, Mapping) or set(proof) != keys or not isinstance(state, Mapping):
        raise NativeEpisodeProofError('invalid native episode receipt')
    if (type(proof['load_serial']) is not int or not 0 < proof['load_serial'] < 2**63
            or type(proof['playlist_entry_id']) is not int or not 0 <= proof['playlist_entry_id'] < 2**63
            or proof['queue_intent_id'] is not None and
                (type(proof['queue_intent_id']) is not int or not 0 < proof['queue_intent_id'] < 2**63)):
        raise NativeEpisodeProofError('invalid native episode scalar')
    if any(type(proof[n]) is not str or not proof[n] or len(proof[n]) > 4096
           or '\x00' in proof[n] for n in ('path', 'player_id', 'session_id')):
        raise NativeEpisodeProofError('invalid native episode identity')
    if (proof['player_id'] != event.player_id or proof['session_id'] != event.session_id
            or state.get('player_id') != event.player_id or state.get('session_id') != event.session_id):
        raise NativeEpisodeProofError('native episode owner mismatch')
    props = state.get('properties')
    if not isinstance(props, Mapping):
        raise NativeEpisodeProofError('invalid native episode state')
    if event.type == 'media.started':
        if (props.get('path') != proof['path'] or props.get('idle_active') is not False
                or state.get('status') not in ('playing', 'paused', 'loading')):
            raise NativeEpisodeProofError('native media start is not current')
    elif event.type == 'native.terminal-pending':
        if (state.get('status') != 'idle' or props.get('path') not in (None, '')
                or props.get('idle_active') is not True
                or event.payload.get('reason') != 'native_end_cause_unavailable'):
            raise NativeEpisodeProofError('native pending cause is not quiescent')
    elif event.type == 'native.end-file':
        nr, err = event.payload.get('native_reason'), event.payload.get('native_error')
        if ((nr is not None and (type(nr) is not int or not -(2**31) <= nr < 2**31))
                or (err is not None and (type(err) is not int or not -(2**31) <= err < 2**31))
                or event.payload.get('terminal_verified') is not True):
            raise NativeEpisodeProofError('native terminal cause is unavailable')
        reason = 'error' if type(err) is int and err < 0 else {0: 'eof', 2: 'stopped', 3: 'stopped', 4: 'error'}.get(nr)
        if (event.payload.get('end_reason') != reason or state.get('status') !=
                {'eof': 'ended', 'stopped': 'stopped', 'error': 'error'}.get(reason, 'idle')):
            raise NativeEpisodeProofError('native terminal cause differs from projection')
        if props.get('path') not in (None, proof['path']):
            raise NativeEpisodeProofError('native terminal names foreign media')
    else:
        raise NativeEpisodeProofError('unsupported native episode event')
    return proof
