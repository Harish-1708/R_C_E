"""
Makes the repo root importable from tests/, so `import parse_refunnel`
etc. works from test files without needing a package/src layout. Also
adds scripts/ so `import determine_workspaces` works the same way.
"""
import os
import sys

_ROOT = os.path.dirname(__file__)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
