---
id: context_builder-list-categories
title: "context_builder.list_categories (ListCategories)"
type: module-action
module: context_builder
action: list_categories
fqn: context_builder.list_categories
short_name: ListCategories
keywords: [context_builder, list_categories, listcategories, discovery, navigation]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.list_categories (ListCategories)

## Description
List all available tool categories (modules) with their descriptions and tool counts. Use this to get an overview of what's available.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [list_categories]
```

## Safety
- Risk level: **low**
