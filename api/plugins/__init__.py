"""Plugin discovery for downstream forks (see docs/downstream-overlay.md).

Modules placed in this package, or in a directory pointed to by the
``XIREACTOR_API_PLUGIN_DIR`` env var, are imported at app startup. Each
plugin module must expose a ``register(app)`` callable that mounts whatever
it needs onto the FastAPI app — typically ``app.include_router(...)``.

Loading is fail-soft: an exception in one plugin is logged and does not
prevent the app from starting or other plugins from loading. Modules whose
name begins with ``_`` are skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("brilliant.plugins")

_ENV_VAR = "XIREACTOR_API_PLUGIN_DIR"


def load_plugins(app: "FastAPI") -> list[str]:
    """Discover and register API plugins. Returns names of plugins that loaded."""
    loaded: list[str] = []

    pkg_dir = Path(__file__).resolve().parent
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod_info.name}")
        except Exception:
            logger.exception("Failed to import plugin %s", mod_info.name)
            continue
        if _call_register(module, app, source=f"{__name__}.{mod_info.name}"):
            loaded.append(mod_info.name)

    ext_dir = os.environ.get(_ENV_VAR)
    if ext_dir:
        ext_path = Path(ext_dir).resolve()
        if not ext_path.is_dir():
            logger.warning(
                "%s=%s is not a directory; skipping external plugin load",
                _ENV_VAR,
                ext_dir,
            )
        else:
            if str(ext_path) not in sys.path:
                sys.path.insert(0, str(ext_path))
            for py in sorted(ext_path.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                spec = importlib.util.spec_from_file_location(
                    f"_xireactor_api_plugin_{py.stem}", str(py)
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    logger.exception("Failed to import plugin %s", py)
                    continue
                if _call_register(module, app, source=str(py)):
                    loaded.append(py.stem)

    if loaded:
        logger.info("Loaded %d API plugin(s): %s", len(loaded), ", ".join(loaded))
    return loaded


def _call_register(module: Any, app: "FastAPI", *, source: str) -> bool:
    register = getattr(module, "register", None)
    if not callable(register):
        return False
    try:
        register(app)
    except Exception:
        logger.exception("Plugin %s register(app) raised", source)
        return False
    return True
