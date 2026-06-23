"""Version reporting contract.

``booktx.__version__`` must be sourced from the generated
``booktx/_version.py`` when present, and fall back to ``"0+unknown"``
otherwise. It must never report the stale hardcoded ``"0.1.0"`` once a
generated version file exists.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_version_sourced_from_generated_file() -> None:
    import booktx

    assert booktx.__version__
    assert booktx.__version__ != "0+unknown"


def test_version_falls_back_when_version_file_missing() -> None:
    # Run in an isolated interpreter so global module state is untouched.
    # Block the generated submodule import, then import booktx fresh.
    script = textwrap.dedent(
        """
        import builtins, sys
        _real = builtins.__import__
        def _imp(name, *args, **kwargs):
            if name == "booktx._version":
                raise ImportError("simulated missing _version.py")
            return _real(name, *args, **kwargs)
        builtins.__import__ = _imp
        sys.modules.pop("booktx", None)
        sys.modules.pop("booktx._version", None)
        import booktx
        print(booktx.__version__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0+unknown"
