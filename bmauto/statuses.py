"""Plain status values and explicit colors in column A."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .core import DEFAULT_COLUMNS, assignment_cell, assignment_from_task, parse_date, table_layout

PLANNED = "Запланировано"
EXPIRED = "Дата прошла"
SENT = PLANNED
EMPTY = ""
APPROVED = "Согласовано"
RULE_MARKER = "BMauto:status:v1"
COLORS = {EXPIRED: {"red": .85, "green": .85, "blue": .85},
          SENT: {"red": 1, "green": .9, "blue": .6},
          APPROVED: {"red": .71, "green": .84, "blue": .66},
          EMPTY: {"red": 1, "green": 1, "blue": 1}}
LEGACY_COLORS = {"Материалы отправлены": COLORS[SENT], PLANNED: COLORS[EMPTY]}


def status_day(value):
    if isinstance(value, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(value))
    return parse_date(str(value))


def publication_status(day, description, current, today):
    if current == APPROVED:
        return APPROVED
    if not assignment_cell(description)[1]:
        return EMPTY
    if day < today:
        return EXPIRED
    return PLANNED


def rules_for(sheet_id, header_row):
    return [{"ranges": [{"sheetId": sheet_id, "startRowIndex": header_row,
                         "startColumnIndex": 0, "endColumnIndex": 1}],
             "booleanRule": {"condition": ({"type": "TEXT_EQ", "values": [
                 {"userEnteredValue": label}]} if label else {"type": "BLANK"}),
                 "format": {"backgroundColor": color}}} for label, color in COLORS.items()]


def is_our_rule(rule):
    condition = rule.get("booleanRule", {}).get("condition", {})
    values = condition.get("values", [])
    legacy = any(RULE_MARKER in str(value.get("userEnteredValue", "")) for value in values)
    color = rule.get("booleanRule", {}).get("format", {}).get("backgroundColor", {})
    label = values[0].get("userEnteredValue") if len(values) == 1 else None
    candidates = ([COLORS[EMPTY]] if condition.get("type") == "BLANK" else
                  [mapping[label] for mapping in (COLORS, LEGACY_COLORS) if label in mapping]
                  if condition.get("type") == "TEXT_EQ" else [])
    current = (bool(rule.get("ranges")) and
               all(r.get("startColumnIndex", 0) == 0 and r.get("endColumnIndex") == 1 for r in rule["ranges"]) and
               any(all(abs(color.get(channel, 0) - expected.get(channel, 0)) < .01 for channel in ("red", "green", "blue")) for expected in candidates))
    return legacy or current


def format_updates(sheet, header):
    sid = sheet["properties"]["sheetId"]
    existing, wanted = sheet.get("conditionalFormats", []), rules_for(sid, header)
    if existing[:len(wanted)] == wanted:
        return []
    requests = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": index}}
                for index in reversed(range(len(existing))) if is_our_rule(existing[index])]
    requests.extend({"addConditionalFormatRule": {"rule": rule, "index": 0}} for rule in reversed(wanted))
    return requests


def cell_update(sheet_id, row, value, color=None):
    cell = {"userEnteredValue": {"stringValue": value}} if value else {}
    fields = "userEnteredValue"
    if color is not None:
        cell["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": color}}
        fields += ",userEnteredFormat.backgroundColorStyle,userEnteredFormat.backgroundColor"
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row - 1,
            "endRowIndex": row, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [cell]}], "fields": fields}}


class StatusSheets:
    def sync_statuses(self, project, sheets, today=None):
        """Recalculate status values and formatting for every monthly worksheet."""
        today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
        from .calendar import MONTHS
        month_names = {name.casefold() for name in MONTHS}
        ids = [s["sheetId"] for s in sheets if s["title"].strip().casefold() in month_names]
        if not ids:
            return
        book = project["spreadsheet_id"]
        metadata = self.request("GET", f"sheets/v4/spreadsheets/{book}", params={
            "fields": "sheets(properties(sheetId),merges,conditionalFormats)"})
        result = self.request("POST", f"sheets/v4/spreadsheets/{book}/values:batchGetByDataFilter", json={
            "dataFilters": [{"gridRange": {"sheetId": sid}} for sid in ids], "valueRenderOption": "FORMATTED_VALUE"})
        values = {}
        for item in result.get("valueRanges", []):
            for data_filter in item.get("dataFilters", []):
                sid = data_filter.get("gridRange", {}).get("sheetId")
                if sid is not None:
                    values[sid] = item.get("valueRange", {}).get("values", [])
        requests = []
        for sheet in metadata.get("sheets", []):
            sid = sheet["properties"]["sheetId"]
            rows = values.get(sid, [])
            if not rows:
                continue
            try:
                header, _ = table_layout(rows, project)
            except ValueError:
                continue
            # Legacy weekly cells can be vertically merged in A. Unmerge A only.
            for merge in sheet.get("merges", []):
                if merge.get("startColumnIndex", 0) == 0 and merge.get("endColumnIndex") == 1 and merge.get("startRowIndex", 0) >= header:
                    requests.append({"unmergeCells": {"range": merge}})
            if rows[header - 1][0] != "Статус":
                requests.append(cell_update(sid, header, "Статус"))
            for number, row in enumerate(rows[header:], header + 1):
                if len(row) < 2 or not row[1]:
                    continue
                try:
                    day = status_day(row[1])
                except ValueError:
                    continue
                current = str(row[0]) if row else ""
                status = publication_status(day, row[3] if len(row) > 3 else "", current, today)
                if current != status:
                    requests.append(cell_update(sid, number, status, COLORS[status]))
            requests.extend(format_updates(sheet, header))
        if requests:
            self.request("POST", f"sheets/v4/spreadsheets/{book}:batchUpdate", json={"requests": requests})

    def set_status(self, project, task, status, url):
        if status not in {SENT, APPROVED}:
            raise ValueError("Неизвестный статус")
        if task.get("source_spreadsheet_id", project["spreadsheet_id"]) != project["spreadsheet_id"]:
            raise ValueError("Таблица проекта изменилась. Откройте дату заново")
        source = {**DEFAULT_COLUMNS, **project, "worksheet": task.get("worksheet", project.get("worksheet", "Авто"))}
        if "sheet_id" in task:
            source["_sheet_id"] = task["sheet_id"]
        else:
            matches = [s for s in self.worksheets(source) if s["title"] == source["worksheet"]]
            if len(matches) != 1:
                raise ValueError("Вкладка задания не найдена")
            source["_sheet_id"] = matches[0]["sheetId"]
        rows = self.rows(source)
        header, headers = table_layout(rows, source)
        number = task["row"]
        if number <= header or number > len(rows):
            raise ValueError("Строка задания изменилась. Откройте дату заново")
        row = rows[number - 1]
        fields = dict(zip(headers, list(row) + [""] * len(headers)))
        description, current_url = assignment_cell(row[3] if len(row) > 3 else "")
        if description != assignment_from_task(task) or any(fields.get(source[key], "") != task["fields"].get(source[key], "") for key in ("date_column", "weekday_column")):
            raise ValueError("Задание в таблице изменилось. Откройте дату заново")
        if not url or current_url != url:
            raise ValueError("Сначала отправьте материалы в таблицу и проверьте ссылку")
        metadata = self.request("GET", f"sheets/v4/spreadsheets/{source['spreadsheet_id']}", params={
            "fields": "sheets(properties(sheetId),conditionalFormats)"})
        sheet = next((s for s in metadata.get("sheets", []) if s["properties"]["sheetId"] == source["_sheet_id"]), None)
        if sheet is None:
            raise ValueError("Вкладка задания не найдена")
        color_status = APPROVED if status == APPROVED else EXPIRED if task["day"] < datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat() else SENT
        self.request("POST", f"sheets/v4/spreadsheets/{source['spreadsheet_id']}:batchUpdate", json={
            "requests": format_updates(sheet, header) + [cell_update(source["_sheet_id"], number, color_status, COLORS[color_status])]})
