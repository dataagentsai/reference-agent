"""The harness, apart from any agent that uses it.

Everything here is mechanism: no clothing shop, no order, no refund threshold.
An agent installs this package and writes its own domain — intents, rules,
wording, pages — and hands those values in at its composition root. The import
contract in the workspace root holds the line: nothing here may import an agent.
"""
