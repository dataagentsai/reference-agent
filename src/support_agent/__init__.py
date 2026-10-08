"""The support agent: this shop's domain, composed on `agent_harness`.

Importing any part of it first declares the shop's vocabulary to the harness —
its intents, its order-id shape and status grammar, and the rules every policy
position runs by default — so no route, rule or check can run before they are
known, whichever module a caller happens to import first.
"""

from support_agent import contracts as contracts
