Use `uv run`to run and not `python`.

## Publishing Process

Releases are automated by `.github/workflows/release.yml`. Pushing a tag named
`bounded_subprocess-v<version>` publishes to PyPI, but only after the workflow
confirms that the tagged commit is on `main`, that the tag version matches
`version` in pyproject.toml, and that the test suite passes.

To cut a release:

1. Increment `version` in pyproject.toml.
2. Run `uv sync` to update the lock file.
3. Commit the changes, which should have just changes to pyproject.toml and
   uv.lock, and get them onto `main`.
4. Tag that commit `bounded_subprocess-v<version>` (matching pyproject.toml
   exactly) and push the tag:

   ```
   git tag bounded_subprocess-v2.9.2
   git push origin bounded_subprocess-v2.9.2
   ```

The workflow authenticates to PyPI with trusted publishing (OIDC), so there is
no API token to manage. If a release fails one of the checks, delete the tag,
fix the problem, and tag again.
