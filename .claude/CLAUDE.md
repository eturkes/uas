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
- `uv` is pre-installed for fast package management
- Full network access and root permissions
- Workspace directory: use `os.environ.get('WORKSPACE', '/workspace')` for all file paths
- No Python packages are pre-installed beyond the standard library. Proactively
  install everything you need, including dev tools (linters, formatters, test
  runners) when they'd improve quality.

## Package Management
- Use `uv` for all package installation -- it is dramatically faster than pip:
  `subprocess.run(["uv", "pip", "install", "--system", ...], check=True)`
- Pin dependency versions (e.g., `requests==2.32.3` not just `requests`)
- If `uv` is not available, fall back to:
  `subprocess.run([sys.executable, "-m", "pip", "install", ...], check=True)`

## Coding Standards
- Produce a single, self-contained Python script with all imports at the top
- Always use `os.path.join(workspace, ...)` for file paths -- never use hardcoded absolute paths
- Always print results and progress to stdout so the caller can track execution
- Handle errors with informative messages -- include what failed and why

## Import Conventions
When generating modules that are part of a multi-file project:
- Use consistent package-relative imports throughout the project
  (e.g., always `from src.module import name`, never bare `from module import name`)
- NEVER use try/except ImportError fallback chains to handle different import paths
  -- this masks real errors. Pick one correct import path and use it.
- When importing from a sibling module produced by a prior step, use the EXACT
  names listed in the dependency context. Do not rename, alias, or guess.
- If the dependency context lists `functions: make_card`, import `make_card`,
  not `create_card` or any other variation.

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
- Use the `tempfile` module for temporary files, not hardcoded paths in `/tmp`
- Validate and sanitize all external inputs (user data, file contents, API responses)

## Code Quality
- Use f-strings for string formatting (not `%` or `.format()`)
- Catch specific exception types -- never use bare `except:` (use `except Exception:` at minimum)
- Use `with` statements (context managers) for all file and resource handling
- Specify `encoding="utf-8"` when opening text files
- Use `sys.exit(0)` for success and `sys.exit(1)` for failure -- use meaningful exit codes
- Include a brief docstring at the top of the script explaining what it does

## Tooling Philosophy
Always use the latest, best-in-class tools available today -- not the legacy
defaults you may have memorized. The ecosystem evolves fast; what was standard
two years ago is often obsolete. Before reaching for a tool or library:
1. Ask yourself: is there a faster, more modern, more actively maintained
   alternative? If you're not sure, check the network.
2. Prefer tools the community has converged on as successors to older ones.
3. When a newer tool is a strict superset or drop-in replacement for an older
   one, always use the newer tool.

As of your last training data, strong defaults include `uv` over pip,
`pyproject.toml` over setup.py/requirements.txt, `ruff` over
flake8/pylint/black/isort, and `pytest` over unittest -- but these are
examples, not an exhaustive list. Apply this principle to every technology
choice: frameworks, database drivers, HTTP clients, data libraries, etc.

## Project Setup Best Practices
When the task involves creating a project or application (not a simple one-off script):
- Create a `README.md` with a brief description, setup instructions, and usage examples
- Create a `pyproject.toml` with project metadata and pinned dependencies
- Structure code into functions rather than top-level procedural code
- Add a `if __name__ == "__main__":` guard for the entry point

## Best Practices
- Check if files exist before reading them
- Wrap network requests in try/except with retries and exponential backoff
- Validate data formats before processing
- For large downloads, print progress indicators
- Clean up temporary files and resources in finally blocks
- Write files atomically when possible (write to temp file, then rename)
