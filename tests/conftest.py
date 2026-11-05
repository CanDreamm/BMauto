import pytest


@pytest.fixture
def project():
    return dict(id="test", name="Test", concept="Concept", prompt="Prompt", date_column="Дата",
                weekday_column="День недели", link_column="Ссылка", public_read=True,
                drive_parent_id="parent", worksheet="Sheet", spreadsheet_id="sheet")
