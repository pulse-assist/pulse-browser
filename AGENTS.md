# Agent notes — browser plugin

- Contract: pulse-plugin SDK, `pulse-plugin.json`, commands in `commands.json` + `@plugin.command`.
- Chromium runs **inside** the plugin Docker image; CDP on 127.0.0.1 inside the container only.
- Persistent profiles under `PULSE_PLUGIN_DATA/profiles/<session>/`.
- Do not put secrets in git; proxy URLs live in plugin data `sessions.json`.
- UI talks via `pulse.plugin(...)` bridge; no alert/confirm in sandbox.
- Line length target: 100.
