---

id: powerpoint
title: PowerPoint Module
sidebar_position: 16
format: md
---




# powerpoint

Create and manipulate Microsoft PowerPoint presentations (.pptx). Full support for slides, text boxes, shapes, charts, tables, images, transitions, themes, and export.

| Property | Value |
|----------|-------|
| **Module ID** | `powerpoint` |
| **Version** | `1.0.0` |
| **Type** | document |
| **Platforms** | All |
| **Dependencies** | `python-pptx` |
| **Declared Permissions** | `filesystem.write` |

---


## Actions (25)

### Presentation Lifecycle

| Action | Description |
|--------|-------------|
| `create_presentation` | Create new presentation |
| `open_presentation` | Open existing .pptx |
| `save_presentation` | Save with optional output path |
| `get_presentation_info` | Slide count, dimensions |

### Slide Management

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_slide` | Add slide with layout | `layout` (title, content, blank, etc.) |
| `delete_slide` | Delete by index | `slide_index` |
| `duplicate_slide` | Duplicate slide | `slide_index` |
| `reorder_slide` | Move slide | `from_index`, `to_index` |
| `list_slides` | List all slides | |
| `read_slide` | Get slide content | `slide_index` |
| `set_slide_layout` | Change layout | `slide_index`, `layout` |
| `set_slide_title` | Set title text | `slide_index`, `title` |

### Content

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_text_box` | Add text box | `slide_index`, `text`, `left`, `top`, `width`, `height` |
| `add_slide_notes` | Add speaker notes | `slide_index`, `notes` |
| `add_shape` | Add shape | `slide_index`, `shape_type`, `left`, `top`, `width`, `height` |
| `format_shape` | Format shape | `shape_id`, `fill_color`, `line_color`, `line_width` |
| `add_image` | Insert image | `slide_index`, `image_path`, position/size |
| `add_table` | Insert table | `slide_index`, `rows`, `cols`, `data` |
| `format_table_cell` | Format cell | `table_id`, `row`, `col`, `fill`, `text`, `borders` |

Shape types: `rectangle`, `ellipse`, `triangle`, `arrow`, `star`, `pentagon`, `hexagon`, `diamond`, `rounded_rectangle`.

### Visual

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `set_slide_background` | Background color/image | `slide_index`, `color` or `image_path` |
| `apply_theme` | Apply theme | `theme_path` |
| `add_transition` | Slide transition | `slide_index`, `transition_type`, `duration` |

Transition types: `fade`, `push`, `wipe`, `split`, `reveal`, `random`.

### Charts

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_chart` | Insert chart | `slide_index`, `chart_type`, `data`, `title` |

Chart types: `bar`, `column`, `line`, `pie`, `doughnut`, `scatter`, `area`, `bubble`, `radar`.

### Export

| Action | Description |
|--------|-------------|
| `export_to_pdf` | Export to PDF (requires LibreOffice) |
| `export_slide_as_image` | Export single slide as PNG/JPG |

---


## Implementation Notes

- Presentation caching by resolved path
- Per-file threading locks
- Shape type mapping handles the mapping from string names to `python-pptx` MSO shape type constants
- Note: shape_type `"ellipse"` (not `"oval"`)
