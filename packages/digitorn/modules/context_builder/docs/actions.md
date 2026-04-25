# Actions

## search_tools

Search for tools by keyword query. Returns ranked results with relevance scores.

### Parameters

| Param       | Type    | Required | Description                    |
|-------------|---------|----------|--------------------------------|
| query       | string  | yes      | Natural-language search query  |
| max_results | integer | no       | Max results (default: 5)       |

---

## get_tool

Get the full JSON Schema and metadata for a specific tool.

### Parameters

| Param | Type   | Required | Description                       |
|-------|--------|----------|-----------------------------------|
| name  | string | yes      | Tool FQN in `module.action` format |

---

## execute_tool

Execute a tool with parameters, subject to security policy.

### Parameters

| Param  | Type   | Required | Description                       |
|--------|--------|----------|-----------------------------------|
| name   | string | yes      | Tool FQN in `module.action` format |
| params | object | no       | Parameters for the tool            |

---

## list_categories

List all available tool categories (modules) with summaries.

### Parameters

None.

---

## browse_category

Browse all tools in a specific category with pagination.

### Parameters

| Param    | Type    | Required | Description               |
|----------|---------|----------|---------------------------|
| category | string  | yes      | Category (module) ID      |
| page     | integer | no       | Page number (default: 1)  |

