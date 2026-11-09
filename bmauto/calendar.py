"""Choose monthly worksheets and keep the origin of each publication task."""
from calendar import monthrange
from dataclasses import asdict

from .core import DEFAULT_COLUMNS, tasks_from_rows

MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def load_tasks(google, project, today):
    project = {**DEFAULT_COLUMNS, **project}
    sheets = google.worksheets(project)
    warnings = []
    try:
        google.sync_statuses(project, sheets)
    except Exception:
        warnings.append("Не удалось обновить статусы в колонке A. Проверьте право редактирования таблицы и откройте проект повторно")

    def read_month(year, month):
        title = MONTHS[month - 1]
        matches = [s for s in sheets if s["title"].strip().casefold() == title.casefold()]
        if len(matches) != 1:
            warnings.append(f"Вкладка «{title}» {'не найдена' if not matches else 'указана неоднозначно'}")
            return []
        sheet = matches[0]
        source = {**project, "worksheet": sheet["title"], "_sheet_id": sheet["sheetId"]}
        try:
            tasks = tasks_from_rows(google.rows(source), source, today)
        except ValueError as exc:
            warnings.append(f"Вкладка «{sheet['title']}»: {exc}")
            return []
        return [{**asdict(t), "day": t.day.isoformat(), "worksheet": sheet["title"],
                 "sheet_id": sheet["sheetId"], "source_spreadsheet_id": project["spreadsheet_id"]}
                for t in tasks if (t.day.year, t.day.month) == (year, month)]

    current = read_month(today.year, today.month)
    remaining = monthrange(today.year, today.month)[1] - today.day + 1
    result = list(current)
    if remaining <= 7 or not current:
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        result.extend(read_month(year, month))
    return sorted(result, key=lambda t: (t["day"], t["row"])), warnings
