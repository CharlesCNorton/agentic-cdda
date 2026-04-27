#!/usr/bin/env python3
"""
Windows-side shim for the CDDA wrapper.
Handles wrapper launch selection, named key mapping, and stderr suppression.

Usage:
    cdda.py                     # capture screen
    cdda.py do k                # move south
    cdda.py do semicolon        # send ; (look mode)
    cdda.py do greater          # send > (go downstairs)
    cdda.py do backspace        # send Backspace (mapped to tmux BSpace)
    cdda.py diff do k           # move south, show only changes
    cdda.py send Enter          # send Enter, no capture
    cdda.py raw                 # raw screen dump
    cdda.py bail                # escape to main menu or game
    cdda.py batch "Enter,Tab,f" # send multiple keys with delays
    cdda.py back                # context-aware back (BTab or Escape)
    cdda.py status              # in chargen, show DESCRIPTION tab content
                                # (auto tabs there and back)
    cdda.py filter "Trivia"     # in chargen, filter list and commit selection

Environment:
    CDDA_WRAPPER_MODE   wsl | native (default: wsl)
    CDDA_WRAPPER_CMD    Full command override for launching wrapper.py
    CDDA_WSL_DISTRO     WSL distro name (default: Ubuntu)
    CDDA_WSL_PYTHON     Python inside WSL (default: python3)
    CDDA_WSL_WRAPPER    Wrapper path inside WSL
    CDDA_NATIVE_PYTHON  Native Python executable (default: current interpreter)
    CDDA_NATIVE_WRAPPER Native wrapper.py path (default: repo-local wrapper.py)
    CDDA_TMUX_SESSION   Forwarded to wrapper.py
    CDDA_LAST_CAPTURE   Forwarded to wrapper.py

Daemon mode (skip wsl.exe spawn cost per call):
    CDDA_DAEMON         "1" / "true" / "yes" to try daemon first; falls back
                        to subprocess if daemon is not reachable
    CDDA_DAEMON_HOST    Daemon host (default: 127.0.0.1)
    CDDA_DAEMON_PORT    Daemon port (default: 9876)
    CDDA_DAEMON_TIMEOUT Connect timeout in seconds (default: 0.3)

    Start the daemon manually inside WSL:
        wsl -d <distro> -- python3 /mnt/d/cataclysm-dda/wrapper_daemon.py
"""
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys

# Named keys for characters that are dangerous in shell contexts
KEY_MAP = {
    'semicolon': ';',
    'greater':   '>',
    'less':      '<',
    'pipe':      '|',
    'amp':       '&',
    'hash':      '#',
    'at':        '@',
    'dollar':    '$',
    'bang':      '!',
    'tilde':     '~',
    'backslash': '\\',
    'quote':     "'",
    'dquote':    '"',
    'lparen':    '(',
    'rparen':    ')',
    'lbrace':    '{',
    'rbrace':    '}',
    'lbracket':  '[',
    'rbracket':  ']',
    'caret':     '^',
    'percent':   '%',
    'question':  '?',
    'comma':     ',',
    'period':    '.',
}

# Common key names normalized to tmux send-keys names. The wrapper passes
# multi-char tokens straight to tmux, which uses BSpace / BTab / IC / DC /
# PageUp / PageDown rather than the more common spellings users type.
NAMED_KEY_MAP = {
    'backspace': 'BSpace',
    'bspace':    'BSpace',
    'delete':    'DC',
    'del':       'DC',
    'insert':    'IC',
    'ins':       'IC',
    'pageup':    'PageUp',
    'pgup':      'PageUp',
    'pagedown':  'PageDown',
    'pgdn':      'PageDown',
    'pgdown':    'PageDown',
    'home':      'Home',
    'end':       'End',
    'space':     'Space',
    'btab':      'BTab',
    'shifttab':  'BTab',
}

FORWARDED_ENV_VARS = (
    'CDDA_TMUX_SESSION',
    'CDDA_LAST_CAPTURE',
)

DAEMON_HOST = os.environ.get('CDDA_DAEMON_HOST', '127.0.0.1')
DAEMON_PORT = int(os.environ.get('CDDA_DAEMON_PORT', '9876'))
DAEMON_TIMEOUT = float(os.environ.get('CDDA_DAEMON_TIMEOUT', '0.3'))


def daemon_enabled():
    return os.environ.get('CDDA_DAEMON', '').strip().lower() in ('1', 'true', 'yes', 'on')


def args_to_daemon_request(args):
    """Translate sys.argv-style args into a daemon JSON request.
    Returns None for commands the daemon cannot handle (e.g. diff, which
    needs LAST_CAPTURE state and is rare enough to not optimize)."""
    if not args:
        return {'cmd': 'capture'}
    cmd = args[0].lower()
    rest = args[1:]
    if cmd in ('raw', 'capture', 'bail', 'ping', 'status'):
        return {'cmd': cmd}
    if cmd == 'do':
        return {'cmd': 'do', 'args': rest}
    if cmd == 'send':
        return {'cmd': 'send', 'args': rest}
    if cmd == 'batch' and rest:
        return {'cmd': 'batch', 'args': [rest[0]]}
    if cmd == 'filter' and rest:
        return {'cmd': 'filter', 'args': [rest[0]]}
    return None


def daemon_call(req):
    """Send req to the daemon. Returns parsed response dict, or None if
    the daemon is not reachable. Raises on protocol errors."""
    forwarded = {
        name: os.environ[name]
        for name in FORWARDED_ENV_VARS
        if os.environ.get(name)
    }
    if forwarded:
        req = {**req, 'env': forwarded}
    try:
        s = socket.socket()
        s.settimeout(DAEMON_TIMEOUT)
        s.connect((DAEMON_HOST, DAEMON_PORT))
        s.settimeout(None)
        s.sendall(json.dumps(req).encode('utf-8') + b'\n')
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b'\n' in chunk:
                break
        s.close()
        if not chunks:
            return None
        line = b''.join(chunks).partition(b'\n')[0]
        return json.loads(line.decode('utf-8'))
    except (ConnectionRefusedError, socket.timeout, socket.error, OSError):
        return None


def emit_daemon_response(resp):
    if 'error' in resp:
        write_text(sys.stderr, f'daemon error: {resp["error"]}\n')
        if 'traceback' in resp:
            write_text(sys.stderr, resp['traceback'])
        raise SystemExit(2)
    if 'raw' in resp:
        write_text(sys.stdout, resp['raw'])
        return
    if 'pong' in resp:
        write_text(sys.stdout, 'pong\n')
        return
    if 'ok' in resp:
        return
    if 'lines' in resp:
        if 'mode' in resp:
            write_text(sys.stdout, f'[{resp["mode"]}]\n')
        elif resp.get('status'):
            write_text(sys.stdout, '[chargen status]\n')
        for line in resp['lines']:
            write_text(sys.stdout, line + '\n')
        return
    # Unknown shape; dump raw JSON for debugging.
    write_text(sys.stdout, json.dumps(resp) + '\n')

def write_text(stream, text):
    if not text:
        return
    try:
        stream.write(text)
    except UnicodeEncodeError:
        buffer = getattr(stream, 'buffer', None)
        if buffer is None:
            raise
        buffer.write(text.encode('utf-8', errors='replace'))
    stream.flush()


def get_wrapper_command():
    custom_cmd = os.environ.get('CDDA_WRAPPER_CMD', '').strip()
    if custom_cmd:
        cmd = shlex.split(custom_cmd, posix=False)
        if not cmd:
            raise SystemExit('CDDA_WRAPPER_CMD is set but empty after parsing')
        return cmd

    mode = os.environ.get('CDDA_WRAPPER_MODE', 'wsl').strip().lower()
    if mode == 'native':
        native_python = os.environ.get('CDDA_NATIVE_PYTHON', sys.executable)
        native_wrapper = os.environ.get(
            'CDDA_NATIVE_WRAPPER',
            str(Path(__file__).with_name('wrapper.py')),
        )
        return [native_python, native_wrapper]

    if mode == 'wsl':
        distro = os.environ.get('CDDA_WSL_DISTRO', 'Ubuntu')
        wsl_python = os.environ.get('CDDA_WSL_PYTHON', 'python3')
        wsl_wrapper = os.environ.get(
            'CDDA_WSL_WRAPPER',
            '/mnt/d/cataclysm-dda/wrapper.py',
        )
        forwarded = [
            f'{name}={os.environ[name]}'
            for name in FORWARDED_ENV_VARS
            if os.environ.get(name)
        ]
        return [
            'wsl.exe', '-d', distro, '--', 'env',
            'PYTHONIOENCODING=utf-8',
            'LANG=C.UTF-8',
            'LC_ALL=C.UTF-8',
            *forwarded,
            wsl_python, wsl_wrapper,
        ]

    raise SystemExit(f'Unsupported CDDA_WRAPPER_MODE: {mode!r}')

def main():
    args = list(sys.argv[1:])

    # 'back' is a synthetic command: send BTab (chargen back-tab)
    if args and args[0].lower() == 'back':
        args = ['do', 'BTab']

    def map_one(token):
        lower = token.lower()
        if lower in KEY_MAP:
            return KEY_MAP[lower]
        if lower in NAMED_KEY_MAP:
            return NAMED_KEY_MAP[lower]
        return token

    # Don't map keys for commands that take string args
    # (batch / filter / status / bail / raw)
    skip_map = {'bail', 'raw', 'status', 'filter'}
    if args and args[0].lower() in skip_map:
        pass
    elif args and args[0].lower() == 'batch':
        # Map keys inside the comma-separated batch string
        if len(args) > 1:
            keys = args[1].split(',')
            mapped_keys = [map_one(k.strip()) for k in keys]
            args = ['batch', ','.join(mapped_keys)]
    else:
        # Map named keys in send/do arguments
        args = [map_one(a) for a in args]

    # Try daemon first if enabled. Falls back to subprocess if not reachable.
    if daemon_enabled():
        req = args_to_daemon_request(args)
        if req is not None:
            resp = daemon_call(req)
            if resp is not None:
                emit_daemon_response(resp)
                return

    cmd = get_wrapper_command() + args

    run_env = os.environ.copy()
    run_env.setdefault('PYTHONIOENCODING', 'utf-8')
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=run_env,
    )
    if result.stdout:
        write_text(sys.stdout, result.stdout)
    if result.returncode != 0:
        if result.stderr:
            write_text(sys.stderr, result.stderr)
        raise SystemExit(result.returncode)
    # stderr suppressed on success (wsl fstab warnings, etc.)

if __name__ == '__main__':
    main()
