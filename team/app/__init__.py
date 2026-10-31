"""Team's own app: talk to the boss on one side, watch the work on the other.

``team app`` serves it on 127.0.0.1. ``server`` is the HTTP surface, ``chat``
runs the boss one message at a time, ``edit`` reviews and applies changes to
Team's role files and settings; LiteLLM and Portkey changes go through the
gateway dashboard's own gated functions (integrations/dashboard/server.py).
"""
