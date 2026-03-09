#!/usr/bin/env python3
"""Desktop app for joining a Google Meet link with multiple simulated users."""

from __future__ import annotations

import queue
import re
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk
except ModuleNotFoundError as exc:
    if exc.name == "_tkinter":
        print(
            "Tkinter is not available in this Python build.\n"
            "On macOS Homebrew Python, install with: brew install python-tk@3.14\n"
            "Then recreate your venv and run again."
        )
        raise SystemExit(1) from exc
    raise

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

Selector = Tuple[str, str]
MEDIA_PROMPT_ACTION_CONTINUE = "continue_without_media"
MEDIA_PROMPT_ACTION_USE = "use_media"
MEDIA_PROMPT_ACTION_MANUAL = "manual"
LAUNCH_MODE_DIFFERENT_WINDOWS = "different_windows"
LAUNCH_MODE_SAME_WINDOW_TABS = "same_window_tabs"
SAME_WINDOW_VIEW_NONE = "none"
SAME_WINDOW_VIEW_EXPANDED = "expanded"
SAME_WINDOW_VIEW_FULLSCREEN = "fullscreen"
EXECUTION_SPEED_FAST = "fast"
EXECUTION_SPEED_BALANCED = "balanced"
EXECUTION_SPEED_RELIABLE = "reliable"

MEDIA_PROMPT_LABEL_TO_VALUE = {
    "Continue without microphone and camera": MEDIA_PROMPT_ACTION_CONTINUE,
    "Use microphone and camera": MEDIA_PROMPT_ACTION_USE,
    "Manual (do not handle popup)": MEDIA_PROMPT_ACTION_MANUAL,
}

MEDIA_PROMPT_VALUE_TO_LABEL = {
    value: label for label, value in MEDIA_PROMPT_LABEL_TO_VALUE.items()
}

LAUNCH_MODE_LABEL_TO_VALUE = {
    "Different incognito windows": LAUNCH_MODE_DIFFERENT_WINDOWS,
    "Same window (incognito tabs)": LAUNCH_MODE_SAME_WINDOW_TABS,
}

LAUNCH_MODE_VALUE_TO_LABEL = {
    value: label for label, value in LAUNCH_MODE_LABEL_TO_VALUE.items()
}

SAME_WINDOW_VIEW_LABEL_TO_VALUE = {
    "No change": SAME_WINDOW_VIEW_NONE,
    "Expanded window": SAME_WINDOW_VIEW_EXPANDED,
    "Full screen": SAME_WINDOW_VIEW_FULLSCREEN,
}

SAME_WINDOW_VIEW_VALUE_TO_LABEL = {
    value: label for label, value in SAME_WINDOW_VIEW_LABEL_TO_VALUE.items()
}

EXECUTION_SPEED_LABEL_TO_VALUE = {
    "Fast": EXECUTION_SPEED_FAST,
    "Balanced": EXECUTION_SPEED_BALANCED,
    "Reliable": EXECUTION_SPEED_RELIABLE,
}

EXECUTION_SPEED_VALUE_TO_LABEL = {
    value: label for label, value in EXECUTION_SPEED_LABEL_TO_VALUE.items()
}

MEET_URL_PATTERN = re.compile(
    r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:[/?].*)?$", re.IGNORECASE
)


@dataclass
class JoinConfig:
    meeting_url: str
    num_users: int
    participant_names: List[str]
    launch_mode: str
    same_window_post_join_view: str
    keep_camera_off: bool
    keep_mic_off: bool
    auto_join: bool
    mute_participant_sound: bool = True
    execution_speed_profile: str = EXECUTION_SPEED_FAST
    media_prompt_action: str = MEDIA_PROMPT_ACTION_CONTINUE
    launch_delay_seconds: float = 0.8
    element_retry_attempts: int = 5
    element_retry_wait_seconds: float = 2.0
    selector_lookup_timeout_seconds: float = 1.0


def is_valid_meet_url(url: str) -> bool:
    """Validate that the URL looks like a Google Meet room link."""
    if not url or not MEET_URL_PATTERN.match(url.strip()):
        return False

    parsed = urlparse(url.strip())
    return parsed.scheme.lower() == "https" and parsed.netloc.lower() == "meet.google.com"


def normalize_manual_names(raw_text: str) -> List[str]:
    return [line.strip() for line in raw_text.splitlines() if line.strip()]


def build_participant_names(total_count: int, manual_names: Sequence[str]) -> List[str]:
    """Fill missing names and ensure every participant name is unique."""
    source_names = list(manual_names[:total_count])
    while len(source_names) < total_count:
        source_names.append(f"User_{len(source_names) + 1}")

    unique_names: List[str] = []
    used: set[str] = set()
    for name in source_names:
        candidate = name
        suffix = 2
        while candidate in used:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used.add(candidate)
        unique_names.append(candidate)

    return unique_names


class MeetJoinController:
    def __init__(
        self,
        status_callback: Callable[[str], None],
        finished_callback: Callable[[], None],
    ) -> None:
        self._status_callback = status_callback
        self._finished_callback = finished_callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._drivers: List[WebDriver] = []
        self._drivers_lock = threading.Lock()
        self._chromedriver_path: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: JoinConfig) -> None:
        if self.is_running:
            raise RuntimeError("Automation is already running.")

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(config,), daemon=True)
        self._thread.start()

    def stop(self, close_browsers: bool = True) -> None:
        self._stop_event.set()
        if close_browsers:
            self._close_all_drivers()

    def shutdown(self) -> None:
        self.stop(close_browsers=True)

    def _run(self, config: JoinConfig) -> None:
        success_count = 0
        failed_count = 0

        try:
            self._emit("Starting automation...")

            if config.launch_mode == LAUNCH_MODE_SAME_WINDOW_TABS:
                self._emit("Launch mode: Same window (incognito tabs).")
                shared_driver = self._create_driver(config)
                self._register_driver(shared_driver)

                tab_handles: List[str] = [shared_driver.current_window_handle]
                if config.num_users > 1:
                    self._emit(
                        f"Preparing {config.num_users} tabs in the same incognito window..."
                    )
                for tab_index in range(2, config.num_users + 1):
                    created_handle = self._open_new_tab_in_same_window(shared_driver)
                    if created_handle is None:
                        raise RuntimeError(
                            f"Could not create tab {tab_index}/{config.num_users} in same window mode."
                        )
                    tab_handles.append(created_handle)
                self._emit(f"Prepared {len(tab_handles)} tab(s) in one incognito window.")

                for index, participant_name in enumerate(config.participant_names, start=1):
                    if self._stop_event.is_set():
                        break

                    self._emit(
                        f"[{index}/{config.num_users}] Launching participant '{participant_name}'"
                    )

                    try:
                        shared_driver.switch_to.window(tab_handles[index - 1])

                        joined = self._join_single(shared_driver, config, participant_name)
                        if joined:
                            success_count += 1
                        else:
                            failed_count += 1
                    except Exception as exc:  # pylint: disable=broad-except
                        failed_count += 1
                        self._emit(f"[{index}/{config.num_users}] Failed: {exc}")

                    if index < config.num_users and not self._stop_event.is_set():
                        time.sleep(config.launch_delay_seconds)

                if not self._stop_event.is_set() and success_count > 0:
                    self._apply_same_window_post_join_view(shared_driver, config)
            else:
                self._emit("Launch mode: Different incognito windows.")

                for index, participant_name in enumerate(config.participant_names, start=1):
                    if self._stop_event.is_set():
                        break

                    self._emit(
                        f"[{index}/{config.num_users}] Launching participant '{participant_name}'"
                    )

                    try:
                        driver = self._create_driver(config)
                        self._register_driver(driver)
                        joined = self._join_single(driver, config, participant_name)
                        if joined:
                            success_count += 1
                        else:
                            failed_count += 1
                    except Exception as exc:  # pylint: disable=broad-except
                        failed_count += 1
                        self._emit(f"[{index}/{config.num_users}] Failed: {exc}")

                    if index < config.num_users and not self._stop_event.is_set():
                        time.sleep(config.launch_delay_seconds)

            if self._stop_event.is_set():
                self._emit("Process stopped by user.")
            else:
                self._emit(
                    f"Process finished. Successful joins: {success_count}, Failed joins: {failed_count}"
                )
        except Exception as exc:  # pylint: disable=broad-except
            self._emit(f"Fatal automation setup error: {exc}")
        finally:
            self._finished_callback()

    def _open_new_tab_in_same_window(self, driver: WebDriver) -> Optional[str]:
        existing_handles = set(driver.window_handles)

        # CDP target creation is most reliable for opening a tab in the same window.
        try:
            driver.execute_cdp_cmd(
                "Target.createTarget",
                {"url": "about:blank", "newWindow": False, "background": False},
            )
        except WebDriverException:
            try:
                driver.execute_script("window.open('about:blank', '_blank');")
            except WebDriverException:
                try:
                    driver.switch_to.new_window("tab")
                    return driver.current_window_handle
                except WebDriverException:
                    return None

        try:
            WebDriverWait(driver, 4).until(
                lambda d: len(d.window_handles) > len(existing_handles)
            )
        except TimeoutException:
            return None

        new_handles = [handle for handle in driver.window_handles if handle not in existing_handles]
        if not new_handles:
            return None

        driver.switch_to.window(new_handles[-1])
        return new_handles[-1]

    def _apply_same_window_post_join_view(self, driver: WebDriver, config: JoinConfig) -> None:
        mode = config.same_window_post_join_view
        if mode == SAME_WINDOW_VIEW_NONE:
            self._emit("Post-join view mode: No change.")
            return

        if mode == SAME_WINDOW_VIEW_EXPANDED:
            try:
                driver.maximize_window()
                self._emit("Post-join view mode applied: Expanded window.")
                return
            except WebDriverException:
                pass

            try:
                driver.execute_script(
                    "window.moveTo(0,0); window.resizeTo(screen.availWidth, screen.availHeight);"
                )
                self._emit("Post-join view mode applied: Expanded window (script fallback).")
            except WebDriverException:
                self._emit("Could not expand window after join.")
            return

        if mode == SAME_WINDOW_VIEW_FULLSCREEN:
            try:
                driver.fullscreen_window()
                self._emit("Post-join view mode applied: Full screen.")
                return
            except WebDriverException:
                self._emit("Could not switch to full screen; trying expanded window fallback.")

            try:
                driver.maximize_window()
                self._emit("Fallback applied: Expanded window.")
            except WebDriverException:
                self._emit("Could not apply fallback expanded window mode.")

    def _create_driver(self, config: JoinConfig) -> WebDriver:
        options = ChromeOptions()
        options.add_argument("--incognito")
        options.add_argument("--window-size=1400,900")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-blink-features=AutomationControlled")

        # In packaged macOS apps, explicit Chrome binary discovery improves reliability.
        chrome_candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        for candidate in chrome_candidates:
            if os.path.exists(candidate):
                options.binary_location = candidate
                break

        if config.mute_participant_sound:
            # Mute tab output audio so joined participants stay silent.
            options.add_argument("--mute-audio")

        media_setting = 2
        if config.media_prompt_action == MEDIA_PROMPT_ACTION_USE:
            media_setting = 1
            # Auto-accept browser-level media permission prompts that Selenium cannot click.
            options.add_argument("--use-fake-ui-for-media-stream")
        elif config.media_prompt_action == MEDIA_PROMPT_ACTION_MANUAL:
            media_setting = 0

        prefs = {
            "profile.default_content_setting_values.media_stream_mic": media_setting,
            "profile.default_content_setting_values.media_stream_camera": media_setting,
            "profile.default_content_setting_values.media_stream": media_setting,
            "profile.managed_default_content_settings.media_stream_mic": media_setting,
            "profile.managed_default_content_settings.media_stream_camera": media_setting,
            "profile.default_content_setting_values.notifications": 2,
        }
        options.add_experimental_option("prefs", prefs)

        selenium_manager_error: Optional[Exception] = None
        try:
            self._emit("Starting Chrome via Selenium Manager...")
            return webdriver.Chrome(options=options)
        except Exception as exc:  # pylint: disable=broad-except
            selenium_manager_error = exc
            self._emit(f"Selenium Manager startup failed, trying fallback driver: {exc}")

        webdriver_manager_error: Optional[Exception] = None
        try:
            if self._chromedriver_path is None:
                self._emit("Resolving ChromeDriver with webdriver-manager...")
                self._chromedriver_path = ChromeDriverManager().install()

            service = ChromeService(executable_path=self._chromedriver_path)
            return webdriver.Chrome(service=service, options=options)
        except Exception as exc:  # pylint: disable=broad-except
            webdriver_manager_error = exc

        raise RuntimeError(
            "Could not start Chrome WebDriver. "
            f"Selenium Manager error: {selenium_manager_error}; "
            f"webdriver-manager error: {webdriver_manager_error}"
        )

    def _join_single(self, driver: WebDriver, config: JoinConfig, participant_name: str) -> bool:
        driver.get(config.meeting_url)

        if self._stop_event.is_set():
            return False

        self._wait_for_prejoin_ui(driver, config)

        continue_without_media_mode = (
            config.media_prompt_action == MEDIA_PROMPT_ACTION_CONTINUE
        )
        media_prompt_handled = False

        # Meet sometimes blocks the pre-join screen with a media consent dialog.
        media_prompt_handled = self._handle_prejoin_media_prompt(driver, config) or media_prompt_handled
        if config.media_prompt_action == MEDIA_PROMPT_ACTION_USE:
            # Recheck once because this prompt can render a moment after page load.
            time.sleep(0.15 if config.execution_speed_profile == EXECUTION_SPEED_FAST else 0.4)
            media_prompt_handled = (
                self._handle_prejoin_media_prompt(driver, config) or media_prompt_handled
            )
        self._dismiss_meet_tips(driver, config)

        self._set_participant_name(driver, participant_name, config)
        self._dismiss_meet_tips(driver, config)

        # Some accounts/sessions surface the media prompt again after name entry.
        media_prompt_handled = self._handle_prejoin_media_prompt(driver, config) or media_prompt_handled
        self._dismiss_meet_tips(driver, config)

        if continue_without_media_mode and (config.keep_camera_off or config.keep_mic_off):
            if media_prompt_handled:
                self._emit(
                    "Continue-without-media flow confirmed. Skipping explicit camera/mic toggle checks."
                )
            else:
                self._emit(
                    "Continue-without-media mode selected. Skipping explicit camera/mic toggle checks "
                    "to avoid unnecessary retries."
                )
        else:
            if config.keep_camera_off:
                self._ensure_device_off(driver, "camera", config)

            if config.keep_mic_off:
                self._ensure_device_off(driver, "microphone", config)

        self._dismiss_meet_tips(driver, config)

        if config.auto_join:
            if self._click_join_button(driver, config):
                self._emit(f"'{participant_name}' requested to join.")
                return True

            self._emit(f"'{participant_name}' could not find the join button.")
            return False

        self._emit(f"'{participant_name}' is waiting on the join screen (Auto Join disabled).")
        return True

    def _wait_for_prejoin_ui(self, driver: WebDriver, config: JoinConfig) -> None:
        selectors: Sequence[Selector] = (
            (By.CSS_SELECTOR, "input[aria-label='Your name']"),
            (By.CSS_SELECTOR, "input[placeholder='Your name']"),
            (By.XPATH, "//button[contains(normalize-space(), 'Ask to join')]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Join now')]"),
        )

        lookup_timeout = max(0.15, config.selector_lookup_timeout_seconds)

        for attempt in range(1, config.element_retry_attempts + 1):
            if self._stop_event.is_set():
                return

            if self._find_first_present(driver, selectors, timeout_seconds=lookup_timeout) is not None:
                return

            self._emit(f"Waiting for Meet pre-join UI (attempt {attempt}/{config.element_retry_attempts}).")
            time.sleep(config.element_retry_wait_seconds)

    def _set_participant_name(
        self,
        driver: WebDriver,
        participant_name: str,
        config: JoinConfig,
    ) -> None:
        selectors: Sequence[Selector] = (
            (By.CSS_SELECTOR, "input[aria-label='Your name']"),
            (By.CSS_SELECTOR, "input[placeholder='Your name']"),
            (
                By.XPATH,
                "//input[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'name')]",
            ),
            (
                By.XPATH,
                "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'name')]",
            ),
            (By.XPATH, "//input[@type='text' and not(@disabled)]"),
            (By.CSS_SELECTOR, "input[aria-label*='name']"),
        )

        name_input = self._find_first_visible(
            driver,
            selectors,
            attempts=config.element_retry_attempts,
            wait_between_attempts=config.element_retry_wait_seconds,
            timeout_seconds=max(0.15, config.selector_lookup_timeout_seconds),
        )

        if name_input is None:
            self._emit(
                f"Name field not found for '{participant_name}'. The page may require manual handling."
            )
            return

        if self._type_participant_name(driver, name_input, participant_name):
            self._emit(f"Name set to '{participant_name}'.")
            return

        self._emit(f"Could not set name for '{participant_name}'.")

    def _type_participant_name(
        self,
        driver: WebDriver,
        input_element: WebElement,
        participant_name: str,
    ) -> bool:
        try:
            input_element.click()
        except WebDriverException:
            pass

        try:
            self._clear_and_type(input_element, participant_name)
        except WebDriverException:
            pass

        current_value = (input_element.get_attribute("value") or "").strip()
        if current_value == participant_name:
            try:
                input_element.send_keys(Keys.TAB)
            except WebDriverException:
                pass
            return True

        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                input_element,
                participant_name,
            )
        except WebDriverException:
            return False

        current_value = (input_element.get_attribute("value") or "").strip()
        if current_value == participant_name:
            try:
                input_element.send_keys(Keys.TAB)
            except WebDriverException:
                pass
            return True

        return False

    def _handle_prejoin_media_prompt(self, driver: WebDriver, config: JoinConfig) -> bool:
        if config.media_prompt_action == MEDIA_PROMPT_ACTION_MANUAL:
            return False

        if config.media_prompt_action == MEDIA_PROMPT_ACTION_USE:
            selectors: Sequence[Selector] = (
                (
                    By.XPATH,
                    "//button[.//span[contains(normalize-space(), 'Use microphone and camera')] or contains(normalize-space(), 'Use microphone and camera')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and contains(normalize-space(), 'Use microphone and camera')]",
                ),
            )
            action_label = "Use microphone and camera"
        else:
            selectors = (
                (
                    By.XPATH,
                    "//button[.//span[contains(normalize-space(), 'Continue without microphone and camera')] or contains(normalize-space(), 'Continue without microphone and camera')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and contains(normalize-space(), 'Continue without microphone and camera')]",
                ),
                (
                    By.XPATH,
                    "//button[.//span[contains(normalize-space(), 'Continue without microphone')] or contains(normalize-space(), 'Continue without microphone')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and contains(normalize-space(), 'Continue without microphone')]",
                ),
            )
            action_label = "Continue without microphone and camera"

        for attempt in range(1, config.element_retry_attempts + 1):
            if self._stop_event.is_set():
                return False

            prompt_button = self._find_first_clickable(
                driver,
                selectors,
                timeout_seconds=max(0.15, config.selector_lookup_timeout_seconds),
            )
            if prompt_button is None:
                return False

            try:
                prompt_button.click()
                self._emit(f"Handled media popup using '{action_label}'.")
                time.sleep(0.2)
                return True
            except (ElementClickInterceptedException, StaleElementReferenceException):
                try:
                    driver.execute_script("arguments[0].click();", prompt_button)
                    self._emit(f"Handled media popup via script click ('{action_label}').")
                    time.sleep(0.2)
                    return True
                except WebDriverException:
                    self._emit(
                        f"Media prompt click retry needed (attempt {attempt}/{config.element_retry_attempts})."
                    )
                    time.sleep(config.element_retry_wait_seconds)

        return False

    def _dismiss_meet_tips(self, driver: WebDriver, config: JoinConfig) -> bool:
        selectors: Sequence[Selector] = (
            (By.XPATH, "//button[contains(normalize-space(), 'Got it')]"),
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Got it')]]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Got it')]"),
            # Fallback for close buttons on floating guidance cards.
            (By.XPATH, "//button[@aria-label='Close' or @aria-label='Dismiss']"),
        )

        dismissed = False
        max_attempts = 1 if config.execution_speed_profile == EXECUTION_SPEED_FAST else config.element_retry_attempts
        tip_timeout = max(0.12, config.selector_lookup_timeout_seconds * 0.6)
        for attempt in range(1, max_attempts + 1):
            if self._stop_event.is_set():
                return dismissed

            tip_button = self._find_first_present(driver, selectors, timeout_seconds=tip_timeout)
            if tip_button is None:
                return dismissed

            try:
                tip_button.click()
                dismissed = True
                self._emit("Dismissed Meet tip popup.")
                time.sleep(0.1)
                continue
            except (ElementClickInterceptedException, StaleElementReferenceException):
                try:
                    driver.execute_script("arguments[0].click();", tip_button)
                    dismissed = True
                    self._emit("Dismissed Meet tip popup via script click.")
                    time.sleep(0.1)
                    continue
                except WebDriverException:
                    self._emit(
                        f"Meet tip dismissal retry needed (attempt {attempt}/{config.element_retry_attempts})."
                    )
                    time.sleep(0.2)

        return dismissed

    def _ensure_device_off(self, driver: WebDriver, device_label: str, config: JoinConfig) -> bool:
        fast_mode = config.execution_speed_profile == EXECUTION_SPEED_FAST
        if device_label == "camera":
            on_selectors: Sequence[Selector] = (
                (
                    By.XPATH,
                    "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off camera') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off video') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'stop camera')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and (contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off camera') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off video') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'stop camera'))]",
                ),
            )
            off_selectors: Sequence[Selector] = (
                (
                    By.XPATH,
                    "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on camera') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on video') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'start camera')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and (contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on camera') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on video') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'start camera'))]",
                ),
            )
            shortcut_key = "e"
        else:
            on_selectors = (
                (
                    By.XPATH,
                    "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off microphone') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mute microphone')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and (contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn off microphone') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mute microphone'))]",
                ),
            )
            off_selectors = (
                (
                    By.XPATH,
                    "//button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on microphone') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unmute microphone')]",
                ),
                (
                    By.XPATH,
                    "//div[@role='button' and (contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'turn on microphone') or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unmute microphone'))]",
                ),
            )
            shortcut_key = "d"

        toggle_timeout = max(0.12, config.selector_lookup_timeout_seconds)
        confirm_timeout = 0.2 if fast_mode else 0.7
        max_attempts = 1 if fast_mode else config.element_retry_attempts

        for attempt in range(1, max_attempts + 1):
            if self._stop_event.is_set():
                return False

            # If the OFF-state button is already present, nothing to do.
            already_off = self._find_first_present(driver, off_selectors, timeout_seconds=toggle_timeout)
            if already_off is not None:
                self._emit(f"{device_label.capitalize()} already OFF.")
                return True

            should_turn_off = self._find_first_clickable(
                driver,
                on_selectors,
                timeout_seconds=toggle_timeout,
            )
            if should_turn_off is None:
                if fast_mode:
                    self._emit(
                        f"Fast mode: could not read {device_label} state quickly, using shortcut fallback."
                    )
                    self._toggle_device_shortcut(driver, shortcut_key, device_label)
                    return True
                self._emit(
                    f"Could not read {device_label} toggle state (attempt {attempt}/{config.element_retry_attempts})."
                )
                time.sleep(config.element_retry_wait_seconds)
                continue

            try:
                should_turn_off.click()
                self._emit(f"Clicked {device_label} control to turn it OFF.")
            except (ElementClickInterceptedException, StaleElementReferenceException):
                try:
                    driver.execute_script("arguments[0].click();", should_turn_off)
                    self._emit(f"Clicked {device_label} control via script to turn it OFF.")
                except WebDriverException:
                    self._emit(
                        f"{device_label.capitalize()} control click retry needed (attempt {attempt}/{config.element_retry_attempts})."
                    )
                    time.sleep(config.element_retry_wait_seconds)
                    continue

            if fast_mode:
                return True

            time.sleep(0.12)
            confirmed_off = self._find_first_present(
                driver,
                off_selectors,
                timeout_seconds=confirm_timeout,
            )
            if confirmed_off is not None:
                self._emit(f"Confirmed {device_label} is OFF before join.")
                return True

        self._emit(
            f"Could not reliably confirm {device_label} OFF from UI. Trying keyboard shortcut fallback."
        )
        self._toggle_device_shortcut(driver, shortcut_key, device_label)
        time.sleep(0.12)
        confirmed_off = self._find_first_present(
            driver,
            off_selectors,
            timeout_seconds=confirm_timeout,
        )
        if confirmed_off is not None:
            self._emit(f"Confirmed {device_label} is OFF after shortcut fallback.")
            return True

        self._emit(f"Warning: unable to verify {device_label} OFF state before join.")
        return False

    def _toggle_device_shortcut(self, driver: WebDriver, key_char: str, device_label: str) -> None:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.click()
            body.send_keys(Keys.CONTROL, key_char)
            self._emit(f"Sent Ctrl+{key_char.upper()} to toggle {device_label}.")
            time.sleep(0.1)
            return
        except WebDriverException:
            pass

        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.click()
            body.send_keys(Keys.COMMAND, key_char)
            self._emit(f"Sent Cmd+{key_char.upper()} to toggle {device_label}.")
            time.sleep(0.1)
        except WebDriverException:
            self._emit(f"Failed to toggle {device_label} shortcut.")

    def _click_join_button(self, driver: WebDriver, config: JoinConfig) -> bool:
        selectors: Sequence[Selector] = (
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Ask to join')]]"),
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Join now')]]"),
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Request to join')]]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Ask to join')]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Join now')]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Request to join')]"),
            (By.XPATH, "//button[contains(@aria-label, 'Ask to join')]"),
            (By.XPATH, "//button[contains(@aria-label, 'Join now')]"),
            (By.XPATH, "//button[contains(@aria-label, 'Request to join')]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Ask to join')]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Join now')]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Request to join')]"),
        )

        for attempt in range(1, config.element_retry_attempts + 1):
            if self._stop_event.is_set():
                return False

            button = self._find_first_present(
                driver,
                selectors,
                timeout_seconds=max(0.2, config.selector_lookup_timeout_seconds),
            )
            if button is None:
                self._emit(
                    f"Join button not found (attempt {attempt}/{config.element_retry_attempts})."
                )
                time.sleep(config.element_retry_wait_seconds)
                continue

            disabled_attr = button.get_attribute("disabled")
            aria_disabled = (button.get_attribute("aria-disabled") or "").strip().lower()
            if disabled_attr is not None or aria_disabled == "true":
                self._emit(
                    f"Join button is disabled (attempt {attempt}/{config.element_retry_attempts}). "
                    "Waiting for name entry to apply."
                )
                if config.media_prompt_action == MEDIA_PROMPT_ACTION_USE and attempt == 1:
                    self._emit(
                        "If Chrome media permission is still visible, wait a moment for auto-allow before join."
                    )
                time.sleep(config.element_retry_wait_seconds)
                continue

            try:
                button.click()
                return True
            except (ElementClickInterceptedException, StaleElementReferenceException):
                try:
                    driver.execute_script("arguments[0].click();", button)
                    return True
                except WebDriverException:
                    self._emit(
                        f"Join click retry needed (attempt {attempt}/{config.element_retry_attempts})."
                    )
                    time.sleep(config.element_retry_wait_seconds)

        return False

    def _clear_and_type(self, input_element: WebElement, text: str) -> None:
        for modifier in (Keys.CONTROL, Keys.COMMAND):
            try:
                input_element.send_keys(modifier, "a")
                input_element.send_keys(Keys.BACKSPACE)
                input_element.send_keys(text)
                return
            except WebDriverException:
                continue

        input_element.clear()
        input_element.send_keys(text)

    def _find_first_visible(
        self,
        driver: WebDriver,
        selectors: Sequence[Selector],
        attempts: int,
        wait_between_attempts: float,
        timeout_seconds: float,
    ):
        for attempt in range(1, attempts + 1):
            if self._stop_event.is_set():
                return None

            visible_element = self._find_first_present(
                driver,
                selectors,
                timeout_seconds=timeout_seconds,
                require_visible=True,
            )
            if visible_element is not None:
                return visible_element

            self._emit(f"Waiting for name field (attempt {attempt}/{attempts}).")
            time.sleep(wait_between_attempts)

        return None

    def _find_first_clickable(
        self,
        driver: WebDriver,
        selectors: Sequence[Selector],
        timeout_seconds: float = 2,
    ):
        return self._find_first_present(
            driver,
            selectors,
            timeout_seconds=timeout_seconds,
            require_clickable=True,
        )

    def _find_first_present(
        self,
        driver: WebDriver,
        selectors: Sequence[Selector],
        timeout_seconds: float = 2,
        require_visible: bool = False,
        require_clickable: bool = False,
        poll_interval_seconds: float = 0.05,
    ):
        end_time = time.time() + timeout_seconds
        while time.time() < end_time:
            if self._stop_event.is_set():
                return None

            for by, value in selectors:
                try:
                    elements = driver.find_elements(by, value)
                except WebDriverException:
                    continue

                for element in elements:
                    if require_visible and not element.is_displayed():
                        continue
                    if require_clickable and (not element.is_displayed() or not element.is_enabled()):
                        continue
                    return element

            time.sleep(poll_interval_seconds)

        return None

    def _register_driver(self, driver: WebDriver) -> None:
        with self._drivers_lock:
            self._drivers.append(driver)

    def _close_all_drivers(self) -> None:
        with self._drivers_lock:
            drivers = list(self._drivers)
            self._drivers.clear()

        for driver in drivers:
            try:
                driver.quit()
            except WebDriverException:
                pass

    def _emit(self, message: str) -> None:
        self._status_callback(message)


class MultiMeetJoinerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Multi User Google Meet Joiner")
        self.geometry("820x700")
        self.minsize(760, 620)

        self._events: queue.Queue[Tuple[str, str]] = queue.Queue()
        self._controller = MeetJoinController(
            status_callback=self._enqueue_status,
            finished_callback=self._enqueue_finished,
        )

        self._meeting_url = tk.StringVar()
        self._num_users = tk.StringVar(value="5")
        self._launch_mode_label = tk.StringVar(
            value=LAUNCH_MODE_VALUE_TO_LABEL[LAUNCH_MODE_DIFFERENT_WINDOWS]
        )
        self._same_window_post_join_view_label = tk.StringVar(
            value=SAME_WINDOW_VIEW_VALUE_TO_LABEL[SAME_WINDOW_VIEW_NONE]
        )
        self._execution_speed_label = tk.StringVar(
            value=EXECUTION_SPEED_VALUE_TO_LABEL[EXECUTION_SPEED_FAST]
        )
        self._keep_camera_off = tk.BooleanVar(value=True)
        self._keep_mic_off = tk.BooleanVar(value=True)
        self._auto_join = tk.BooleanVar(value=True)
        self._mute_participant_sound = tk.BooleanVar(value=True)
        self._media_prompt_label = tk.StringVar(
            value=MEDIA_PROMPT_VALUE_TO_LABEL[MEDIA_PROMPT_ACTION_CONTINUE]
        )
        self._status_text = tk.StringVar(value="Status: Waiting...")

        self._build_ui()
        self._launch_mode_label.trace_add("write", self._on_launch_mode_changed)
        self._update_same_window_view_visibility()
        self._append_status("Application started. Enter meeting details and click Start Joining.")
        self._poll_events()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=14)
        container.grid(row=0, column=0, sticky="nsew")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        title = ttk.Label(container, text="Multi Meet Joiner", font=("Helvetica", 18, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 12))

        link_frame = ttk.LabelFrame(container, text="Meeting Configuration", padding=10)
        link_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        link_frame.columnconfigure(1, weight=1)

        ttk.Label(link_frame, text="Meeting Link").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(link_frame, textvariable=self._meeting_url).grid(
            row=0, column=1, sticky="ew", pady=(0, 8)
        )

        ttk.Label(link_frame, text="Number of Users").grid(row=1, column=0, sticky="w", padx=(0, 10))
        ttk.Spinbox(link_frame, from_=1, to=100, textvariable=self._num_users, width=10).grid(
            row=1, column=1, sticky="w"
        )

        ttk.Label(link_frame, text="Launch Mode").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ttk.Combobox(
            link_frame,
            textvariable=self._launch_mode_label,
            values=list(LAUNCH_MODE_LABEL_TO_VALUE.keys()),
            state="readonly",
            width=34,
        ).grid(row=2, column=1, sticky="w", pady=(8, 0))

        self._same_window_view_label_widget = ttk.Label(
            link_frame,
            text="After Join (Same Window)",
        )
        self._same_window_view_label_widget.grid(
            row=3,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=(8, 0),
        )

        self._same_window_view_combo_widget = ttk.Combobox(
            link_frame,
            textvariable=self._same_window_post_join_view_label,
            values=list(SAME_WINDOW_VIEW_LABEL_TO_VALUE.keys()),
            state="readonly",
            width=34,
        )
        self._same_window_view_combo_widget.grid(row=3, column=1, sticky="w", pady=(8, 0))

        names_frame = ttk.LabelFrame(container, text="Names List (one per line)", padding=10)
        names_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        names_frame.columnconfigure(0, weight=1)
        names_frame.rowconfigure(0, weight=1)

        self._names_text = scrolledtext.ScrolledText(names_frame, height=8, wrap=tk.WORD)
        self._names_text.grid(row=0, column=0, sticky="nsew")
        self._names_text.insert("1.0", "Student 1\nStudent 2\nStudent 3\n")

        options_frame = ttk.LabelFrame(container, text="Options", padding=10)
        options_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        ttk.Checkbutton(
            options_frame,
            text="Keep Camera OFF",
            variable=self._keep_camera_off,
        ).grid(row=0, column=0, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options_frame,
            text="Keep Mic OFF",
            variable=self._keep_mic_off,
        ).grid(row=0, column=1, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options_frame,
            text="Auto Join",
            variable=self._auto_join,
        ).grid(row=0, column=2, sticky="w")

        ttk.Checkbutton(
            options_frame,
            text="Mute Participant Sound After Join",
            variable=self._mute_participant_sound,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(options_frame, text="Pre-Join Popup Action").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Combobox(
            options_frame,
            textvariable=self._media_prompt_label,
            values=list(MEDIA_PROMPT_LABEL_TO_VALUE.keys()),
            state="readonly",
            width=42,
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(options_frame, text="Execution Speed").grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Combobox(
            options_frame,
            textvariable=self._execution_speed_label,
            values=list(EXECUTION_SPEED_LABEL_TO_VALUE.keys()),
            state="readonly",
            width=20,
        ).grid(row=3, column=1, sticky="w", pady=(8, 0))

        controls = ttk.Frame(container)
        controls.grid(row=4, column=0, sticky="ew", pady=(0, 10))

        self._start_button = ttk.Button(controls, text="Start Joining", command=self._on_start)
        self._start_button.grid(row=0, column=0, padx=(0, 8))

        self._stop_button = ttk.Button(controls, text="Stop", command=self._on_stop, state=tk.DISABLED)
        self._stop_button.grid(row=0, column=1)

        status_frame = ttk.LabelFrame(container, text="Status", padding=10)
        status_frame.grid(row=5, column=0, sticky="nsew")
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(1, weight=1)

        ttk.Label(status_frame, textvariable=self._status_text).grid(row=0, column=0, sticky="w")

        self._status_log = scrolledtext.ScrolledText(status_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self._status_log.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        container.rowconfigure(2, weight=1)
        container.rowconfigure(5, weight=1)

    def _on_start(self) -> None:
        if self._controller.is_running:
            messagebox.showinfo("Automation Running", "Automation is already running.")
            return

        config = self._collect_config()
        if config is None:
            return

        self._set_running_state(True)
        self._append_status("----------------------------------------")
        self._append_status("Start requested.")
        self._append_status(f"Launch mode: {LAUNCH_MODE_VALUE_TO_LABEL[config.launch_mode]}")
        if config.launch_mode == LAUNCH_MODE_SAME_WINDOW_TABS:
            self._append_status(
                "Post-join same-window view: "
                f"{SAME_WINDOW_VIEW_VALUE_TO_LABEL[config.same_window_post_join_view]}"
            )
        self._append_status(
            f"Execution speed profile: {EXECUTION_SPEED_VALUE_TO_LABEL[config.execution_speed_profile]}"
        )
        self._append_status(
            f"Pre-join popup action: {MEDIA_PROMPT_VALUE_TO_LABEL[config.media_prompt_action]}"
        )
        if config.mute_participant_sound:
            self._append_status("Participant speaker sound will be muted after join.")
        else:
            self._append_status("Participant speaker sound will remain ON after join.")
        self._append_status(
            f"Validated names list count: {len(config.participant_names)} for {config.num_users} participant(s)."
        )

        try:
            self._controller.start(config)
        except RuntimeError as exc:
            self._set_running_state(False)
            messagebox.showerror("Could Not Start", str(exc))

    def _on_stop(self) -> None:
        self._append_status("Stop requested. Closing all active browser sessions...")
        self._stop_button.configure(state=tk.DISABLED)
        self._controller.stop(close_browsers=True)

    def _is_same_window_mode_selected(self) -> bool:
        launch_mode_raw = self._launch_mode_label.get().strip().lower()
        if "same window" in launch_mode_raw:
            return True

        mapped_value = LAUNCH_MODE_LABEL_TO_VALUE.get(self._launch_mode_label.get().strip())
        return mapped_value == LAUNCH_MODE_SAME_WINDOW_TABS

    def _on_launch_mode_changed(self, *_: object) -> None:
        self._update_same_window_view_visibility()

    def _update_same_window_view_visibility(self) -> None:
        if self._is_same_window_mode_selected():
            self._same_window_view_label_widget.grid()
            self._same_window_view_combo_widget.grid()
        else:
            self._same_window_view_label_widget.grid_remove()
            self._same_window_view_combo_widget.grid_remove()
            self._same_window_post_join_view_label.set(
                SAME_WINDOW_VIEW_VALUE_TO_LABEL[SAME_WINDOW_VIEW_NONE]
            )

    def _collect_config(self) -> Optional[JoinConfig]:
        meeting_url = self._meeting_url.get().strip()
        if not is_valid_meet_url(meeting_url):
            self._append_status("Invalid meeting link. Please enter a valid Google Meet URL.")
            messagebox.showerror(
                "Invalid Meeting Link",
                "Enter a valid Google Meet URL.\nExample: https://meet.google.com/abc-defg-hij",
            )
            return None

        try:
            num_users = int(self._num_users.get().strip())
        except ValueError:
            self._append_status("Invalid number of users. Enter a whole number.")
            messagebox.showerror("Invalid Number", "Number of users must be an integer.")
            return None

        if num_users < 1 or num_users > 100:
            self._append_status("Number of users out of range. Allowed range is 1 to 100.")
            messagebox.showerror("Invalid Number", "Number of users must be between 1 and 100.")
            return None

        manual_names = normalize_manual_names(self._names_text.get("1.0", tk.END))
        if len(manual_names) != num_users:
            self._append_status(
                "Names list count does not match number of users. "
                f"Provided names: {len(manual_names)}, Number of users: {num_users}."
            )
            messagebox.showerror(
                "Names Count Mismatch",
                "Provide exactly one participant name per line.\n"
                f"Names entered: {len(manual_names)}\n"
                f"Number of users: {num_users}",
            )
            return None

        participant_names = list(manual_names)
        launch_mode_raw = self._launch_mode_label.get().strip()
        launch_mode = LAUNCH_MODE_LABEL_TO_VALUE.get(
            launch_mode_raw,
            LAUNCH_MODE_DIFFERENT_WINDOWS,
        )
        if launch_mode == LAUNCH_MODE_DIFFERENT_WINDOWS and "same window" in launch_mode_raw.lower():
            launch_mode = LAUNCH_MODE_SAME_WINDOW_TABS
        same_window_post_join_view = SAME_WINDOW_VIEW_LABEL_TO_VALUE.get(
            self._same_window_post_join_view_label.get().strip(),
            SAME_WINDOW_VIEW_NONE,
        )
        if launch_mode != LAUNCH_MODE_SAME_WINDOW_TABS:
            same_window_post_join_view = SAME_WINDOW_VIEW_NONE

        execution_speed_profile = EXECUTION_SPEED_LABEL_TO_VALUE.get(
            self._execution_speed_label.get().strip(),
            EXECUTION_SPEED_FAST,
        )
        if execution_speed_profile == EXECUTION_SPEED_FAST:
            launch_delay_seconds = 0.05
            element_retry_attempts = 1
            element_retry_wait_seconds = 0.15
            selector_lookup_timeout_seconds = 0.28
        elif execution_speed_profile == EXECUTION_SPEED_BALANCED:
            launch_delay_seconds = 0.2
            element_retry_attempts = 2
            element_retry_wait_seconds = 0.45
            selector_lookup_timeout_seconds = 0.6
        else:
            launch_delay_seconds = 0.8
            element_retry_attempts = 5
            element_retry_wait_seconds = 2.0
            selector_lookup_timeout_seconds = 1.2

        media_prompt_action = MEDIA_PROMPT_LABEL_TO_VALUE.get(
            self._media_prompt_label.get(),
            MEDIA_PROMPT_ACTION_CONTINUE,
        )

        return JoinConfig(
            meeting_url=meeting_url,
            num_users=num_users,
            participant_names=participant_names,
            launch_mode=launch_mode,
            same_window_post_join_view=same_window_post_join_view,
            keep_camera_off=self._keep_camera_off.get(),
            keep_mic_off=self._keep_mic_off.get(),
            auto_join=self._auto_join.get(),
            mute_participant_sound=self._mute_participant_sound.get(),
            execution_speed_profile=execution_speed_profile,
            media_prompt_action=media_prompt_action,
            launch_delay_seconds=launch_delay_seconds,
            element_retry_attempts=element_retry_attempts,
            element_retry_wait_seconds=element_retry_wait_seconds,
            selector_lookup_timeout_seconds=selector_lookup_timeout_seconds,
        )

    def _set_running_state(self, running: bool) -> None:
        self._start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self._stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _enqueue_status(self, message: str) -> None:
        self._events.put(("status", message))

    def _enqueue_finished(self) -> None:
        self._events.put(("finished", ""))

    def _poll_events(self) -> None:
        while True:
            try:
                event_type, payload = self._events.get_nowait()
            except queue.Empty:
                break

            if event_type == "status":
                self._append_status(payload)
            elif event_type == "finished":
                self._set_running_state(False)

        self.after(150, self._poll_events)

    def _append_status(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {message}"
        self._status_text.set(f"Status: {message}")
        print(line, flush=True)
        self._status_log.configure(state=tk.NORMAL)
        self._status_log.insert(tk.END, f"{line}\n")
        self._status_log.see(tk.END)
        self._status_log.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self._controller.is_running:
            should_close = messagebox.askyesno(
                "Exit Application",
                "Automation is still running. Stop all sessions and exit?",
            )
            if not should_close:
                return

        self._controller.shutdown()
        self.destroy()


def main() -> None:
    app = MultiMeetJoinerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
