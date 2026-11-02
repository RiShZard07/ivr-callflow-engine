"""ivr-callflow-engine: a small IVR self-service call-flow engine.

The package has five layers, each importable on its own:

- ``ivr.flow``      declarative call-flow definitions loaded from JSON
- ``ivr.validate``  static checks that catch broken flows before any call runs
- ``ivr.engine``    a deterministic interpreter with a simulated clock and event log
- ``ivr.testing``   scripted call-flow test cases, the release regression suite
- ``ivr.simulate`` and ``ivr.analytics``  synthetic callers and call-data metrics

Everything here is synthetic. There is no telephony, audio, or speech
recognition, and no real customer data of any kind.
"""

__version__ = "0.1.0"
