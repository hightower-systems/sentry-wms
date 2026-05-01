"""Entry point for ``python -m services.webhook_dispatcher``.

Delegates to ``main()`` in the package ``__init__``. Kept as a
separate module so the package's ``__init__`` import side effects
(logging config) only fire when the daemon is actually launched,
not when a test or sibling module imports a helper.
"""

from . import main


if __name__ == "__main__":
    main()
