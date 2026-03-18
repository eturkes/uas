# Reusable prompt for Claude Code sessions

Paste this prompt at the start of each Claude Code session:

---

Read PLAN.md and find the next section marked `[TODO]`. Implement the fix described
in that section, including the tests specified. Run `python3 -m pytest tests/ -x -q`
to verify no regressions, then mark the section `[DONE]` in PLAN.md. Do not work on
more than one section per session. The `rehab/` directory contains a real project
built by UAS that can be used as reference for understanding the problems described.
Commit your changes when done.
