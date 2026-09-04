# Insecure defaults audit — NOT RUN — required tool unavailable

`insecure-defaults@trailofbits` plugin is installed and its reference corpus is present
(`references/*.md`, 6 categories: debug-features, default-credentials, fail-open-security,
fallback-secrets, permissive-access, weak-crypto). The skill's own instructions require
invoking a `Workflow` tool (`insecure-defaults:audit-pipeline`, backed by
`workflows/audit.js`) to run the parallel-sweep-then-verify pipeline.

No `Workflow` tool exists in this session (confirmed via tool search — no match for
`Workflow` among available or deferred tools). Per the skill's own instructions, an
incomplete run is not a clean result, so no report was generated and no findings were
approximated by hand.

No output was generated or approximated for this pass, per the instruction not to
substitute my own judgement for a skill's output and present it as that skill's finding.
