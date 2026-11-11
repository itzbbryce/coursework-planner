# Coursework Planner

A local, single-user coursework planner built with Python and vanilla JavaScript. Track courses, assignments and deadlines, or import them from Canvas.

## Features

- Courses, assignments, completion tracking and notes.
- Canvas API integration for assignments, grades and announcements.
- Calendar-feed import for deadlines without an API token.
- Automatic sync while running, plus manual refresh.
- Backup/import, sample workspace, light/dark themes and timezone settings.

## Run

Requires Python 3.10 or later. No third-party Python packages are required.

```sh
cd coursework-planner
python3 server.py --open
```

On macOS, you can also run `zsh "Start Coursework.command"`.
The app listens on localhost at port 8765. Keep the terminal open while using it; press Ctrl-C to stop.

Open Settings to change your name/timezone and connect Canvas. The UI currently defaults to University of Delaware Canvas; the school address can be edited when connecting. Calendar feeds omit grades, announcements and undated assignments. Recurring calendar events are skipped.

## Data and privacy

This is a local personal tool, not a hosted multi-user service. Canvas access is read-only. Connection credentials and planner records are saved under `data/`; do not commit or share that folder. Tokens are stored in local files with restricted permissions, not encrypted. Backups omit connection credentials but still contain personal coursework information.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The 14 tests cover parsing, mocked Canvas responses, validation, persistence, sync preservation and credential separation. Live Canvas integration and the browser UI are not covered by these tests.

## Structure

- `server.py`: localhost HTTP server and planner state.
- `canvas.py`: Canvas API and calendar-feed imports.
- `static/`: HTML, CSS and JavaScript interface.
- `tests/`: automated unit tests.
