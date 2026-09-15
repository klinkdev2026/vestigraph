"""Read-only, budgeted preview of a saved layout version (Preview JSON v1).

The parser (klayout.db) runs in a separate, killable process; the service
process never imports it for rendering. Output is a flat, viewport-bounded
list of boxes/paths/polygons in database units, not an object model.
"""
RENDERER_VERSION = 1
