"""Vercel serverless entry point.

Sets up imports so 'from api.X' works when deployed from api/ directory.
"""

import os
import sys
from types import ModuleType

api_dir = os.path.dirname(os.path.abspath(__file__))

api_module = ModuleType("api")
api_module.__path__ = [api_dir]
api_module.__file__ = os.path.join(api_dir, "__init__.py")
sys.modules["api"] = api_module

if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

from main import app  # noqa: E402, F401
