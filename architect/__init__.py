"""UAS Architect package.

The block below scrubs the unsafe leading entry that Python prepends to
``sys.path`` when this package is imported via ``python -m architect.X``
from an arbitrary working directory. Python 3.11+ prepends the cwd in
two different forms depending on invocation mode:

- ``python -c "..."`` and bare REPL prepend ``""`` (empty string).
- ``python -m module.path`` prepends the cwd as an absolute path.
- ``python script.py`` prepends the script's directory.

Any of these unsafe forms causes a workspace-local file (e.g.
``config.py``, ``state.py``, ``hooks.py``) to shadow a same-named
framework module on import. The scrub removes ``sys.path[0]`` whenever
it represents the cwd (in either form) AND is not the framework root
itself, which prevents the shadowing class of bug structurally. See
PLAN.md Section 6 for the full rationale.
"""
import os as _os
import sys as _sys

_FRAMEWORK_ROOT = _os.path.dirname(
    _os.path.dirname(_os.path.abspath(__file__))
)

if _sys.path:
    _first = _sys.path[0]
    if _first == "":
        _sys.path.pop(0)
    else:
        try:
            _first_abs = _os.path.abspath(_first)
            _cwd_abs = _os.path.abspath(_os.getcwd())
        except OSError:
            _first_abs = None
            _cwd_abs = None
        if (
            _first_abs is not None
            and _first_abs == _cwd_abs
            and _first_abs != _FRAMEWORK_ROOT
        ):
            _sys.path.pop(0)
