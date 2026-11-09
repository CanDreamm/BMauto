import hashlib
import json

from bmauto.core import append_materials
from test_projects import Conversation, bot_at


def test_generation_uses_D_without_other_channel_texts(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    bot.google.table[2][3] = "Основное задание в D"
    bot.google.table[2][4] = "НЕ_ИСПОЛЬЗОВАТЬ_ИЗ_E"
    captured = []
    class LLM:
        def generate(self, project, task, supplement):
            captured.append(task)
            return "Готовый текст"
    bot.llm = LLM()
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    assert "НЕ_ИСПОЛЬЗОВАТЬ_ИЗ_E" not in bot.vk.messages[-1][0]
    chat.send("generate")
    chat.send("no_supplement")
    assert len(captured) == 1
    assert "Основное задание в D" in captured[0]
    assert "НЕ_ИСПОЛЬЗОВАТЬ_ИЗ_E" not in captured[0]
    chat.send("publish")
    assert bot.google.table[2][3].startswith("Основное задание в D\n\nГотовые материалы:\n")
    assert bot.google.table[2][4] == "НЕ_ИСПОЛЬЗОВАТЬ_ИЗ_E"


def test_existing_draft_migrates_and_keeps_identity_after_footer_and_other_channel_changes(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    session = bot.store.session(1)
    task = session["task"]
    old_fields = {k: v for k, v in task["fields"].items() if k != "Ссылка"}
    parts = ["test", task["row"], old_fields, [task["source_spreadsheet_id"], task["sheet_id"]]]
    old_key = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    draft = bot.store.draft(old_key)
    draft["text"] = "Сохранённый черновик"
    bot.store.save_draft(old_key, draft)
    new_key, migrated = bot.draft_for(session, project)
    assert migrated["text"] == "Сохранённый черновик"
    assert bot.store.has_draft(old_key)
    bot.google.table[2][3] = append_materials("Текст", "https://drive.google.com/drive/folders/ready")
    bot.google.table[2][4] = "Изменился другой канал"
    chat.send("project_home")
    chat.send("task", i=0)
    refreshed_key, saved = bot.draft_for(bot.store.session(1), project)
    assert new_key == refreshed_key
    assert saved["text"] == "Сохранённый черновик"
    assert bot.store.session(1)["task"]["material_url"].endswith("/ready")


def test_previously_uploaded_materials_can_be_linked_in_D_without_reupload(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    key, draft = bot.draft_for(bot.store.session(1), project)
    draft.update(text="Ранее готовый текст", folder="existing_folder", published=True,
                 photos=[{"id": "old", "path": "not-needed-for-relink.jpg"}])
    bot.store.save_draft(key, draft)
    chat.send("task_home")
    assert bot.vk.messages[-1][1][-1][1] == ("Отправить в таблицу", {"a": "publish", "_color": "positive"})
    chat.send("publish")
    assert not bot.google.created
    assert bot.google.table[2][3] == append_materials("Текст", "https://drive.google.com/drive/folders/existing_folder")
    assert bot.store.session(1)["task"]["material_url"].endswith("/existing_folder")
