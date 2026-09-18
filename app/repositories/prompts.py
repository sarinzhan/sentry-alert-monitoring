"""PromptsRepo — prepared prompts for the web investigation form.

kind='role': who the LLM answer is written for; the text replaces the
answer-style section of the explain prompt (see investigation.explain).
kind='problem': common complaint templates; the text prefills the
description field in the form.

Seeded with defaults on first run, then the admin edits them in the web UI.
"""
import time

KINDS = ("role", "problem")

COLS = ("id", "kind", "name", "text", "updated")

ROLE_SEEDS = (
    ("Для поддержки",
     "Ответ для СОТРУДНИКА ПОДДЕРЖКИ (не программиста). Простой русский, без "
     "жаргона, без стектрейсов и HTTP-кодов (расшифруй, если без термина "
     "никак). Обычный текст, без markdown, строго такая структура:\n"
     "Что произошло: 1-2 предложения простыми словами\n"
     "Причина: почему это происходит — бизнес-правило, ошибка сервиса или "
     "проблема данных; если точно не ясно, самая вероятная версия с пометкой "
     "«предположительно»\n"
     "Доказательства: конкретные строки логов и события (время, сервис, "
     "дословная цитата), на которых основан вывод — единственный раздел, где "
     "уместен технический текст\n"
     "Что сказать клиенту: готовая вежливая формулировка\n"
     "Что дальше: может ли поддержка решить сама (и как), или передать "
     "разработчикам — какой команде и с какой информацией"),
    ("Для клиента",
     "Ответ НАПРЯМУЮ КЛИЕНТУ мобильного оператора. Максимально простой и "
     "вежливый русский: никакого жаргона, никаких внутренних деталей (имена "
     "сервисов, логи, коды ошибок). Обычный текст, без markdown, структура:\n"
     "Что произошло: одним-двумя предложениями, по-человечески\n"
     "Почему: причина без технических подробностей\n"
     "Что делаем / что сделать вам: конкретные шаги\n"
     "Если проблема на нашей стороне — короткое извинение."),
    ("Для тестировщика",
     "Ответ для ТЕСТИРОВЩИКА (QA). Русский язык, обычный текст, без markdown, "
     "структура:\n"
     "Суть бага: одним предложением\n"
     "Шаги воспроизведения: пронумерованные, с конкретными данными "
     "(идентификаторы, окружение, время)\n"
     "Ожидаемое / фактическое: что должно было случиться и что случилось\n"
     "Где ломается: сервис, операция/endpoint, условие срабатывания\n"
     "Доказательства: события и строки логов (время, сервис, дословная "
     "цитата)\n"
     "Данные для проверки: чем воспроизводить после фикса"),
    ("Для разработчика",
     "Ответ для BACKEND-РАЗРАБОТЧИКА — технический, по делу. Русский язык, "
     "обычный текст, без markdown, структура:\n"
     "Симптом: что наблюдается\n"
     "Корневая причина: сервис, класс/метод, строка кода, коммит — если "
     "нашёл в репозитории\n"
     "Цепочка отказа: какой сервис у кого что вызвал и где оборвалось\n"
     "Доказательства: события, логи, фрагменты стека — дословно\n"
     "Как чинить: конкретное предложение\n"
     "Как проверить: запрос или сценарий для проверки фикса"),
)

PROBLEM_SEEDS = (
    ("Не проходит оплата",
     "Клиент пытается оплатить услугу или пополнить баланс — оплата не "
     "проходит, либо деньги списались, а услуга не подключилась."),
    ("Не приходит SMS/код",
     "Клиенту не приходит SMS с кодом подтверждения при входе или при "
     "выполнении операции."),
    ("Не активировался пакет",
     "Клиент подключил пакет (интернет/минуты/SMS), деньги списаны, но пакет "
     "не активировался или не работает."),
    ("Ошибка в приложении",
     "Приложение показывает ошибку при выполнении действия (укажите, какого "
     "именно: вход, оплата, подключение услуги и т.д.)."),
)


class PromptsRepo:
    def __init__(self, conn):
        self.db = conn
        if not self.db.execute("SELECT 1 FROM prompt_preset LIMIT 1").fetchone():
            for name, text in ROLE_SEEDS:
                self.create("role", name, text)
            for name, text in PROBLEM_SEEDS:
                self.create("problem", name, text)

    def _row(self, r):
        return dict(zip(COLS, r)) if r else None

    def list(self, kind=None):
        q = f"SELECT {', '.join(COLS)} FROM prompt_preset"
        args = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        return [self._row(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def get(self, pid):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return self._row(self.db.execute(
            f"SELECT {', '.join(COLS)} FROM prompt_preset WHERE id=?",
            (pid,)).fetchone())

    def create(self, kind, name, text):
        cur = self.db.execute(
            "INSERT INTO prompt_preset(kind, name, text, updated)"
            " VALUES (?, ?, ?, ?)", (kind, name, text, time.time()))
        self.db.commit()
        return self.get(cur.lastrowid)

    def update(self, pid, name=None, text=None):
        row = self.get(pid)
        if row is None:
            return None
        self.db.execute(
            "UPDATE prompt_preset SET name=?, text=?, updated=? WHERE id=?",
            (name if name is not None else row["name"],
             text if text is not None else row["text"],
             time.time(), row["id"]))
        self.db.commit()
        return self.get(pid)

    def delete(self, pid):
        self.db.execute("DELETE FROM prompt_preset WHERE id=?", (int(pid),))
        self.db.commit()
