#!/usr/bin/env python3
"""Desktop app for joining a Google Meet link with multiple simulated users."""

from __future__ import annotations

import queue
import re
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
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

Selector = Tuple[str, str]
MEET_URL_PATTERN = re.compile(
    r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:[/?].*)?$", re.IGNORECASE
)


@dataclass
class JoinConfig:
    meeting_url: str
    num_users: int
    participant_names: List[str]
    keep_camera_off: bool
    keep_mic_off: bool
    auto_join: bool
    launch_delay_seconds: float = 0.8
    element_retry_attempts: int = 3
    element_retry_wait_seconds: float = 2.0


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

            for index, participant_name in enumerate(config.participant_names, start=1):
                if self._stop_event.is_set():
                    break

                self._emit(f"[{index}/{config.num_users}] Launching participant '{participant_name}'")

                try:
                    driver = self._create_driver()
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
        finally:
            self._finished_callback()

    def _create_driver(self) -> WebDriver:
        options = webdriver.ChromeOptions()
        options.add_argument("--incognito")
        options.add_argument("--window-size=1400,900")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-blink-features=AutomationControlled")

        prefs = {
            "profile.default_content_setting_values.media_stream_mic": 2,
            "profile.default_content_setting_values.media_stream_camera": 2,
            "profile.default_content_setting_values.notifications": 2,
        }
        options.add_experimental_option("prefs", prefs)

        if self._chromedriver_path is None:
            self._emit("Resolving ChromeDriver (first launch may take a moment)...")
            self._chromedriver_path = ChromeDriverManager().install()

        service = ChromeService(executable_path=self._chromedriver_path)
        return webdriver.Chrome(service=service, options=options)

    def _join_single(self, driver: WebDriver, config: JoinConfig, participant_name: str) -> bool:
        driver.get(config.meeting_url)

        if self._stop_event.is_set():
            return False

        # Meet sometimes blocks the pre-join screen with a media consent dialog.
        self._handle_prejoin_media_prompt(driver, config)

        self._set_participant_name(driver, participant_name, config)

        if config.keep_camera_off:
            self._toggle_device(driver, "e", "camera")

        if config.keep_mic_off:
            self._toggle_device(driver, "d", "microphone")

        # Some accounts/sessions surface the media prompt again after toggles.
        self._handle_prejoin_media_prompt(driver, config)

        if config.auto_join:
            if self._click_join_button(driver, config):
                self._emit(f"'{participant_name}' requested to join.")
                return True

            self._emit(f"'{participant_name}' could not find the join button.")
            return False

        self._emit(f"'{participant_name}' is waiting on the join screen (Auto Join disabled).")
        return True

    def _set_participant_name(
        self,
        driver: WebDriver,
        participant_name: str,
        config: JoinConfig,
    ) -> None:
        selectors: Sequence[Selector] = (
            (By.CSS_SELECTOR, "input[aria-label='Your name']"),
            (By.CSS_SELECTOR, "input[placeholder='Your name']"),
            (By.CSS_SELECTOR, "input[aria-label*='name']"),
        )

        name_input = self._find_first_visible(
            driver,
            selectors,
            attempts=config.element_retry_attempts,
            wait_between_attempts=config.element_retry_wait_seconds,
        )

        if name_input is None:
            self._emit(
                f"Name field not found for '{participant_name}'. The page may require manual handling."
            )
            return

        name_input.click()
        self._clear_and_type(name_input, participant_name)
        self._emit(f"Name set to '{participant_name}'.")

    def _handle_prejoin_media_prompt(self, driver: WebDriver, config: JoinConfig) -> bool:
        selectors: Sequence[Selector] = (
            (
                By.XPATH,
                "//button[.//*[contains(normalize-space(), 'Continue without microphone and camera')]]",
            ),
            (
                By.XPATH,
                "//button[contains(normalize-space(), 'Continue without microphone and camera')]",
            ),
            (
                By.XPATH,
                "//div[@role='button' and contains(normalize-space(), 'Continue without microphone and camera')]",
            ),
            (
                By.XPATH,
                "//button[.//*[contains(normalize-space(), 'Continue without microphone')]]",
            ),
        )

        for attempt in range(1, config.element_retry_attempts + 1):
            if self._stop_event.is_set():
                return False

            prompt_button = self._find_first_clickable(driver, selectors, timeout_seconds=1)
            if prompt_button is None:
                return False

            try:
                prompt_button.click()
                self._emit("Dismissed media prompt using 'Continue without microphone and camera'.")
                time.sleep(0.5)
                return True
            except (ElementClickInterceptedException, StaleElementReferenceException):
                try:
                    driver.execute_script("arguments[0].click();", prompt_button)
                    self._emit("Dismissed media prompt via script click.")
                    time.sleep(0.5)
                    return True
                except WebDriverException:
                    self._emit(
                        f"Media prompt click retry needed (attempt {attempt}/{config.element_retry_attempts})."
                    )
                    time.sleep(config.element_retry_wait_seconds)

        return False

    def _toggle_device(self, driver: WebDriver, key_char: str, device_label: str) -> None:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.click()
            body.send_keys(Keys.chord(Keys.CONTROL, key_char))
            self._emit(f"Sent Ctrl+{key_char.upper()} to toggle {device_label}.")
            time.sleep(0.3)
            return
        except WebDriverException:
            pass

        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.click()
            body.send_keys(Keys.chord(Keys.COMMAND, key_char))
            self._emit(f"Sent Cmd+{key_char.upper()} to toggle {device_label}.")
            time.sleep(0.3)
        except WebDriverException:
            self._emit(f"Failed to toggle {device_label} shortcut.")

    def _click_join_button(self, driver: WebDriver, config: JoinConfig) -> bool:
        selectors: Sequence[Selector] = (
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Ask to join')]]"),
            (By.XPATH, "//button[.//span[contains(normalize-space(), 'Join now')]]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Ask to join')]"),
            (By.XPATH, "//button[contains(normalize-space(), 'Join now')]"),
            (By.XPATH, "//button[contains(@aria-label, 'Ask to join')]"),
            (By.XPATH, "//button[contains(@aria-label, 'Join now')]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Ask to join')]"),
            (By.XPATH, "//div[@role='button' and contains(normalize-space(), 'Join now')]"),
        )

        for attempt in range(1, config.element_retry_attempts + 1):
            if self._stop_event.is_set():
                return False

            button = self._find_first_clickable(driver, selectors, timeout_seconds=2)
            if button is None:
                self._emit(
                    f"Join button not found (attempt {attempt}/{config.element_retry_attempts})."
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
                input_element.send_keys(Keys.chord(modifier, "a"))
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
    ):
        for attempt in range(1, attempts + 1):
            if self._stop_event.is_set():
                return None

            for by, value in selectors:
                try:
                    return WebDriverWait(driver, 2).until(
                        EC.visibility_of_element_located((by, value))
                    )
                except TimeoutException:
                    continue

            self._emit(f"Waiting for name field (attempt {attempt}/{attempts}).")
            time.sleep(wait_between_attempts)

        return None

    def _find_first_clickable(
        self,
        driver: WebDriver,
        selectors: Sequence[Selector],
        timeout_seconds: float = 2,
    ):
        for by, value in selectors:
            try:
                return WebDriverWait(driver, timeout_seconds).until(
                    EC.element_to_be_clickable((by, value))
                )
            except TimeoutException:
                continue
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
        self._keep_camera_off = tk.BooleanVar(value=True)
        self._keep_mic_off = tk.BooleanVar(value=True)
        self._auto_join = tk.BooleanVar(value=True)
        self._status_text = tk.StringVar(value="Status: Waiting...")

        self._build_ui()
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

        provided_names = normalize_manual_names(self._names_text.get("1.0", tk.END))
        generated_count = max(0, config.num_users - len(provided_names))

        self._set_running_state(True)
        self._append_status("----------------------------------------")
        self._append_status("Start requested.")
        if generated_count:
            self._append_status(f"Auto-generated {generated_count} name(s) to match user count.")

        try:
            self._controller.start(config)
        except RuntimeError as exc:
            self._set_running_state(False)
            messagebox.showerror("Could Not Start", str(exc))

    def _on_stop(self) -> None:
        self._append_status("Stop requested. Closing all active browser sessions...")
        self._stop_button.configure(state=tk.DISABLED)
        self._controller.stop(close_browsers=True)

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
        participant_names = build_participant_names(num_users, manual_names)

        return JoinConfig(
            meeting_url=meeting_url,
            num_users=num_users,
            participant_names=participant_names,
            keep_camera_off=self._keep_camera_off.get(),
            keep_mic_off=self._keep_mic_off.get(),
            auto_join=self._auto_join.get(),
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
