#!/usr/bin/env python3
"""Coursework: a private, single-user planner on localhost."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from canvas import CanvasError, safe_url, synchronize

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
DATA = ROOT / 'data'
CSRF = secrets.token_urlsafe(32)
STATE = {}
CONFIG = {}
PORT = 8765


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def empty_state():
    return {'version': 1, 'profile': {'name': 'Student', 'timezone': 'America/New_York'},
            'courses': [], 'assignments': [], 'announcements': [],
            'sync': {'lastSync': None, 'error': None, 'warnings': []}}


def write_private(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def initialize(directory=DATA):
    global DATA, STATE, CONFIG
    DATA = Path(directory)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    STATE = json.loads((DATA / 'planner.json').read_text()) if (DATA / 'planner.json').exists() else empty_state()
    CONFIG = json.loads((DATA / 'connection.json').read_text()) if (DATA / 'connection.json').exists() else {}
    # Fail visibly instead of overwriting unreadable existing data.
    if not isinstance(STATE, dict) or STATE.get('version') != 1:
        raise ValueError('The saved planner format is not supported. Your existing files were not changed.')


def save():
    write_private(DATA / 'planner.json', STATE)


def public_state():
    with LOCK:
        result = copy.deepcopy(STATE)
        result['connection'] = {'connected': bool(CONFIG), 'mode': CONFIG.get('mode'),
                                'host': urllib.parse.urlsplit(CONFIG.get('url', '')).hostname or ''}
        result['sync']['running'] = SYNC_LOCK.locked()
        result['csrf'] = CSRF
        return result


def text(value, limit=500, required=False):
    if not isinstance(value, str):
        raise ValueError('Please enter text in this field.')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError('Please enter ' + ('a value' if required else 'text') + f' of no more than {limit} characters.')
    return value


def valid_due(value):
    value = text(value, 64)
    if value:
        try:
            if len(value) == 10:
                dt.date.fromisoformat(value)
            else:
                parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
        except ValueError:
            raise ValueError('Choose a valid due date and time.') from None
    return value


def find_item(collection, ident):
    for item in STATE[collection]:
        if item['id'] == ident:
            return item
    raise ValueError('This item could not be found. Refresh and try again.')


def make_course(data, ident=None):
    color = data.get('color', '#357760')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('Choose a valid course color.')
    return {'id': ident or 'local-' + uuid.uuid4().hex, 'name': text(data.get('name', ''), 200, True),
            'code': text(data.get('code', ''), 100, True), 'color': color, 'grade': None, 'source': 'manual', 'url': ''}


def make_assignment(data, ident=None):
    cid = data.get('courseId', '')
    if cid and not any(c['id'] == cid for c in STATE['courses']):
        raise ValueError('Choose an existing course or Personal.')
    kind = data.get('kind', 'Assignment')
    if kind not in ('Assignment', 'Exam', 'Quiz', 'Reading', 'Discussion', 'Event'):
        raise ValueError('Choose an assignment type.')
    return {'id': ident or 'local-' + uuid.uuid4().hex, 'title': text(data.get('title', ''), 300, True),
            'courseId': cid, 'due': valid_due(data.get('due', '')), 'kind': kind, 'source': 'manual',
            'done': bool(data.get('done', False)), 'submitted': False, 'notes': text(data.get('notes', ''), 10000),
            'url': '', 'description': '', 'score': None, 'points': None}


def sync(config=None):
    global CONFIG
    if not SYNC_LOCK.acquire(blocking=False):
        raise ValueError('A sync is already running. Give it a moment.')
    try:
        with LOCK:
            selected = copy.deepcopy(config if config is not None else CONFIG)
            previous_config = copy.deepcopy(CONFIG)
            timezone = STATE['profile']['timezone']
        if not selected:
            raise ValueError('Connect Canvas first.')
        imported = synchronize(selected, timezone)
        with LOCK:
            if config is None and CONFIG != previous_config:
                return  # A disconnect took place while fetching.
            preserve = selected.get('url') == previous_config.get('url') and selected.get('mode') == previous_config.get('mode')
            previous = {a['id']: a for a in STATE['assignments']} if preserve else {}
            colors = {c['id']: c['color'] for c in STATE['courses']} if preserve else {}
            for item in imported['assignments']:
                old = previous.get(item['id'], {})
                item['done'] = bool(old.get('done', False))
                item['notes'] = old.get('notes', '')
            for course in imported['courses']:
                course['color'] = colors.get(course['id'], course['color'])
            new_course_ids = {c['id'] for c in imported['courses']}
            manual = [a for a in STATE['assignments'] if a['source'] == 'manual']
            keep_ids = {a['courseId'] for a in manual} - new_course_ids
            retained = [c for c in STATE['courses'] if c['id'] not in new_course_ids and (c['source'] == 'manual' or c['id'] in keep_ids)]
            # Keep course labels for manual tasks even if a course leaves Canvas.
            for course in retained:
                if course['source'] != 'manual':
                    course['source'] = 'manual'
                    course['grade'] = None
            STATE['courses'] = retained + imported['courses']
            STATE['assignments'] = manual + imported['assignments']
            STATE['announcements'] = imported['announcements']
            STATE['sync'] = {'lastSync': now(), 'error': None, 'warnings': imported['warnings']}
            CONFIG = selected
            write_private(DATA / 'connection.json', CONFIG)
            save()
    except CanvasError as exc:
        with LOCK:
            STATE['sync']['error'] = str(exc)
            save()
        raise
    finally:
        SYNC_LOCK.release()


def auto_sync():
    while True:
        time.sleep(900)
        if CONFIG:
            try:
                sync()
            except (ValueError, OSError):
                pass


def mutate(path, data):
    global CONFIG
    if path == '/api/connect':
        mode = data.get('mode')
        if mode not in ('api', 'feed'):
            raise ValueError('Choose a connection method.')
        url = safe_url(text(data.get('url', ''), 4000, True))
        config = {'mode': mode, 'url': url}
        if mode == 'api':
            parsed = urllib.parse.urlsplit(url)
            config['url'] = 'https://' + parsed.netloc
            config['token'] = text(data.get('token', ''), 4096, True)
            if any(c.isspace() for c in config['token']):
                raise ValueError('The token should not contain spaces. Copy it again from Canvas.')
        sync(config)
        return
    if path == '/api/sync':
        sync()
        return
    with LOCK:
        if path == '/api/disconnect':
            CONFIG = {}
            write_private(DATA / 'connection.json', {})
            STATE['sync']['error'] = None
        elif path == '/api/course':
            ident = data.get('id')
            if ident:
                old = find_item('courses', ident)
                if old['source'] != 'manual':
                    raise ValueError('Manage synced course details in Canvas.')
                old.update(make_course(data, ident))
            else:
                STATE['courses'].append(make_course(data))
        elif path == '/api/course/delete':
            course = find_item('courses', data.get('id'))
            if course['source'] != 'manual':
                raise ValueError('Manage synced courses in Canvas.')
            STATE['courses'].remove(course)
            for a in STATE['assignments']:
                if a['courseId'] == course['id']:
                    a['courseId'] = ''
        elif path == '/api/assignment':
            ident = data.get('id')
            if ident:
                old = find_item('assignments', ident)
                if old['source'] != 'manual':
                    old['notes'] = text(data.get('notes', old.get('notes', '')), 10000)
                else:
                    old.update(make_assignment(data, ident))
            else:
                STATE['assignments'].append(make_assignment(data))
        elif path == '/api/assignment/toggle':
            assignment = find_item('assignments', data.get('id'))
            if not isinstance(data.get('done'), bool):
                raise ValueError('Choose a completion status.')
            assignment['done'] = data['done']
        elif path == '/api/assignment/delete':
            assignment = find_item('assignments', data.get('id'))
            if assignment['source'] != 'manual':
                raise ValueError('Synced assignments are managed in Canvas. You can mark them done here.')
            STATE['assignments'].remove(assignment)
        elif path == '/api/profile':
            name = text(data.get('name', ''), 80, True)
            timezone = text(data.get('timezone', 'America/New_York'), 100, True)
            try:
                ZoneInfo(timezone)
            except (KeyError, ValueError):
                raise ValueError('Choose a valid timezone, such as America/New_York.') from None
            STATE['profile'] = {'name': name, 'timezone': timezone}
        elif path == '/api/import':
            backup = data.get('backup')
            if not isinstance(backup, dict) or backup.get('version') != 1:
                raise ValueError('Choose a Coursework backup file.')
            # Restore as manual snapshots, leaving any active Canvas connection intact.
            course_map, courses, assignments = {}, [], []
            for item in backup.get('courses', []):
                course = make_course(item)
                course_map[item['id']] = course['id']
                courses.append(course)
            previous_courses = STATE['courses']
            STATE['courses'] = previous_courses + courses
            try:
                for item in backup.get('assignments', []):
                    entry = dict(item, courseId=course_map.get(item.get('courseId'), ''), done=bool(item.get('done') or item.get('submitted')))
                    assignments.append(make_assignment(entry))
            except Exception:
                STATE['courses'] = previous_courses
                raise
            STATE['assignments'].extend(assignments)
        else:
            raise ValueError('This action was not found.')
        save()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Coursework/1.0'

    def log_message(self, fmt, *args):
        pass  # No request bodies, private feed URLs, or credentials in logs.

    def send(self, status, content, content_type='application/json', extra=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Open Coursework from its localhost address.'})
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and self.path.startswith('/api/'):
            return self.send(403, {'error': 'Open Coursework directly.'})
        if self.path == '/api/state':
            return self.send(200, public_state())
        if self.path == '/api/export':
            with LOCK:
                backup = copy.deepcopy(STATE)
                backup.pop('sync', None)
            return self.send(200, backup, extra={'Content-Disposition': 'attachment; filename="coursework-backup.json"'})
        static = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8'), '/icon.svg': ('icon.svg', 'image/svg+xml')}
        if self.path not in static:
            return self.send(404, {'error': 'Not found'})
        file, kind = static[self.path]
        return self.send(200, (ROOT / 'static' / file).read_bytes(), kind)

    def do_POST(self):
        expected_origins = (f'http://localhost:{PORT}', f'http://127.0.0.1:{PORT}')
        if not self.valid_host() or self.headers.get('Origin') not in expected_origins or self.headers.get('X-Coursework-Token') != CSRF:
            return self.send(403, {'error': 'Refresh Coursework and try again.'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'Use JSON for this action.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 5_000_000:
                return self.send(413, {'error': 'That file is too large. Use a backup under 5 MB.'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('This request was not recognized.')
            mutate(self.path, data)
            return self.send(200, public_state())
        except (ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Check the file or form fields and try again.'
            return self.send(400, {'error': message})
        except Exception:
            return self.send(500, {'error': 'Something could not be saved. Check available disk space and try again.'})


def main():
    global PORT
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=DATA)
    args = parser.parse_args()
    PORT = args.port
    initialize(args.data_dir)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except PermissionError:
        print('Coursework was not allowed to start a local server. Open Start Coursework.command from Finder.')
        return
    except OSError:
        import urllib.request
        address = f'http://127.0.0.1:{PORT}'
        try:
            with urllib.request.urlopen(address + '/api/state', timeout=2) as response:
                if not response.headers.get('Server', '').startswith('Coursework/'):
                    raise ValueError('Another app is using this port.')
                if args.open:
                    webbrowser.open(address)
                print(f'Coursework is already running at {address}')
        except Exception:
            print(f'Another app is using port {PORT}. Start with a different port: python3 server.py --port 8766 --open')
        return
    threading.Thread(target=auto_sync, daemon=True).start()
    address = f'http://127.0.0.1:{PORT}'
    print(f'Coursework is ready at {address}\nKeep this window open while using your planner. Press Control-C to stop.', flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    main()
