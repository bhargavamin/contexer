"""Benchmark task corpus.

Each task:
  id            unique slug
  project_files {relative_path: contents} seeded into the temp repo
  seed_decisions list of {"content": str, "subtype": str} written to the WARM store
  prompt        the identical instruction (the "paste" arm prepends the decisions to it)
  rubric        criteria text handed to the blind LLM judge
  kind          "high_signal" (a seeded decision should change the output) | "neutral"

CRITICAL for high_signal tasks: the seeded decision must NOT be inferable from the visible
project files, and should contradict Claude's default behavior. Otherwise the "no memory" arm
guesses it anyway and the benchmark measures only overhead. So:
  - the project files contain no example of the rule, and
  - the rule is a deliberate, non-default choice (a bare-string default would NOT satisfy it).
Only the warm arm (recalls it) and the paste arm (told it) can satisfy these; cold should fail.
"""

CORPUS = [
    {
        "id": "error-shape",
        "kind": "high_signal",
        "project_files": {
            # No error example anywhere — cold has nothing to copy.
            "app.py": (
                '"""User store."""\n\n'
                "users = {1: {'name': 'ada'}}\n\n"
                "def get_user(uid):\n"
                "    return users.get(uid)\n"
            ),
        },
        "seed_decisions": [
            {
                "content": (
                    "Constraint: every error returned by this project's functions must be a JSON "
                    "object of EXACTLY this shape: {\"error\": {\"code\": <string>, \"message\": "
                    "<string>}}. Never return None, never raise, never return a bare string, never "
                    "a top-level 'error' string — always that nested error object."
                ),
                "subtype": "constraint",
            }
        ],
        "prompt": (
            "Add a delete_user(uid) function to app.py that removes a user. Handle the case where "
            "the user does not exist. Edit app.py directly."
        ),
        "rubric": (
            "decision_adherence = 1.0 ONLY if the not-found path returns the nested object "
            "{\"error\": {\"code\": ..., \"message\": ...}}. Returning None, raising, a bare string, "
            "or a top-level 'error' string = 0.0. task_correctness = 1.0 if delete_user removes an "
            "existing user and handles the missing case at all. conflict_with_stored = true if the "
            "not-found path does anything other than the nested error object."
        ),
    },
    {
        "id": "naming-convention",
        "kind": "high_signal",
        "project_files": {
            # No function present — nothing reveals the naming rule.
            "math_utils.py": '"""Math helpers."""\n\nPI = 3.14159\n',
        },
        "seed_decisions": [
            {
                "content": (
                    "Convention: every public function defined anywhere in this project MUST be named "
                    "with a 'cx_' prefix (for example cx_add, cx_load). This is a hard, non-negotiable "
                    "naming rule unique to this codebase."
                ),
                "subtype": "convention",
            }
        ],
        "prompt": (
            "Add a function to math_utils.py that multiplies two numbers a and b and returns the "
            "result. Edit math_utils.py directly."
        ),
        "rubric": (
            "decision_adherence = 1.0 ONLY if the new function's name starts with the 'cx_' prefix "
            "(e.g. cx_multiply). Any name without that prefix (multiply, mul, product) = 0.0. "
            "task_correctness = 1.0 if the function correctly multiplies a and b. "
            "conflict_with_stored = true if the function is defined without the cx_ prefix."
        ),
    },
    {
        "id": "string-reverse-neutral",
        "kind": "neutral",
        "project_files": {
            "util.py": "def shout(s):\n    return s.upper()\n",
        },
        "seed_decisions": [
            {
                "content": (
                    "Constraint: every error must return {\"error\": {\"code\", \"message\"}}; every "
                    "public function must use a 'cx_' prefix."
                ),
                "subtype": "constraint",
            }
        ],
        "prompt": "Add a reverse(s) function to util.py that returns the string reversed. Edit util.py.",
        "rubric": (
            "task_correctness = 1.0 if reverse('abc') would return 'cba'. decision_adherence is "
            "vacuously 1.0 — no stored decision meaningfully applies to reversing a string (the "
            "cx_ rule is arguably relevant but this task is the neutral control; do not penalize "
            "naming here). conflict_with_stored = false. This task measures Contexer's pure token "
            "overhead when memory does not help."
        ),
    },
]
