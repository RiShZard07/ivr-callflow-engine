# Learning guide: IVR call flows, testing and measurement

Thirty interview-style questions with short answers, written around this repository. Each answer names the file that shows the idea in code.

## IVR basics

**1. What is an IVR, and what does "self-service" mean here?**
An IVR is the automated phone system that answers a call, plays prompts, listens for key presses or speech, looks things up and either resolves the request or routes the caller to an agent queue. Self-service means the request is resolved without an agent: in `flows/care_v2.json` a known caller can hear a balance, check an outage, hear data usage, set a payment arrangement or reset a voicemail pin and hang up satisfied.

**2. DTMF versus speech: what is the difference and why support both?**
DTMF is the tone a keypad press sends; it is exact and cheap to handle. Speech is what the caller says, which must be recognized and then matched to an intent, with real uncertainty. Supporting both lets a caller say "billing" or press 1, and gives the flow a reliable fallback (keys) when speech fails. In the engine, `Dtmf` and `Speech` are two input types handled by `_menu_input` in `ivr/engine.py`; `ivr/speech.py` does the matching.

**3. Define containment, transfer rate and abandonment.**
Containment is the share of handled calls the IVR resolved on its own (`contained / (contained + transferred + abandoned)`). Transfer rate is the share handed to an agent queue. Abandonment is the share where the caller hung up while still in the IVR. The three sum to one over handled calls. `ivr/analytics.py` computes them exactly this way, and error-outcome calls are reported separately so they cannot inflate or hide any of the three.

**4. What is AHT and what part of it does this project measure?**
Average handle time is the mean duration of a call. Here it is only the IVR portion, measured on a simulated clock (prompts at 15 characters per second, half a second per key press, two seconds per utterance, the node's timeout for silence). Queue waiting and agent talk time are out of scope, and the README says so.

**5. Why is containment alone a dangerous metric?**
Because a flow can raise containment by making it hard to reach an agent, which pushes callers to hang up. Those hang-ups show up as abandonment, and a frustrated caller who hangs up is worse than a transfer. That is why the release gate in `ivr/cli.py` requires both higher containment and abandonment not more than one point worse.

**6. What is ANI and how does v2 use it?**
ANI is the caller's phone number as delivered with the call. The `identify` node in `ivr/engine.py` looks it up in the directory; a known caller gets `account`, `zip` and `customer.*` variables set and skips the account-number step. That one change removes the longest and most error-prone part of the v1 call for two thirds of callers.

**7. Why confirm digits by reading them back?**
Ten digits typed on a phone are easy to get wrong, and a wrong account number that passes a length check would route the caller to the wrong record. Reading the digits back and asking for 1 or 2 catches that before anything acts on the number. The cost is about six seconds per call, which is why v2 only pays it for callers the ANI lookup did not identify.

**8. What is the Luhn check and where does it belong in an IVR?**
A check-digit formula (double every second digit from the right, subtract nine when above nine, require the sum to be a multiple of ten) that catches single-digit typos and most transpositions. It belongs at the collect step so an obviously mis-keyed number is re-prompted immediately instead of going to the backend. `ivr/digits.py` implements it and `collect` nodes can require it with `"validation": "luhn"`.

## Call-flow design

**9. Why define flows as data instead of code?**
A JSON flow can be validated, diffed, reviewed and versioned without touching the engine, and non-engineers can read it. The engine stays small and generic, and a change to a prompt or a menu option is a data change with a test, not a deployment of new logic. `ivr/flow.py` loads the JSON into frozen dataclasses so a loaded flow cannot be mutated by accident.

**10. What does `max_retries` mean and why does the exact semantics matter?**
In this engine it is the number of re-prompts after a failed attempt, so a caller gets `max_retries + 1` attempts before `on_max_retries`. The semantics matter because off-by-one errors here either strand callers (too few attempts) or trap them (too many), and because the test suite asserts the exact count (`test_menu_retries_then_falls_back_after_max_retries_plus_one_attempts` in `tests/test_engine.py`).

**11. What should happen when speech matching is uncertain?**
Re-prompt rather than guess. If two options score equally, acting on either would send half the callers down the wrong path. `match_option` in `ivr/speech.py` returns `ambiguous`, the engine logs an `input_invalid` with that reason and replays the menu, and the scripted case "ambiguous speech is re-prompted" pins that behavior.

**12. How do you keep every path to an agent?**
Every menu and collect has an `on_max_retries` target that ends in a transfer, every action has an `on_failure` path, and the main menu has an explicit agent option. The validator makes the first of these an error when it is missing (`missing_max_retries_target` in `ivr/validate.py`).

**13. Why does a flow need a loop guard if it was validated?**
Validation catches cycles with no input node, but an unvalidated or hand-edited flow can still be loaded and run. The loop guard in `ivr/engine.py` ends a call after 200 node entries with outcome `error`, so a bad flow produces a measurable error rate instead of a hung process. The gate requires that rate to be zero.

## Static validation

**14. Why validate flows statically at all?**
Because a broken edge, an unknown queue or an infinite play loop fails for a real caller at the worst time, and because scripted tests only cover the paths someone thought to script. Static checks cover every node and edge in milliseconds, so they run on every save and every pull request.

**15. How do you detect an infinite loop in a flow graph?**
Restrict the graph to nodes that never wait for the caller (play, identify, action, branch), then look for any cycle in that subgraph with a depth-first search that tracks the current path. A cycle through a menu is fine because the caller can break out; a cycle with no input node is an error. See `_check_input_free_cycles` in `ivr/validate.py`.

**16. How can a validator know whether a prompt variable will be set?**
By reachability: collect the variables each node type can set (a collect sets its variable, identify sets the customer variables, each action sets a documented list), then for a prompt that reads `{var}` walk the predecessors of its node and check whether any of them can set `var`. It is a warning, not an error, because the check is path-insensitive: it says "some path can set it", not "every path does".

**17. What is the difference between a load error and a validation issue here?**
A load error is a shape problem: the wrong type for a field, an unknown node type, a `priority` of 9. The file never becomes a `Flow` object. A validation issue is a graph problem in a well-formed flow: a dangling edge, an unreachable node. Both are reported with the node id and a code so tests can assert on them.

## Testing call flows

**18. How do you test a call flow automatically?**
Script the call: a caller number, a list of inputs (`dtmf:1`, `speech:pay my bill`, `timeout`, `hangup`) and an expectation (outcome, transfer queue, prompts that must be heard in order, nodes that must be visited, a handle-time ceiling). Run it against the flow in a deterministic engine and diff the result. `ivr/testing.py` is that runner and `flows/tests/` holds 25 such cases; `python -m ivr test flows/tests` fails CI on any mismatch.

**19. What does a good failure message for a call-flow test contain?**
What was expected, what was seen, and enough context to see why: the final node, the full list of prompts played, the path of nodes visited. `check_expectation` produces lines like `outcome: expected 'contained', saw 'transferred' (final node 'transfer_general')` so the person reading the CI log does not have to re-run the call.

**20. Why does the engine need to be deterministic for testing?**
Because a test that sometimes passes is worse than no test. Deterministic prompt timing, a seeded directory and backend actions that are pure functions of the customer record mean the same script always yields the same events, so assertions can be exact (down to the handle time) and a failure always means a real change.

**21. What is the difference between the scripted cases and the pytest suite?**
The pytest suite (`tests/`) tests the engine, validator and analytics as software: parsing, retry semantics, statistics against hand-computed values. The scripted cases test a specific flow as a product: does the past-due caller hear the right prompt and end in the billing queue. Both run in CI, and a flow author who never touches Python only needs the second kind.

## Rolling out a change and measuring it

**22. How would you roll out v2 safely?**
Validate it, run the scripted cases, then route a fraction of live calls to v2 and the rest to v1 for the same period, with the split decided by something neutral like the last digit of a call id. Compare containment, abandonment and handle time with the same statistical test the gate uses, and watch abandonment and transfer reasons by queue for signs that callers are being trapped. Roll forward only when the difference is significant and no guardrail metric moved the wrong way.

**23. Why compare rates with a two-proportion z-test?**
Containment is a proportion, and the question is whether two proportions differ by more than chance. The pooled two-proportion z-test is the standard test for that with large samples: pool the two successes, compute the standard error of the difference, and read a two-sided p-value from the normal distribution. `two_proportion_z_test` in `ivr/analytics.py` does exactly this with `math.erfc`, and a unit test pins it to 600/1000 versus 650/1000 (z = 2.309, p = 0.0209).

**24. Why report a Wilson interval instead of the usual p plus or minus 1.96 times the standard error?**
The Wald interval misbehaves for proportions near 0 or 1 and for small samples: it can extend below 0 or above 1 and its coverage is poor. The Wilson score interval stays inside [0, 1] and has better coverage at the same cost. v1's 4% containment is exactly the kind of rate where that matters; its interval is [3.4%, 4.8%].

**25. The simulation plays the same personas through both flows. Does the independent-samples z-test still apply?**
It applies but is conservative. With paired observations and positive correlation between the two arms, the independent-samples standard error is larger than the true standard error of the difference, so the p-value is too large rather than too small. A paired test (McNemar's) would be sharper; for a difference of 48 points on 3,000 calls the conclusion does not change.

**26. What is p < 0.01 protecting against, and what is it not protecting against?**
Against declaring a win on noise: with that threshold, a flow change with no real effect passes the containment check about one time in a hundred. It does not protect against a flow that is better on containment and worse on something the gate does not measure, which is why the gate also checks abandonment and error outcomes, and why transfer reasons by queue and the per-node funnel are in the report.

**27. What would you look at in the per-node funnel after a release?**
Where calls leave: the nodes with the highest abandonment, menus with a high invalid-input rate (a sign the prompt wording or the option set is confusing), collect nodes with many retries, and action nodes with low success rates. In v2 the main menu's invalid-input rate is 5.6% and the after-balance menu's is 0.4%, which says the longer menu is where callers struggle.

## Mapping to real platforms

**28. How does this engine relate to real contact-center flow definitions?**
Commercial contact-center platforms express call flows as directed graphs of typed blocks (play prompt, get input, check attribute, invoke a function, transfer to queue, disconnect) stored in a declarative format, and VoiceXML-style systems express the same ideas as forms with fields, grammars, event handlers for noinput and nomatch, and submit targets. This project's node types line up with those blocks one to one, and its `noinput` and `nomatch` handling is the `timeout_prompt` and `invalid_prompt` pair with `max_retries`. The engine here is deliberately simpler and does not claim to match any vendor's exact semantics.

**29. Where would real speech recognition plug in?**
Between the audio and `match_option`: a recognizer turns audio into a transcript with a confidence score, and the matcher turns the transcript into an option. The one addition a real system needs is a confidence threshold, so a low-confidence transcript is treated like an ambiguous match and re-prompted or confirmed. `SpeechMatch` in `ivr/speech.py` already carries per-option scores for that purpose.

**30. What would you build next?**
In order: a paired test for the A/B comparison; a confidence model for speech so personas can produce noisy transcripts; barge-in, so a caller who presses a key during a prompt is charged less time; a per-node abandonment report in the CLI; backend failures and timeouts so `on_failure` paths are exercised under infrastructure trouble, not just business rules; and a flow diff tool that shows which scripted cases and which simulated paths a change touches. Each of these is a contained change because flows are data and every metric is computed from the event log.
