# C++ Reference Writing Guide

The `cpp-reference` section explains C++ language and standard-library behavior for working software engineers. It is not an interview-preparation section; interview-specific material belongs in `leetcode-patterns`.

## Audience and Tone

- Write for readers ranging from SDE1 to principal engineer without labelling sections by seniority.
- Use neutral, technical language. Prefer “default guidance,” “design consideration,” and “failure mode” over “beginner rule,” “interview trick,” or “principal-level answer.”
- Introduce the direct mental model first, then add the language-lawyer, performance, and system-design details that qualify it.
- Explain jargon when it first appears, but do not replace precise C++ terminology with analogy alone.

## Article Shape

A focused reference article should normally contain:

1. The problem the type or feature solves.
2. Its core invariant or mental model.
3. Canonical syntax and required headers.
4. Operation contracts and worked state transitions.
5. Complexity and allocation behavior.
6. Iterator, pointer, and reference invalidation where relevant.
7. Preconditions, edge cases, and common failure modes.
8. Selection or design trade-offs.
9. A practical checklist.
10. Links to the C++ working draft or another authoritative source.

Long articles may layer these sections progressively. A reader should be able to stop after the basic contract without being given an incorrect model.

## Operation Contracts

Do not present an unfamiliar call as an unexplained code fragment. For each important operation, state:

- **Syntax:** the representative call or overload family.
- **Receiver:** what the object before the dot represents.
- **Arguments:** the role and ownership of each argument.
- **Effect:** what changes in the receiver and in any source object.
- **Return:** the result and how absence or failure is represented.
- **Preconditions:** requirements such as non-emptiness, iterator provenance, ordering, or allocator compatibility.
- **Invalidation:** which iterators, pointers, and references remain valid.
- **Complexity:** including average, amortized, or worst-case qualifiers.
- **Version:** when the preferred syntax depends on a particular C++ standard.

Use a before-and-after example when argument direction or state movement is not obvious. `list::splice`, range erasure, bounds queries, and ownership transfer all benefit from an explicit state transition.

## Correctness Conventions

- Treat `[first, last)` as the standard range form and identify which container owns each iterator.
- Check lookup results against `end()` before dereferencing.
- State empty-container preconditions for `front`, `back`, `top`, and `pop` operations.
- Distinguish iterators from pointers and references when their invalidation rules differ.
- Describe ordered-container behavior in comparator terms before using shorthand such as `>=` or `>`.
- Qualify unordered-container complexity as average case and discuss rehashing when retained iterators matter.
- Prefer complete, compilable examples. If a fragment is intentionally partial, label its omitted context.

## Metadata and Review

Every post should provide `description`, `last_modified_at`, `toc`, and focused tags. Before publishing:

1. Search the section for interview or seniority-based framing.
2. Verify every code block is balanced and uses the declared C++ baseline.
3. Build the site with `bundle exec jekyll build`.
4. Confirm internal post links resolve and inspect the rendered tables and code blocks.
5. Recheck technical contracts against the C++ working draft or another authoritative source.
