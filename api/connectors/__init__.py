"""Connector registry for ERP/commerce integrations.

The ConnectorRegistry is the central catalog of available connectors.
It supports both explicit registration and auto-discovery of connector
modules in the connectors directory.

Usage::

    from connectors import registry

    # List all registered connectors
    registry.list_all()

    # Get a specific connector class
    cls = registry.get("netsuite")
    connector = cls(config={"api_key": "...", "base_url": "..."})
    result = connector.test_connection()
"""

import importlib
import os
import pkgutil

from connectors.base import BaseConnector


class ConnectorRegistry:
    """Stores and retrieves connector classes by name.

    Connectors register themselves by calling registry.register() or
    by being discovered via discover(). The registry validates that
    every registered class is a concrete subclass of BaseConnector
    with all abstract methods implemented.
    """

    def __init__(self):
        self._connectors: dict[str, type[BaseConnector]] = {}

    def register(self, name: str, cls: type[BaseConnector]) -> None:
        """Register a connector class under the given name.

        Args:
            name: Short identifier for this connector (e.g. "netsuite").
            cls: The connector class. Must be a subclass of BaseConnector
                 with all abstract methods implemented.

        Raises:
            TypeError: If cls is not a subclass of BaseConnector or has
                       unimplemented abstract methods.
            ValueError: If a connector is already registered under the
                        same name. V-010: silently overwriting was a
                        supply-chain foothold (a second import wins).
        """
        if not isinstance(cls, type) or not issubclass(cls, BaseConnector):
            raise TypeError(f"{cls} is not a subclass of BaseConnector")

        # Check for unimplemented abstract methods. ABC does not raise
        # until instantiation, so we check explicitly here to fail fast.
        abstract = getattr(cls, "__abstractmethods__", frozenset())
        if abstract:
            missing = ", ".join(sorted(abstract))
            raise TypeError(
                f"Cannot register {cls.__name__}: missing required methods: {missing}"
            )

        if name in self._connectors:
            existing = self._connectors[name].__name__
            raise ValueError(
                f"Connector name {name!r} is already registered to {existing}; "
                f"cannot re-register to {cls.__name__}"
            )

        self._connectors[name] = cls

    def get(self, name: str) -> type[BaseConnector]:
        """Retrieve a registered connector class by name.

        Args:
            name: The connector identifier.

        Returns:
            The connector class.

        Raises:
            KeyError: If no connector is registered under that name.
        """
        if name not in self._connectors:
            raise KeyError(f"No connector registered with name '{name}'")
        return self._connectors[name]

    def list_all(self) -> dict[str, type[BaseConnector]]:
        """Return a copy of all registered connectors.

        Returns:
            Dict mapping connector names to their classes.
        """
        return dict(self._connectors)

    def discover(self) -> None:
        """Auto-discover and import connector modules in this package.

        Scans the connectors directory for Python files (excluding
        __init__.py and base.py) and imports them. Connector modules
        that want to auto-register should call registry.register()
        at module level.

        Modules that fail to import are silently skipped -- a broken
        connector should not prevent the rest of the app from starting.
        """
        package_dir = os.path.dirname(__file__)
        skip = {"__init__", "base", "example"}

        for importer, module_name, is_pkg in pkgutil.iter_modules([package_dir]):
            if module_name in skip:
                continue
            try:
                importlib.import_module(f"connectors.{module_name}")
            except Exception:
                # Don't let a broken connector take down the app
                pass


# Module-level singleton -- importable as `from connectors import registry`
registry = ConnectorRegistry()
