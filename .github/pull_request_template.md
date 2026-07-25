## Description
Provide a clear description of the changes.

---

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation
- [ ] Test
- [ ] Breaking change

---

## Testing
- [ ] Unit tests added/updated
- [ ] All tests passing (`make test`)
- [ ] Manual testing performed (if required)

---

## Checklist

### Code Quality
- [ ] Code is properly formatted (`make format` — ruff format + black)
- [ ] Linting passed (`make lint` — ruff)
- [ ] Type checking passed (`make lint` — mypy --strict)
- [ ] No debug logs / commented code

---

### Observability (IMPORTANT for your system)
- [ ] Logs include `request_id` / `correlation_id` (via `fastapicommon.logging.get_logger`, not stdlib `logging`)
- [ ] Errors use a `PlatformException` subclass, not a bare `HTTPException`
- [ ] Middleware ordering preserved if middleware changed (see `ARCHITECTURE.md#execution-model`)

---

### Security
- [ ] No secrets hardcoded
- [ ] Input validation handled
- [ ] RBAC (`RequireRole` / `RequireAnyRole` / `RequireAllRoles`) checked (if endpoint changes)

---

### Documentation
- [ ] README updated (if needed)
- [ ] `ARCHITECTURE.md` / ADRs updated (if a cross-cutting standard changed)

---

## 🔗 Related Issue
Closes #<issue-id>

---

## Deployment Notes
Mention anything important for deployment.
