# Global Engineering Rules (Applied to All Repositories)

## 1. Architecture & Code Quality
- Separate pure business/calculation logic from UI components so core logic is easily unit-testable.
- Never leave magic numbers or hardcoded strings in logic; extract them into named constants.
- Prefer strict typing and explicit null/error handling over silent fallbacks or `any` / dynamic casts.

## 2. Safety & Scope Guardrails
- Keep diffs minimal and focused strictly on the requested issue or single improvement target.
- NEVER modify signing configs, API keys, `.github/workflows/`, or upgrade major dependencies unless explicitly instructed.
- Do not alter existing public API signatures or persisted data schemas without backward compatibility.

## 3. Verification & Output
- Always run the repository's existing linter, typechecker, and unit tests before pushing.
- Write clear, concise commit messages and PR summaries in natural Japanese.