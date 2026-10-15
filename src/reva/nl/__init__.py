"""Natural language -> draft ScenarioSpec (V2 step 2).

The LLM only drafts: it turns a sentence into a draft spec, a restatement and a
list of unsupported parts. Every draft is re-validated server-side by the same
validator as hand-written specs and runs only when the user submits it to
``POST /v1/scenarios`` after review. See ``docs/methodology.md`` (Natural-language
drafts) and ``docs/dev/v2_nlc.md``.
"""
