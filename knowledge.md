# Apache Superset Conventions

## Project Structure

- `superset/` - Main application code
- `tests/` - Test suite
- `superset/connectors/` - Database connector implementations
- `superset/sqla/` - SQLAlchemy models and utilities

## SQL Query Construction

Superset uses SQLAlchemy for database interactions. When constructing queries:

1. **Use parameterized queries** - Never concatenate user input into SQL strings
2. **Leverage SQLAlchemy's text() with bind parameters** - `text("SELECT * FROM table WHERE id = :id").bind(id=user_input)`
3. **Use ORM methods when possible** - `session.query(Model).filter(Model.id == user_input)`

## Common Patterns

### Safe Query Construction

```python
from sqlalchemy import text

# GOOD - parameterized
query = text("SELECT * FROM users WHERE id = :user_id")
result = session.execute(query, {"user_id": user_input})

# BAD - string concatenation (what Bandit B608 flags)
query = "SELECT * FROM users WHERE id = " + user_input
```

### Using SQLAlchemy ORM

```python
# GOOD - ORM filter
result = session.query(User).filter(User.id == user_id).all()

# GOOD - ORM with parameters
result = session.query(User).filter(text("id = :user_id")).params(user_id=user_id).all()
```

## Testing Conventions

- Tests use pytest framework
- Unit tests are in `tests/unit_tests/`
- Integration tests use test fixtures
- Database tests use rollback fixtures to avoid side effects

## Code Style

- Follow PEP 8
- Use type hints where practical
- Add docstrings to functions and classes
- Keep functions focused and modular

## Common Security Issues in Superset

1. **SQL Injection (B608)** - String concatenation in query construction
2. **Path Traversal** - Unvalidated file paths
3. **XSS** - Unescaped user input in templates
4. **Authentication bypass** - Weak session management

## File-Specific Notes

### `superset/connectors/sqla/models.py`

This file contains SQLAlchemy models for SQL connectors. When fixing SQL injection here:
- Check for `query = "..." + variable` patterns
- Replace with `text("... :param").bind(param=variable)`
- Test with the connector's test suite

### `superset/charts/`

Chart visualization code often constructs queries dynamically. Ensure:
- User-provided filters are parameterized
- Column names are validated against allowlists
- Sorting parameters are sanitized

## Remediation Strategy

When fixing a Bandit finding:

1. **Understand the context** - What is this code trying to do?
2. **Find the safe equivalent** - How can this be done with parameters?
3. **Test thoroughly** - Run the relevant test subset
4. **Verify with Bandit** - The finding must disappear from scan output
5. **Check for regressions** - Ensure functionality is preserved

## Working with Superset's Database Layer

Superset has a complex database abstraction layer. When modifying queries:

- Check if a higher-level API exists (e.g., `get_dataframe()`)
- Respect the existing abstraction boundaries
- Don't bypass the ORM unless absolutely necessary
- Test with multiple database backends if possible