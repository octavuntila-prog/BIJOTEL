"""BIJOTEL API route modules.

Each submodule exposes an ``APIRouter`` named ``router`` which is mounted
by :func:`bijotel.api.app.create_app`. Keep route declarations dumb (no
heavy logic) — anything beyond shape transformation belongs in
``bijotel.processors`` / ``bijotel.policy`` / ``bijotel.layers``.
"""
