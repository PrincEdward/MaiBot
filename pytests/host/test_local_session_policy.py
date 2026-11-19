import ast
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.host import sessions as module
from src.host.authentication import HostCredentials
from src.host.webui_auth import register_host_auth


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = HostCredentials(self.root / 'host/webui.json')
        self.credentials.update('LocalTestOnly!Abcd1234')
        self.sessions = module.HostSessions(self.root, credentials=self.credentials)

    def tearDown(self):
        self.temp.cleanup()

    def restart(self):
        return module.HostSessions(self.root, credentials=HostCredentials(self.credentials.path))

    def test_no_time_expiry_and_restart_preserves_session(self):
        token = self.sessions.create_session('http://127.0.0.1:8001')
        future = SimpleNamespace(time=lambda: time.time() + 100 * 365 * 86400)
        with patch.object(module, 'time', future):
            self.assertTrue(self.sessions.valid(token))
            restarted = self.restart()
            self.assertTrue(restarted.valid(token))
            self.assertEqual(restarted.login_origin(token), 'http://127.0.0.1:8001')
        with closing(sqlite3.connect(self.sessions._path)) as db:
            self.assertEqual(db.execute('SELECT origin FROM sessions').fetchone()[0], 'http://127.0.0.1:8001')

    def test_migration_preserves_active_but_never_revives_expired(self):
        with closing(sqlite3.connect(self.sessions._path)) as db, db:
            db.execute('ALTER TABLE sessions ADD COLUMN expires_at REAL NOT NULL DEFAULT 0')
            for token, expiry in [('old-active', time.time()+300), ('old-expired', time.time()-300), ('local-permanent', 0)]:
                db.execute('INSERT INTO sessions(digest, expires_at, origin) VALUES(?,?,?)',
                           (self.sessions._digest(token), expiry, None))
        restarted = self.restart()
        self.assertTrue(restarted.valid('old-active'))
        self.assertTrue(restarted.valid('local-permanent'))
        self.assertFalse(restarted.valid('old-expired'))

    def test_revoke_only_current_session_and_clear_all(self):
        a, b = self.sessions.create_session(), self.sessions.create_session()
        self.sessions.revoke(a)
        self.assertFalse(self.restart().valid(a))
        self.assertTrue(self.restart().valid(b))
        self.sessions.clear_all()
        self.assertFalse(self.restart().valid(b))

    def test_password_change_invalidates_even_across_restart(self):
        token = self.sessions.create_session()
        self.credentials.update('ChangedLocal!Abcd1234')
        self.assertFalse(self.sessions.valid(token))
        self.assertFalse(self.restart().valid(token))

    def test_ticket_still_one_use_and_short_lived(self):
        ticket = self.sessions.issue_ticket()
        token = self.sessions.exchange(ticket)
        self.assertTrue(self.sessions.valid(token))
        with self.assertRaises(HTTPException):
            self.sessions.exchange(ticket)
        ticket = self.sessions.issue_ticket()
        clock = SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic()+61)
        with patch.object(module, 'time', clock), self.assertRaises(HTTPException):
            self.sessions.exchange(ticket)

    def app(self):
        app = FastAPI()
        sessions = self.sessions
        async def require_session(request: Request):
            if not sessions.valid(request.cookies.get(sessions.cookie_name)):
                raise HTTPException(401)
        register_host_auth(app, sessions, self.credentials, require_session, lambda _: "127.0.0.1")
        @app.get('/api/private')
        async def private(request: Request):
            await require_session(request)
            return {'ok': True}
        @app.get('/api/error')
        async def error():
            raise HTTPException(400)
        # Execute the exact edited middleware in isolation; no host or QQ process starts.
        tree = ast.parse(Path(module.__file__).with_name('webui.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'protect_requests')
        env = {'app': app, 'sessions': sessions, 'stopping': asyncio.Event(), 'Request': Request,
            'Response': Response, 'Any': object, 'JSONResponse': JSONResponse,
            'same_webui_origin': lambda headers, origin: True}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'real_middleware', 'exec'), env)
        return app

    def login(self, client):
        response = client.post('/api/webui/auth/verify', json={'token': self.credentials.token})
        self.assertTrue(response.json()['valid'])
        return response

    def test_cookie_renewal_retains_protection_flags(self):
        with TestClient(self.app()) as client:
            self.login(client)
            response = client.get('/api/private')
            self.assertEqual(response.status_code, 200)
            cookie = response.headers['set-cookie']
            self.assertIn('Max-Age=34560000', cookie)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=lax', cookie)
            self.assertIn('Path=/', cookie)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_unauthenticated_requests_never_get_login_cookie(self):
        with TestClient(self.app()) as client:
            response = client.get('/api/private')
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('set-cookie', response.headers)
            response = client.get('/api/webui/auth/check')
            self.assertFalse(response.json()['authenticated'])
            self.assertNotIn('set-cookie', response.headers)

    def test_logout_does_not_get_renewed_by_middleware(self):
        with TestClient(self.app()) as client:
            self.login(client)
            old = client.cookies.get(self.sessions.cookie_name)
            response = client.post('/api/webui/auth/logout')
            self.assertIn('Max-Age=0', response.headers['set-cookie'])
            self.assertNotIn('34560000', response.headers['set-cookie'])
            self.assertFalse(self.sessions.valid(old))
            self.assertEqual(client.get('/api/private').status_code, 401)

    def test_relogin_reuses_valid_cookie_and_does_not_leak_slots(self):
        with TestClient(self.app()) as client:
            self.login(client)
            old = client.cookies.get(self.sessions.cookie_name)
            response = self.login(client)
            current = client.cookies.get(self.sessions.cookie_name)
            self.assertEqual(old, current)
            self.assertEqual(len(response.headers.get_list('set-cookie')), 1)
            self.assertTrue(self.sessions.valid(current))
            self.assertEqual(len(self.sessions._sessions), 1)

    def test_desktop_ticket_reuses_valid_session_but_not_foreign_origin(self):
        origin = 'http://127.0.0.1:8001'
        token = self.sessions.create_session(origin)
        for _ in range(70):
            ticket = self.sessions.issue_ticket()
            self.assertEqual(self.sessions.exchange(ticket, origin, token), token)
        self.assertEqual(len(self.sessions._sessions), 1)
        ticket = self.sessions.issue_ticket()
        other = self.sessions.exchange(ticket, 'http://localhost:8001', token)
        self.assertNotEqual(other, token)

    def test_password_update_does_not_get_renewed(self):
        with TestClient(self.app()) as client:
            self.login(client)
            response = client.post('/api/webui/auth/update', json={'new_token': 'ChangedLocal!ABcd123'})
            self.assertTrue(response.json()['success'])
            self.assertIn('Max-Age=0', response.headers['set-cookie'])
            self.assertEqual(client.get('/api/private').status_code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
