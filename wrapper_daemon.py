#!/usr/bin/env python3
"""Persistent wrapper daemon. Listens on a TCP port for JSON commands and
returns JSON responses. Run inside WSL once per tmux session to skip the
~1.5s cold-start cost of `wsl.exe -- python3 wrapper.py` per call.

Usage (inside WSL):
    python3 wrapper_daemon.py [--port 9876] [--bind 127.0.0.1]

Protocol: one JSON object per request, terminated by newline. Response is
one JSON object per request, terminated by newline.

Requests:
    {"cmd": "ping"}                              -> {"pong": true}
    {"cmd": "raw"}                               -> {"raw": "..."}
    {"cmd": "capture"}                           -> {"mode": "...", "lines": [...]}
    {"cmd": "do",     "args": ["Down","Enter"]}  -> {"mode": "...", "lines": [...]}
    {"cmd": "send",   "args": ["Y"]}             -> {"ok": true}
    {"cmd": "batch",  "args": ["Down,Enter"]}    -> {"mode": "...", "lines": [...]}
    {"cmd": "bail"}                              -> {"mode": "...", "lines": [...]}
    {"cmd": "status"}                            -> {"lines": [...]}
    {"cmd": "filter", "args": ["Trivia"]}        -> {"mode": "...", "lines": [...]}

Optional in any request:
    {"env": {"CDDA_TMUX_SESSION": "cdda", ...}}  -> updates daemon's view of forwarded env
"""
import argparse
import json
import os
import socket
import sys
import threading
import traceback

import wrapper


_FORWARDED = ('CDDA_TMUX_SESSION', 'CDDA_LAST_CAPTURE')


def apply_env(env):
    if not env:
        return
    for k, v in env.items():
        if k in _FORWARDED:
            os.environ[k] = v
    wrapper.SESSION = os.environ.get('CDDA_TMUX_SESSION', wrapper.SESSION)
    wrapper.LAST_CAPTURE = os.environ.get('CDDA_LAST_CAPTURE', wrapper.LAST_CAPTURE)


def handle_request(req):
    apply_env(req.get('env'))
    cmd = req.get('cmd', '')
    args = req.get('args', []) or []

    if cmd == 'ping':
        return {'pong': True}
    if cmd == 'raw':
        return {'raw': wrapper.capture_raw()}
    if cmd == 'capture':
        raw = wrapper.capture_raw()
        mode = wrapper.detect_mode(raw)
        return {'mode': mode, 'lines': wrapper.parse(raw, mode)}
    if cmd == 'do':
        raw = wrapper.drive_keys(args)
        mode = wrapper.detect_mode(raw)
        return {'mode': mode, 'lines': wrapper.parse(raw, mode)}
    if cmd == 'send':
        wrapper.send_keys(*args)
        return {'ok': True}
    if cmd == 'batch':
        raw = wrapper.batch(args[0] if args else '')
        mode = wrapper.detect_mode(raw)
        return {'mode': mode, 'lines': wrapper.parse(raw, mode)}
    if cmd == 'bail':
        raw, mode = wrapper.bail()
        return {'mode': mode, 'lines': wrapper.parse(raw, mode)}
    if cmd == 'status':
        return {'lines': wrapper.chargen_status(), 'status': True}
    if cmd == 'filter':
        raw = wrapper.chargen_filter(args[0] if args else '')
        mode = wrapper.detect_mode(raw)
        return {'mode': mode, 'lines': wrapper.parse(raw, mode)}
    return {'error': f'unknown command: {cmd}'}


def handle_client(conn):
    with conn:
        try:
            data = b''
            while b'\n' not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            line, _, _ = data.partition(b'\n')
            try:
                req = json.loads(line.decode('utf-8'))
            except json.JSONDecodeError as exc:
                resp = {'error': f'invalid JSON: {exc}'}
            else:
                try:
                    resp = handle_request(req)
                except wrapper.TmuxError as exc:
                    resp = {'error': f'tmux: {exc}'}
                except Exception as exc:
                    resp = {'error': str(exc), 'traceback': traceback.format_exc()}
            conn.sendall(json.dumps(resp).encode('utf-8') + b'\n')
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('CDDA_DAEMON_PORT', '9876')))
    ap.add_argument('--bind', default=os.environ.get('CDDA_DAEMON_BIND', '127.0.0.1'))
    args = ap.parse_args()

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.listen(8)
    sys.stderr.write(f'wrapper daemon listening on {args.bind}:{args.port}\n')
    sys.stderr.flush()
    try:
        while True:
            conn, _ = sock.accept()
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
