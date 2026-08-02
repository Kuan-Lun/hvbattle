# HVBattle

HVBattle provides reusable HentaiVerse battle-domain APIs. It builds on
`hvbrowser` for the authenticated Hentaiverse browser session and keeps the
private command-line runner in a separate application workspace.

The first release preserves the historical `BattleDriver` API while adding a
safe `run_current()` boundary. That method only runs an already active battle;
it never repairs equipment, recovers stamina, or starts Arena/GrindFest.
Automatic next-battle toggles default to off and require explicit opt-in.
`BattleDriver` preloads the PonyChart classifier and ONNX model before opening
the browser, so a timed challenge never pays the first-load cost.

```python
import asyncio

from hvbattle import BattleDriver


async def main() -> None:
    async with BattleDriver(headless=True) as driver:
        await driver.run_current()


asyncio.run(main())
```

The legacy `battle()` loop remains available for the private runner. It can
repair equipment and recover stamina; starting another Arena or GrindFest also
requires explicitly enabling the corresponding auto-next option.

## Development

Build a clean environment backed by PyPI releases:

```bash
bash scripts/rebuild-env.sh
```

For coordinated local development before all dependent releases are on PyPI,
overlay editable checkouts in dependency order:

```bash
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hbrowser.clone
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hvbrowser.clone
```

Commands that must preserve these editable overlays use `uv run --no-sync`.
