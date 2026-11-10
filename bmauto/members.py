from .vk import back_footer


class MemberMenus:
    def __init__(self, store, vk, admins):
        self.store, self.vk, self.admins = store, vk, admins

    def show(self, user, project, page=0):
        if not self.store.can_manage(project["id"], user, self.admins):
            raise ValueError("Нет доступа к участникам этого проекта")
        members = list(self.store.members(project["id"]).items())
        page = min(max(0, page), max(0, (len(members) - 1) // 6))
        lines = [f"Участники проекта «{project['name']}»"]
        buttons = []
        for member, role in members[page * 6:(page + 1) * 6]:
            lines.append(f"https://vk.com/id{member} — {'владелец' if role == 'owner' else 'участник'}")
            if role != "owner" and member not in self.admins:
                buttons.append((f"Удалить id{member}", {"a": "member_remove", "user": member}))
        lines.append("Администраторы бота сохраняют доступ ко всем проектам. Участники могут подключать Google проекта и работать с заданиями и общими черновиками; все настройки проекта доступны его участникам.")
        if page:
            buttons.append(("Назад", {"a": "members", "page": page - 1}))
        if (page + 1) * 6 < len(members):
            buttons.append(("Далее", {"a": "members", "page": page + 1}))
        buttons.append(back_footer({"a": "settings"}, ("Добавить участника", {"a": "member_add", "_color": "positive"})))
        session = self.store.session(user)
        session.update(view="members", members_page=page, mode=None)
        session.pop("wizard", None)
        session.pop("member_input", None)
        session.pop("member_remove", None)
        self.store.save_session(user, session)
        self.vk.send(user, "\n".join(lines), buttons)

    def ask(self, user):
        project = self.store.projects()[self.store.session(user)["project"]]
        self.vk.send(user, f"Участник для проекта «{project['name']}». Отправьте числовой ID пользователя ВК или ссылку на его профиль. После добавления ему нужно написать сообществу «Начать».",
                     [back_footer({"a": "members"})])

    def handle(self, user, action, payload, text, session):
        if action not in {"members", "member_add", "member_remove", "member_remove_confirm"} and not (session.get("member_input") and session.get("view") == "member_add" and action is None):
            return False
        project = self.store.projects().get(session.get("project"))
        if not project or not self.store.can_manage(project["id"], user, self.admins):
            raise ValueError("Нет доступа к участникам этого проекта")
        if action == "members":
            self.show(user, project, int(payload.get("page", 0)))
        elif action == "member_add":
            session.pop("wizard", None)
            session.update(mode=None, view="member_add", member_input=True)
            self.store.save_session(user, session)
            self.ask(user)
        elif action == "member_remove":
            session.pop("wizard", None)
            target = payload.get("user")
            if type(target) is not int or self.store.members(project["id"]).get(target) != "member" or target in self.admins:
                raise ValueError("Этого пользователя нельзя удалить")
            session.update(view="member_remove", member_remove=target, mode=None)
            session.pop("member_input", None)
            self.store.save_session(user, session)
            self.vk.send(user, f"Удалить https://vk.com/id{target} из этого проекта?", [back_footer(
                {"a": "members"}, ("Да, удалить", {"a": "member_remove_confirm", "user": target, "_color": "negative"}))])
        elif action == "member_remove_confirm":
            target = payload.get("user")
            if session.get("view") != "member_remove" or type(target) is not int or target != session.get("member_remove"):
                raise ValueError("Подтверждение устарело. Откройте список участников")
            self.store.remove_member(project["id"], user, target, self.admins)
            session.pop("member_remove", None)
            self.store.save_session(user, session)
            self.vk.send(user, "Участник удалён из проекта")
            self.show(user, project, session.get("members_page", 0))
        else:
            target = self.vk.resolve_user(text)
            self.store.add_member(project["id"], user, target, self.admins)
            self.vk.send(user, f"Доступ к проекту предоставлен: https://vk.com/id{target}. Попросите участника написать сообществу «Начать», выбрать проект → «Настройки» → «Подключить Google»")
            self.show(user, project, session.get("members_page", 0))
        return True
