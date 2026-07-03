"""DrissionPage-backed Grok registration automation."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.platform.logging.logger import logger

_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F"
_PATCH_DIR = Path(__file__).resolve().parent / "turnstilePatch"
_EMAIL_ENTRY_PATTERNS = (
    "signupwithemail",
    "sign up with email",
    "useemail",
    "use email",
    "registerwithemail",
    "emailsignup",
    "使用邮箱注册",
    "邮箱注册",
)
_SUBMIT_EMAIL_PATTERNS = (
    "signup",
    "sign up",
    "register",
    "continue",
    "next",
    "submit",
    "注册",
    "继续",
    "下一步",
    "提交",
)
_CONFIRM_EMAIL_PATTERNS = (
    "confirmemail",
    "confirm email",
    "verifyemail",
    "continue",
    "next",
    "确认邮箱",
    "继续",
    "下一步",
)
_COMPLETE_SIGNUP_PATTERNS = (
    "completesignup",
    "complete sign up",
    "complete signup",
    "finishsignup",
    "finish sign up",
    "createaccount",
    "完成注册",
    "完成",
)


class DrissionRegistrationRunner:
    """Real-browser registration flow using DrissionPage and a Turnstile patch."""

    def __init__(self) -> None:
        self.browser: Any = None
        self.page: Any = None
        self._xvfb_process: subprocess.Popen[str] | None = None
        self._managed_display: str | None = None
        self._previous_display: str | None = None
        self._tmp_root: Path | None = None

    def start(
        self,
        *,
        headless: bool = True,
        proxy_url: str | None = None,
        executable_path: str | None = None,
        browser_channel: str | None = None,
    ) -> None:
        from DrissionPage import Chromium, ChromiumOptions

        self.stop()
        self._ensure_virtual_display(headless=headless)
        self._tmp_root = self._make_tmp_root()
        co = ChromiumOptions()
        co.set_tmp_path(str(self._tmp_root))
        co.auto_port()
        co.set_timeouts(base=1)
        co.add_extension(str(_PATCH_DIR))
        co.headless(headless)
        co.set_argument("--lang", "en-US")
        co.set_argument("--disable-blink-features", "AutomationControlled")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--window-size", "1280,800")
        co.set_argument("--remote-debugging-address", "127.0.0.1")
        co.set_argument("--password-store", "basic")
        co.set_argument("--use-mock-keychain")

        if os.name == "posix":
            # Chromium runs as root in many Docker deployments; these flags are
            # required before the DevTools port can come up reliably.
            co.set_argument("--no-sandbox")
            co.set_argument("--disable-setuid-sandbox")
            co.set_argument("--disable-gpu")
            co.set_argument("--no-zygote")

        resolved_browser = self._resolve_browser_path(executable_path, browser_channel)
        if resolved_browser:
            co.set_browser_path(resolved_browser)
        if proxy_url:
            co.set_proxy(proxy_url)

        try:
            self.browser = Chromium(co)
        except Exception as exc:
            context = self._browser_start_context(
                headless=headless,
                proxy_url=proxy_url,
                resolved_browser=resolved_browser,
                tmp_root=self._tmp_root,
            )
            raise RuntimeError(f"DrissionPage browser start failed: {exc}; {context}") from exc
        tabs = self.browser.get_tabs()
        self.page = tabs[-1] if tabs else self.browser.new_tab()
        logger.info(
            "drission registration browser started: headless={} proxy={} browser={} tmp={} display={}",
            headless,
            bool(proxy_url),
            resolved_browser or "auto",
            self._tmp_root,
            os.environ.get("DISPLAY", ""),
        )

    def stop(self) -> None:
        if self.browser is not None:
            try:
                self.browser.quit()
            except Exception:
                pass
        self.browser = None
        self.page = None
        if self._xvfb_process is not None:
            try:
                self._xvfb_process.terminate()
                self._xvfb_process.wait(timeout=5)
            except Exception:
                try:
                    self._xvfb_process.kill()
                except Exception:
                    pass
            self._xvfb_process = None
        if self._managed_display and os.environ.get("DISPLAY") == self._managed_display:
            if self._previous_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = self._previous_display
        self._managed_display = None
        self._previous_display = None
        if self._tmp_root is not None:
            shutil.rmtree(self._tmp_root, ignore_errors=True)
            self._tmp_root = None

    def refresh_active_page(self) -> Any:
        if self.browser is None:
            raise RuntimeError("browser is not started")
        tabs = self.browser.get_tabs()
        self.page = tabs[-1] if tabs else self.browser.new_tab()
        return self.page

    def open_signup_page(self, timeout: float = 20.0) -> None:
        page = self.refresh_active_page()
        page.get(_SIGNUP_URL)
        self.click_email_signup_button(timeout=timeout)

    def click_email_signup_button(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.has_email_input():
                return
            clicked = self.page.run_js(
                """
const patterns = JSON.parse(arguments[0]);
const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const compact = (value) => normalize(value).replace(/\\s+/g, '');
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const target = Array.from(document.querySelectorAll('button, a, [role=\"button\"]')).find((node) => {
  if (!visible(node)) return false;
  const text = compact(node.innerText || node.textContent || '');
  return patterns.some((pattern) => text.includes(pattern));
});
if (!target) return false;
target.click();
return true;
                """,
                json.dumps(list(_EMAIL_ENTRY_PATTERNS)),
            )
            if clicked:
                time.sleep(1)
                if self.has_email_input():
                    return
            time.sleep(0.5)
        raise RuntimeError(f'email signup entry not found; {self.describe_page()}')

    def has_email_input(self) -> bool:
        return bool(
            self.page.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
return !!Array.from(document.querySelectorAll('input[data-testid=\"email\"], input[name=\"email\"], input[type=\"email\"], input[autocomplete=\"email\"]')).find((node) => {
  return visible(node) && !node.disabled && !node.readOnly;
});
                """
            )
        )

    def fill_email_and_submit(self, email: str, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.page.run_js(
                """
const email = arguments[0];
const patterns = JSON.parse(arguments[1]);
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const compact = (value) => normalize(value).replace(/\\s+/g, '');
const setValue = (input, value) => {
  input.focus();
  input.click();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) {
    setter.call(input, '');
    setter.call(input, value);
  } else {
    input.value = '';
    input.value = value;
  }
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.dispatchEvent(new Event('blur', { bubbles: true }));
};
const input = Array.from(document.querySelectorAll('input[data-testid=\"email\"], input[name=\"email\"], input[type=\"email\"], input[autocomplete=\"email\"]')).find((node) => {
  return visible(node) && !node.disabled && !node.readOnly;
});
if (!input) return 'not-ready';
setValue(input, email);
if (String(input.value || '').trim() !== email || !input.checkValidity()) return 'fill-failed';
const submitButton = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).find((node) => {
  if (!visible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
  const text = compact(node.innerText || node.textContent || '');
  return patterns.some((pattern) => text.includes(pattern));
});
if (!submitButton) return 'button-not-found';
submitButton.focus();
submitButton.click();
return 'submitted';
                """,
                email,
                json.dumps(list(_SUBMIT_EMAIL_PATTERNS)),
            )
            if state == "submitted":
                return
            if state not in {"not-ready", "fill-failed", "button-not-found"}:
                logger.debug("drission fill_email unexpected state: {}", state)
            time.sleep(0.5)
        raise RuntimeError(f'email submit failed for {email}; {self.describe_page()}')

    def wait_for_verification_prompt(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.has_verification_form() or self.has_profile_form():
                return True
            time.sleep(0.5)
        return False

    def has_verification_form(self) -> bool:
        return bool(
            self.page.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const codeInput = Array.from(document.querySelectorAll('input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]')).find((node) => {
  return visible(node) && !node.disabled && !node.readOnly;
});
if (codeInput) return true;
const body = String(document.body.innerText || '').toLowerCase();
return ['verify your email', 'check your email', 'confirmation code', 'one-time security code', '验证您的邮箱', '验证码', '检查你的邮箱', '检查您的邮箱'].some((text) => body.includes(text));
                """
            )
        )

    def open_verification_link(self, url: str) -> None:
        self.refresh_active_page().get(url)

    def fill_code_and_submit(self, code: str, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.has_profile_form():
                return
            state = self.page.run_js(
                """
const code = String(arguments[0] || '').trim();
const patterns = JSON.parse(arguments[1]);
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const compact = (value) => normalize(value).replace(/\\s+/g, '');
const setValue = (input, value) => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) {
    setter.call(input, '');
    setter.call(input, value);
  } else {
    input.value = '';
    input.value = value;
  }
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
};
const aggregate = Array.from(document.querySelectorAll('input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]')).find((node) => {
  return visible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
});
const boxes = Array.from(document.querySelectorAll('input')).filter((node) => {
  if (!visible(node) || node.disabled || node.readOnly) return false;
  const maxLength = Number(node.maxLength || 0);
  const autocomplete = String(node.autocomplete || '').toLowerCase();
  return maxLength === 1 || autocomplete === 'one-time-code';
});
if (!aggregate && boxes.length < code.length) return 'not-ready';
if (aggregate) {
  aggregate.focus();
  aggregate.click();
  setValue(aggregate, code);
  if (String(aggregate.value || '').trim() !== code) return 'code-mismatch';
} else {
  boxes.slice(0, code.length).forEach((box, index) => {
    box.focus();
    box.click();
    setValue(box, code[index] || '');
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: code[index] || '' }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: code[index] || '' }));
  });
  const merged = boxes.slice(0, code.length).map((node) => String(node.value || '').trim()).join('');
  if (merged !== code) return 'box-mismatch';
}
const button = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).find((node) => {
  if (!visible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
  const text = compact(node.innerText || node.textContent || '');
  return patterns.some((pattern) => text.includes(pattern));
});
if (!button) return 'button-not-found';
button.focus();
button.click();
return 'submitted';
                """,
                code,
                json.dumps(list(_CONFIRM_EMAIL_PATTERNS)),
            )
            if state == "submitted":
                time.sleep(2)
                if self.has_profile_form():
                    return
            elif state == "not-ready" and self.has_profile_form():
                return
            time.sleep(0.5)
        raise RuntimeError(f'email verification submit failed; {self.describe_page()}')

    def has_profile_form(self) -> bool:
        return bool(
            self.page.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const given = Array.from(document.querySelectorAll('input[data-testid=\"givenName\"], input[name=\"givenName\"], input[autocomplete=\"given-name\"]')).find((node) => visible(node) && !node.disabled);
const family = Array.from(document.querySelectorAll('input[data-testid=\"familyName\"], input[name=\"familyName\"], input[autocomplete=\"family-name\"]')).find((node) => visible(node) && !node.disabled);
const password = Array.from(document.querySelectorAll('input[data-testid=\"password\"], input[name=\"password\"], input[type=\"password\"]')).find((node) => visible(node) && !node.disabled);
return !!(given && family && password);
                """
            )
        )

    def fill_profile_and_submit(self, timeout: float = 180.0) -> dict[str, str]:
        given_name, family_name, password = self.build_profile()
        deadline = time.time() + timeout
        turnstile_token = ""
        while time.time() < deadline:
            state = self.page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const pickInput = (selector) => Array.from(document.querySelectorAll(selector)).find((node) => visible(node) && !node.disabled && !node.readOnly) || null;
const setValue = (input, value) => {
  if (!input) return false;
  input.focus();
  input.click();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) {
    setter.call(input, '');
    setter.call(input, value);
  } else {
    input.value = '';
    input.value = value;
  }
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.dispatchEvent(new Event('blur', { bubbles: true }));
  return String(input.value || '') === String(value || '');
};
const givenInput = pickInput('input[data-testid=\"givenName\"], input[name=\"givenName\"], input[autocomplete=\"given-name\"]');
const familyInput = pickInput('input[data-testid=\"familyName\"], input[name=\"familyName\"], input[autocomplete=\"family-name\"]');
const passwordInput = pickInput('input[data-testid=\"password\"], input[name=\"password\"], input[type=\"password\"]');
if (!givenInput || !familyInput || !passwordInput) return 'not-ready';
const givenOk = setValue(givenInput, givenName);
const familyOk = setValue(familyInput, familyName);
const passwordOk = setValue(passwordInput, password);
if (!givenOk || !familyOk || !passwordOk) return 'fill-failed';
return 'filled';
                """,
                given_name,
                family_name,
                password,
            )
            if state == "not-ready":
                time.sleep(0.5)
                continue
            if state != "filled":
                time.sleep(0.5)
                continue

            turnstile_state = self._turnstile_state()
            if turnstile_state == "pending" and not turnstile_token:
                turnstile_token = self.get_turnstile_token()
                if turnstile_token:
                    self._sync_turnstile_token(turnstile_token)
                    time.sleep(1)

            clicked = self.page.run_js(
                """
const patterns = JSON.parse(arguments[0]);
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const compact = (value) => String(value || '').replace(/\\s+/g, '').trim().toLowerCase();
const challengeInput = document.querySelector('input[name=\"cf-turnstile-response\"]');
if (challengeInput && !String(challengeInput.value || '').trim()) return false;
const submitButton = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).find((node) => {
  if (!visible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
  const text = compact(node.innerText || node.textContent || '');
  return patterns.some((pattern) => text.includes(pattern));
});
if (!submitButton) return false;
submitButton.focus();
submitButton.click();
return true;
                """,
                json.dumps(list(_COMPLETE_SIGNUP_PATTERNS)),
            )
            if clicked:
                return {
                    "given_name": given_name,
                    "family_name": family_name,
                    "password": password,
                }
            time.sleep(0.5)
        raise RuntimeError(f'profile completion failed; {self.describe_page()}')

    def wait_for_sso_cookie(self, timeout: float = 120.0) -> str:
        deadline = time.time() + timeout
        seen_names: set[str] = set()
        while time.time() < deadline:
            self.refresh_active_page()
            cookies = self.page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()
                if name:
                    seen_names.add(name)
                if name == "sso" and value:
                    return value
            time.sleep(1)
        raise RuntimeError(f"sso cookie not found; cookies={sorted(seen_names)}")

    def describe_page(self) -> str:
        return str(
            self.page.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const buttons = Array.from(document.querySelectorAll('button')).filter(visible).map((node) => ({
  text: String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim(),
  disabled: !!node.disabled,
}));
const inputs = Array.from(document.querySelectorAll('input')).filter(visible).map((node) => ({
  type: node.type || '',
  name: node.name || '',
  autocomplete: node.autocomplete || '',
  value: String(node.value || ''),
}));
return JSON.stringify({
  url: location.href,
  title: document.title,
  body: String(document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400),
  buttons,
  inputs,
});
                """
            )
        )

    def _turnstile_state(self) -> str:
        return str(
            self.page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) return 'not-found';
return String(challengeInput.value || '').trim() ? 'ready' : 'pending';
                """
            )
        )

    def _sync_turnstile_token(self, token: str) -> bool:
        return bool(
            self.page.run_js(
                """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) return false;
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (setter) {
  setter.call(challengeInput, token);
} else {
  challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                """,
                token,
            )
        )

    def get_turnstile_token(self, timeout: float = 20.0) -> str:
        self.page.run_js("try { turnstile.reset() } catch(e) {}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                existing = self.page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
                if existing:
                    return str(existing)
            except Exception:
                pass
            try:
                challenge = self.page.ele("@name=cf-turnstile-response")
                wrapper = challenge.parent()
                iframe = wrapper.shadow_root.ele("tag:iframe")
                iframe.run_js(
                    """
function getRandomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}
const screenX = getRandomInt(800, 1200);
const screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                    """
                )
                iframe_body = iframe.ele("tag:body").shadow_root
                challenge_button = iframe_body.ele("tag:input")
                challenge_button.click()
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("turnstile challenge was not solved in time")

    @staticmethod
    def build_profile() -> tuple[str, str, str]:
        first_names = [
            "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
            "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth",
            "Barbara", "Susan", "Jessica", "Sarah", "Karen", "Emma", "Olivia", "Ava",
            "Isabella", "Sophia", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn",
        ]
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
            "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White",
            "Harris", "Clark", "Lewis", "Robinson", "Walker", "Hall", "Allen", "Young",
        ]
        given_name = secrets.choice(first_names)
        family_name = secrets.choice(last_names)
        password = given_name[0] + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
        return given_name, family_name, password

    @staticmethod
    def _resolve_browser_path(executable_path: str | None, browser_channel: str | None) -> str | None:
        explicit = str(executable_path or os.getenv("REGISTRATION_BROWSER_EXECUTABLE", "")).strip()
        if explicit and Path(explicit).exists():
            return explicit

        channel = str(browser_channel or "").strip().lower()
        candidates: list[str] = []
        if os.name == "nt":
            if channel in {"", "msedge", "edge"}:
                candidates.extend(
                    [
                        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                    ]
                )
            if channel in {"", "chrome", "chromium"}:
                candidates.extend(
                    [
                        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    ]
                )
        else:
            if channel in {"", "chrome", "chromium", "msedge", "edge"}:
                candidates.extend(
                    [
                        "/usr/local/bin/registration-chromium",
                        "/usr/bin/chromium",
                        "/usr/bin/chromium-browser",
                        "/usr/bin/google-chrome",
                        "/usr/bin/google-chrome-stable",
                        "/opt/google/chrome/chrome",
                    ]
                )
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate
        return None

    def _ensure_virtual_display(self, *, headless: bool) -> None:
        """Start Xvfb when a headed browser is required inside Linux containers."""
        if headless:
            return
        if os.name != "posix":
            return
        if os.environ.get("DISPLAY"):
            return

        x11_socket_dir = Path("/tmp/.X11-unix")
        try:
            x11_socket_dir.mkdir(parents=True, exist_ok=True)
            x11_socket_dir.chmod(0o1777)
        except Exception:
            logger.debug("drission registration: unable to prepare /tmp/.X11-unix")

        xvfb_binary = self._find_binary("Xvfb")
        if not xvfb_binary:
            raise RuntimeError("headed DrissionPage session requires Xvfb, but Xvfb is not installed")

        display = self._pick_display()
        cmd = [
            xvfb_binary,
            display,
            "-screen",
            "0",
            "1280x800x24",
            "-ac",
            "-nolisten",
            "tcp",
        ]
        self._xvfb_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._previous_display = os.environ.get("DISPLAY")
        self._managed_display = display
        os.environ["DISPLAY"] = display
        socket_path = Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")
        deadline = time.time() + 5
        while time.time() < deadline and self._xvfb_process.poll() is None:
            if socket_path.exists():
                break
            time.sleep(0.1)
        if self._xvfb_process.poll() is not None or not socket_path.exists():
            self._xvfb_process = None
            self._managed_display = None
            self._previous_display = None
            os.environ.pop("DISPLAY", None)
            raise RuntimeError("failed to start Xvfb for headed DrissionPage session")
        logger.info("drission registration: started virtual display {}", display)

    @staticmethod
    def _make_tmp_root() -> Path:
        base = Path(os.getenv("DRISSION_TMP_DIR", "") or Path(tempfile.gettempdir()) / "grok2api-drission")
        base.mkdir(parents=True, exist_ok=True)
        root = base / f"run-{os.getpid()}-{secrets.token_hex(6)}"
        root.mkdir(parents=True, exist_ok=False)
        return root

    @staticmethod
    def _browser_start_context(
        *,
        headless: bool,
        proxy_url: str | None,
        resolved_browser: str | None,
        tmp_root: Path | None,
    ) -> str:
        display = os.environ.get("DISPLAY", "")
        browser = resolved_browser or "auto"
        browser_version = ""
        if resolved_browser:
            try:
                completed = subprocess.run(
                    [resolved_browser, "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                browser_version = (completed.stdout or completed.stderr or "").strip()
            except Exception as exc:
                browser_version = f"version check failed: {exc}"
        return (
            f"headless={headless} proxy={bool(proxy_url)} browser={browser!r} "
            f"browser_version={browser_version!r} display={display!r} tmp={str(tmp_root)!r}"
        )

    @staticmethod
    def _find_binary(name: str) -> str | None:
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        for entry in path_entries:
            if not entry:
                continue
            candidate = Path(entry) / name
            if candidate.exists():
                return str(candidate)
        return None

    @staticmethod
    def _pick_display() -> str:
        for number in range(90, 100):
            lock_path = Path(f"/tmp/.X{number}-lock")
            socket_path = Path(f"/tmp/.X11-unix/X{number}")
            if not lock_path.exists() and not socket_path.exists():
                return f":{number}"
        return ":99"


__all__ = ["DrissionRegistrationRunner"]
