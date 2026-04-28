"""Notebook module tests - covers all 4 actions."""

from __future__ import annotations

import json
import pytest

from digitorn.modules.notebook.module import NotebookModule
from digitorn.modules.notebook.params import (
    ReadNotebookParams, EditCellParams, AddCellParams, DeleteCellParams,
)


def _make_notebook(path, cells=None):
    """Create a minimal .ipynb file."""
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python"}},
        "cells": cells or [
            {"cell_type": "code", "source": ["print('hello')"], "metadata": {}, "outputs": [], "execution_count": 1},
            {"cell_type": "markdown", "source": ["# Title"], "metadata": {}},
            {"cell_type": "code", "source": ["x = 42"], "metadata": {}, "outputs": [], "execution_count": 2},
        ],
    }
    path.write_text(json.dumps(nb))
    return path


@pytest.fixture
def nb():
    return NotebookModule()


class TestRead:
    @pytest.mark.asyncio
    async def test_read_all(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.read(ReadNotebookParams(path=str(f)))
        assert r.success
        assert r.data["total_cells"] == 3
        assert len(r.data["cells"]) == 3
        assert r.data["kernel"] == "Python 3"

    @pytest.mark.asyncio
    async def test_read_range(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.read(ReadNotebookParams(path=str(f), cell_range="0-1"))
        assert r.success
        assert len(r.data["cells"]) == 2

    @pytest.mark.asyncio
    async def test_read_not_found(self, nb):
        r = await nb.read(ReadNotebookParams(path="/nonexistent.ipynb"))
        assert not r.success

    @pytest.mark.asyncio
    async def test_read_not_ipynb(self, nb, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not a notebook")
        r = await nb.read(ReadNotebookParams(path=str(f)))
        assert not r.success


class TestEditCell:
    @pytest.mark.asyncio
    async def test_edit(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.edit_cell(EditCellParams(path=str(f), cell_index=0, content="print('edited')"))
        assert r.success
        # Verify content changed
        data = json.loads(f.read_text())
        assert data["cells"][0]["source"] == ["print('edited')"]

    @pytest.mark.asyncio
    async def test_edit_out_of_range(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.edit_cell(EditCellParams(path=str(f), cell_index=99, content="x"))
        assert not r.success

    @pytest.mark.asyncio
    async def test_change_type(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.edit_cell(EditCellParams(path=str(f), cell_index=1, content="code now", cell_type="code"))
        assert r.success
        data = json.loads(f.read_text())
        assert data["cells"][1]["cell_type"] == "code"


class TestAddCell:
    @pytest.mark.asyncio
    async def test_append(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.add_cell(AddCellParams(path=str(f), content="new cell"))
        assert r.success
        assert r.data["total_cells"] == 4
        assert r.data["cell_index"] == 3

    @pytest.mark.asyncio
    async def test_insert_at_position(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.add_cell(AddCellParams(path=str(f), content="inserted", position=0))
        assert r.success
        assert r.data["cell_index"] == 0
        data = json.loads(f.read_text())
        assert "inserted" in data["cells"][0]["source"][0]

    @pytest.mark.asyncio
    async def test_markdown_cell(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.add_cell(AddCellParams(path=str(f), content="# Header", cell_type="markdown"))
        assert r.success
        assert r.data["cell_type"] == "markdown"


class TestDeleteCell:
    @pytest.mark.asyncio
    async def test_delete(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.delete_cell(DeleteCellParams(path=str(f), cell_index=1))
        assert r.success
        assert r.data["total_cells"] == 2

    @pytest.mark.asyncio
    async def test_delete_out_of_range(self, nb, tmp_path):
        f = _make_notebook(tmp_path / "test.ipynb")
        r = await nb.delete_cell(DeleteCellParams(path=str(f), cell_index=99))
        assert not r.success
