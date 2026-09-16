Below are three specifications, and nothing else:

1. An AOAS — the specification of one agent: a customer support agent for a clothing store.
2. The AHC — a catalog of capabilities an agent harness must have, plus the ports it declares.
3. The AAC — a catalog of assurance obligations the agent must be shown to meet.

You are going to implement this agent in Python, from these specifications alone. Before writing any code, **describe the module structure you would build**. Do not write code.

Give:

1. **The top-level packages or modules**, as a tree. For each, one or two sentences on what it owns, and the spec identifiers (AOAS sections, AHC-xxxx, AAC-xxxx, port names) that make it necessary.
2. **The dependency direction.** Which modules may import which. Draw it as layers if that is how you would enforce it.
3. **The interfaces (seams)** you would declare, meaning the things with more than one implementation (for example a real one and a test double), and why each is a seam.
4. **The entrypoint.** What one unit of work is, and what function or object handles it.
5. **Where the specifications did not decide the structure for you.** List every place where you picked a split, a boundary or a shape that the specs do not require, and say what you picked and why. Be specific. This section matters most.

Be concrete and name things. Base your answer only on what is in the specifications below, and say where you are inferring.

