"""CLAUDE.md template for workspace-level Claude Code CLI guidance."""

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md - Workspace Instructions for UAS Code Generation

## Role
You are generating a self-contained Python script to accomplish a specific task.
Your script will run inside an isolated workspace directory.

## Environment
- Python 3.12 (full standard library available)
- Full network access and root permissions
- Workspace directory: use `os.environ.get('WORKSPACE', '/workspace')` for all file paths
- Packages are NOT pre-installed beyond the standard library -- install what you need

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

## Best Practices
- Check if files exist before reading them
- Wrap network requests in try/except with retries
- Validate data formats before processing
- For large downloads, print progress indicators
"""


def get_claude_md_content() -> str:
    """Return the CLAUDE.md template content."""
    return CLAUDE_MD_TEMPLATE
