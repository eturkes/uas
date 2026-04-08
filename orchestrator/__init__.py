"""UAS Orchestrator package.

See ``architect/__init__.py`` and PLAN.md Section 6 for the rationale
behind the sys.path scrub below. The scrub removes ``sys.path[0]`` when
it represents the cwd (either ``""`` from ``-c``/REPL mode or the
absolute cwd path from ``-m`` mode) and is not the framework root,
preventing workspace-local files from shadowing same-named framework
modules.
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
