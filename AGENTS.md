Use `uv run`to run and not `python`.

## Publishing Process

Increment the version number as appropriate in pyproject.toml. Delete old
builds from dist/. Run uv sync to update the lock file. Commit the changes,
which should have just changes to pyproject.taml and uv.lock. Run `uv build`.

At this point, stop and tell me to run `uv publish`. Do not try to automate
this step. Remind me that I must enter __token__ for the username. It says that
on the screen, but I don't read instructions. The password is in the keychain.
