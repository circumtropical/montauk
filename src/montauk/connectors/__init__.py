"""Inbound source connectors (spec 10, 11).

Connectors are **inbound only**. Nothing in this package -- or reachable
from it -- may send, reply, react, mark-read, delete, or edit a remote
message. :func:`montauk.connectors.base.assert_inbound_only` and
``tests/phase2/test_connector_readonly.py`` enforce that.
"""
