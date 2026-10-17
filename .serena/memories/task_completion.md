# Definition of Done / Task Completion

When a coding task is completed:
1. Run full test suite: `.\venv\Scripts\pytest test_system.py -q`. All 316+ tests must pass green.
2. Confirm no test databases leaked on disk (`test_*.db*`).
3. If changes affect production behavior or schema, check git status (`git status`).
4. Keep commit messages concise, descriptive, and conventional (`feat: ...`, `fix: ...`, `chore: ...`).
5. Ensure synchronization with AWS production instance if live deployment is requested.
