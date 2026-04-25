---

id: word
title: Word Module
sidebar_position: 15
format: md
---




# word

Create and manipulate Microsoft Word documents (.docx). Full support for paragraphs, tables, images, styles, headers, footers, and PDF export.

| Property | Value |
|----------|-------|
| **Module ID** | `word` |
| **Version** | `1.0.0` |
| **Type** | document |
| **Platforms** | All |
| **Dependencies** | `python-docx` |
| **Declared Permissions** | `filesystem.write` |

---


## Actions (30)

### Document Lifecycle

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `create_document` | Create new document | `path` |
| `open_document` | Open existing .docx | `path` |
| `save_document` | Save with optional output path | `path`, `output_path` |
| `set_document_properties` | Set title, author, subject, keywords | `path`, `title`, `author`, etc. |
| `get_document_meta` | Get metadata | `path` |
| `set_margins` | Top, bottom, left, right margins | `path`, `top`, `bottom`, `left`, `right` |
| `set_default_font` | Set default font | `path`, `font_name`, `font_size`, `font_color` |

### Content Reading

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `read_document` | Get full document text | `path` |
| `list_paragraphs` | List all paragraphs | `path` |
| `list_tables` | List all tables | `path` |
| `extract_text` | Extract text content | `path` |
| `count_words` | Word, paragraph, character count | `path` |

### Content Writing

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `write_paragraph` | Add paragraph with formatting | `path`, `text`, `style`, `alignment`, `font`, `size`, `bold`, `italic` |
| `format_text` | Character-level formatting | `path`, `paragraph_index`, `bold`, `italic`, `underline`, `color`, `highlight`, `font` |
| `apply_style` | Apply built-in or custom style | `path`, `paragraph_index`, `style` |
| `delete_paragraph` | Delete by index | `path`, `paragraph_index` |
| `insert_page_break` | Insert page break | `path` |
| `insert_section_break` | Insert section break | `path`, `break_type` |
| `insert_list` | Bulleted or numbered list | `path`, `items`, `list_type` |
| `find_replace` | Find and replace text | `path`, `find`, `replace` |

### Tables

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `insert_table` | Create table | `path`, `rows`, `cols`, `data` |
| `modify_table_cell` | Edit cell content/formatting | `path`, `table_index`, `row`, `col`, `text`, `bold` |
| `add_table_row` | Append row | `path`, `table_index`, `data` |

### Media and Links

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `insert_image` | Insert image | `path`, `image_path`, `width`, `height` |
| `insert_hyperlink` | Add hyperlink | `path`, `text`, `url` |
| `add_bookmark` | Add bookmark | `path`, `name`, `paragraph_index` |

### Structure

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_comment` | Add paragraph comment | `path`, `paragraph_index`, `text`, `author` |
| `insert_toc` | Insert table of contents | `path` |
| `add_header_footer` | Add header/footer | `path`, `header_text`, `footer_text` |

### Export

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `export_to_pdf` | Export to PDF | `path`, `output_path` |

---


## Implementation Notes

- Document caching by resolved path
- Per-file threading locks
- All I/O via `asyncio.to_thread()`
- Character formatting: bold, italic, underline, color, highlight, font name, font size
- Paragraph styles: Title, Heading 1-9, Normal, ListBullet, ListNumber, etc.
