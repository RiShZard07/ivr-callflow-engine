# Verified results

Everything below was produced on the machine described here, in the order shown, on the final state of the repository. Nothing is copied from a design target.

## Environment

- Python 3.13.17 (the code is written for 3.12 or newer and the CI workflow runs on 3.12)
- pytest 9.2.0
- Linux 6.18.44 x86_64, 2 CPU cores
- Runtime dependencies: none beyond the standard library. `requirements.txt` lists pytest only.

## Commands run and what came out

| command | exit | wall time | result |
|---|---|---|---|
| `python3 -m pytest -q` | 0 | 1.7 s | `153 passed in 1.31s` |
| `pytest -q` (bare, from the repo root) | 0 | 1.5 s | `153 passed in 1.19s` |
| `python3 -m ivr validate flows/care_v1.json flows/care_v2.json` | 0 | 0.1 s | 0 errors, 0 warnings for each flow |
| `python3 -m ivr validate flows/broken_example.json` | 1 | 0.1 s | 5 errors, 5 warnings (listed below) |
| `python3 -m ivr test flows/tests` | 0 | 0.1 s | `25 of 25 scripted cases passed` |
| `python3 -m ivr demo --check` | 0 | 1.8 s | all six gate lines `true` (verbatim below) |

Also run during development, with the same results: `ruff check --select I,E,F,W,B,UP,SIM --target-version py312 --line-length 120 ivr tests` reports no findings, and `python3 -m pyflakes ivr tests` is clean.

## Test counts

- 153 pytest tests collected and passed (104 test functions; 8 of them are parametrized tables, which account for the remaining 49 items).
- 25 scripted call-flow cases in `flows/tests/` (9 for care v1, 16 for care v2), all passing.
- Test files: `tests/test_flow.py`, `test_validate.py`, `test_conditions.py`, `test_engine.py`, `test_speech.py`, `test_directory_backend.py`, `test_testing.py`, `test_simulate.py`, `test_analytics.py`, `test_cli.py`.

## Validation of the broken example (verbatim)

```
flows/broken_example.json: 5 error(s), 5 warning(s)
  error: [main_menu] max_retries must be between 1 and 10, got 15 (max_retries_out_of_range)
  error: [main_menu] no on_max_retries target: callers who keep failing have nowhere to go (missing_max_retries_target)
  error: [orphan] points at unknown node 'nowhere' (dangling_target)
  error: [support_info] cycle with no input node: support_info -> support_more -> support_info (input_free_cycle)
  error: [transfer_billing] transfer to unknown queue 'bililng' (unknown_queue)
  warning: [greeting] prompt uses {customer.first_name} but no upstream node sets it (unset_prompt_variable)
  warning: [main_menu] no invalid_prompt: callers get no explanation before the re-prompt (missing_invalid_prompt)
  warning: [main_menu] no timeout_prompt: silent callers get no nudge before the re-prompt (missing_timeout_prompt)
  warning: [main_menu] option 2 (Support) has no intents, so speech callers cannot choose it (option_without_intents)
  warning: [orphan] node cannot be reached from the start node (unreachable_node)
```

## `python3 -m ivr demo --check` output (verbatim)

```
validate flows/care_v1.json: 0 error(s), 0 warning(s)
validate flows/care_v2.json: 0 error(s), 0 warning(s)
scripted cases: 25 of 25 passed
simulating 3000 calls per flow with seed 7...
care 1.0: 3000 calls
  containment 4.0%   transfer 86.3%   abandonment 9.7%   errors 0.0%
  handle time: average 37.8s, p95 59.0s, retries per call 0.65
  actions: outage_check 568/568
  transfers by queue: account_services 391, billing 1192, general 602, tech_support 403
care 2.0: 3000 calls
  containment 52.0%   transfer 43.8%   abandonment 4.2%   errors 0.0%
  handle time: average 34.8s, p95 54.1s, retries per call 0.29
  actions: balance_inquiry 743/743, data_usage 369/369, outage_check 562/562, payment_arrangement 246/406, reset_voicemail_pin 90/117
  transfers by queue: account_services 51, billing 306, general 472, tech_support 486

care 2.0 versus care 1.0:
  containment  4.0% [3.4%, 4.8%]  ->  52.0% [50.2%, 53.8%]   difference +48.0%, z +41.38, p < 0.0001
  transfer     86.3% [85.0%, 87.5%]  ->  43.8% [42.1%, 45.6%]   difference -42.4%, z -34.47, p < 0.0001
  abandonment  9.7% [8.7%, 10.8%]  ->  4.2% [3.5%, 5.0%]   difference -5.5%, z -8.42, p < 0.0001
  handle time  average -3.0s, p95 -4.9s
wrote output/report.json and output/summary.txt in 1.7s

no_validation_errors: true
all_scripted_cases_pass: true
v2_containment_higher_p_below_0_01: true
v2_abandonment_not_worse_by_more_than_1_point: true
zero_error_outcomes: true
all_passed: true
```

## v1 versus v2 in numbers (from `output/report.json` of that run)

| | care 1.0 | care 2.0 |
|---|---|---|
| calls | 3000 | 3000 |
| contained | 120 (4.00%) | 1559 (51.97%) |
| transferred | 2588 (86.27%) | 1315 (43.83%) |
| abandoned | 292 (9.73%) | 126 (4.20%) |
| error | 0 | 0 |
| containment Wilson 95% | 0.0336 to 0.0476 | 0.5018 to 0.5375 |
| average handle time | 37.80 s | 34.76 s |
| p95 handle time | 59.03 s | 54.13 s |
| retries per call | 0.646 | 0.287 |
| main_menu invalid-input rate | 6.5% | 5.6% |
| after_balance_menu invalid-input rate | not present | 0.4% |
| identify hit rate | not present | 67.4% |

Containment difference +0.4797, z = 41.38; transfer difference -0.4243, z = -34.47; abandonment difference -0.0553, z = -8.42. All three two-sided p-values are below 0.0001 (the containment p-value underflows to 0 in double precision). Average handle time difference -3.04 s, p95 difference -4.90 s.

Both flows are played by the same 3,000 personas drawn from seed 7, so the comparison is paired; the z-test treats the arms as independent, which is conservative.

## Timing

- Full test suite: about 1.3 seconds inside pytest, under 2 seconds wall clock.
- `demo --check` (validate both flows, 25 scripted cases, 6,000 simulated calls, comparison, report files): 1.7 seconds reported by the command itself, 1.8 seconds wall clock, on 2 cores. The requirement was under 60 seconds.
- One `simulate` run of 3,000 calls: about 1 second.

## Code size (final state)

| part | files | lines | non-blank lines |
|---|---|---|---|
| `ivr/` package | 14 | 3176 | 2670 |
| `tests/` | 11 | 1631 | 1346 |
| `flows/` JSON (3 flows + 2 case files) | 5 | 814 | |
| `README.md` | 1 | 269 | |
| `LEARNING_GUIDE.md` | 1 | 105 | |

Largest modules: `ivr/flow.py` 550 lines, `ivr/engine.py` 549, `ivr/validate.py` 331, `ivr/cli.py` 290, `ivr/analytics.py` 276.

## Checks on the text

A scan of every `.md`, `.py`, `.json`, `.yml`, `.ini` and `.txt` file in the repository found no em dashes, no en dashes, no non-ASCII characters in the documentation or flow files, and none of the words leverage, robust, seamless, cutting-edge or comprehensive. No carrier, employer or vendor name appears anywhere in the repository.

## Not done

- No real telephony, audio, or speech recognition was built or exercised; the speech layer is text keyword matching.
- The CI workflow was written but not executed here: there is no GitHub Actions runner in this environment. Its four steps are the same commands listed in the table above, which all passed locally.
- The project was not run on Python 3.12 itself. It uses no feature newer than 3.10 (`X | Y` unions, `zip(strict=)`), and ruff's `--target-version py312` check passes.
