# Releasing

Versions follow `MAJOR.MINOR.PATCH` (for example `0.2.0`). Test versions add `rc1`, `rc2` and so on
(`0.2.0rc1`) and are published as pre-releases. Between releases the code carries a `.dev0` version.

The version is written in one place only: `__version__` in `src/photorecover/__init__.py`. The Python package,
`photorecover --version`, the report and the Windows "Properties > Details" text all read it from there
(`packaging/make_version_info.py` writes the Windows text during the build).

## Steps

1. Make sure `main` is green (tests and windows build) and the newest `dev-build` has been run on the real
   test cards with no unexpected changes (see the automated card tests).
2. On a branch, set `__version__` to the new version, for example `"0.2.0"`.
3. In `CHANGELOG.md`, rename `## Unreleased` to `## 0.2.0` and check it reads well for non-technical people.
   Add a fresh empty `## Unreleased` above it.
4. Merge the pull request.
5. Tag the merge commit and push the tag:

   ```
   git tag v0.2.0
   git push origin v0.2.0
   ```

6. The windows build workflow checks the tag matches `__version__`, builds and smoke-tests the program, and
   publishes the release "Photo Recovery 0.2.0" with `PhotoRecovery-0.2.0-setup.exe` (the installer),
   `PhotoRecovery-0.2.0-windows-portable.zip`, `SHA256SUMS.txt` and the changelog section as its notes.
7. Download the installer, have it scanned by Windows Defender, and run it once.
8. Set `__version__` to the next `.dev0` (for example `"0.2.1.dev0"`) in the next pull request.

If the tag and the version disagree, or the changelog has no section for the version, the build fails before
anything is published: fix it, delete the tag (`git push origin :v0.2.0`), and tag again.

The `dev-build` pre-release keeps being rebuilt from every push to `main` and pull request, for testing.
