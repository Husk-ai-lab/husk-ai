# Packaging the Husk Studio binary

```bash
uv run python packaging/build.py            # -> dist/husk-studio/husk-studio
uv run python -m pytest packaging/tests -q  # boots the built binary and exercises it
```

`--onefile` builds a single executable instead of a folder. Binaries are platform-native:
PyInstaller does not cross-compile, so build on each OS you ship to. Needs Node 20+ the
first time (to build the Studio UI that gets baked in); the resulting binary needs nothing.
