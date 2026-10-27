# Hand-off: herdr web panes (for a Claude Code session in the deck project)

Written 2026-10-27 by the Team session. Team (this repo) will show the Hermes
dashboard, the Portkey console and later its own pages inside herdr, next to
terminals. herdr needs one feature for that: a pane that renders a web page.
The deck session owns that work; Team only declares the panes in `.work.json`.

What Team will point at, so the feature is designed against real URLs:
- `http://127.0.0.1:9119/team` (Hermes dashboard, Team tab) and `/kanban`
- `http://127.0.0.1:8787/public/` (Portkey gateway console, token login)
- later, pages served by Team itself on loopback

Team's layout entry shape, already in its `.work.json`:
`{"label": "dashboard", "cwd": "", "web": "http://127.0.0.1:9119/team"}`
(today it is `"command": "open <url>"` until the pane type exists).

The brief Eli pastes into the deck session is self-contained, because that
session cannot read this repo.
