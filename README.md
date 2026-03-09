# Multi User Google Meet Joiner

Desktop automation tool to simulate multiple Google Meet participants from one machine using separate Chrome incognito sessions.

## Features (v1.0)

- Configure a Google Meet URL
- Launch multiple participants from one computer
- Use one incognito Chrome session per participant
- Enter custom participant names
- Auto-generate missing names (`User_#`)
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
├── main.py
├── requirements.txt
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

## Usage

1. Enter a valid Meet link, for example:
	 `https://meet.google.com/abc-defg-hij`
2. Enter `Number of Users`
3. Add names (one per line) in `Names List`
4. Choose options:
	 - `Keep Camera OFF`
	 - `Keep Mic OFF`
	 - `Auto Join`
5. Click `Start Joining`
6. Use `Stop` any time to cancel and close all open sessions

## Functional Behavior

- If names are fewer than user count, additional names are auto-generated.
- Name duplicates are automatically made unique (`Name`, `Name_2`, etc.).
- Each participant launches in a separate Chrome incognito session.
- Retry logic is used when key Meet elements are not found.

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
