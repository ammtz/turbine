# Turbine

An agent loop where the worker never decides it's done.
The worker only proposes better versions of one artifact (code, text, config).
A plain-code governor scores each proposal 0–1 and forces the exit; "finish" is a quit rule, never a quit option.

## Tests

```bash
python -m unittest discover -s tests -v
```

Requires Python 3.11+. Standard library only.
