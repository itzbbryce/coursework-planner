import copy
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import canvas
import server


class CalendarTests(unittest.TestCase):
    def test_folded_escaped_timezone_and_date_only(self):
        text = 'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:assignment-42\r\nSUMMARY:Read chapter 2\\, then\r\n  review [CPEG 323]\r\nDTSTART;TZID=America/New_York:20261101T235900\r\nURL:https://udel.instructure.com/courses/1/assignments/42\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:event-2\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261123\r\nEND:VEVENT\r\nEND:VCALENDAR'
        result = canvas.parse_ics(text)
        a = result['assignments'][0]
        self.assertEqual(a['title'], 'Read chapter 2, then review')
        self.assertEqual(a['due'], '2026-11-01T23:59:00-05:00')
        self.assertEqual(result['courses'][0]['code'], 'CPEG 323')
        self.assertEqual(result['assignments'][1]['due'], '2026-11-23')
        self.assertEqual(result['assignments'][1]['kind'], 'Event')

    def test_cancelled_repeating_and_invalid_dates_are_not_false_deadlines(self):
        source = 'BEGIN:VCALENDAR\n'
        for name, special in [('cancelled','STATUS:CANCELLED'),('repeat','RRULE:FREQ=WEEKLY'),('invalid','DTSTART:not-a-date')]:
            source += f'BEGIN:VEVENT\nUID:{name}\nSUMMARY:{name}\n{special}\nEND:VEVENT\n'
        source += 'END:VCALENDAR'
        result = canvas.parse_ics(source)
        self.assertEqual(result['assignments'], [])
        self.assertEqual(len(result['warnings']), 2)

    def test_login_page_is_rejected(self):
        with self.assertRaises(canvas.CanvasError):
            canvas.parse_ics('<html>Please log in</html>')

    def test_stable_identity_across_due_date_change(self):
        feed = 'BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:assignment-42\nSUMMARY:Project\nDTSTART:20261029T235900Z\nEND:VEVENT\nEND:VCALENDAR'
        a = canvas.parse_ics(feed)['assignments'][0]
        b = canvas.parse_ics(feed.replace('20261029', '20261030'))['assignments'][0]
        self.assertEqual(a['id'], b['id'])


class NetworkTests(unittest.TestCase):
    def test_private_destinations_rejected(self):
        with patch('canvas.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(canvas.CanvasError):
                canvas.safe_url('https://localhost/feed.ics')
        for url in ['http://udel.instructure.com', 'https://name:password@udel.instructure.com', 'file:///tmp/private']:
            with self.assertRaises(canvas.CanvasError):
                canvas.safe_url(url)

    def test_pagination_and_no_credential_leak_on_host_change(self):
        calls = [(json.dumps([{'id':1}]).encode(), {'Link':'<https://udel.instructure.com/api/v1/courses?page=2>; rel="next"'}), (b'[{"id":2}]', {})]
        with patch('canvas.fetch', side_effect=calls) as fetch:
            self.assertEqual(len(canvas.api_list('https://udel.instructure.com','/api/v1/courses','SECRET')), 2)
            self.assertEqual(fetch.call_count, 2)
        with patch('canvas.fetch', return_value=(b'[]', {'Link':'<https://other.example/api>; rel="next"'})) as fetch:
            with self.assertRaises(canvas.CanvasError):
                canvas.api_list('https://udel.instructure.com','/api/v1/courses','SECRET')
            self.assertEqual(fetch.call_count, 1)

    def test_canvas_submission_and_grade_mapping(self):
        pages = [[{'id':1,'name':'Computer Systems','course_code':'CPEG 323','enrollments':[{'computed_current_score':92.5}]}],
                 [{'id':42,'name':'Project','due_at':'2026-10-29T23:59:00Z','submission_types':['online_upload'],
                   'submission':{'workflow_state':'submitted','score':9},'points_possible':10,'description':'<p>Do &amp; learn</p>'}], []]
        with patch('canvas.api_list', side_effect=pages):
            result = canvas.sync_api({'url':'https://udel.instructure.com','token':'SECRET'})
        self.assertEqual(result['courses'][0]['grade'], 92.5)
        self.assertTrue(result['assignments'][0]['submitted'])
        self.assertEqual(result['assignments'][0]['description'], 'Do & learn')


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.initialize(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def manual(self):
        server.mutate('/api/course', {'name':'Systems', 'code':'CPEG 323', 'color':'#357760'})
        cid = server.STATE['courses'][0]['id']
        server.mutate('/api/assignment', {'title':'Project', 'courseId':cid, 'due':'2026-10-30T03:59:00Z'})
        return cid, server.STATE['assignments'][0]['id']

    def snapshot(self):
        return {'courses':[{'id':'canvas-1','name':'Systems','code':'CPEG 323','color':'#357760','grade':95,'source':'canvas','url':''}],
                'assignments':[{'id':'canvas-2','title':'Project','courseId':'canvas-1','due':'2026-10-29T23:59:00Z','kind':'Assignment','source':'canvas','done':False,'submitted':False,'notes':'','url':''}],
                'announcements':[], 'warnings':[]}

    def test_crud_persistence_and_course_removal_preserves_work(self):
        cid, aid = self.manual()
        server.mutate('/api/assignment/toggle', {'id':aid,'done':True})
        server.initialize(self.temp.name)
        self.assertTrue(server.STATE['assignments'][0]['done'])
        server.mutate('/api/course/delete', {'id':cid})
        self.assertEqual(server.STATE['assignments'][0]['courseId'], '')
        server.mutate('/api/assignment/delete', {'id':aid})
        self.assertEqual(server.STATE['assignments'], [])

    def test_sync_preserves_notes_checks_and_manual_assignments(self):
        _, manual_id = self.manual()
        config={'mode':'api','url':'https://udel.instructure.com','token':'SECRET'}
        with patch('server.synchronize', return_value=self.snapshot()):
            server.sync(config)
        server.mutate('/api/assignment/toggle', {'id':'canvas-2','done':True})
        server.mutate('/api/assignment', {'id':'canvas-2','notes':'Start with wiring','title':'Cannot override'})
        snapshot=self.snapshot();snapshot['assignments'][0]['due']='2026-11-01T23:59:00Z'
        with patch('server.synchronize', return_value=snapshot):
            server.sync()
        assignment=server.find_item('assignments','canvas-2')
        self.assertTrue(assignment['done'])
        self.assertEqual(assignment['notes'],'Start with wiring')
        self.assertEqual(assignment['title'],'Project')
        self.assertEqual(assignment['due'],'2026-11-01T23:59:00Z')
        self.assertEqual(server.find_item('assignments',manual_id)['source'],'manual')

    def test_failed_sync_preserves_previous_snapshot_and_credentials(self):
        self.manual()
        previous=copy.deepcopy(server.STATE['assignments'])
        with patch('server.synchronize', side_effect=canvas.CanvasError('Unavailable')):
            with self.assertRaises(canvas.CanvasError):
                server.sync({'mode':'api','url':'https://udel.instructure.com','token':'SECRET'})
        self.assertEqual(server.STATE['assignments'],previous)
        self.assertEqual(server.CONFIG,{})
        self.assertEqual(server.STATE['sync']['error'],'Unavailable')

    def test_credentials_not_in_public_state_and_private_permissions(self):
        config={'mode':'feed','url':'https://udel.instructure.com/feeds/calendars/SECRET.ics'}
        with patch('server.synchronize', return_value=self.snapshot()):
            server.sync(config)
        self.assertNotIn('SECRET', json.dumps(server.public_state()))
        self.assertEqual((Path(self.temp.name)/'connection.json').stat().st_mode & 0o777,0o600)
        server.mutate('/api/disconnect',{})
        self.assertFalse(server.public_state()['connection']['connected'])
        self.assertEqual(len(server.STATE['assignments']),1)

    def test_backup_import_and_atomic_validation(self):
        self.manual()
        backup=copy.deepcopy(server.STATE)
        server.mutate('/api/import',{'backup':backup})
        self.assertEqual(len(server.STATE['assignments']),2)
        before=copy.deepcopy(server.STATE)
        backup['assignments'][0]['due']='invalid'
        with self.assertRaises(ValueError):
            server.mutate('/api/import',{'backup':backup})
        self.assertEqual(server.STATE,before)

    def test_invalid_timezone_due_and_course_are_rejected(self):
        for path, data in [('/api/profile',{'name':'Bryce','timezone':'Invalid/Zone'}),
                           ('/api/assignment',{'title':'Test','due':'2026-13-99'}),
                           ('/api/assignment',{'title':'Test','due':'2026-10-29T23:59:00'}),
                           ('/api/assignment',{'title':'Test','courseId':'missing'})]:
            with self.assertRaises(ValueError):
                server.mutate(path,data)

    def test_course_reappearing_does_not_duplicate(self):
        config={'mode':'api','url':'https://udel.instructure.com','token':'SECRET'}
        with patch('server.synchronize',return_value=self.snapshot()):
            server.sync(config)
        server.mutate('/api/assignment',{'title':'Personal work','courseId':'canvas-1'})
        with patch('server.synchronize',return_value={'courses':[],'assignments':[],'announcements':[],'warnings':[]}):
            server.sync()
        self.assertEqual(server.STATE['courses'][0]['source'],'manual')
        with patch('server.synchronize',return_value=self.snapshot()):
            server.sync()
        self.assertEqual(len(server.STATE['courses']),1)


if __name__ == '__main__':
    unittest.main()
