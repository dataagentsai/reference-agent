"""The parts of a turn every agent's entrypoint uses.

An agent's turn — which routes it has, what each answers, what it asks before
acting — is its own orchestration (the reference agent's is
`support_agent.entrypoint`). What it is built from is here: handing a
conversation to a person (`handoff`), recording a turn as it opens and ends and
screening its reply (`ending`), and persisting what the turn changed (`persist`).
"""
