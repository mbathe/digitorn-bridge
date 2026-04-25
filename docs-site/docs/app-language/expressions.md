---

id: expressions
title: Expressions
sidebar_position: 11
format: md
---


# Expressions

The LLMOS expression engine provides template syntax for dynamic values throughout your app YAML.

## Template Syntax

Expressions are enclosed in double curly braces: `{{expression}}`.

```yaml
agent:
  system_prompt: "Workspace: {{workspace}}"

flow:
  - action: filesystem.read_file
    params:
      path: "{{workspace}}/{{filename}}"
```

## Namespaces

Expressions can access several namespaces:

| Namespace | Description | Example |
|-----------|-------------|---------|
| `result` | Step results | `{{result.step_id.field}}` |
| `trigger` | Trigger data | `{{trigger.input}}` |
| `memory` | Memory values | `{{memory.key}}` |
| `env` | Environment variables | `{{env.HOME}}` |
| `secret` | Secrets | `{{secret.API_KEY}}` |
| `agent` | Agent state | `{{agent.no_tool_calls}}` |
| `run` | Run metadata | `{{run.id}}` |
| `app` | App metadata | `{{app.name}}` |
| `loop` | Loop context | `{{loop.iteration}}` |
| `macro` | Macro parameters | `{{macro.param_name}}` |
| `context` | Extra context | `{{context.key}}` |
| (variables) | User variables | `{{workspace}}` |

### Variable Resolution Order

1. Direct namespace match (`result`, `trigger`, `env`, etc.)
2. User-defined `variables:` block
3. Extra context

## Dot Access

Navigate nested structures with dot notation:

```yaml
# Access step result fields
"{{result.read_file.content}}"

# Deep nesting
"{{result.api_call.response.data.items}}"

# Dict keys
"{{trigger.body.pull_request.title}}"
```

## Array Indexing

Access array elements with bracket notation:

```yaml
"{{result.list_files[0]}}"
"{{result.search.items[2].name}}"
```

## Optional Chaining

Use `?.` to safely access nested fields that may not exist:

```yaml
"{{result.api_call?.response?.data}}"
```

Returns `null` instead of throwing an error if any segment is missing.

## Null Coalescing

Use `??` to provide fallback values:

```yaml
"{{result.search?.results ?? 'No results found'}}"
"{{memory.last_review ?? 'No previous review'}}"
"{{env.CUSTOM_MODEL ?? 'claude-sonnet-4-20250514'}}"
```

## Comparison Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `==` | Equals | `{{result.code == 0}}` |
| `!=` | Not equals | `{{status != 'failed'}}` |
| `>` | Greater than | `{{result.count > 10}}` |
| `<` | Less than | `{{score < 0.5}}` |
| `>=` | Greater or equal | `{{progress >= 1.0}}` |
| `<=` | Less or equal | `{{attempts <= 3}}` |

Comparisons return boolean values. Numbers are compared numerically, strings lexicographically.

## Logical Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `and` | Logical AND | `{{ready and confirmed}}` |
| `or` | Logical OR | `{{error or timeout}}` |
| `not` | Logical NOT | `{{not result.failed}}` |

```yaml
branch:
  "on": "{{result.tests.passed and result.lint.clean}}"
  cases:
    "true":
      - agent: default
        input: "All checks passed!"
```

## Filters

Filters transform values using the pipe syntax: `{{value | filter}}`.

### String Filters

| Filter | Description | Example | Output |
|--------|-------------|---------|--------|
| `upper` | Uppercase | `{{"hello" \| upper}}` | `HELLO` |
| `lower` | Lowercase | `{{"HELLO" \| lower}}` | `hello` |
| `trim` | Strip whitespace | `{{text \| trim}}` | Trimmed text |
| `truncate(n)` | Truncate to n chars | `{{text \| truncate(100)}}` | First 100 chars + `...` |
| `replace(a, b)` | Replace substring | `{{path \| replace('/', '-')}}` | Modified string |
| `split(sep)` | Split into array | `{{"a,b,c" \| split(',')}}` | `["a", "b", "c"]` |
| `matches(regex)` | Regex match | `{{name \| matches('^test_')}}` | `true`/`false` |
| `startswith(str)` | Starts with | `{{name \| startswith('test_')}}` | `true`/`false` |
| `endswith(str)` | Ends with | `{{file \| endswith('.py')}}` | `true`/`false` |

### Array Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `first` | First element | `{{items \| first}}` |
| `last` | Last element | `{{items \| last}}` |
| `count` | Length | `{{items \| count}}` |
| `join(sep)` | Join into string | `{{items \| join(', ')}}` |
| `slice(start, end)` | Slice array | `{{items \| slice(0, 5)}}` |
| `sort` | Sort | `{{items \| sort}}` |
| `sort(field)` | Sort by field | `{{items \| sort('name')}}` |
| `unique` | Remove duplicates | `{{items \| unique}}` |
| `filter(pattern)` | Filter by glob | `{{files \| filter('*.py')}}` |
| `filter(field)` | Filter by truthy field | `{{items \| filter('active')}}` |
| `map(field)` | Extract field | `{{items \| map('name')}}` |

### Data Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `json` | Serialize to JSON | `{{data \| json}}` |
| `parse_json` | Parse JSON string | `{{text \| parse_json}}` |
| `default(val)` | Default if null/empty | `{{name \| default('unknown')}}` |
| `required` | Error if null | `{{config \| required}}` |

### Path Filters

| Filter | Description | Example | Output |
|--------|-------------|---------|--------|
| `basename` | File name | `{{path \| basename}}` | `main.py` |
| `dirname` | Directory | `{{path \| dirname}}` | `/src` |

### Number Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `round(n)` | Round to n decimals | `{{score \| round(2)}}` |
| `abs` | Absolute value | `{{diff \| abs}}` |

### Formatting Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `descriptions` | Format list as descriptions | `{{tools \| descriptions}}` |

## Filter Chaining

Chain multiple filters:

```yaml
"{{result.files | filter('*.py') | sort | join('\n')}}"
"{{result.search.items | map('title') | unique | first}}"
"{{name | lower | replace(' ', '-') | truncate(50)}}"
```

## Type Preservation

When the entire string is a single expression, the result type is preserved:

```yaml
# Returns integer, not string
count: "{{result.items | count}}"

# Returns boolean
ready: "{{result.tests.passed}}"

# Returns array
files: "{{result.search.items}}"
```

When mixed with text, the result is always a string:

```yaml
# Always a string (interpolation)
message: "Found {{result.items | count}} items"
```

## Literals

Use literals in expressions:

```yaml
"{{true}}"                  # Boolean true
"{{false}}"                 # Boolean false
"{{null}}"                  # Null
"{{42}}"                    # Integer
"{{3.14}}"                  # Float
"{{'hello'}}"               # String (single quotes)
```

## Secrets

The `secret` namespace provides access to encrypted secrets stored per-application in the identity database. Secrets are resolved at runtime and never exposed in logs, API responses, or YAML output.

### Storing Secrets

```bash
# CLI
llmos-bridge app secret set <app-name> MY_SECRET "secret-value"

# API
PUT /applications/{app_id}/secrets/MY_SECRET
Content-Type: application/json
{"value": "secret-value"}
```

You can also manage secrets from the Dashboard: **Applications > Select app > Secrets > Add Secret**.

### Using Secrets

Use `{{secret.KEY_NAME}}` anywhere in your YAML:

```yaml
# LLM provider API key
brain:
  provider: google
  model: gemini-2.0-flash
  config:
    api_key: "{{secret.GOOGLE_API_KEY}}"

# System prompt
agent:
  system_prompt: |
    Use this internal API token: {{secret.INTERNAL_TOKEN}}

# Variables
variables:
  db_password: "{{secret.DB_PASSWORD}}"

# Flow step parameters
flow:
  - action: api_http.http_post
    params:
      url: "https://api.example.com/data"
      headers:
        Authorization: "Bearer {{secret.API_TOKEN}}"

# Trigger webhook validation
triggers:
  - type: webhook
    auth:
      secret: "{{secret.WEBHOOK_SECRET}}"
```

Secrets are resolved everywhere the expression engine is used: `brain.config`, `system_prompt`, `variables`, `constraints`, flow steps, and triggers.

## Usage in Different Contexts

### System Prompt

```yaml
agent:
  system_prompt: |
    Workspace: {{workspace}}
    User: {{env.USER}}
    Previous context: {{memory.last_session ?? 'First session'}}
```

### Flow Parameters

```yaml
flow:
  - action: filesystem.read_file
    params:
      path: "{{workspace}}/{{result.selected_file}}"
```

### Branch Conditions

```yaml
- branch:
    "on": "{{result.test.exit_code}}"
    cases:
      "0": [...]
      "1": [...]
```

### Loop Conditions

```yaml
- loop:
    until: "{{result.check.status == 'ready' or loop.iteration >= 10}}"
    body: [...]
```

### Trigger Filters

```yaml
triggers:
  - type: webhook
    filters:
      - "{{trigger.body.action == 'opened'}}"
      - "{{trigger.body.repository.private == false}}"
```

## Common Gotchas

### Single Block vs Multiple Blocks

Logical operators (`and`, `or`, `not`) **only work inside a single `{{...}}` block**. If you split them across blocks, the result is string concatenation, not logic:

```yaml
# ✅ CORRECT — single block, logical evaluation
when: "{{params.name | endswith('.py') or params.name | endswith('.ts')}}"

# ❌ WRONG — two blocks, becomes string "true or false" (always truthy!)
when: "{{params.name | endswith('.py')}} or {{params.name | endswith('.ts')}}"
```

### List Parameters Need `join` Before String Filters

Some module actions accept **list** parameters (e.g., `os_exec.run_command` takes `command` as a list like `['git', 'push']`). String filters like `startswith`, `endswith`, and `matches` operate on strings. Use `| join(' ')` to convert a list to a string first:

```yaml
# ✅ CORRECT — join list to string, then check prefix
when: "{{params.command | join(' ') | startswith('git push')}}"

# ❌ WRONG — startswith on a list throws an error
when: "{{params.command | startswith('git push')}}"
```

This pattern is especially important in `capabilities.approval_required` and `capabilities.deny` rules:

```yaml
capabilities:
  approval_required:
    - module: os_exec
      action: run_command
      when: "{{params.command | join(' ') | startswith('git push') or params.command | join(' ') | startswith('git reset')}}"
      message: "Approve destructive git operation?"
```

### Error Behavior in `when:` Conditions

When a `when:` expression fails to evaluate (e.g., accessing a non-existent field, calling a filter on the wrong type):

- **Deny rules** (`capabilities.deny`): Errors default to **`true`** (fail-closed — the action is denied). This is the safe default.
- **Approval rules** (`capabilities.approval_required`): Errors default to **`false`** (fail-open — no approval needed). This prevents broken conditions from blocking every action.

This means a broken `when:` expression on a deny rule will deny everything (safe), while a broken `when:` on an approval rule will approve everything (lenient). Always test your expressions with `llmos app validate`.
