---
name: response-safety
description: Enforces response constraints for generated code and explanations. Use when generating code or scripts, or when requirements are unclear and clarification is needed.
---

# Response Safety

## Core Rules

1. Do not use emoji characters in generated code, scripts, configuration files, or command examples.
2. If any user requirement is unclear, ambiguous, or conflicting, ask a clarification question first.
3. Do not guess, infer, or fabricate missing requirements when uncertainty materially affects correctness.
4. When creating a new function or method, add a concise comment above it that explains purpose/intent (not line-by-line behavior).

## Clarification Workflow

1. Detect ambiguous points (platform, path, version, naming, expected output).
2. Ask concise clarification questions before writing code.
3. Continue implementation only after the user confirms the unclear parts.

## Output Style

- Keep generated code and scripts plain and professional.
- Prefer deterministic defaults only when explicitly approved by the user.
- For newly added functions, prefer 1-2 line comments that explain why the function exists and when it is used.
