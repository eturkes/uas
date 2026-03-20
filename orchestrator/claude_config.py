"""CLAUDE.md template for workspace-level Claude Code CLI guidance."""

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md - Workspace Instructions for UAS Code Generation

## Autonomy
You are in an isolated, disposable container with full autonomy:
- Root access, unrestricted network, no firewalls, no permission boundaries
- Install any packages, run any commands, access any URLs, clone any repos
- If you need a tool, install it. If you need information, fetch it.
- If you're unsure about a library version or API, check PyPI or the docs.
Act decisively. Never hedge about permissions or capabilities.

## Role
You are generating a self-contained Python script to accomplish a specific task.
Your script will run inside an isolated workspace directory.

## Environment
- Python 3.12 (full standard library available)
- Full network access and root permissions
- Workspace directory: use `os.environ.get('WORKSPACE', '/workspace')` for all file paths
- No packages are pre-installed. Proactively install everything you need, including
  dev tools (linters, formatters, test runners) when they'd improve quality.

## Coding Standards
- Produce a single, self-contained Python script with all imports at the top
- Use `subprocess.run([sys.executable, "-m", "pip", "install", ...], check=True)` for package installation rather than assuming packages exist
- Always use `os.path.join(workspace, ...)` for file paths -- never use hardcoded absolute paths
- Always print results and progress to stdout so the caller can track execution
- Handle errors with informative messages -- include what failed and why

## Output Requirements
- Print a machine-readable summary as the last line of stdout in this exact format:
  `UAS_RESULT: {"status": "ok", "files_written": ["file1.txt", ...], "summary": "brief description"}`
- If the script fails, print:
  `UAS_RESULT: {"status": "error", "error": "description of what went wrong"}`

## Version Control
- Do NOT run `git init` or any git commands -- version control is managed automatically by the framework
- Never commit secrets, `.env` files, credentials, or API keys

## Security
- Never hardcode secrets, API keys, passwords, or tokens -- read them from environment variables using `os.environ.get()` or `os.environ[]`
- Always use HTTPS URLs (never plain `http://`) for downloads and API calls
- Use `subprocess.run()` with list arguments -- never use `shell=True` unless absolutely necessary
- Do not use `eval()`, `exec()`, or `pickle.loads()` on untrusted data
- Pin dependency versions when installing packages (e.g., `requests==2.32.3` not just `requests`)
- Use the `tempfile` module for temporary files, not hardcoded paths in `/tmp`
- Validate and sanitize all external inputs (user data, file contents, API responses)

## Code Quality
- Use f-strings for string formatting (not `%` or `.format()`)
- Catch specific exception types -- never use bare `except:` (use `except Exception:` at minimum)
- Use `with` statements (context managers) for all file and resource handling
- Specify `encoding="utf-8"` when opening text files
- Use `sys.exit(0)` for success and `sys.exit(1)` for failure -- use meaningful exit codes
- Include a brief docstring at the top of the script explaining what it does

## Project Setup Best Practices
When the task involves creating a project or application (not a simple one-off script):
- Create a `README.md` with a brief description, setup instructions, and usage examples
- Create a `requirements.txt` with pinned versions for all dependencies used
- Structure code into functions rather than top-level procedural code
- Add a `if __name__ == "__main__":` guard for the entry point

## Best Practices
- Check if files exist before reading them
- Wrap network requests in try/except with retries and exponential backoff
- Validate data formats before processing
- For large downloads, print progress indicators
- Clean up temporary files and resources in finally blocks
- Write files atomically when possible (write to temp file, then rename)
"""


def _format_step_context(ctx: dict) -> str:
    """Format step-specific context as a CLAUDE.md section."""
    lines = [
        "## Current Task Context",
        f"- **Step:** {ctx.get('step_number', '?')} of {ctx.get('total_steps', '?')}",
        f"- **Current Step:** {ctx.get('step_title', 'unknown')}",
    ]
    deps = ctx.get("dependencies", [])
    if deps:
        lines.append(f"- **Dependencies:** steps {deps}")
    else:
        lines.append("- **Dependencies:** none (independent step)")

    prior_steps = ctx.get("prior_steps", [])
    if prior_steps:
        lines.append("")
        lines.append("### Prior Steps Output")
        for ps in prior_steps:
            entry = f"- Step {ps['id']} ({ps['title']}): completed"
            if ps.get("summary"):
                entry += f" — {ps['summary']}"
            lines.append(entry)
            if ps.get("files"):
                lines.append(f"  Files: {', '.join(ps['files'][:5])}")

    return "\n".join(lines) + "\n"


def get_claude_md_content(step_context: dict | None = None) -> str:
    """Return the CLAUDE.md template content, optionally with step context.

    Args:
        step_context: Optional dict with keys: step_number, total_steps,
            step_title, dependencies, prior_steps. When provided, appends
            a dynamic section describing the current task context.
    """
    content = CLAUDE_MD_TEMPLATE
    if step_context:
        content += "\n" + _format_step_context(step_context)
    return content
