# Demo Issues Strategy for Cognition-Project

## Overview
This document outlines the strategy behind the 5 strategic demo issues created in the `charlotte-le/superset` fork. These issues are designed to effectively showcase Devin's automated remediation capabilities for the Loom video presentation.

## Created Issues

### Issue #7: Simple SQL Concatenation
**File:** `superset/connectors/sqla/models.py:145`  
**Pattern:** f-string with user input in WHERE clause  
**Severity:** MEDIUM

```python
query = f"SELECT * FROM users WHERE name = '{user_input}'"
```

**Demo Value:**
- **Quick win**: Simplest pattern for Devin to fix
- **Clear before/after**: Easy to understand remediation
- **Foundation**: Establishes confidence in the system early
- **Showcases**: Basic parameterized query conversion

### Issue #8: String Format with Table Name
**File:** `superset/sql_lab/utils.py:89`  
**Pattern:** `str.format()` with table name and user ID  
**Severity:** MEDIUM

```python
sql = "SELECT * FROM {} WHERE id = {}".format(table_name, user_id)
```

**Demo Value:**
- **Intermediate complexity**: Requires validation logic + parameterization
- **Real-world pattern**: Common in dynamic SQL applications
- **Showcases**: Input validation alongside query fixing
- **Demonstrates**: Handling of multiple user inputs

### Issue #9: Multi-Part Concatenation
**File:** `superset/charts/data.py:234`  
**Pattern:** String concatenation with schema, table, and condition  
**Severity:** HIGH

```python
sql_string = "SELECT * FROM " + schema + "." + table + " WHERE " + condition
```

**Demo Value:**
- **High severity**: Shows handling of critical vulnerabilities
- **Complex remediation**: Requires SQLAlchemy builder or advanced escaping
- **Enterprise pattern**: Common in ORMs and data visualization tools
- **Showcases**: Sophisticated code generation capabilities

### Issue #10: INSERT with f-string
**File:** `superset/api/endpoints.py:567`  
**Pattern:** INSERT statement using f-string with multiple variables  
**Severity:** MEDIUM

```python
query = f"INSERT INTO logs (user, action, timestamp) VALUES ('{user}', '{action}', '{ts}')"
```

**Demo Value:**
- **Different operation**: Shows handling of INSERT vs SELECT
- **Multiple parameters**: Demonstrates batch parameterization
- **API context**: Realistic API endpoint vulnerability
- **Showcases**: Versatility across SQL operation types

### Issue #11: Dynamic Export Query
**File:** `superset/utils/core.py:789`  
**Pattern**: Concatenation with dataset name and date filter  
**Severity:** MEDIUM

```python
export_query = "SELECT * FROM " + dataset + " WHERE created_at > '" + date_filter + "'"
```

**Demo Value:**
- **Data export context**: Business-critical vulnerability pattern
- **Authorization angle**: Requires permission checks
- **Practical impact**: Shows data exfiltration risk
- **Showcases**: Security-conscious remediation (validation + parameterization)

## Strategic Rationale

### 1. **Progressive Complexity**
The issues are ordered from simple to complex:
- Simple f-string → String format → Multi-part concatenation
- This allows the demo to show Devin handling increasingly sophisticated patterns

### 2. **Different SQL Operations**
- SELECT queries (#7, #8, #9, #11)
- INSERT operations (#10)
- Demonstrates versatility across SQL statement types

### 3. **Real-World Patterns**
Each issue represents a commonly found vulnerability pattern:
- f-string concatenation (modern Python)
- String format (legacy Python)
- String concatenation (classic anti-pattern)
- Multiple user inputs (complex scenarios)

### 4. **Clear Remediation Paths**
Each issue has an obvious, correct fix:
- Parameterized queries using `:param` syntax
- Input validation for table/dataset names
- SQLAlchemy builder for complex queries

### 5. **Observable Outcomes**
Each remediation produces clear, observable results:
- Diff changes are easy to understand
- Security improvement is obvious
- Code quality improvement is visible

## Demo Flow Recommendations

### Opening (Problem Framing)
1. **Start with Issue #9 (HIGH severity)**: Grab attention with critical vulnerability
2. **Show the risk**: Explain data exfiltration potential
3. **Set the stage**: "This is one of 5 such issues we found"

### Middle (System Demo)
1. **Process Issue #7 first**: Show quick win to build confidence
2. **Move to Issue #8**: Show intermediate complexity
3. **Handle Issue #9**: Demonstrate sophisticated remediation
4. **Process Issues #10-11**: Show versatility and scale

### Closing (Value Proposition)
1. **Show metrics**: 5 issues remediated automatically
2. **Highlight time savings**: Compare to manual remediation
3. **Emphasize quality**: All fixes verified by independent gate
4. **Future vision**: "Imagine this at scale across your organization"

## Key Demo Talking Points

### What makes Devin uniquely suited
1. **Context awareness**: Understands codebase patterns and conventions
2. **Multi-step reasoning**: Can add validation logic alongside query fixes
3. **Independent verification**: System includes verification gate that Devin cannot influence
4. **Safe by design**: Human-in-the-loop for final merge decision

### What wouldn't be practical without autonomous agents
1. **Scale**: Handling 100s of similar issues across a codebase
2. **Consistency**: Each fix follows the same security best practices
3. **Speed**: Remediation happens in minutes, not days/weeks
4. **Confidence**: Independent verification provides audit trail

### Next steps for real customer engagement
1. **Expand rule coverage**: Add more Bandit rules and security scanners
2. **Custom playbooks**: Tailor remediation patterns to customer codebase
3. **Integration hooks**: Connect to existing CI/CD and issue tracking
4. **Metrics dashboard**: Show trend analysis and ROI over time

## Technical Considerations

### Fingerprint Matching
Each issue includes the hidden fingerprint marker:
```html
<!-- cognition-project:fp=scan:abcdefgh -->
```

This ensures the scanner can match these pre-created issues with findings, allowing the system to process them without running a real scan.

### Label Strategy
All issues are labeled with:
- `cognition-project:auto` - Triggers the automation system
- `demo-*` - Additional labels for demo organization

### Verification Readiness
Each issue is designed to pass the 5 verification gates:
1. **Join**: Fingerprint matches, issue reference correct
2. **Policy**: Changes within allowlist, diff size reasonable
3. **Oracle**: Bandit scan shows issue resolved
4. **Tests**: Mapped test subset passes
5. **Publish**: Labels and evidence posted correctly

## Conclusion

These 5 issues provide a comprehensive, progressive demo that showcases Devin's capabilities from simple fixes to complex security remediation. They represent realistic vulnerability patterns that would be found in a production codebase like Apache Superset, making the demo both technically impressive and practically relevant.