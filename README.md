# Multi User Google Meet Joiner

Desktop automation tool to simulate multiple Google Meet participants from one machine using separate Chrome incognito sessions.

## Features (v1.0)

- Configure a Google Meet URL
- Launch multiple participants from one computer
- Use one incognito Chrome session per participant
- Enter custom participant names
- Enforce one name per participant (line-by-line validation)
- Keep camera off (shortcut automation)
- Keep mic off (shortcut automation)
- Auto click `Ask to join` / `Join now`
- Stop process and close all active browser sessions
- Live status log in the desktop UI

## Tech Stack

- Python 3
- Tkinter (desktop UI)
- Selenium
- WebDriver Manager
- Google Chrome + ChromeDriver

## Project Structure

```text
.
├── .github/workflows/build-desktop.yml
├── main.py
├── requirements-packaging.txt
├── requirements.txt
├── scripts/build_macos.sh
├── scripts/build_windows.ps1
├── .gitignore
└── README.md
```

## Prerequisites

- macOS / Windows / Linux
- Python 3.10+
- Google Chrome installed
- Stable internet connection

## Setup

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

macOS note (Homebrew Python 3.14): if you see `No module named '_tkinter'`, install Tk support first:

```bash
brew install python-tk@3.14
```

Then recreate the virtual environment.

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Run the app.

```bash
python main.py
```

## Build Desktop Apps (macOS and Windows)

This project uses PyInstaller to create distributable desktop builds.

### Local build on macOS

```bash
chmod +x scripts/build_macos.sh
./scripts/build_macos.sh
```

Expected output:

- `dist/MultiUserGoogleMeetJoiner.app`

### Local build on Windows

Run in PowerShell:

```powershell
./scripts/build_windows.ps1
```

Expected output:

- `dist\MultiUserGoogleMeetJoiner\MultiUserGoogleMeetJoiner.exe`

### Automated builds (GitHub Actions)

Workflow file:

- `.github/workflows/build-desktop.yml`

How to trigger:

1. Push a tag like `v1.0.0`, or
2. Run `Build Desktop Apps` manually from the Actions tab.

Artifacts produced:

- `MultiUserGoogleMeetJoiner-macos.zip`
- `MultiUserGoogleMeetJoiner-windows.zip`

## Usage

1. Enter a valid Meet link, for example:
	 `https://meet.google.com/abc-defg-hij`
2. Enter `Number of Users`
3. Add names (one per line) in `Names List`
   - Validation rule: number of non-empty name lines must exactly equal `Number of Users`
4. Choose options:
	 - `Launch Mode`:
	   - `Different incognito windows`
	   - `Same window (incognito tabs)`
	   - `After Join (Same Window)`:
	     - `No change`
	     - `Expanded window`
	     - `Full screen`
	 - `Keep Camera OFF`
	 - `Keep Mic OFF`
	 - `Auto Join`
	 - `Mute Participant Sound After Join`
	 - `Execution Speed`:
	   - `Fast` (default)
	   - `Balanced`
	   - `Reliable`
	 - `Pre-Join Popup Action`:
	   - `Continue without microphone and camera`
	   - `Use microphone and camera`
	   - `Manual (do not handle popup)`
5. Click `Start Joining`
6. Use `Stop` any time to cancel and close all open sessions

## Functional Behavior

- Each participant launches in a Chrome incognito session.
- `Names List` must contain exactly one line per participant (`Number of Users` must match line count).
- Participants can launch in separate incognito windows or as tabs in one shared incognito window.
- For same-window mode, you can choose post-join display mode (`No change`, `Expanded window`, `Full screen`).
- Retry logic is used when key Meet elements are not found.
- Join timing and retry behavior are configurable via `Execution Speed` profile.
- `Fast` mode minimizes waits and retries for speed; use `Balanced` or `Reliable` for unstable UI/network.
- Pre-join media popup handling is configurable from the UI before launch.
- In `Continue without microphone and camera` mode, the app skips extra device-toggle verification to reduce join delay.
- Participant speaker output can be muted so admitted users stay silent.

## Important Notes and Risks

- Google Meet UI and selectors can change, which may break automation.
- Google may detect or limit automated behavior.
- High participant counts consume significant CPU and RAM.
- Run with lower counts first (for example 3-5 users) before scaling.

## Troubleshooting

- If Chrome does not launch:
	- Verify Google Chrome is installed and updated.
	- Reinstall dependencies in the virtual environment.
- If join button is not clicked:
	- Meet UI language/layout may differ.
	- Retry after refreshing Chrome and app.
- If camera/mic toggle does not apply:
	- Shortcut handling may vary by OS layout.
	- Toggle manually in pre-join screen if needed.

## Future Enhancements (v2 ideas)

- Random name generator
- Delay control per participant
- Join status dashboard
- Auto leave after timer
- Proxy support
- Headless mode

## Disclaimer

Use responsibly and in compliance with Google Meet terms and your local policies. This project is intended for test and simulation scenarios.
