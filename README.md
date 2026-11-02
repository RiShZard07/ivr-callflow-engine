# ivr-callflow-engine

A small IVR (interactive voice response) self-service call-flow engine in plain Python. It covers the pieces a call-flow team works with every day, end to end:

1. declarative call flows written as JSON (`flows/`),
2. a static validator that catches broken flows before any call runs (`ivr/validate.py`),
3. a deterministic interpreter with a simulated clock and a structured event log (`ivr/engine.py`),
4. scripted call-flow test cases that run in CI on every change (`ivr/testing.py`, `flows/tests/`),
5. a synthetic caller simulator that plays thousands of personas through a flow (`ivr/simulate.py`),
6. call-data analytics that compare two flow versions with a two-proportion z-test and Wilson intervals (`ivr/analytics.py`).

The example flows are a customer care line. Version 1 is a baseline that mostly collects an account number and transfers to an agent queue. Version 2 identifies known callers by phone number and handles balance, outage, data usage, payment arrangement and voicemail pin reset inside the IVR, with an agent transfer kept as the fallback on every path.

## Honest status

This is an AI-assisted educational prototype built with Claude Code. Everything in it is synthetic:

- no telephony, SIP or VoiceXML, no audio and no speech recognition: "speech" is text matched by keywords,
- no real customers, carriers or companies: the directory is generated from a seed, phone numbers use the fictional 555-01xx range, zip codes are made up,
- the two example flows were designed by hand so that v2 is a plausible improvement over v1; the simulation measures flows that were written to behave that way, it does not discover anything about real callers,
- the caller personas follow a simple scripted policy. Real callers are messier.

What is claimed: the engine, validator, test runner, simulator and statistics all work as documented, every number in this file was produced by the commands below, and the code is small enough to read in one sitting. What is not claimed: any production use, any real call data, or any particular vendor's behavior.

## How to run

```
pip install -r requirements.txt          # pytest only; the package itself has no dependencies
python -m pytest -q                      # 153 tests, about one second
python -m ivr validate flows/care_v1.json flows/care_v2.json      # exit 1 on validation errors
python -m ivr validate flows/broken_example.json                   # shows 5 errors and 5 warnings
python -m ivr run flows/care_v2.json --ani 5550123456 --inputs "dtmf:1,dtmf:3"
python -m ivr test flows/tests           # 25 scripted cases, exit 1 on any failure
python -m ivr simulate flows/care_v1.json --calls 2000 --seed 7 --out output/
python -m ivr simulate flows/care_v2.json --calls 2000 --seed 7 --out output/
python -m ivr report output/care_v1.0_results.json output/care_v2.0_results.json --out output/
python -m ivr demo --check               # the release gate, about two seconds
```

`run` prints a transcript: every node entered, every prompt with its simulated time stamp, every caller input, and the outcome. `--inputs` is a comma-separated list of `dtmf:<keys>`, `speech:<utterance>`, `timeout` and `hangup`. `--json` prints the full call result instead.

`simulate --out` writes one results file per flow with the full event log of every call (about 5 KB per call), and `report` reads one or two of them. `demo --check` validates both flows, runs every scripted case, simulates 3,000 calls per flow with seed 7, writes `output/report.json` and `output/summary.txt`, prints each gate check as `name: true/false`, and exits 1 unless all of them hold: no validation errors, all scripted cases pass, v2 containment above v1 with p < 0.01, v2 abandonment at most one point worse than v1, and zero error outcomes. `output/` is ignored by git.

## Flow definition

A flow is a JSON object with `name`, `version`, `start`, `queues` (the agent queues a transfer may target) and `nodes`, a map from node id to node. The engine starts at `start` and follows `next`, option and `on_*` edges until it reaches a `transfer` or `hangup` node.

```json
{
  "name": "care", "version": "2.0", "start": "greeting",
  "queues": ["billing", "general"],
  "nodes": {
    "greeting": {"type": "play", "prompt": "Thank you for calling.", "next": "identify"},
    "identify": {"type": "identify", "on_found": "menu", "on_not_found": "menu"},
    "menu": {
      "type": "menu",
      "prompt": "For your balance, press 1. To speak with an agent, press 0.",
      "options": {
        "1": {"label": "Balance", "next": "need_account", "intents": ["balance", "billing", "how much do I owe"]},
        "0": {"label": "Agent", "next": "agent", "intents": ["agent", "representative"]}
      },
      "invalid_prompt": "Sorry, that is not a valid option.",
      "timeout_prompt": "I did not hear a selection.",
      "max_retries": 2, "on_max_retries": "agent", "timeout_seconds": 5
    },
    "need_account": {"type": "branch", "conditions": [{"when": "account exists", "next": "balance"}],
                     "default": "collect_account"},
    "collect_account": {"type": "collect", "prompt": "Enter your ten digit account number, then pound.",
                        "variable": "account", "min_digits": 10, "max_digits": 10, "terminator": "#",
                        "validation": "account", "confirm": true,
                        "invalid_prompt": "That does not look right.", "timeout_prompt": "I did not get that.",
                        "max_retries": 2, "on_max_retries": "agent", "next": "balance"},
    "balance": {"type": "action", "name": "balance_inquiry", "on_success": "say_balance", "on_failure": "agent"},
    "say_balance": {"type": "play", "prompt": "Your balance is {balance} dollars, due on {due_date}.", "next": "bye"},
    "agent": {"type": "transfer", "queue": "general", "priority": 4, "reason": "caller_request_or_fallback"},
    "bye": {"type": "hangup", "prompt": "Goodbye."}
  }
}
```

Node types:

| type | fields | what it does |
|---|---|---|
| `play` | `prompt`, `next` | plays a prompt and continues |
| `menu` | `prompt`, `options` (key -> `label`, `next`, `intents`), `invalid_prompt`, `timeout_prompt`, `max_retries`, `on_max_retries`, `timeout_seconds` | waits for a DTMF key or a speech utterance |
| `collect` | `prompt`, `variable`, `min_digits`, `max_digits`, `terminator`, `validation` (`digits`, `luhn` or `account` = exactly ten digits), `confirm`, `invalid_prompt`, `timeout_prompt`, `max_retries`, `on_max_retries`, `next` | collects a digit string into a variable, optionally reading it back for a 1 = accept, 2 = re-enter confirmation |
| `identify` | `on_found`, `on_not_found` | looks the caller up by phone number and sets `account`, `zip` and `customer.*` |
| `action` | `name`, `on_success`, `on_failure` | calls one of five synthetic backend actions and stores its results as variables |
| `branch` | `conditions` (ordered `when` / `next` pairs), `default` | routes on variables; the first true condition wins |
| `transfer` | `queue`, `priority` (1 to 5), `reason` | ends the call as `transferred` |
| `hangup` | optional `prompt` | ends the call as `contained` |

Prompts may read variables with `{name}`; a variable that is not set renders as `?` and emits a `prompt_variable_missing` event. Branch conditions are limited to `var == value`, `var != value`, `var exists`, `var > number` and `var < number`.

`max_retries` is the number of re-prompts after a failed attempt, so a caller gets `max_retries + 1` attempts before the flow moves to `on_max_retries`. A rejected read-back at a confirming collect uses one retry too, so every loop in a flow is bounded.

The five actions live in `ivr/backend.py` and read the seeded directory: `balance_inquiry` sets `balance`, `due_date`, `past_due`; `data_usage` sets `data_used_gb`, `data_limit_gb`, `data_remaining_gb`; `outage_check` sets `outage` and, when there is one, `outage_eta` (it uses the `zip` variable, or the customer's zip through `account`); `payment_arrangement` sets `arrangement_date` and `arrangement_amount`, and fails for accounts that are not eligible or owe nothing; `reset_voicemail_pin` sets `voicemail_pin` and fails for accounts without voicemail.

## Engine and event model

`Call(flow, ani, directory, backend, clock)` walks the flow. `call.step(None)` starts it and runs until a menu or collect node needs the caller; `call.step(input)` applies one of `Dtmf(digits)`, `Speech(utterance)`, `Timeout()` or `Hangup()` and runs on. `run_script(flow, inputs, ani=...)` does this for a whole list of inputs and returns a `CallResult` with `outcome`, `events`, `handle_time_seconds`, `final_node`, `variables`, `transfer_queue` and `nodes_visited`.

Time is simulated: a prompt takes `len(text) / 15` seconds, each DTMF press 0.5 seconds, a speech utterance 2 seconds, and a timeout the node's `timeout_seconds`. Handle time is the clock reading when the IVR part of the call ends. Queue and agent time are out of scope.

Every call produces an ordered list of `Event(kind, time, node_id, data)`:

| kind | data |
|---|---|
| `node_entered` | `type` |
| `prompt_played` | `text` (after substitution), `seconds` |
| `prompt_variable_missing` | `variable` |
| `input_received` | `kind` (dtmf, speech, timeout), `value`, `matched` option key or `digits` |
| `input_invalid` | `reason` (no_match, ambiguous, timeout, validation_failed), `attempt`, optional `detail` |
| `retry` | `attempt`, `retries_left`, `reason` = reentered for a rejected read-back |
| `max_retries_reached` | `attempts` |
| `variable_set` | `name`, `value` |
| `identify_result` | `found` |
| `action_called` | `name`, `success`, `message` |
| `transferred` | `queue`, `reason`, `priority` |
| `hangup` | (the IVR ended the call: outcome `contained`) |
| `abandoned` | `phase` (the caller hung up at a menu, collect or confirm step) |
| `error` | `message` (loop guard, unknown node, bad condition) |

Outcomes are `contained`, `transferred`, `abandoned` and `error`. A loop guard ends any call that enters more than 200 nodes with outcome `error`; validated flows never reach it.

## Speech matching

`ivr/speech.py` is keyword matching, not speech recognition. An utterance is lower-cased, stripped of punctuation, run through a small synonym map ("bill" -> billing, "pay" -> payment, "internet", "wifi" and "service down" -> outage, "representative" -> agent, and so on) and cleared of stop words. Each menu option is scored by how many tokens the utterance shares with the option's `intents`. The best option wins if it shares at least one token and no other option ties with it. A tie is reported as ambiguous and the engine re-prompts; for example "pay my bill" is ambiguous in v2, where billing and payment are separate options, and clear in v1, where one option covers both. Spoken keys ("press two", "two") select options directly, spoken digits ("four one five ...") work at collect nodes, and yes/no answers work at read-back confirmations.

## Validation rules

`python -m ivr validate` runs these checks without placing a call. Errors break or strand calls; warnings are design smells.

| code | level | rule |
|---|---|---|
| `unknown_start` | error | `start` is not a node id |
| `empty_node_id` | error | a node id is empty (duplicate ids are rejected earlier, when the JSON is parsed) |
| `dangling_target` | error | a `next`, option, `on_*` or `default` edge points at a missing node |
| `missing_max_retries_target` | error | a menu or collect has no `on_max_retries` |
| `max_retries_out_of_range` | error | `max_retries` is not between 1 and 10 |
| `unknown_queue` | error | a transfer targets a queue that is not in `queues` |
| `invalid_condition` | error | a branch `when` does not follow the grammar |
| `input_free_cycle` | error | a cycle made only of play, identify, action and branch nodes (an infinite loop with no way for the caller to break out) |
| `no_terminal_node` | error | no transfer or hangup node anywhere |
| `unreachable_node` | warning | the node cannot be reached from `start` |
| `missing_invalid_prompt`, `missing_timeout_prompt` | warning | a menu or collect replays its prompt without explaining why |
| `option_without_intents` | warning | speech callers cannot choose this option |
| `unset_prompt_variable` | warning | a prompt reads `{var}` but no node on any path to it can set `var` (reachability based: collect sets its variable, identify sets the customer variables, each action sets its documented outputs) |

Field types, enumerations and ranges (unknown node type, `priority` of 9, `min_digits` above `max_digits`, a misspelled field) are rejected when the file is loaded, with a message that names the node and field. `flows/broken_example.json` trips five errors and five warnings and is kept as a demonstration.

## Scripted call-flow tests

A case is a JSON object naming the flow, the caller, the inputs and the expectation. `flows/tests/` holds 25 cases across both flows; `python -m ivr test flows/tests` runs them and CI fails if any fails.

```json
{
  "name": "v2 known caller hears the balance and finishes in the IVR",
  "flow": "../care_v2.json",
  "ani": "5550123456",
  "inputs": ["dtmf:1", "dtmf:3"],
  "expect": {
    "outcome": "contained",
    "final_node": "goodbye",
    "prompts_contain": ["Welcome back, Avery", "Your current balance is 87.50 dollars", "Goodbye"],
    "nodes_visited_includes": ["identify", "balance", "after_balance_menu"],
    "max_handle_time_seconds": 40
  }
}
```

`expect.outcome` is required. `transfer_queue`, `final_node`, `prompts_contain` (substrings that must appear in played prompts, in order), `nodes_visited_includes` and `max_handle_time_seconds` are optional. A file holds one case or `{"cases": [...]}`, and the flow path is relative to the file. A failing case prints what was expected and what was seen, for example `outcome: expected 'contained', saw 'transferred' (final node 'transfer_general')` followed by the prompts that were played. If a script runs out of inputs while the flow still waits, the caller is treated as having hung up.

The shipped cases cover happy paths for every self-service action, invalid input and retries, timeouts, the max-retry fallback, unknown callers, the ineligible payment arrangement, speech selection, ambiguous speech followed by a re-prompt, spoken digits, read-back rejection, and abandonment.

## Simulator personas

`simulate(flow, n_calls, seed)` draws one persona per call from a seeded generator and plays it through the flow, so the same seed always produces the same calls. A persona has:

- an intent: billing 30%, outage 20%, data 15%, payment 15%, agent 15%, voicemail 5%,
- whether they are a customer at all (90%) and, if so, whether they call from their registered phone (75%), which is what lets v2's `identify` step find them,
- whether they know their account number (70%),
- a mis-key probability between 0 and 15% per entry,
- a preference for speaking (40%) or pressing keys,
- patience: a hang-up after 60 to 240 seconds, or after 2 to 4 failed attempts.

The policy at each wait point: at a menu, pick the option whose label and intents match the intent (or speak a phrase for it; a mumble is possible for speech callers), fall back to the agent option when nothing matches, and press the "finished" option once the action that satisfies the intent has succeeded; at a collect, key the account number or zip code with possible mis-keys, or stay silent when the number is unknown; at a read-back, accept only when the digits match; hang up when patience runs out. Both versions are played by the same 3,000 personas, so the comparison is paired and the independent-samples z-test below is the conservative choice.

## Metrics

With `n_handled = contained + transferred + abandoned` and `n_total` all calls:

- containment rate = contained / n_handled
- transfer rate = transferred / n_handled
- abandonment rate = abandoned / n_handled
- error rate = error / n_total (error outcomes are excluded from the three rates above and reported separately; the gate requires zero)
- average handle time = mean of `handle_time_seconds`; p95 = linear-interpolation 95th percentile
- invalid-input rate per menu = `input_invalid` events at the menu / `input_received` events at the menu
- retries per call = `retry` events / n_total
- action success rate = successful `action_called` / all `action_called`, per action
- per-node funnel = calls that entered the node and, for those calls, the final outcome
- transfer reasons by queue = count of `transferred` events by queue and reason

`compare(a, b)` reports, for containment, transfer and abandonment: both counts and rates, a 95% Wilson score interval for each rate, the difference b minus a, and a pooled two-proportion z statistic with a two-sided p-value from the normal CDF (`erfc(|z| / sqrt(2))`). Unit tests pin the z-test to a hand-computed case (600/1000 versus 650/1000 gives z = 2.309, p = 0.0209) and the Wilson interval for 600/1000 to [0.5693, 0.6299].

## Results: care v1 versus care v2

3,000 simulated calls per flow, seed 7, the same personas against both flows (`python -m ivr demo --check`).

| metric | care 1.0 | care 2.0 | difference |
|---|---|---|---|
| contained | 120 (4.0%, Wilson 3.4% to 4.8%) | 1,559 (52.0%, Wilson 50.2% to 53.8%) | +48.0 points, z = 41.4, p < 0.0001 |
| transferred | 2,588 (86.3%) | 1,315 (43.8%) | -42.4 points, z = -34.5, p < 0.0001 |
| abandoned | 292 (9.7%) | 126 (4.2%) | -5.5 points, z = -8.4, p < 0.0001 |
| error outcomes | 0 | 0 | |
| average handle time | 37.8 s | 34.8 s | -3.0 s |
| p95 handle time | 59.0 s | 54.1 s | -4.9 s |
| retries per call | 0.65 | 0.29 | |
| main menu invalid-input rate | 6.5% | 5.6% | |
| identify hit rate | not used | 67.4% | |

Where v2's containment comes from: balance inquiry 743 of 743 calls succeeded, data usage 369 of 369, outage check 562 of 562 (an outage was found for callers in the three seeded outage zips, the rest were transferred to technical support), payment arrangement 246 of 406 (160 failed on eligibility or a zero balance and went to billing), voicemail pin reset 90 of 117. v1's only contained path is the zip-code outage check (120 calls). v1's abandons are almost all callers who do not know their account number and give up at the collect step; v2 cuts those because two thirds of callers are identified by phone number and never see that step.

The gate output and the environment are recorded in `VERIFIED_RESULTS.md`.

## Design decisions

- Flows are data, not code. A flow author edits JSON and the validator tells them what is wrong before anything runs. The engine has no knowledge of any particular flow.
- The interpreter is deterministic and clock-free in real time. Prompts cost `len / 15` seconds and inputs cost fixed amounts, so handle time is reproducible and tests can assert exact timing.
- Events are the one source of truth. Transcripts, scripted assertions and all analytics are computed from the event list; nothing is counted on the side.
- Shape checks happen at load time, graph checks in `validate`. A node with the wrong field types never becomes an object; a well-formed node that points nowhere is a validation error with a code that tests can assert on.
- Speech is keyword matching with explicit ambiguity. Returning "ambiguous" instead of guessing is the behavior a real system should have when two options tie, and it is what the re-prompt test exercises.
- Retries are literal: `max_retries` re-prompts, then the fallback. Rejecting a read-back counts as a retry so no caller input can loop forever, and a loop guard protects against an unvalidated flow.
- The gate is statistical, not a single threshold. A containment improvement must be significant at p < 0.01 and abandonment must not get materially worse, which is how a flow change would be judged from real call data.
- Standard library only at runtime. The project installs nothing but pytest.

## Limits

- Speech is text matching; there is no acoustic model, confidence score or barge-in.
- Personas follow a scripted policy with a handful of parameters; real caller behavior is far more varied, and the mis-key and patience distributions were chosen by hand.
- The paired comparison is analyzed with an independent-samples z-test, which overstates the standard error for paired data; the result is conservative, not exact.
- Handle time stops at transfer. Queue waiting and agent time, which dominate real cost, are not modeled.
- No real telephony concepts beyond the ones listed: no barge-in, no DTMF type-ahead across nodes, no language selection, no call recording, no retry of failed backend actions.
- The backend is a lookup into a seeded directory; it never fails for infrastructure reasons, so action success rates only reflect business rules.

## Layout

```
ivr/
  flow.py        flow model: dataclasses, JSON loading, shape checks
  conditions.py  the five-form condition grammar for branch nodes
  validate.py    static validation rules and Issue records
  engine.py      Call, inputs, events, simulated clock, run_script
  speech.py      utterance normalization and intent matching
  directory.py   seeded synthetic customers (ANI, Luhn-valid accounts, zips)
  backend.py     the five self-service actions
  testing.py     scripted case format, runner and report
  simulate.py    personas and the caller policy
  analytics.py   metrics, z-test, Wilson interval, reports
  cli.py         python -m ivr commands and the demo gate
  digits.py      Luhn and digit-string helpers
flows/
  care_v1.json, care_v2.json, broken_example.json
  tests/         25 scripted call-flow cases
tests/           153 pytest tests
.github/workflows/ci.yml   pytest, validate, scripted cases, demo --check on Python 3.12
LEARNING_GUIDE.md          interview questions and answers for this domain
VERIFIED_RESULTS.md        what was run, where, and what came out
```
