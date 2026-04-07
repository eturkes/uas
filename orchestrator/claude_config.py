"""CLAUDE.md template for workspace-level Claude Code CLI guidance."""

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md - Workspace Instructions for UAS Code Generation

## Autonomy
You are in an isolated, disposable container with research tools enabled:
- Root access, unrestricted network, no firewalls
- Read-only and research tools available — Read, Grep, Glob, WebSearch,
  WebFetch, and Bash for quick verification commands
- File-modification tools (Write, Edit, NotebookEdit) are DISABLED for this
  generation step. You cannot create or modify files via tools.
- If you're unsure about a library version or API, use WebFetch / WebSearch
  to check the docs or registry, or run a Bash command to inspect the
  environment.
Act decisively. Research with the tools you have. Never hedge about
permissions for the tools that ARE available.

## Role
You are generating a self-contained Python script as TEXT output.
Use the available research tools (Read, Grep, Glob, WebSearch, WebFetch,
Bash) to verify package versions, API signatures, and environment details
before coding.
The Write, Edit, and NotebookEdit tools are DISABLED — you cannot create
files via tools. Output the complete script in a single fenced code block
in your response. The framework will extract and execute the script later
inside an isolated workspace directory.

## Output Mode — TEXT ONLY
Do NOT attempt to create any files or directories. The Write, Edit, and
NotebookEdit tools are DISABLED in this session and any attempt to use
them will fail. Your only job is to produce the Python script text in
your response inside a single ```python fenced code block. The framework
will extract and execute the script.

## Environment
- Python 3.12 (full standard library available)
- `uv` is pre-installed for fast package management
- Full network access and root permissions
- The WORKSPACE environment variable points to the project root.
- For the fallback, use the script's own directory:
  `os.environ.get("WORKSPACE", os.path.dirname(os.path.abspath(__file__)))`
- NEVER hardcode `/workspace` or `/workspace/workspace` as a fallback.
- When a module is inside a subdirectory (e.g., dashboard/), the fallback
  should be the PARENT directory:
  `os.environ.get("WORKSPACE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`
- No Python packages are pre-installed beyond the standard library. Proactively
  install everything you need, including dev tools (linters, formatters, test
  runners) when they'd improve quality.

## Critical: Workspace IS the project root
The workspace directory IS the project root. Do NOT create a subdirectory
named after the project. If the project is called "myapp", do NOT create
"myapp/" inside the workspace. Write files directly to the workspace:
- CORRECT: os.path.join(workspace, "src", "myapp", "main.py")
- WRONG:   os.path.join(workspace, "myapp", "src", "myapp", "main.py")

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
defaults you may have memorized. Every ecosystem evolves fast; what was standard
two years ago is often obsolete. Before reaching for any tool, library, or
framework -- in ANY language or ecosystem:
1. Ask yourself: is there a faster, more modern, more actively maintained
   alternative? If you're not sure, check the network.
2. Prefer tools the community has converged on as successors to older ones.
3. When a newer tool is a strict superset or drop-in replacement for an older
   one, always use the newer tool.

This applies to everything: package managers, build tools, linters, formatters,
test frameworks, HTTP clients, ORMs, bundlers, task runners, etc. -- regardless
of language. Research what the current best practice is for the specific
ecosystem the goal targets. Do not default to what you memorized during
training if the ecosystem may have moved on.

## Project Setup Best Practices
When the task involves creating a project or application (not a simple one-off script):
- Create a `README.md` with a brief description, setup instructions, and usage examples
- Include a dependency manifest appropriate for the language/ecosystem, with pinned versions
- Structure code into well-organized modules rather than flat procedural code
- Follow the target ecosystem's current conventions for project layout

## Best Practices
- Check if files exist before reading them
- Wrap network requests in try/except with retries and exponential backoff
- Validate data formats before processing
- For large downloads, print progress indicators
- Clean up temporary files and resources in finally blocks
- Write files atomically when possible (write to temp file, then rename)
"""


def _collect_module_apis(prior_steps: list) -> list[tuple[str, dict]]:
    """Collect module API info from prior steps that produced .py files.

    Returns a list of (filepath, api_dict) tuples where api_dict has keys
    'functions', 'classes', 'constants', 'variables'.
    """
    result = []
    for ps in prior_steps:
        module_apis = ps.get("module_apis", {})
        for filepath, api in module_apis.items():
            if api.get("functions") or api.get("classes") or api.get("constants") or api.get("variables"):
                result.append((filepath, api))
    return result


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

    workspace_name = ctx.get("workspace_name", "")
    if workspace_name:
        lines.append(
            f"- **Workspace Name:** `{workspace_name}` — do NOT create a "
            f"subdirectory called `{workspace_name}/` inside the workspace"
        )

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

    # Include module API information for .py files from prior steps
    module_apis = _collect_module_apis(prior_steps)
    if module_apis:
        lines.append("")
        lines.append("### Available Module APIs")
        for filepath, api in module_apis:
            parts = []
            if api.get("functions"):
                parts.append(f"functions=[{', '.join(api['functions'])}]")
            if api.get("classes"):
                parts.append(f"classes=[{', '.join(api['classes'])}]")
            if api.get("constants"):
                parts.append(f"constants=[{', '.join(api['constants'])}]")
            if api.get("variables"):
                parts.append(f"variables=[{', '.join(api['variables'])}]")
            if parts:
                lines.append(f"- `{filepath}`: {', '.join(parts)}")
        lines.append("")
        lines.append(
            "IMPORTANT: Before writing a new helper function, check if an equivalent\n"
            "already exists in the modules listed above. Import and reuse existing\n"
            "functions rather than reimplementing them. In particular:\n"
            "- Translation helpers: use the one from dashboard/translations.py\n"
            "- Data loading: use the one from data_loader.py\n"
            "- Feature engineering: use functions from feature_engineering.py"
        )

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
