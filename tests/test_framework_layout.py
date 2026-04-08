"""Structural invariant: top-level framework modules must be uas_*-prefixed.

This is the structural defense against workspace shadowing of bare-imported
top-level modules. If you add a new top-level .py file at the framework root
that another framework module imports as ``import X`` or ``from X import``,
it MUST be uas_*-prefixed so a same-named user workspace file cannot shadow
it on sys.path.

Replaces the runtime sys.path scrubs that previously lived in
architect/__init__.py and orchestrator/__init__.py: instead of patching
sys.path at every package import, the rule is enforced once at test time
on the file system.
"""

from pathlib import Path

# Personal dev/verification scripts at the framework root that are NOT
# imported by any framework module. Verified by:
#   grep -rn '^(import|from) (check_environment|run_test_verification|...)'
# These are gitignored ad-hoc utilities and may keep their natural names.
_AD_HOC_SCRIPTS = {
    "check_environment.py",
    "run_test_verification.py",
    "test_goal_result.py",
    "test_goal_verify.py",
    "verify_test_goal.py",
}


def test_framework_root_only_has_uas_prefixed_importables():
    framework_root = Path(__file__).resolve().parent.parent
    top_level = {
        p.name for p in framework_root.iterdir()
        if p.is_file() and p.suffix == ".py"
    }
    importable = top_level - _AD_HOC_SCRIPTS
    bad = sorted(name for name in importable if not name.startswith("uas_"))
    assert not bad, (
        f"Top-level framework module(s) without uas_ prefix: {bad}. "
        f"A user workspace file of the same name would shadow them on "
        f"sys.path. Fix by either renaming to uas_<name>.py, moving into "
        f"a package (architect/, orchestrator/, uas/), or adding to "
        f"_AD_HOC_SCRIPTS in this test if it is a standalone script not "
        f"imported by framework code."
    )
