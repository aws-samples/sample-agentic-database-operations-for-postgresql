# Product rules

The product's invariants live in `docs/PRODUCT-RULES.md`. Read that file before changing
behaviour a user can observe, and treat it as the checklist for every review: a change that
breaks one of its rules is a bug, however reasonable the code looks on its own.

#[[file:docs/PRODUCT-RULES.md]]

Changing a rule is a product decision. Change `docs/PRODUCT-RULES.md` first, in the same
commit as the code, so the doc and the behaviour never disagree. The golden harness
(`tests/golden/`) asserts these rules at every step of every scenario.
