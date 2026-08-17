## What this changes

<!-- One or two sentences. Link the issue it closes, if any. -->

## Why

<!-- The reasoning. If this changes learning, persistence or the energy-balance
     model, explain what the previous behaviour got wrong. -->

## Scope check

Phase 1 is observation only. Tick the one that applies:

- [ ] This does not add battery control, charge/discharge decisions, price-based
      trading or EV scheduling.
- [ ] This does add one of the above, and I have explained why it belongs here
      rather than in a later phase.

## Checklist

- [ ] `python -m pytest` passes locally
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] Tests cover the new behaviour, and a test would fail without the change
- [ ] `docs/ARCHITECTURE.md` updated if a documented design decision changed
- [ ] `README.md` updated if user-visible behaviour changed
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] No real entity ids, tokens, diagnostics dumps or personal data added

## Risk areas touched

<!-- Delete what does not apply. These are the parts where a subtle change has
     historically caused silent data loss. -->

- Interval identity / daylight saving
- Persistence schema or migration
- Sign-convention normalisation
- Flexible-load (EV) baseline derivation
- Energy-balance tolerance model
- Forecast or confidence model
