"""Small helpers for API-key environment variables.

On Windows, ``setx`` writes user variables to HKCU\\Environment, but already
running shells (and programs launched by them) can retain an older environment
block until they are restarted.  The scanner normally uses ``os.getenv``.  If
that is empty on Windows, we also read the user's persisted Environment value
from the registry.  Secret values are never logged or returned by health APIs.
"""
from __future__ import annotations

import os
from typing import Optional


def _windows_persisted_env(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore
        locations = (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        )
        for hive, path in locations:
            try:
                with winreg.OpenKey(hive, path) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                value = str(value or "").strip()
                if value:
                    return value
            except OSError:
                continue
    except (ImportError, OSError):
        pass
    return ""


def env_secret(name: Optional[str], default_name: str) -> str:
    """Return a secret by environment-variable *name*, never by config value.

    The configured name is stripped so an accidental trailing space in JSON
    cannot silently turn a present key into "NO KEY".
    """
    env_name = str(name or default_name).strip() or default_name
    value = str(os.getenv(env_name, "") or "").strip()
    if value:
        return value
    return _windows_persisted_env(env_name)
