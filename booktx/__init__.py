"""booktx: deterministic translation-preparation CLI for Markdown and EPUB.

booktx does NOT translate text. It extracts translatable sentence records into
JSON chunks, validates chunks that a coding agent has translated, and rebuilds
the final translated document. See :mod:`booktx.cli` for the command surface.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
