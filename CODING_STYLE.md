# Coding style

Write code for humans first. A reader who does not know this codebase should be able to understand what a function does, why it exists, and what can go wrong — without running it. Prefer clarity over brevity. Prefer explicitness over cleverness.

- All methods must have complete type signatures: annotate every parameter and the return type (use `-> None` for procedures).
- Pure functions with no dependency on `self` should be decorated with `@staticmethod`.
- Variable names should be clear English words. Avoid opaque abbreviations like `fname` — prefer `name`. Conventional short names are fine: `n`, `fs`, `ver`, `t`, `f`, `chunk`, `root`, etc.
- Keep methods short and focused: one method, one concern. Extract a helper when (a) the method is getting long, and (b) the extracted piece has a clear semantic name. Do not extract 2–3 line blocks just to reduce line count — the name must earn its existence. Loop and event-handler bodies in particular should read as a sequence of named steps rather than inline logic (e.g. `run()` calls `_seed_new()` and `_maybe_cleanup()` rather than containing those loops directly).
