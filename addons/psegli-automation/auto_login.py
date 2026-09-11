#!/usr/bin/env python3
"""
PSEG Long Island Auto Login Addon
Uses realistic browsing pattern to avoid detection and obtain authentication cookies.
"""

import asyncio
import json
import logging
import os
import random
import subprocess
import tempfile
import time
from http.cookies import SimpleCookie
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
import speech_recognition as sr

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
STORAGE_STATE_PATH = os.path.join(DATA_DIR, "storage_state.json")
BROWSER_PROFILE_PATH = os.path.join(DATA_DIR, "browser_profile")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
_LOGGER = logging.getLogger(__name__)

class PSEGAutoLogin:
    """PSEG Long Island automated login using realistic browsing pattern."""
    
    def __init__(
        self,
        email: str,
        password: str,
        mfa_code: Optional[str] = None,
        mfa_method: str = "sms",  # "email" or "sms"
        headless: bool = True,
    ):
        """Initialize PSEG auto login."""
        self.email = email
        self.password = password
        self.mfa_code = mfa_code
        self.mfa_method = mfa_method.lower() if mfa_method else "sms"
        self.headless = headless
        self.storage_state_path = STORAGE_STATE_PATH
        self.browser_profile_path = BROWSER_PROFILE_PATH
        self.last_error = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.login_cookies = {}
        self.exceptional_dashboard_data = None
        
        # URLs for the realistic browsing flow
        self.brave_search_url = "https://search.brave.com/search?q=PSEG+Smart+Energy&source=desktop"
        self.mysmartenergy_dashboard_url = "https://mysmartenergy.psegliny.com/Dashboard"
        self.login_page_url = "https://myaccount.psegliny.com/user/login"
        self.id_domain = "https://id.myaccount.psegliny.com/"
        self.dashboard_url = "https://myaccount.psegliny.com/dashboards"
        self.exceptional_dashboard = "https://myaccount.psegliny.com/dashboards/exceptionaldashboard"
        self.mysmartenergy_redirect = "https://myaccount.psegliny.com/LI/Header/RedirectMDMWidget"
        self.final_dashboard = "https://mysmartenergy.psegliny.com/Dashboard"
    
    async def setup_browser(self) -> bool:
        """Initialize Playwright browser with stealth options."""
        try:
            _LOGGER.info("🚀 Initializing Playwright browser...")
            self.playwright = await async_playwright().start()
            
            # Launch browser with clean anti-detection options
            launch_args = [
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-infobars',
            ]
            # A persistent Chromium profile preserves more than Playwright's
            # storage_state (browser identity, IndexedDB, service workers, etc.).
            # That matters because PSEG otherwise sees every refresh as a new
            # browser and is much more likely to present reCAPTCHA.
            context_kwargs = {
                'viewport': {'width': 1920, 'height': 1080},
                'locale': 'en-US',
                'timezone_id': 'America/New_York',
                'permissions': ['geolocation'],
                'screen': {
                    'width': 1920,
                    'height': 1080
                }
            }
            os.makedirs(self.browser_profile_path, exist_ok=True)
            self.context = await self.playwright.chromium.launch_persistent_context(
                self.browser_profile_path,
                headless=self.headless,
                args=launch_args,
                ignore_default_args=['--enable-automation'],
                **context_kwargs,
            )

            # Import cookies from releases that only used storage_state.json.
            # The persistent profile takes over after this first migration.
            if (
                os.path.exists(self.storage_state_path)
                and os.path.getsize(self.storage_state_path) > 0
                and not await self.context.cookies()
            ):
                try:
                    with open(self.storage_state_path, encoding="utf-8") as state_file:
                        legacy_state = json.load(state_file)
                    legacy_cookies = legacy_state.get("cookies", [])
                    if legacy_cookies:
                        await self.context.add_cookies(legacy_cookies)
                        _LOGGER.info("📂 Migrated saved cookies into the persistent browser profile")
                except Exception as sse:
                    _LOGGER.warning(f"Could not migrate saved browser state: {sse}")
            
            # Apply playwright-stealth if available
            if HAS_STEALTH:
                try:
                    await Stealth().apply_stealth_async(self.context)
                    _LOGGER.info("✅ Applied playwright-stealth to browser context")
                except Exception as ste:
                    _LOGGER.warning(f"Could not apply playwright-stealth: {ste}")
            
            # Persistent contexts can reopen their previous page. Reuse it so
            # browser state is not needlessly discarded.
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            
            # Set up request interception
            await self.setup_request_interception()
            
            _LOGGER.info("✅ Playwright browser initialized successfully")
            return True
            
        except Exception as e:
            _LOGGER.error(f"Failed to setup browser: {e}")
            return False

    @staticmethod
    def _is_authenticated_dashboard(current_url: str, page_content: str) -> bool:
        """Return whether the loaded page is an authenticated Smart Energy dashboard."""
        page_content_lower = page_content.lower()
        return (
            "mysmartenergy.psegliny.com" in current_url
            and "/dashboard" in current_url.lower()
            and (
                "__requestverificationtoken" in page_content_lower
                or 'id="propertyselect"' in page_content_lower
                or 'id="ajaxcontent"' in page_content_lower
            )
            and "loginemail" not in page_content_lower
        )

    async def _seed_smart_energy_cookies(self, cookie_string: str) -> None:
        """Import an existing HA cookie header into the persistent browser profile."""
        if not cookie_string:
            return

        parsed = SimpleCookie()
        try:
            parsed.load(cookie_string)
        except Exception as exc:
            _LOGGER.warning("Could not parse existing Home Assistant cookies: %s", exc)
            return

        cookies = [
            {
                "name": name,
                "value": morsel.value,
                "domain": ".mysmartenergy.psegliny.com",
                "path": "/",
                "secure": True,
            }
            for name, morsel in parsed.items()
            if name and morsel.value
        ]
        if cookies:
            await self.context.add_cookies(cookies)
            _LOGGER.info("🍪 Imported the active Home Assistant session into the browser profile")

    async def _capture_cookies_and_state(self) -> str:
        """Capture the authenticated cookie jar and persist browser state."""
        context_cookies = await self.context.cookies()
        for cookie in context_cookies:
            if cookie['domain'].lstrip('.').endswith('psegliny.com'):
                self.login_cookies[cookie['name']] = cookie['value']

        os.makedirs(os.path.dirname(self.storage_state_path), exist_ok=True)
        await self.context.storage_state(path=self.storage_state_path, indexed_db=True)
        _LOGGER.info("💾 Refreshed persistent browser session")
        return self.format_cookies_for_api()

    async def refresh_saved_session(self, cookie_string: str = "") -> Optional[str]:
        """Keep an existing browser session alive without submitting credentials."""
        try:
            if not await self.setup_browser():
                self.last_error = "Failed to set up browser for session refresh"
                return None

            await self._seed_smart_energy_cookies(cookie_string)
            _LOGGER.info("🔄 Refreshing saved Smart Energy browser session...")
            await self.page.goto(self.final_dashboard, wait_until='domcontentloaded')
            try:
                await self.page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass

            page_content = await self.page.content()
            if not self._is_authenticated_dashboard(self.page.url, page_content):
                self.last_error = "Saved browser session is not authenticated"
                _LOGGER.warning("⚠️ Saved browser session is not authenticated; credentials were not submitted")
                return None

            cookies = await self._capture_cookies_and_state()
            return cookies or None
        except Exception as exc:
            self.last_error = f"Saved session refresh failed: {exc}"
            _LOGGER.warning("⚠️ %s", self.last_error)
            return None
        finally:
            await self.cleanup()
    
    async def setup_request_interception(self):
        """Set up request interception to capture cookies and exceptional dashboard data."""
        try:
            await self.page.route("**/*", self.handle_request)
            _LOGGER.info("✅ Request interception setup complete")
        except Exception as e:
            _LOGGER.warning(f"Could not setup request interception: {e}")
    
    async def handle_request(self, route):
        """Handle intercepted requests to capture cookies and exceptional dashboard data."""
        try:
            request = route.request
            if "mysmartenergy.psegliny.com" in request.url:
                # Capture cookies from MySmartEnergy requests
                if hasattr(request, 'headers') and 'cookie' in request.headers:
                    cookie_header = request.headers['cookie']
                    if cookie_header:
                        # Parse cookies and store them
                        self.parse_cookies(cookie_header)
            elif "exceptionaldashboard" in request.url and request.method == "POST":
                # Capture exceptional dashboard request data
                _LOGGER.info("🔍 Intercepted exceptional dashboard POST request")
                self.exceptional_dashboard_data = {
                    'url': request.url,
                    'method': request.method,
                    'headers': dict(request.headers),
                    'post_data': request.post_data if hasattr(request, 'post_data') else None
                }
                _LOGGER.info(f"📋 Captured exceptional dashboard data")
        except Exception as e:
            _LOGGER.debug(f"Error handling request: {e}")
        
        # Continue with the request
        await route.continue_()

    @staticmethod
    def _recognize_audio(audio_bytes: bytes) -> str:
        """Convert a reCAPTCHA MP3 challenge to WAV and transcribe it."""
        recognizer = sr.Recognizer()
        with tempfile.TemporaryDirectory() as temp_dir:
            mp3_path = os.path.join(temp_dir, "challenge.mp3")
            wav_path = os.path.join(temp_dir, "challenge.wav")
            with open(mp3_path, "wb") as audio_file:
                audio_file.write(audio_bytes)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3_path, wav_path],
                check=True,
            )
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
        return recognizer.recognize_google(audio)

    async def _solve_recaptcha_audio(self) -> bool:
        """Attempt the no-key audio challenge path using SpeechRecognition."""
        try:
            anchor_frame = next(
                (frame for frame in self.page.frames if "api2/anchor" in frame.url),
                None,
            )
            if not anchor_frame:
                return False

            checkbox = anchor_frame.locator("#recaptcha-anchor")
            if await checkbox.count() and not await checkbox.is_checked():
                await checkbox.click()
                await asyncio.sleep(1)

            challenge_frame = next(
                (frame for frame in self.page.frames if "api2/bframe" in frame.url),
                None,
            )
            if not challenge_frame:
                return False

            audio_button = challenge_frame.locator("#recaptcha-audio-button")
            if not await audio_button.count() or not await audio_button.is_visible():
                return False
            await audio_button.click()

            audio_link = challenge_frame.locator(
                "#audio-source, .rc-audiochallenge-tdownload-link"
            ).first
            await audio_link.wait_for(state="visible", timeout=10000)
            audio_url = await audio_link.get_attribute("src") or await audio_link.get_attribute("href")
            if not audio_url:
                return False

            audio_response = await self.page.request.get(audio_url)
            if not audio_response.ok:
                return False
            transcript = await asyncio.to_thread(
                self._recognize_audio, await audio_response.body()
            )
            _LOGGER.info("🎙️ Transcribed reCAPTCHA audio challenge")
            await challenge_frame.locator("#audio-response").fill(transcript)
            await challenge_frame.locator("#recaptcha-verify-button").click()
            await asyncio.sleep(2)
            return True
        except Exception as exc:
            _LOGGER.warning("SpeechRecognition CAPTCHA solve failed; keeping manual fallback: %s", exc)
            return False

    async def _dismiss_access_banner(self) -> None:
        """Dismiss the sticky access-message banner when it overlays the login form."""
        banner = self.page.locator("#errorMessageHousing")
        try:
            if not await banner.count() or not await banner.is_visible():
                return

            close_button = banner.locator("button.btn-close, button[aria-label='Close']")
            if await close_button.count() and await close_button.is_visible():
                await close_button.click(force=True)
                _LOGGER.debug("Dismissed PSEG access-message banner")

            # Some responses leave the sticky wrapper in the DOM after closing.
            if await banner.is_visible():
                await banner.evaluate(
                    "element => element.style.setProperty('display', 'none', 'important')"
                )
        except Exception as exc:
            _LOGGER.debug("Could not dismiss PSEG access-message banner: %s", exc)
    
    def _log_mfa_error(self, current_url: str):
        """Log clear error when MFA is required."""
        _LOGGER.error("❌ PSEG now requires multi-factor authentication (MFA).")
        _LOGGER.error("   After entering your password, you receive a verification code via email or SMS.")
        _LOGGER.error("   This addon cannot complete MFA automatically.")
        _LOGGER.error("   Workaround: Use the 'mfa_code' parameter when calling the addon API")
        _LOGGER.error("   (check your email for the code, then retry with the code).")
        _LOGGER.error("   Or log in manually in a browser and export cookies for the integration.")
        _LOGGER.error(f"   Current URL: {current_url[:100]}...")
    
    def parse_cookies(self, cookie_header: str):
        """Parse cookie header and extract important cookies."""
        try:
            cookies = cookie_header.split(';')
            for cookie in cookies:
                cookie = cookie.strip()
                if '=' in cookie:
                    name, value = cookie.split('=', 1)
                    name = name.strip()
                    value = value.strip()
                    
                    # Store important cookies
                    if name in ['MM_SID', '__RequestVerificationToken', 'ASP.NET_SessionId']:
                        self.login_cookies[name] = value
        except Exception as e:
            _LOGGER.warning(f"Error parsing cookies: {e}")
    
    async def simulate_realistic_browsing(self) -> bool:
        """Simulate realistic browsing pattern to avoid detection."""
        try:
            _LOGGER.info("🌐 Starting realistic browsing pattern...")
            
            # Set page timeout to be more generous for the entire process
            self.page.set_default_timeout(30000)  # 30 seconds instead of 20
            
            # Step 1: Start with Brave search
            _LOGGER.info("🔍 Step 1: Navigating to Brave search...")
            await self.page.goto(self.brave_search_url, wait_until='domcontentloaded')
            await asyncio.sleep(random.uniform(2.0, 3.0))
            
            # Simulate reading search results
            await self.page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            _LOGGER.info("✅ Brave search loaded")
            
            # Step 2: Open Smart Energy directly
            _LOGGER.info("🏠 Step 2: Navigating directly to PSEG Smart Energy...")
            await self.page.goto(self.mysmartenergy_dashboard_url, wait_until='domcontentloaded')
            try:
                await self.page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass

            current_url = self.page.url
            page_content = await self.page.content()
            page_content_lower = page_content.lower()
            await self._dismiss_access_banner()

            # Check if we are already authenticated on Smart Energy dashboard.
            is_smart_energy_dashboard = self._is_authenticated_dashboard(current_url, page_content)

            if is_smart_energy_dashboard:
                _LOGGER.info("✅ Already authenticated on Smart Energy dashboard (session active)")
            else:
                # Step 4: Fill login form on Smart Energy
                _LOGGER.info("📝 Step 4: Filling Smart Energy login form...")

                # Wait for email and password fields
                email_field = await self.page.wait_for_selector(
                    'input[name="LoginEmail"], #LoginEmail, input[type="email"], input[type="text"]',
                    timeout=10000,
                )
                password_field = await self.page.wait_for_selector(
                    'input[name="LoginPassword"], #LoginPassword, input[name="password"], input[type="password"]',
                    timeout=10000,
                )

                if not email_field or not password_field:
                    _LOGGER.error("❌ Login form fields not found on Smart Energy page")
                    self.last_error = "Login form fields not found on Smart Energy page"
                    return False

                # Type credentials with human-like delays
                await email_field.click()
                await asyncio.sleep(random.uniform(0.3, 0.6))
                for char in self.email:
                    await self.page.keyboard.type(char)
                    await asyncio.sleep(random.uniform(0.04, 0.12))
                _LOGGER.info("✅ Username/Email entered")
                await asyncio.sleep(random.uniform(0.5, 0.9))

                await password_field.click()
                await asyncio.sleep(random.uniform(0.3, 0.6))
                for char in self.password:
                    await self.page.keyboard.type(char)
                    await asyncio.sleep(random.uniform(0.04, 0.12))
                _LOGGER.info("✅ Password entered")
                await asyncio.sleep(random.uniform(0.5, 1.0))

                # Check "Remember Me" toggle so ASP.NET sets a persistent cookie
                remember_me = await self.page.query_selector('#RememberMe, input[name="RememberMe"]')
                if remember_me:
                    try:
                        if not await remember_me.is_checked():
                            await remember_me.click()
                            _LOGGER.info("✅ Checked 'Remember Me'")
                    except Exception:
                        pass
                await asyncio.sleep(random.uniform(0.5, 1.0))

                # Find and click LOG IN button
                login_submit_button = await self.page.wait_for_selector(
                    'button.loginBtn, button[type="submit"]:has-text("LOG IN"), button:has-text("Login")',
                    timeout=10000,
                )
                if not login_submit_button:
                    _LOGGER.error("❌ LOG IN button not found")
                    self.last_error = "LOG IN button not found"
                    return False

                _LOGGER.info("✅ Moving cursor and clicking LOG IN button...")
                box = await login_submit_button.bounding_box()
                if box:
                    target_x = box['x'] + box['width'] * random.uniform(0.3, 0.7)
                    target_y = box['y'] + box['height'] * random.uniform(0.3, 0.7)
                    await self.page.mouse.move(target_x, target_y, steps=random.randint(15, 25))
                    await asyncio.sleep(random.uniform(0.2, 0.4))
                await login_submit_button.click()

                # Poll for login outcome (allow up to 120s in headed mode for user to solve challenge)
                max_polls = 120 if not self.headless else 35
                _LOGGER.info(f"🔄 Waiting up to {max_polls}s for login response / dashboard...")
                login_success = False
                for poll_i in range(max_polls):
                    await asyncio.sleep(1.0)
                    try:
                        current_url = self.page.url
                        page_content = await self.page.content()
                    except Exception as exc:
                        # The login click can start a redirect while the
                        # document is being replaced. Treat this as a
                        # transient state and inspect the page on the next
                        # polling interval instead of failing the whole flow.
                        _LOGGER.debug(
                            "Login page is still navigating at poll %s: %s",
                            poll_i + 1,
                            exc,
                        )
                        continue
                    page_content_lower = page_content.lower()

                    # 1. Success check: Dashboard loaded and login form is gone
                    has_dashboard_marker = (
                        "__requestverificationtoken" in page_content_lower
                        or 'id="propertyselect"' in page_content_lower
                        or 'id="ajaxcontent"' in page_content_lower
                    )
                    if has_dashboard_marker and "loginemail" not in page_content_lower:
                        _LOGGER.info(f"🎉 Smart Energy dashboard loaded successfully after {poll_i + 1}s!")
                        login_success = True
                        is_smart_energy_dashboard = True
                        break

                    # 2. Check for real on-screen interactive reCAPTCHA puzzle (not the badge!)
                    challenge_visible = False
                    try:
                        for cf in await self.page.locator('iframe[title*="recaptcha challenge"], iframe[src*="bframe"]').all():
                            box = await cf.bounding_box()
                            if box and box['width'] > 200 and box['height'] > 200 and box['y'] >= 0:
                                challenge_visible = True
                                break
                    except Exception:
                        pass

                    if challenge_visible:
                        if await self._solve_recaptcha_audio():
                            continue
                        if not self.headless:
                            if poll_i % 10 == 0:
                                _LOGGER.info(f"🧩 Interactive reCAPTCHA puzzle active on screen. Please solve it in the browser! ({poll_i}s elapsed)")
                            # Do NOT abort! Continue polling so user can solve the challenge
                            continue
                        else:
                            _LOGGER.error("❌ Interactive Google reCAPTCHA puzzle appeared in headless mode")
                            self.last_error = (
                                "Interactive reCAPTCHA puzzle appeared. Please run once in headed mode "
                                "(e.g. 'HEADED=1 python run.py' or 'python auto_login.py --headed --email ... --password ...') "
                                "to solve it once in a browser window and save the session."
                            )
                            try:
                                with open("captcha_page_debug.html", "w", encoding="utf-8") as debug_file:
                                    debug_file.write(page_content)
                                await self.page.screenshot(path="captcha_challenge.png")
                            except Exception:
                                pass
                            return False

                    # 3. Check for login validation error from server (e.g. wrong password)
                    error_elem = await self.page.query_selector('.field-validation-error:not(:empty), .validation-summary-errors:not(:empty)')
                    if error_elem:
                        err_text = (await error_elem.inner_text() or "").strip()
                        if err_text:
                            _LOGGER.error(f"❌ Login validation error from Smart Energy: {err_text}")
                            self.last_error = err_text
                            return False

                if not login_success:
                    _LOGGER.error("❌ Login timed out waiting for Smart Energy dashboard")
                    self.last_error = "Login timed out waiting for Smart Energy dashboard"
                    return False
            is_mfa_page = False
            if is_mfa_page and not is_smart_energy_dashboard:
                    _LOGGER.info("🔐 MFA/verification challenge detected")
                    
                    # Select delivery method (SMS vs Email) if user prefers SMS
                    if self.mfa_method == "sms":
                        # First try "Use a different method" - may reveal SMS option
                        diff_method = None
                        for diff_sel in ['a:has-text("Use a different method")', 'a:has-text("Try another way")', 'a:has-text("Choose a different")']:
                            diff_method = await self.page.query_selector(diff_sel)
                            if diff_method:
                                break
                        if diff_method:
                            _LOGGER.info("📱 Expanding auth options...")
                            await diff_method.click()
                            await asyncio.sleep(2.0)
                        
                        # Try multiple selectors for SMS/Text/Phone
                        sms_selectors = [
                            ('a:has-text("SMS")', True),
                            ('a:has-text("Text")', True),
                            ('button:has-text("SMS")', True),
                            ('button:has-text("Text")', True),
                            ('a:has-text("Send code via SMS")', True),
                            ('a:has-text("Text me")', True),
                            ('a:has-text("phone")', True),
                            ('button:has-text("phone")', True),
                            ('[data-se="sms"]', True),
                            ('[data-se="phone"]', True),
                        ]
                        sms_clicked = False
                        for sel, _ in sms_selectors:
                            el = await self.page.query_selector(sel)
                            if el:
                                el_text = (await el.text_content() or "").lower()
                                if "sms" in el_text or "text" in el_text or "phone" in el_text or not el_text:
                                    _LOGGER.info("📱 Selecting SMS/Text option")
                                    await el.click()
                                    await asyncio.sleep(2.0)
                                    sms_clicked = True
                                    break
                        if not sms_clicked:
                            _LOGGER.warning("⚠️ SMS option not found; checking for email verification")
                            try:
                                debug_content = await self.page.content()
                                with open("mfa_page_debug.html", "w", encoding="utf-8") as f:
                                    f.write(debug_content)
                            except Exception:
                                pass
                    
                    # Click "Send code" / "Receive code" - required to trigger the verification code
                    send_selectors = [
                        'button:has-text("Receive a code via SMS")',
                        'input[value="Receive a code via SMS"]',
                        'a:has-text("Receive a code via SMS")',
                        'button:has-text("Send code via SMS")',
                        'button:has-text("Send Code")',
                        'input[value="Send Code"]',
                        'input[value="Send code"]',
                        'button:has-text("Send code")',
                        'a:has-text("Send Code")',
                        'input[value="Email me a code"]',
                        'button:has-text("Email me a code")',
                        'button[data-se="save"]:has-text("Send me an email")',
                        'button:has-text("Send me an email")',
                        'input[value="Text me a code"]',
                        'button:has-text("Text me a code")',
                    ]
                    # Wait for the send/receive button to appear (page may load after SMS option click)
                    send_code_btn = None
                    for _ in range(3):  # Retry a few times as page may still be loading
                        for sel in send_selectors:
                            send_code_btn = await self.page.query_selector(sel)
                            if send_code_btn:
                                break
                        if send_code_btn:
                            break
                        await asyncio.sleep(1.5)
                    if send_code_btn:
                        _LOGGER.info("📤 Clicking to trigger verification code...")
                        await send_code_btn.click()
                        await asyncio.sleep(3.0)
                    
                    if self.mfa_code:
                        # Try to enter the MFA code
                        _LOGGER.info("📝 Entering MFA code...")
                        try:
                            # Okta typically uses input[name="answer"] or similar for verification codes
                            mfa_input = await self.page.query_selector(
                                'input[name="answer"], input[name="verificationCode"], '
                                'input[type="text"][autocomplete="one-time-code"], '
                                'input[id*="verification"], input[id*="answer"]'
                            )
                            if mfa_input:
                                await mfa_input.click()
                                await mfa_input.fill(self.mfa_code)
                                
                                # Find and click Verify/Submit button
                                verify_btn = await self.page.query_selector(
                                    'input[type="submit"], button[type="submit"], '
                                    'button:has-text("Verify"), button:has-text("Submit"), '
                                    'input[value="Verify"], input[value="Submit"]'
                                )
                                if verify_btn:
                                    await verify_btn.click()
                                    _LOGGER.info("✅ MFA code submitted, waiting for dashboard...")
                                    await asyncio.sleep(2.0)
                                else:
                                    _LOGGER.warning("⚠️ Verify button not found, trying Enter key")
                                    await self.page.keyboard.press("Enter")
                            else:
                                _LOGGER.error("❌ MFA input field not found on page")
                                self._log_mfa_error(current_url)
                                return False
                        except Exception as mfa_err:
                            _LOGGER.error(f"❌ MFA code entry failed: {mfa_err}")
                            self._log_mfa_error(current_url)
                            return False
                    else:
                        # MFA required but no code provided - signal caller to use two-phase flow
                        return "MFA_REQUIRED"
            
            if not is_smart_energy_dashboard:
                _LOGGER.info("🔄 Waiting for dashboard to load...")
                try:
                    await self.page.wait_for_url(lambda url: "myaccount.psegliny.com/dashboards" in url, timeout=25000)
                    await self.page.wait_for_load_state('networkidle')
                    _LOGGER.info("✅ Dashboard loaded")
                except Exception as e:
                    # Check if we're still on the login/OAuth page (login failed)
                    current_url = self.page.url
                    if "id.myaccount.psegliny.com/oauth2" in current_url:
                        page_content = await self.page.content()
                        if any(x in page_content.lower() for x in ["verification", "code", "multi-factor", "authenticate"]):
                            self._log_mfa_error(current_url)
                        else:
                            _LOGGER.error(f"❌ Login failed - still on login page: {current_url}")
                    else:
                        _LOGGER.error(f"❌ Failed to reach dashboard: {current_url}")
                        try:
                            with open("login_fail_debug.html", "w", encoding="utf-8") as debug_file:
                                debug_file.write(await self.page.content())
                            await self.page.screenshot(path="login_fail_debug.png", full_page=True)
                            _LOGGER.error("Login diagnostics saved to login_fail_debug.html and login_fail_debug.png")
                        except Exception as debug_error:
                            _LOGGER.debug("Could not save login diagnostics: %s", debug_error)
                    return False
            
                # Step 5: Wait for exceptional dashboard to load and manually make redirect request
                _LOGGER.info("⚡ Step 5: Waiting for exceptional dashboard and manually making redirect request...")
                
                # Wait for the exceptional dashboard POST request to complete
                await asyncio.sleep(3.0)  # Give time for the POST request to complete
                
                # Scroll to simulate browsing and wait for content to load
                await self.page.mouse.wheel(0, random.randint(600, 800))
                await asyncio.sleep(random.uniform(1.0, 2.0))
                
                # Add additional wait to ensure page is fully loaded
                try:
                    await self.page.wait_for_load_state('domcontentloaded', timeout=10000)
                except Exception as e:
                    _LOGGER.warning(f"⚠️ DOM content load wait failed: {e}")
                
                # Check if we captured the exceptional dashboard data
                if not self.exceptional_dashboard_data:
                    _LOGGER.warning("⚠️ Exceptional dashboard data not captured, trying direct navigation...")
                    await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded')
                else:
                    _LOGGER.info("✅ Exceptional dashboard data captured, manually making redirect request...")
                    
                    # Manually make the redirect request with the captured headers
                    try:
                        headers = self.exceptional_dashboard_data['headers']
                        important_headers = {
                            'accept': headers.get('accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8'),
                            'accept-language': headers.get('accept-language', 'en-US,en;q=0.5'),
                            'referer': headers.get('referer', self.exceptional_dashboard),
                            'sec-fetch-dest': 'document',
                            'sec-fetch-mode': 'navigate',
                            'sec-fetch-site': 'same-origin',
                            'upgrade-insecure-requests': '1'
                        }
                        
                        # Get cookies from context for the request
                        context_cookies = await self.context.cookies()
                        cookie_string = '; '.join([f"{cookie['name']}={cookie['value']}" for cookie in context_cookies if cookie['domain'] in ['.psegliny.com', '.myaccount.psegliny.com']])
                        
                        if cookie_string:
                            important_headers['cookie'] = cookie_string
                        
                        _LOGGER.info(f"🔍 Making manual redirect request to {self.mysmartenergy_redirect}")
                        
                        # Make the redirect request manually
                        response = await self.page.request.get(self.mysmartenergy_redirect, headers=important_headers)
                        
                        if response.status == 302:
                            _LOGGER.info("✅ Redirect response received (302)")
                            final_url = response.headers.get('location')
                            if final_url:
                                _LOGGER.info(f"🔄 Following redirect to: {final_url}")
                                try:
                                    await self.page.goto(final_url, wait_until='domcontentloaded', timeout=20000)
                                except Exception as nav_error:
                                    _LOGGER.warning(f"⚠️ Redirect navigation failed: {nav_error}, trying direct navigation...")
                                    await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                            else:
                                _LOGGER.warning("⚠️ No location header in redirect, trying direct navigation...")
                                await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                        else:
                            _LOGGER.warning(f"⚠️ Unexpected response status: {response.status}, trying direct navigation...")
                            await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                            
                    except Exception as e:
                        _LOGGER.warning(f"⚠️ Manual redirect failed: {e}, falling back to direct navigation...")
                        await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                
                # Wait for MySmartEnergy dashboard
                try:
                    await self.page.wait_for_url(lambda url: "mysmartenergy.psegliny.com/Dashboard" in url, timeout=20000)
                except Exception as e:
                    _LOGGER.warning(f"⚠️ URL wait failed: {e}, trying alternative approach...")
                    await self.page.wait_for_load_state('networkidle', timeout=20000)
                    current_url = self.page.url
                    if "mysmartenergy.psegliny.com/Dashboard" not in current_url:
                        _LOGGER.warning(f"⚠️ Not on expected dashboard, current URL: {current_url}")
                        await self.page.goto(self.final_dashboard, wait_until='domcontentloaded', timeout=20000)
                
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                _LOGGER.info("✅ MySmartEnergy Dashboard loaded")
            
            # Step 6: Get cookies from the final dashboard
            _LOGGER.info("🍪 Step 6: Capturing cookies from final dashboard...")
            await asyncio.sleep(2.0)
            
            try:
                await self._capture_cookies_and_state()
            except Exception as se:
                _LOGGER.warning(f"Could not persist browser session: {se}")
            
            _LOGGER.info("✅ Realistic browsing pattern completed successfully")
            return True
            
        except Exception as e:
            _LOGGER.error(f"Error during realistic browsing: {e}")
            return False
    
    async def continue_after_mfa(self, mfa_code: str) -> Optional[str]:
        """
        Continue login flow after MFA challenge. Call when get_cookies() returns 'MFA_REQUIRED'.
        Browser must still be on the MFA challenge page.
        """
        try:
            _LOGGER.info("📝 Entering MFA code...")
            # Wait for code input - check main frame and iframes (Okta may use iframe)
            mfa_input_selectors = [
                'input[name="answer"]',
                'input[name="verificationCode"]',
                'input[type="text"][autocomplete="one-time-code"]',
                'input[type="tel"][inputmode="numeric"]',
                'input[type="tel"]',
                'input[id*="verification"]',
                'input[id*="answer"]',
                'input[data-se="answer"]',
                'input[type="text"]',  # Fallback: any text input
            ]
            mfa_input = None
            mfa_frame = self.page.main_frame  # Frame containing the MFA form
            frames_to_check = [self.page.main_frame]
            try:
                frames_to_check.extend([f for f in self.page.frames if f != self.page.main_frame])
            except Exception:
                pass
            for frame in frames_to_check:
                for sel in mfa_input_selectors:
                    try:
                        mfa_input = await frame.wait_for_selector(sel, state="visible", timeout=2000)
                        if mfa_input:
                            # Skip if it looks like a long text field (not OTP)
                            try:
                                maxlen = await mfa_input.get_attribute("maxlength")
                                if maxlen and int(maxlen) > 20:
                                    mfa_input = None
                                    continue
                            except (TypeError, ValueError):
                                pass
                            mfa_frame = frame
                            _LOGGER.info("Found MFA input with selector: %s", sel)
                            break
                    except Exception:
                        mfa_input = None
                        continue
                if mfa_input:
                    break
            
            # Fallback: try get_by_placeholder (Okta often uses "Enter code" or similar)
            if not mfa_input:
                for placeholder in ["Enter code", "Verification code", "Code", "Enter the code"]:
                    try:
                        loc = self.page.get_by_placeholder(placeholder).first
                        await loc.wait_for(state="visible", timeout=2000)
                        mfa_input = await loc.element_handle()
                        if mfa_input:
                            _LOGGER.info("Found MFA input via placeholder: %s", placeholder)
                            break
                    except Exception:
                        mfa_input = None
                        continue
            
            if not mfa_input:
                _LOGGER.error("❌ MFA input field not found - page may have changed. Current URL: %s", self.page.url)
                try:
                    content = await self.page.content()
                    if len(content) > 500:
                        with open("mfa_fail_debug.html", "w", encoding="utf-8") as f:
                            f.write(content)
                    _LOGGER.error("Page saved to mfa_fail_debug.html for inspection")
                except Exception:
                    pass
                return None
            
            await mfa_input.click()
            await mfa_input.fill(mfa_code)
            
            # Find and click Verify/Submit - Okta uses various button patterns
            verify_selectors = [
                'input[type="submit"]',
                'button[type="submit"]',
                'button:has-text("Verify")',
                'button:has-text("Submit")',
                'input[value="Verify"]',
                'input[value="Submit"]',
                'button:has-text("Next")',
                'a:has-text("Verify")',
                'input[data-se="verify"]',
            ]
            verify_btn = None
            for sel in verify_selectors:
                try:
                    verify_btn = await mfa_frame.query_selector(sel)
                    if verify_btn and await verify_btn.is_visible():
                        break
                except Exception:
                    continue
            if verify_btn:
                await verify_btn.click()
            else:
                _LOGGER.info("Verify button not found, pressing Enter")
                await mfa_input.press("Enter")
            
            await asyncio.sleep(2.0)  # Let form submit
            _LOGGER.info("🔄 Waiting for dashboard after MFA...")
            await self.page.wait_for_url(lambda url: "myaccount.psegliny.com/dashboards" in url, timeout=25000)
            await self.page.wait_for_load_state('networkidle')
            _LOGGER.info("✅ Dashboard loaded after MFA")
            
            # Continue from Step 5 (exceptional dashboard)
            await asyncio.sleep(3.0)
            await self.page.mouse.wheel(0, random.randint(600, 800))
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            if not self.exceptional_dashboard_data:
                await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded')
            else:
                try:
                    headers = self.exceptional_dashboard_data['headers']
                    important_headers = {
                        'accept': headers.get('accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
                        'accept-language': headers.get('accept-language', 'en-US,en;q=0.5'),
                        'referer': headers.get('referer', self.exceptional_dashboard),
                        'sec-fetch-dest': 'document', 'sec-fetch-mode': 'navigate',
                        'sec-fetch-site': 'same-origin', 'upgrade-insecure-requests': '1'
                    }
                    context_cookies = await self.context.cookies()
                    cookie_string = '; '.join([f"{c['name']}={c['value']}" for c in context_cookies if c['domain'] in ['.psegliny.com', '.myaccount.psegliny.com']])
                    if cookie_string:
                        important_headers['cookie'] = cookie_string
                    response = await self.page.request.get(self.mysmartenergy_redirect, headers=important_headers)
                    if response.status == 302:
                        final_url = response.headers.get('location')
                        if final_url:
                            await self.page.goto(final_url, wait_until='domcontentloaded', timeout=20000)
                        else:
                            await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                    else:
                        await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
                except Exception as e:
                    _LOGGER.warning(f"Manual redirect failed: {e}")
                    await self.page.goto(self.mysmartenergy_redirect, wait_until='domcontentloaded', timeout=20000)
            
            await self.page.wait_for_url(lambda url: "mysmartenergy.psegliny.com/Dashboard" in url, timeout=20000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            await asyncio.sleep(3.0)
            
            context_cookies = await self.context.cookies()
            for cookie in context_cookies:
                if cookie['domain'] in ['.psegliny.com', '.myaccount.psegliny.com', '.mysmartenergy.psegliny.com']:
                    self.login_cookies[cookie['name']] = cookie['value']
            
            return self.format_cookies_for_api()
        except Exception as e:
            _LOGGER.error("MFA continuation failed: %s (type: %s)", e, type(e).__name__)
            _LOGGER.error("Current URL at failure: %s", self.page.url if self.page else "no page")
            import traceback
            _LOGGER.debug("Traceback: %s", traceback.format_exc())
            return None
    
    def format_cookies_for_api(self) -> str:
        """Format cookies in the format expected by the API."""
        try:
            # Keep the complete authenticated cookie jar. Smart Energy may use
            # additional session cookies beyond MM_SID for Chart and ChartData.
            cookie_strings = [
                f"{name}={value}"
                for name, value in self.login_cookies.items()
                if name and value
            ]
            
            if cookie_strings:
                result = "; ".join(cookie_strings)
                _LOGGER.info(f"🍪 Formatted cookies for API: {result[:100]}...")
                return result
            else:
                _LOGGER.warning("⚠️ No valid cookies to format for API")
                return ""
                
        except Exception as e:
            _LOGGER.warning(f"Error formatting cookies for API: {e}")
            return ""
    
    async def get_cookies(self) -> Optional[str]:
        """Get cookies by following the realistic browsing pattern."""
        result = None
        try:
            if not await self.setup_browser():
                _LOGGER.error("❌ Failed to setup browser")
                return None
            
            # Follow the realistic browsing pattern
            result = await self.simulate_realistic_browsing()
            if result is False:
                _LOGGER.error("❌ Realistic browsing pattern failed")
                return None
            if result == "MFA_REQUIRED":
                # Caller should use continue_after_mfa(code) - do NOT cleanup, keep browser alive
                return "MFA_REQUIRED"
            
            # Check if we got the cookies we need
            if self.login_cookies:
                _LOGGER.info("✅ SUCCESS: Got cookies from realistic browsing pattern")
                for name, value in self.login_cookies.items():
                    _LOGGER.info(f"🍪 {name}: {value[:50]}...")
                
                # Format cookies for API use
                return self.format_cookies_for_api()
            else:
                _LOGGER.warning("⚠️ No cookies captured, but browsing completed")
                return ""
                
        except Exception as e:
            _LOGGER.error(f"Error getting cookies: {e}")
            return None
        finally:
            # Don't cleanup when MFA is required - caller needs the browser for continue_after_mfa()
            if result != "MFA_REQUIRED":
                await self.cleanup()
    
    async def cleanup(self):
        """Clean up browser resources."""
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
        except Exception as e:
            _LOGGER.warning(f"Error during cleanup: {e}")

# API Endpoints for Home Assistant integration
async def get_pseg_cookies(email: str, password: str) -> Optional[str]:
    """
    Get PSEG cookies for Home Assistant integration.
    
    Args:
        email: PSEG account email/username
        password: PSEG account password
    
    Returns:
        Cookie string in format "MM_SID=value; __RequestVerificationToken=value" or None if failed
    """
    try:
        _LOGGER.info("🚀 Starting PSEG cookie acquisition for Home Assistant...")
        cookie_getter = PSEGAutoLogin(email=email, password=password)
        return await cookie_getter.get_cookies()
    except Exception as e:
        _LOGGER.error(f"Failed to get PSEG cookies: {e}")
        return None

def get_pseg_cookies_sync(email: str, password: str) -> Optional[str]:
    """
    Synchronous wrapper for get_pseg_cookies.
    
    Args:
        email: PSEG account email/username
        password: PSEG account password
    
    Returns:
        Cookie string in format "MM_SID=value; __RequestVerificationToken=value" or None if failed
    """
    try:
        return asyncio.run(get_pseg_cookies(email, password))
    except Exception as e:
        _LOGGER.error(f"Failed to get PSEG cookies synchronously: {e}")
        return None

# Compatibility wrapper for existing integration
async def get_fresh_cookies(username: str, password: str) -> Optional[str]:
    """
    Compatibility wrapper for existing integration.
    This function maintains the same interface as the old implementation.
    
    Args:
        username: PSEG account email/username
        password: PSEG account password
    
    Returns:
        Cookie string in format "MM_SID=value; __RequestVerificationToken=value" or None if failed
    """
    try:
        _LOGGER.info(f"Login attempt for user: {username}")
        return await get_pseg_cookies(username, password)
    except Exception as e:
        _LOGGER.error(f"Login error: {e}")
        return None

# Test function for standalone usage
async def main():
    """Test function for standalone usage."""
    import argparse
    
    parser = argparse.ArgumentParser(description='PSEG Long Island Auto Login - Home Assistant Addon')
    parser.add_argument('--email', required=True, help='PSEG account email/username')
    parser.add_argument('--password', required=True, help='PSEG account password')
    parser.add_argument('--mfa-method', choices=['email', 'sms'], default='sms',
                        help='MFA delivery: email (default) or sms')
    parser.add_argument('--headed', action='store_true',
                        help='Run with visible browser (for debugging)')
    
    args = parser.parse_args()
    
    _LOGGER.info("🚀 Starting PSEG Auto Login - Home Assistant Addon")
    _LOGGER.info(f"📧 Email: {args.email}")
    _LOGGER.info("🔒 Headless mode: %s", not args.headed)
    _LOGGER.info("📱 MFA method: %s", args.mfa_method)
    
    cookie_getter = PSEGAutoLogin(
        email=args.email,
        password=args.password,
        mfa_method=args.mfa_method,
        headless=not args.headed,
    )
    cookies = await cookie_getter.get_cookies()
    
    if cookies == "MFA_REQUIRED":
        _LOGGER.error("❌ MFA required - PSEG sends a verification code to your email or phone.")
        _LOGGER.error("   Check for the code, then run again with the addon API:")
        _LOGGER.error("   POST /login (triggers email) → POST /login/mfa with code")
        return 1
    elif cookies:
        _LOGGER.info("🎉 SUCCESS: Cookies obtained successfully!")
        _LOGGER.info("=" * 80)
        _LOGGER.info("COOKIE STRING (for Home Assistant integration):")
        _LOGGER.info("=" * 80)
        _LOGGER.info(cookies)
        _LOGGER.info("=" * 80)
        _LOGGER.info(f"📋 Total length: {len(cookies)} characters")
        return 0
    else:
        _LOGGER.error("❌ FAILED: Could not obtain cookies")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
