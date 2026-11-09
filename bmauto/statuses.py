"""Live status formulas in column A; approvals are explicit values."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .core import DEFAULT_COLUMNS, assignment_cell, assignment_from_task, parse_date, table_layout

PLANNED = "Запланировано"
EXPIRED = "Дата прошла"
SENT = "Материалы отправлены"
APPROVED = "Согласовано"
RULE_MARKER = "BMauto:status:v1"
COLORS = {EXPIRED: {"red": .85, "green": .85, "blue": .85},
          SENT: {"red": 1, "green": .9, "blue": .6},
          APPROVED: {"red": .71, "green": .84, "blue": .66},
          PLANNED: {"red": 1, "green": 1, "blue": 1}}


def status_formula(row, separator=","):
    b, d = f"$B{row}", f"$D{row}"
    # Dates can be Sheets serial numbers, dd.mm.yyyy / dd/mm/yyyy, or ISO strings.
    day = (f'IF(ISNUMBER({b}),INT({b}),IF(MID({b},5,1)="-",'
           f'DATE(VALUE(LEFT({b},4)),VALUE(MID({b},6,2)),VALUE(RIGHT({b},2))),'
           f'DATE(VALUE(RIGHT({b},4)),VALUE(MID({b},4,2)),VALUE(LEFT({b},2)))))')
    formula = (f'=IF({b}="","",IF(IFERROR({day}<TODAY(),FALSE),"{EXPIRED}",'
               f'IF(ISNUMBER(SEARCH("Готовые материалы:",{d})),"{SENT}","{PLANNED}")))')
    return formula.replace(",", separator)


def argument_separator(metadata):
    locale = metadata.get("properties", {}).get("locale", "en_US").replace("-", "_")
    return ";" if locale.split("_")[0] in {"ru", "uk", "be", "de", "fr", "it", "pt", "pl", "nl", "da", "fi", "no", "nb", "sv", "cs", "sk", "hu", "tr", "el", "ro", "bg", "hr", "sr", "sl", "lt", "lv", "et"} else ","


def rules_for(sheet_id, header_row):
    return [{"ranges": [{"sheetId": sheet_id, "startRowIndex": header_row,
                         "startColumnIndex": 0, "endColumnIndex": 1}],
             "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [
                 {"userEnteredValue": label}]},
                 "format": {"backgroundColor": color}}} for label, color in COLORS.items()]


def is_our_rule(rule):
    condition = rule.get("booleanRule", {}).get("condition", {})
    values = condition.get("values", [])
    legacy = any(RULE_MARKER in str(value.get("userEnteredValue", "")) for value in values)
    color = rule.get("booleanRule", {}).get("format", {}).get("backgroundColor", {})
    expected = COLORS.get(values[0].get("userEnteredValue"), {}) if len(values) == 1 else {}
    current = (condition.get("type") == "TEXT_EQ" and len(values) == 1 and
               values[0].get("userEnteredValue") in COLORS and bool(rule.get("ranges")) and
               all(r.get("startColumnIndex", 0) == 0 and r.get("endColumnIndex") == 1 for r in rule["ranges"]) and
               all(abs(color.get(channel, 0) - expected.get(channel, 0)) < .01 for channel in ("red", "green", "blue")))
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
    cell = {"userEnteredValue": {"formulaValue" if value.startswith("=") else "stringValue": value}}
    fields = "userEnteredValue"
    if color is not None:
        cell["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": color}}
        fields += ",userEnteredFormat.backgroundColorStyle,userEnteredFormat.backgroundColor"
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row - 1,
            "endRowIndex": row, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [cell]}], "fields": fields}}


class StatusSheets:
    def sync_statuses(self, project, sheets):
        """Install formulas and idempotent formatting for every monthly worksheet."""
        from .calendar import MONTHS
        month_names = {name.casefold() for name in MONTHS}
        ids = [s["sheetId"] for s in sheets if s["title"].strip().casefold() in month_names]
        if not ids:
            return
        book = project["spreadsheet_id"]
        metadata = self.request("GET", f"sheets/v4/spreadsheets/{book}", params={
            "fields": "properties(locale),sheets(properties(sheetId),merges,conditionalFormats)"})
        separator = argument_separator(metadata)
        result = self.request("POST", f"sheets/v4/spreadsheets/{book}/values:batchGetByDataFilter", json={
            "dataFilters": [{"gridRange": {"sheetId": sid}} for sid in ids], "valueRenderOption": "FORMULA"})
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
                    if not isinstance(row[1], (int, float)) and not str(row[1]).startswith("="):
                        parse_date(str(row[1]))
                except ValueError:
                    continue
                current = str(row[0]) if row else ""
                if current != APPROVED and current != status_formula(number, separator):
                    requests.append(cell_update(sid, number, status_formula(number, separator)))
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
            "fields": "properties(locale),sheets(properties(sheetId),conditionalFormats)"})
        sheet = next((s for s in metadata.get("sheets", []) if s["properties"]["sheetId"] == source["_sheet_id"]), None)
        if sheet is None:
            raise ValueError("Вкладка задания не найдена")
        value = APPROVED if status == APPROVED else status_formula(number, argument_separator(metadata))
        color_status = APPROVED if status == APPROVED else EXPIRED if task["day"] < datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat() else SENT
        self.request("POST", f"sheets/v4/spreadsheets/{source['spreadsheet_id']}:batchUpdate", json={
            "requests": format_updates(sheet, header) + [cell_update(source["_sheet_id"], number, value, COLORS[color_status])]})
