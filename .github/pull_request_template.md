## What and why

<!-- One topic per pull request. Link the issue if there is one. -->

## Checklist

- [ ] Tests cover the change. For detection logic, a test walks the probe sequence that could
      cause a false "out" or "back" post.
- [ ] `ruff check . && ruff format --check . && mypy` pass, and so do the tests.
- [ ] No token or URL is logged.
- [ ] README / CHANGELOG updated if behaviour or settings changed.
