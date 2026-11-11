"""Read-only Canvas integration. No third-party packages required."""
import datetime as dt
import hashlib
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


class CanvasError(ValueError):
    pass


def safe_url(value):
    value = str(value).strip().replace('webcal://', 'https://', 1)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise CanvasError('Use an https:// address from your school’s Canvas website.')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise CanvasError('Use your school’s public Canvas website address.')
    except socket.gaierror:
        raise CanvasError('That website could not be found. Check the address and your internet connection.') from None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a Canvas credential to a redirected sign-in page or another host.
        raise CanvasError('Canvas redirected this address. Use the address shown after you sign in to Canvas.')


def fetch(url, token=None):
    url = safe_url(url)
    headers = {'Accept': 'application/json' if token else 'text/calendar', 'User-Agent': 'Coursework-Personal/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=25) as response:
            data = response.read(12_000_001)
            if len(data) > 12_000_000:
                raise CanvasError('The Canvas response was too large to import safely.')
            return data, response.headers
    except urllib.error.HTTPError as exc:
        messages = {401: 'Canvas did not accept that token. It may have expired; create a new one in Canvas Settings.',
                    403: 'Canvas denied access. Your school may restrict this feature; try the calendar feed instead.',
                    404: 'Canvas could not find that address. Check your school URL or calendar feed link.',
                    429: 'Canvas is busy. Wait a few minutes and sync again.'}
        raise CanvasError(messages.get(exc.code, 'Canvas is temporarily unavailable. Try syncing again later.')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CanvasError('Could not reach Canvas. Your saved assignments are still here; try again when you’re online.') from None


def api_list(base, path, token):
    url = base + path
    items, visited = [], set()
    for _ in range(100):
        if url in visited or urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base).netloc:
            raise CanvasError('Canvas returned an unexpected pagination link.')
        visited.add(url)
        raw, headers = fetch(url, token)
        try:
            page = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CanvasError('This address did not return Canvas data. Check your school URL.') from None
        if not isinstance(page, list):
            raise CanvasError('Canvas returned an unexpected response.')
        items.extend(page)
        match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get('Link', ''))
        if not match:
            return items
        url = urllib.parse.urljoin(base, match.group(1))
    raise CanvasError('Canvas returned too many pages. No partial import was saved.')


def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', ' ', value or '')).strip()[:5000]


def sync_api(config):
    base, token = config['url'].rstrip('/'), config['token']
    raw_courses = api_list(base, '/api/v1/courses?enrollment_state=active&enrollment_type=student&include[]=total_scores&per_page=100', token)
    courses, assignments, announcements, warnings = [], [], [], []
    palette = ['#357760', '#b9713b', '#6765a9', '#397eaa', '#ad5c7b', '#76843d']
    for i, course in enumerate(raw_courses):
        if not course.get('name') or course.get('access_restricted_by_date'):
            continue
        cid = 'canvas-' + str(course['id'])
        enrollments = course.get('enrollments') or []
        score = next((e.get('computed_current_score') for e in enrollments if e.get('computed_current_score') is not None), None)
        courses.append({'id': cid, 'name': course['name'], 'code': course.get('course_code', course['name']),
                        'color': palette[i % len(palette)], 'source': 'canvas', 'grade': score, 'url': base + '/courses/' + str(course['id'])})
        # Assignment failures abort the import, preserving the last complete snapshot.
        rows = api_list(base, '/api/v1/courses/' + str(course['id']) + '/assignments?include[]=submission&per_page=100', token)
        for row in rows:
            submission = row.get('submission') or {}
            types = row.get('submission_types') or []
            kind = 'Quiz' if 'online_quiz' in types else 'Discussion' if 'discussion_topic' in types else 'Assignment'
            submitted = submission.get('workflow_state') in ('submitted', 'graded', 'pending_review') or bool(submission.get('excused'))
            assignments.append({'id': 'canvas-' + str(row['id']), 'title': row.get('name', 'Assignment'), 'courseId': cid,
                                'due': row.get('due_at') or '', 'kind': kind, 'source': 'canvas',
                                'submitted': submitted, 'done': False, 'notes': '', 'url': row.get('html_url', ''),
                                'points': row.get('points_possible'), 'score': submission.get('score'), 'description': plain(row.get('description'))})
        try:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).date().isoformat()
            rows = api_list(base, '/api/v1/announcements?context_codes[]=course_' + str(course['id']) + '&start_date=' + since + '&per_page=100', token)
            for row in rows:
                announcements.append({'id': 'canvas-' + str(row['id']), 'courseId': cid, 'title': row.get('title', ''),
                                      'body': plain(row.get('message')), 'date': row.get('posted_at') or row.get('created_at') or '', 'url': row.get('html_url', '')})
        except CanvasError:
            warnings.append('Announcements are unavailable for ' + course.get('course_code', course['name']) + '.')
    return {'courses': courses, 'assignments': assignments, 'announcements': announcements, 'warnings': warnings}


def unescape_ics(value):
    return re.sub(r'\\([nN,;\\])', lambda m: '\n' if m[1].lower() == 'n' else m[1], value)


def ics_date(value, params, timezone):
    if not value:
        return ''
    if re.fullmatch(r'\d{8}', value):
        return dt.datetime.strptime(value, '%Y%m%d').date().isoformat()
    if not re.fullmatch(r'\d{8}T\d{6}Z?', value):
        raise ValueError('Unsupported calendar date')
    zone = dt.timezone.utc if value.endswith('Z') else ZoneInfo(params.get('TZID', timezone).strip('"'))
    return dt.datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S').replace(tzinfo=zone).isoformat()


def parse_ics(text, timezone='America/New_York'):
    if 'BEGIN:VCALENDAR' not in text:
        raise CanvasError('That link did not return a calendar. Copy the Calendar Feed link from Canvas → Calendar.')
    unfolded = re.sub(r'\r?\n[ \t]', '', text)
    events, current = [], None
    for line in unfolded.splitlines():
        if line == 'BEGIN:VEVENT':
            current = {}
        elif line == 'END:VEVENT' and current is not None:
            events.append(current)
            current = None
        elif current is not None and ':' in line:
            key, value = line.split(':', 1)
            pieces = key.split(';')
            current[pieces[0]] = (value, dict(p.split('=', 1) for p in pieces[1:] if '=' in p))
    courses, assignments, warnings = {}, [], []
    for event in events:
        get = lambda k: unescape_ics(event.get(k, ('', {}))[0])
        if get('STATUS') == 'CANCELLED':
            continue
        if 'RRULE' in event or 'RECURRENCE-ID' in event:
            warnings.append('A recurring calendar item was skipped. Check repeating class meetings in Canvas.')
            continue
        title = get('SUMMARY') or 'Calendar item'
        course_match = re.search(r'\s*\[([^\]]+)\]\s*$', title)
        code = course_match[1] if course_match else 'Canvas calendar'
        if course_match:
            title = title[:course_match.start()].strip()
        cid = 'feed-' + hashlib.sha256(code.encode()).hexdigest()[:12]
        courses[cid] = {'id': cid, 'code': code, 'name': code, 'source': 'feed', 'color': '#357760', 'grade': None, 'url': ''}
        try:
            date_value, params = event.get('DTSTART', ('', {}))
            due = ics_date(date_value, params, timezone)
        except (ValueError, KeyError):
            warnings.append('A calendar item had an unsupported date and was skipped.')
            continue
        uid = get('UID') or title + due
        assignments.append({'id': 'feed-' + hashlib.sha256(uid.encode()).hexdigest()[:24], 'title': title, 'due': due,
                            'courseId': cid, 'kind': 'Assignment' if 'assignment' in uid.lower() else 'Event',
                            'source': 'feed', 'submitted': False, 'done': False, 'notes': '', 'url': get('URL'),
                            'points': None, 'score': None, 'description': get('DESCRIPTION')[:5000]})
    return {'courses': list(courses.values()), 'assignments': assignments, 'announcements': [], 'warnings': list(dict.fromkeys(warnings))}


def synchronize(config, timezone):
    if config['mode'] == 'api':
        return sync_api(config)
    raw, _ = fetch(config['url'])
    return parse_ics(raw.decode('utf-8-sig'), timezone)
