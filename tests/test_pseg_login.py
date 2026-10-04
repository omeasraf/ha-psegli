"""Regression checks for audio availability and bounded session recovery."""
import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ADDON = Path(__file__).resolve().parents[1] / 'addons' / 'psegli-automation'
sys.path.insert(0, str(ADDON))
from auto_login import PSEGAutoLogin
from login_policy import LoginGate

spec = importlib.util.spec_from_file_location('pseg_addon_server', ADDON / 'run.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def element(count=0, value=None, text='', visible=True):
    e = MagicMock()
    e.first = e
    e.count = AsyncMock(return_value=count)
    e.get_attribute = AsyncMock(return_value=value)
    e.inner_text = AsyncMock(return_value=text)
    e.is_visible = AsyncMock(return_value=visible)
    e.wait_for = AsyncMock()
    e.click = AsyncMock()
    e.fill = AsyncMock()
    return e


class AudioTests(unittest.IsolatedAsyncioTestCase):
    def make_frame(self, elements):
        frame = MagicMock()
        frame.url = 'https://www.google.com/recaptcha/api2/bframe'
        frame.locator.side_effect = lambda selector: elements.get(selector, element())
        return frame

    async def test_falls_back_when_audio_element_has_no_source(self):
        frame = self.make_frame({
            'body': element(text='Enter the words you hear'),
            '#audio-source': element(count=1),
            '.rc-audiochallenge-tdownload-link': element(count=1, value='/audio'),
        })
        self.assertEqual(await PSEGAutoLogin('', '')._audio_challenge_state(frame), ('ready', '/audio'))

    async def test_refusal_is_not_an_audio_timeout(self):
        frame = self.make_frame({'body': element(text='Your computer or network may be sending automated queries. Please try again later.')})
        self.assertEqual(await PSEGAutoLogin('', '')._audio_challenge_state(frame), ('blocked', None))

    async def test_blocked_audio_never_reaches_transcription(self):
        frame = self.make_frame({'#recaptcha-audio-button': element(count=1)})
        login = PSEGAutoLogin('', '')
        login.page = MagicMock(frames=[frame])
        login._audio_challenge_state = AsyncMock(return_value=('blocked', None))
        login._save_captcha_diagnostic = AsyncMock()
        with patch.object(login, '_recognize_audio') as recognize:
            self.assertFalse(await login._solve_recaptcha_audio())
            recognize.assert_not_called()
        self.assertIn('declined', login._save_captcha_diagnostic.call_args.args[0])

    async def test_submitting_an_answer_is_not_success(self):
        frame = self.make_frame({
            '#recaptcha-audio-button': element(count=1),
            '#audio-response': element(count=1),
            '#recaptcha-verify-button': element(count=1),
            '.rc-audiochallenge-error-message': element(count=1, text='Incorrect. Try again.'),
        })
        login = PSEGAutoLogin('', '')
        login.page = MagicMock(frames=[frame], url='https://mysmartenergy.psegliny.com/')
        login.page.content = AsyncMock(return_value='<input id="LoginEmail">')
        login.page.request.get = AsyncMock(return_value=MagicMock(ok=True, body=AsyncMock(return_value=b'audio')))
        login._audio_challenge_state = AsyncMock(return_value=('ready', 'https://www.google.com/audio'))
        login._save_captcha_diagnostic = AsyncMock()
        with patch.object(login, '_recognize_audio', return_value='sample answer'), patch('auto_login.asyncio.sleep', new=AsyncMock()):
            self.assertFalse(await login._solve_recaptcha_audio())
        self.assertIn('not accept', login._save_captcha_diagnostic.call_args.args[0])


class GateTests(unittest.TestCase):
    def test_restart_preserves_cooldown_and_success_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gate.json'
            gate = LoginGate(path)
            self.assertTrue(gate.allowed())
            gate.attempted()
            restarted = LoginGate(path)
            self.assertFalse(restarted.allowed())
            restarted.succeeded()
            self.assertTrue(LoginGate(path).allowed())


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        server._automatic_login_gate = LoginGate(Path(self.directory.name) / 'gate.json')
        server._browser_lock = asyncio.Lock()
        server._mfa_session = None
        self.request = server.SessionRefreshRequest(cookie='old', username='user', password='password', allow_login=True)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def session(self, error, cookie=None):
        return MagicMock(last_error=error, refresh_saved_session=AsyncMock(return_value=cookie), get_cookies=AsyncMock(return_value=None))

    async def test_timeout_does_not_submit_password(self):
        session = self.session('Saved session refresh failed: timeout')
        with patch.object(server, 'PSEGAutoLogin', return_value=session):
            result = await server.refresh_session(self.request)
        self.assertFalse(result.success)
        session.get_cookies.assert_not_awaited()
        self.assertTrue(server._automatic_login_gate.allowed())

    async def test_repeated_polls_only_attempt_one_login(self):
        session = self.session('Saved browser and My Account sessions are not authenticated')
        with patch.object(server, 'PSEGAutoLogin', return_value=session):
            first = await server.refresh_session(self.request)
            second = await server.refresh_session(self.request)
        self.assertFalse(first.success)
        self.assertFalse(second.success)
        session.get_cookies.assert_awaited_once()
        self.assertIsNotNone(second.retry_after)

    async def test_successful_automatic_login_returns_cookie(self):
        session = self.session('Saved browser and My Account sessions are not authenticated')
        session.get_cookies.return_value = 'MM_SID=renewed'
        with patch.object(server, 'PSEGAutoLogin', return_value=session):
            result = await server.refresh_session(self.request)
        self.assertTrue(result.success)
        self.assertEqual(result.cookies, 'MM_SID=renewed')
        self.assertTrue(server._automatic_login_gate.allowed())

    async def test_keepalive_cannot_submit_credentials(self):
        session = self.session('Saved browser and My Account sessions are not authenticated')
        with patch.object(server, 'PSEGAutoLogin', return_value=session):
            await server.refresh_session(server.SessionRefreshRequest(cookie='old'))
        session.get_cookies.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
