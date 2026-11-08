"""HTTP presentation package.

The mounted routers live in their own modules (``api.ua``, ``api.v1`` -
``api.v1.monolithic``, ``api.v1.*_routes``, ``api.mcp.router``). This package
intentionally holds no router of its own; a previous copy of the ``/ua`` routes kept here was
never mounted, served a ``/ua/state`` that ``README.md`` documented but no app exposed, and
duplicated the JSON-safe helpers of ``api/ua.py``. ``GET /ua/status`` is the served endpoint.
"""
