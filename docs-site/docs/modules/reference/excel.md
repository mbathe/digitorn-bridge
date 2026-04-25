---

id: excel
title: Excel Module
sidebar_position: 14
format: md
---




# excel

Comprehensive Excel spreadsheet manipulation. Create, read, write, format, chart, and export `.xlsx` files.

| Property | Value |
|----------|-------|
| **Module ID** | `excel` |
| **Version** | `1.0.0` |
| **Type** | document |
| **Platforms** | All |
| **Dependencies** | `openpyxl >= 3.1` |
| **Declared Permissions** | `filesystem.write` |

---


## Actions (42)

### Workbook Management

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `create_workbook` | Create new workbook | `path` |
| `open_workbook` | Open existing .xlsx | `path` |
| `close_workbook` | Close and release resources | `path` |
| `save_workbook` | Save with optional new path | `path`, `output_path` |
| `get_workbook_info` | Sheet count, metadata | `path` |

### Sheet Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `list_sheets` | List all sheet names | `path` |
| `create_sheet` | Create new sheet | `path`, `name`, `index` |
| `delete_sheet` | Delete sheet | `path`, `name` or `index` |
| `rename_sheet` | Rename sheet | `path`, `old_name`, `new_name` |
| `copy_sheet` | Copy sheet | `path`, `source`, `target` |
| `get_sheet_info` | Dimensions, frozen panes | `path`, `sheet` |
| `protect_sheet` | Password protect | `path`, `sheet`, `password` |
| `unprotect_sheet` | Remove protection | `path`, `sheet`, `password` |
| `set_page_setup` | Paper, orientation, margins | `path`, `sheet`, `orientation`, `paper_size` |

### Cell Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `read_cell` | Read single cell | `path`, `sheet`, `cell` |
| `write_cell` | Write single cell | `path`, `sheet`, `cell`, `value` |
| `read_range` | Read cell range | `path`, `sheet`, `range` |
| `write_range` | Write 2D array to range | `path`, `sheet`, `start_cell`, `data` |
| `copy_range` | Copy with formatting | `path`, `sheet`, `source_range`, `target_cell` |

### Row/Column Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `insert_rows` | Insert rows at index | `path`, `sheet`, `row`, `count` |
| `delete_rows` | Delete rows | `path`, `sheet`, `row`, `count` |
| `insert_columns` | Insert columns | `path`, `sheet`, `column`, `count` |
| `delete_columns` | Delete columns | `path`, `sheet`, `column`, `count` |
| `merge_cells` | Merge range | `path`, `sheet`, `range` |
| `unmerge_cells` | Unmerge range | `path`, `sheet`, `range` |
| `freeze_panes` | Freeze rows/columns | `path`, `sheet`, `cell` |
| `set_column_width` | Set width | `path`, `sheet`, `column`, `width` |
| `set_row_height` | Set height | `path`, `sheet`, `row`, `height` |

### Data Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `find_replace` | Find and replace text | `path`, `sheet`, `find`, `replace` |
| `remove_duplicates` | Remove duplicate rows | `path`, `sheet`, `columns` |
| `apply_formula` | Set Excel formula | `path`, `sheet`, `cell`, `formula` |
| `add_named_range` | Create named range | `path`, `name`, `range`, `sheet` |

### Formatting

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `format_range` | Font, fill, border, alignment, number format | `path`, `sheet`, `range`, `font`, `fill`, `border`, `alignment`, `number_format` |
| `apply_conditional_format` | Conditional formatting rules | `path`, `sheet`, `range`, `rule_type`, `formula`, `format` |
| `add_data_validation` | Data validation (list, date, number) | `path`, `sheet`, `range`, `type`, `values` |
| `add_autofilter` | Auto-filter on range | `path`, `sheet`, `range` |

### Charts and Media

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `create_chart` | Create chart | `path`, `sheet`, `chart_type`, `data_range`, `title` |
| `insert_image` | Insert image | `path`, `sheet`, `image_path`, `cell` |

Chart types: `bar`, `column`, `line`, `pie`, `scatter`, `area`, `bubble`, `radar`, `doughnut`.

### Comments

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_comment` | Add cell comment | `path`, `sheet`, `cell`, `text`, `author` |
| `delete_comment` | Remove comment | `path`, `sheet`, `cell` |

### Export

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `export_to_csv` | Export sheet to CSV | `path`, `sheet`, `output_path`, `delimiter` |
| `export_to_pdf` | Export to PDF | `path`, `output_path` |

PDF export requires LibreOffice to be installed on the system.

---


## Format Specification

The `format_range` action accepts a rich formatting specification:

```json
{
  "font": {
    "name": "Arial",
    "size": 12,
    "bold": true,
    "italic": false,
    "color": "FF0000"
  },
  "fill": {
    "color": "FFFF00",
    "pattern": "solid"
  },
  "border": {
    "style": "thin",
    "color": "000000"
  },
  "alignment": {
    "horizontal": "center",
    "vertical": "center",
    "wrap_text": true
  },
  "number_format": "#,##0.00"
}
```

---


## Implementation Notes

- Workbook caching: opened workbooks are cached by resolved path to avoid redundant I/O
- Per-file threading locks: concurrent access to the same file is serialized
- All I/O via `asyncio.to_thread()` wrapping `openpyxl` synchronous calls
- Alignment vertical values: `"center"`, `"top"`, `"bottom"` (not `"middle"`)
