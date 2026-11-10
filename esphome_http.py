"""Minimal HTTP client for the ESPHome web server. Standard library only.

Venus OS ships Python 3.12 and ``dbus-python``; it has no ``requests``, no
``aioesphomeapi`` and no protobuf. The ESPHome web server exposes every entity
as ``GET /<domain>/<entity>`` returning ``{"id": ..., "value": ..., "state": ...}``,
which is enough for a meter and needs nothing but ``http.client``.

The client is deliberately dumb: one request, one timeout, one exception type.
Scheduling, retries and staleness belong to the service.
"""

from __future__ import annotations

import http.client
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from teleinfo import PayloadError, canonical_entity, normalize_entity, parse_payload

# An entity payload is ~60 bytes. Cap the read so a wrong URL that answers with
# something huge cannot eat the GX's memory.
MAX_RESPONSE_BYTES = 4096


class EspHomeError(RuntimeError):
    """One entity could not be read. Not fatal: the caller decides."""


class EspHomeClient:
    """Reads entities from one ESPHome web server."""

    def __init__(self, host: str, port: int = 80, timeout: float = 3.0) -> None:
        if not host:
            raise ValueError("an ESPHome host is required")
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def url_for(self, entity: str) -> str:
        """URL of one entity, quoted the way the ESPHome web server expects.

        Entity names are free text: ``PAPP 1`` carries a space and ``njouf+1`` a
        plus sign, and an unencoded ``+`` is read as a space by the server, so
        everything is escaped, including ``+``.
        """
        domain, object_id = normalize_entity(entity)
        return f"/{domain}/{quote(object_id, safe='')}"

    def fetch(self, entity: str) -> Any:
        """Return the entity's current value, or raise :class:`EspHomeError`."""
        path = self.url_for(entity)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(
                "GET",
                path,
                headers={"Accept": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES)
            if response.status != 200:
                raise EspHomeError(f"{path}: HTTP {response.status}")
        except (OSError, http.client.HTTPException) as exc:
            # socket.timeout, ECONNREFUSED, EHOSTUNREACH and a truncated answer
            # all land here; the message is the only thing the logs will show.
            raise EspHomeError(f"{path}: {type(exc).__name__}: {exc}") from exc
        finally:
            connection.close()
        try:
            return parse_payload(body)
        except PayloadError as exc:
            raise EspHomeError(f"{path}: {exc}") from exc

    def fetch_all(self, entities: Iterable[str]) -> tuple[dict[str, Any], list[str]]:
        """Read every entity, returning ``(values, errors)``.

        One unreachable entity must not hide the others: a teleinfo contract
        without injection has no SINSTI at all, and the meter is still readable.
        """
        values: dict[str, Any] = {}
        errors: list[str] = []
        for entity in entities:
            name = canonical_entity(entity)
            try:
                values[name] = self.fetch(name)
            except (EspHomeError, ValueError) as exc:
                errors.append(str(exc))
        return values, errors

    def probe(self, entities: Iterable[str]) -> Mapping[str, Any]:
        """Read everything once, for ``--probe``: no service, no state, no loop."""
        values, errors = self.fetch_all(entities)
        return {"values": values, "errors": list(errors)}
