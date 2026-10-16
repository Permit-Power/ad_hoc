---
name: analysis-code-style
description: Conventions for writing and cleaning up analysis code in this repo — notebook structure, module layout, comments, assumptions, QA checks, labeled outputs, and plain unembellished writing. Use whenever creating a new analysis notebook or src/ module, refactoring or reviewing an existing one, adding a section or result to a notebook, or updating the README for an analysis, even if the user only asks to "clean up", "tidy", "review", or "add" something.
---

# Analysis code style

The reference implementation is the heat pump + solar analysis:
`notebooks/solar_bill_analysis.ipynb`, `src/solar_bill_analysis.py`, `src/eia_rates.py`, and its README section. When in doubt, open those and match them.

The goal is code a colleague can read top to bottom and verify: what is being estimated, under which assumptions, and where each number comes from.

## Layout

- **`src/<module>.py`** holds pure functions: data loading, simulation, computation. No notebook state, no printing results, no hard-coded scenario values, no absolute local paths.
- **`notebooks/<analysis>.ipynb`** orchestrates: settings, scenarios, calling module functions, building result tables, plotting, saving outputs. Modules return data; figures are drawn in the notebook unless the same plot is reused across notebooks.
- Input paths are constants at the top of the notebook. If data lives outside `../data/` (e.g. an external drive), say so in the README.
- A module starts with a short docstring listing its main functions in the order the notebook calls them.
- Keep reusable data sources in their own module (e.g. `eia_rates.py`) so other analyses can import them.

## Notebook structure

1. **Title cell** (markdown): what the notebook estimates, as numbered results; a brief methodology (4–6 numbered steps, one line each, from data to results); scope (which homes, which upgrade); what is excluded (e.g. equipment costs); a one-line list of sections. The methodology lets a reader see the whole approach before reading any code.
2. **Imports**: standard library, third-party, then local modules; only what is used.
3. **Numbered sections** (`## 1. Sample`, `## 2. Simulation settings`, …), each a short markdown cell followed by its code. The markdown states what the section does and its assumptions as bullets.
4. **Settings as UPPER_CASE constants** at the top of the section that uses them (`SAMPLE_SIZE`, `HP_UPGRADE`, `SCENARIOS`, financing dicts). Scenario definitions are data (a list of tuples or dicts), not branches in code.
5. **Results sections** (`## Result 1. …`): one sentence defining the metric, then print the table and save it to `../outputs/<descriptive_name>.csv`. A figure gets its own section with a one-sentence caption saying what is plotted and for which scenario.

Each result answers one question. When two metrics could be confused (e.g. "is the heat pump cheaper to run" vs. "do overall bills go down"), give each its own section and state in one sentence what each comparison includes and excludes.

## Assumptions

- Put assumptions in one place (a section and its constants), not scattered through the code.
- Every externally sourced number gets a comment with its source and date (e.g. a tariff value and the rate card it came from). Estimated values say "estimated" and how. If the source is unknown, leave `# source needed` and ask the user.
- Choices that change the answer (what is compared to what, sizing rules, which policy applies) are the user's to make. State the options and ask before implementing; don't pick one silently.
- Keep a list of assumptions and their source (user, original code, or Claude) when making many changes, so they can be audited.

## Code

- Names say what a value is and carry units: `elec_hp_kwh`, `gas_rate` ($/therm, noted where defined), `system_kw_solar_only`, `export_wholesale_usd`. Avoid names that encode old setups (`annual_elec_7_kwh`).
- No dead code, unused imports, unused parameters, or commented-out blocks. Remove a helper when nothing calls it. An unused *modeling option* (an alternative assumption someone may want back, such as a sensitivity case) is a user choice: ask before removing it.
- Defaults in module functions match the current analysis, or the parameter has no default.
- Vectorize with numpy/pandas; use thread pools for I/O-bound work (S3, HTTP).
- Expensive steps run in batches cached to `../data/` as each finishes, so a failed or repeated run resumes. Changing cached columns means renaming the cache directory or rerunning.
- Network calls retry on transient errors.

## Comments and docstrings

- Comments explain what a block does or why, in a few words. Docstrings (numpy style) describe current behavior, parameters with units, and returns.
- Don't narrate history ("now", "previously", "fixed"), address the reader ("your case"), or editorialize ("the honest result", "importantly", "the real takeaway", "note that"). State the fact.
- Don't restate the code. `# Rooftop PV sized to the home's load with the heat pump` is useful; `# call compute_pv` is not.

## QA

- Check invariants with `assert`, not printouts: uniform sample weights, every state mapped to a policy, energy balance (imports − exports + generation = load), no missing rates.
- Before a long run, test the whole notebook on a small sample (e.g. a copy with `SAMPLE_SIZE = 20`, outputs written to a scratch directory) and delete the test cache afterwards. Without a sampling setting, use a subset of the inputs.
- After a refactor, rerun and confirm results match the previous outputs before committing. If the real data isn't reachable, compare old and new code on small synthetic inputs and say the full rerun is still pending.

## Outputs and labels

- Table columns say what and in which units: `Current policy: homes that save`, `median savings ($/yr)`, `1,200W: share of heat pump electricity cost`.
- Row labels name the scenario and its inputs: `Peer-country: solar $1.00/W, battery $400/kWh`.
- Format shares as percentages and dollars with thousands separators when printing; save unformatted numbers to CSV.
- `data/` and `outputs/` are gitignored; note any input files a rerun needs.

## Writing (markdown and README)

- Plain, short sentences. Facts and definitions, not persuasion. No adjectives selling results.
- README section per analysis: primary artifacts, purpose, key results (tables), data sources, key calculations, known limitations, outputs.
- When changing an analysis, update its README section in the same change: numbers from the latest run, function and notebook names, removed options. Update `CLAUDE.md` when modules or data sources change. If asked not to touch repo files, list the needed README updates instead.
