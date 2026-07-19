"""Import boundaries for the public app.tools package."""

from __future__ import annotations

import subprocess
import sys


def test_importing_app_tools_does_not_load_registry_or_rag_stack() -> None:
    code = (
        "import sys; import app.tools; "
        "assert 'app.tools.registry' not in sys.modules; "
        "assert 'app.services.rag_retrieval_service' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", code], check=True)


def test_lazy_registry_export_remains_available() -> None:
    code = (
        "import sys; from app.tools import ToolRegistry; "
        "assert ToolRegistry.__name__ == 'ToolRegistry'; "
        "assert 'app.tools.registry' in sys.modules"
    )

    subprocess.run([sys.executable, "-c", code], check=True)
