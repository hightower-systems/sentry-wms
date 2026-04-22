# Event schemas moved

The v1.5.0 integration event JSON Schemas now live at
[`api/schemas_v1/events/`](../../api/schemas_v1/events/).

They were relocated from this directory in #137 so that the api
container's Docker image (which builds from the `./api/` context)
actually carries them. The schemas are a runtime API contract loaded
at Flask boot by `api/services/events_schema_registry.py`, not
human-facing documentation, so they belong with the code that
consumes them.

Each subdirectory is named after an event type (`receipt.completed`,
`adjustment.applied`, etc.) and contains one JSON Schema file per
version (`1.json`, future `2.json`, ...). See the
[API reference](../api-reference.md) for the event catalog and the
wire format.
