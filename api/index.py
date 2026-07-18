"""Vercel entrypoint: the whole app (UI + API + cron endpoints) as one ASGI function.

Vercel's Python runtime serves the module-level `app`. Scheduling comes from Vercel
Cron (vercel.json) hitting /api/internal/*, not the in-process scheduler."""

from oblag.web.app import create_app

app = create_app()
