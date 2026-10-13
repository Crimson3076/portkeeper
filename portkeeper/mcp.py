"""Model Context Protocol endpoint (streamable HTTP, JSON responses) for outside agents.

Off until the person creates a token in Settings. Only the hash of the token is stored.
Agents get the same tools as the built-in assistant; see modeltools.
"""
import hashlib
import hmac
import json
import secrets

from .modeltools import TOOLS
from .safety import Invalid
from .storage import now

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Portkeeper packages open-source game ports into portable downloads. Use these tools to draft a packaging "
                "definition for a game: read packaging_format first, then read_game, read_material and read_source; "
                "validate_definition, then test_build_draft and get_test_build until the test passes or only steps needing "
                "the person's game files are unverified; then submit_draft with an honest explanation. The person reviews "
                "and approves drafts in Portkeeper. You cannot approve, publish or access their game files.")


class MCP:
    def __init__(self, engine):
        self.engine = engine

    def config(self):
        return self.engine.get_setting("mcp", {})

    def public_config(self):
        config = self.config()
        return {"enabled": bool(config.get("token_sha256")), "created_at": config.get("created_at"), "last_used_at": config.get("last_used_at")}

    def configure(self, payload):
        if payload.get("enabled") is True:
            token = "pk_" + secrets.token_urlsafe(32)
            self.engine.save_setting("mcp", {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "created_at": now()})
            return {**self.public_config(), "token": token}
        if payload.get("enabled") is False:
            self.engine.save_setting("mcp", {})
            return self.public_config()
        raise Invalid("Set enabled to true to create a new token, or false to turn the connection off")

    def authorized(self, header):
        expected = self.config().get("token_sha256")
        if not expected or not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        if not hmac.compare_digest(hashlib.sha256(header[7:].strip().encode()).hexdigest(), expected):
            return False
        config = self.config()
        if (config.get("last_used_at") or "")[:16] != now()[:16]:
            self.engine.save_setting("mcp", {**config, "last_used_at": now()})
        return True

    def handle(self, message):
        """One JSON-RPC message. Returns a response object, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
        if "id" not in message:
            return None
        ident, method, params = message["id"], message["method"], message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            return _result(ident, {"protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                                   "capabilities": {"tools": {"listChanged": False}},
                                   "serverInfo": {"name": "portkeeper", "title": "Portkeeper", "version": "1"},
                                   "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": TOOLS})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            try:
                if name == "test_build_draft":
                    arguments = {**arguments, "source": "connected agent"}
                value = self.engine.model_tools.call(name, arguments)
            except (Invalid, KeyError, TypeError, ValueError) as error:
                text = str(error).strip("'")
                if isinstance(error, TypeError):
                    text = f"Invalid arguments for {name}: {error}"
                return _result(ident, {"content": [{"type": "text", "text": text}], "isError": True})
            text = json.dumps(value, indent=1)
            return _result(ident, {"content": [{"type": "text", "text": text}], "structuredContent": value, "isError": False})
        return _error(ident, -32601, f"Method not found: {method}")


def _result(ident, value):
    return {"jsonrpc": "2.0", "id": ident, "result": value}


def _error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
